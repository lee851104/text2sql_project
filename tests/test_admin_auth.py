from __future__ import annotations

import hashlib
import warnings
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request, Response

from serving import admin_auth
from serving.admin_auth import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    ALLOWED_HOSTS_ENV,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    SESSION_TTL_ENV,
    TRUSTED_PROXIES_ENV,
    AdminAuthConfigurationError,
    AdminAuthManager,
    CsrfValidationFailed,
    DefaultCredentialsRemoteAccessDenied,
    DefaultCredentialsWarning,
    ExpiredAdminSession,
    InvalidAdminSession,
    InvalidCredentials,
    LoginRateLimited,
    RequestOriginDenied,
)


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)
        self.monotonic = 1_000.0

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic_now(self) -> float:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.monotonic += seconds


def _manager(clock: FakeClock | None = None, **kwargs: object) -> AdminAuthManager:
    selected_clock = clock or FakeClock()
    return AdminAuthManager(
        username="review-admin",
        password="correct horse battery staple",
        session_ttl_seconds=900,
        pbkdf2_iterations=1_000,
        wall_clock=selected_clock.wall_now,
        monotonic_clock=selected_clock.monotonic_now,
        **kwargs,
    )


def _request(
    *,
    token: str | None = None,
    csrf_token: str | None = None,
    origin: str | None = "http://127.0.0.1:8000",
    host: str = "127.0.0.1:8000",
    client_host: str = "127.0.0.1",
    fetch_site: str | None = "same-origin",
    scheme: str = "http",
    forwarded_for: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode("ascii"))]
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    if token is not None:
        headers.append((b"cookie", f"powerquery_admin_session={token}".encode("ascii")))
    if csrf_token is not None:
        headers.append((b"x-powerquery-csrf", csrf_token.encode("ascii")))
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode("ascii")))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/admin/session",
            "raw_path": b"/api/admin/session",
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 50_000),
            "server": (host.rsplit(":", 1)[0], int(host.rsplit(":", 1)[1])),
        }
    )


def test_environment_defaults_warn_and_override_is_all_or_nothing() -> None:
    with pytest.warns(DefaultCredentialsWarning):
        default = AdminAuthManager.from_environment({}, pbkdf2_iterations=1_000)
    assert default.username == DEFAULT_ADMIN_USERNAME
    assert default.using_default_credentials is True
    grant = default.login(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
    assert grant.principal.using_default_credentials is True

    environment = {
        ADMIN_USERNAME_ENV: "operator",
        ADMIN_PASSWORD_ENV: "environment-only-secret",
        SESSION_TTL_ENV: "600",
        ALLOWED_HOSTS_ENV: "localhost, 10.20.30.40",
    }
    with warnings.catch_warnings(record=True) as caught:
        configured = AdminAuthManager.from_environment(
            environment,
            pbkdf2_iterations=1_000,
        )
    assert not caught
    assert configured.username == "operator"
    assert configured.using_default_credentials is False
    assert configured.session_ttl_seconds == 600
    assert configured.allowed_hosts == frozenset({"localhost", "10.20.30.40"})
    assert configured.login("operator", "environment-only-secret").principal.username == "operator"

    with pytest.raises(AdminAuthConfigurationError, match="必須同時設定"):
        AdminAuthManager.from_environment(
            {ADMIN_USERNAME_ENV: "operator"},
            pbkdf2_iterations=1_000,
        )


@pytest.mark.parametrize("ttl", ["not-a-number", "299", "3601"])
def test_environment_rejects_invalid_session_ttl(ttl: str) -> None:
    environment = {
        ADMIN_USERNAME_ENV: "operator",
        ADMIN_PASSWORD_ENV: "environment-only-secret",
        SESSION_TTL_ENV: ttl,
    }
    with pytest.raises(AdminAuthConfigurationError):
        AdminAuthManager.from_environment(environment, pbkdf2_iterations=1_000)


def test_unknown_user_and_wrong_password_have_one_safe_failure_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    real_pbkdf2 = hashlib.pbkdf2_hmac
    calls: list[tuple[str, int]] = []

    def tracked_pbkdf2(
        hash_name: str,
        password: bytes,
        salt: bytes,
        iterations: int,
    ) -> bytes:
        calls.append((hash_name, iterations))
        return real_pbkdf2(hash_name, password, salt, iterations)

    monkeypatch.setattr(admin_auth.hashlib, "pbkdf2_hmac", tracked_pbkdf2)
    messages = []
    for username, password in (
        ("nobody", "wrong-password"),
        ("review-admin", "wrong-password"),
    ):
        before = len(calls)
        with pytest.raises(InvalidCredentials) as captured:
            manager.login(username, password, client_id=username)
        messages.append(str(captured.value))
        assert len(calls) == before + 1

    assert messages == ["帳號或密碼錯誤。", "帳號或密碼錯誤。"]
    assert "wrong-password" not in repr(manager)
    assert "nobody" not in repr(manager)


def test_login_rate_limit_is_per_client_and_expires() -> None:
    clock = FakeClock()
    manager = _manager(
        clock,
        login_failure_limit=2,
        login_failure_window_seconds=60,
    )

    for _attempt in range(2):
        with pytest.raises(InvalidCredentials):
            manager.login("review-admin", "wrong-password", client_id="127.0.0.1")
    with pytest.raises(LoginRateLimited) as captured:
        manager.login(
            "review-admin",
            "correct horse battery staple",
            client_id="127.0.0.1",
        )
    assert captured.value.retry_after_seconds == 60

    # A different source is not locked, and the original source recovers when
    # its fixed failure window ends.
    other = manager.login(
        "review-admin",
        "correct horse battery staple",
        client_id="127.0.0.2",
    )
    assert manager.authenticate(other.token).username == "review-admin"
    clock.advance(61)
    recovered = manager.login(
        "review-admin",
        "correct horse battery staple",
        client_id="127.0.0.1",
    )
    assert recovered.principal.username == "review-admin"


def test_session_values_are_opaque_digest_only_and_never_in_repr() -> None:
    manager = _manager()
    grant = manager.login("review-admin", "correct horse battery staple")

    assert grant.token not in repr(grant)
    assert grant.csrf_token not in repr(grant)
    assert grant.token not in repr(manager)
    assert grant.csrf_token not in repr(manager)
    assert "correct horse battery staple" not in repr(manager)
    assert manager.active_session_count == 1
    assert all(isinstance(key, bytes) for key in manager._sessions)
    assert grant.token.encode() not in manager._sessions
    assert manager.authenticate(grant.token).username == "review-admin"


def test_sessions_have_absolute_expiry_and_logout_revokes_them() -> None:
    clock = FakeClock()
    manager = _manager(clock)
    grant = manager.login("review-admin", "correct horse battery staple")

    clock.advance(899)
    principal = manager.authenticate(grant.token)
    assert principal.expires_at == datetime(2026, 9, 13, 4, 15, tzinfo=UTC)
    clock.advance(1)
    with pytest.raises(ExpiredAdminSession):
        manager.authenticate(grant.token)
    assert manager.active_session_count == 0

    replacement = manager.login("review-admin", "correct horse battery staple")
    assert manager.revoke(replacement.token) is True
    assert manager.revoke(replacement.token) is False
    with pytest.raises(InvalidAdminSession):
        manager.authenticate(replacement.token)


def test_csrf_can_rotate_after_page_refresh_without_extending_session() -> None:
    clock = FakeClock()
    manager = _manager(clock)
    grant = manager.login("review-admin", "correct horse battery staple")
    original_expiry = grant.principal.expires_at
    clock.advance(300)

    principal, replacement_csrf = manager.rotate_csrf(grant.token)

    assert principal.expires_at == original_expiry
    assert replacement_csrf != grant.csrf_token
    assert replacement_csrf not in repr(manager)
    with pytest.raises(CsrfValidationFailed):
        manager.validate_csrf(grant.token, grant.csrf_token)
    assert manager.validate_csrf(grant.token, replacement_csrf) == principal

    clock.advance(600)
    with pytest.raises(ExpiredAdminSession):
        manager.rotate_csrf(grant.token)


def test_session_limit_evicts_the_oldest_grant() -> None:
    clock = FakeClock()
    manager = _manager(clock, maximum_sessions=2)
    first = manager.login("review-admin", "correct horse battery staple")
    clock.advance(1)
    second = manager.login("review-admin", "correct horse battery staple")
    clock.advance(1)
    third = manager.login("review-admin", "correct horse battery staple")

    with pytest.raises(InvalidAdminSession):
        manager.authenticate(first.token)
    assert manager.authenticate(second.token).username == "review-admin"
    assert manager.authenticate(third.token).username == "review-admin"
    assert manager.active_session_count == 2


def test_request_authentication_requires_exact_origin_and_session_csrf() -> None:
    manager = _manager()
    grant = manager.login("review-admin", "correct horse battery staple")
    valid = _request(token=grant.token, csrf_token=grant.csrf_token)

    principal = manager.authenticate_request(valid, require_csrf=True)
    assert principal.username == "review-admin"

    # Read-only authentication does not need Origin/CSRF, because it cannot be
    # triggered to mutate state by a cross-site form.
    read_request = _request(
        token=grant.token,
        origin=None,
        fetch_site=None,
    )
    assert manager.authenticate_request(read_request).username == "review-admin"

    with pytest.raises(CsrfValidationFailed):
        manager.authenticate_request(
            _request(token=grant.token, csrf_token="wrong-csrf"),
            require_csrf=True,
        )
    with pytest.raises(CsrfValidationFailed):
        manager.authenticate_request(
            _request(token=grant.token, csrf_token=None),
            require_csrf=True,
        )
    with pytest.raises(RequestOriginDenied):
        manager.authenticate_request(
            _request(
                token=grant.token,
                csrf_token=grant.csrf_token,
                origin="http://attacker.example",
            ),
            require_csrf=True,
        )
    with pytest.raises(RequestOriginDenied):
        manager.authenticate_request(
            _request(
                token=grant.token,
                csrf_token=grant.csrf_token,
                fetch_site="cross-site",
            ),
            require_csrf=True,
        )


def test_default_credentials_are_rejected_for_non_loopback_clients() -> None:
    with pytest.warns(DefaultCredentialsWarning):
        manager = AdminAuthManager.from_environment({}, pbkdf2_iterations=1_000)

    manager.validate_login_request(_request())
    with pytest.raises(DefaultCredentialsRemoteAccessDenied):
        manager.validate_login_request(_request(client_host="192.168.50.20"))
    with pytest.raises(RequestOriginDenied):
        manager.validate_login_request(
            _request(
                origin="http://powerquery.example:8000",
                host="powerquery.example:8000",
            )
        )


def test_cookie_is_host_only_http_only_strict_and_non_cacheable() -> None:
    manager = _manager()
    grant = manager.login("review-admin", "correct horse battery staple")
    response = Response(content=b"{}", media_type="application/json")

    manager.set_session_cookie(response, grant, secure=False)

    cookie = response.headers["set-cookie"]
    cookie_attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert cookie.startswith(f"{manager.cookie_name}={grant.token};")
    assert "httponly" in cookie_attributes
    assert "samesite=strict" in cookie_attributes
    assert "path=/api" in cookie_attributes
    assert f"max-age={manager.session_ttl_seconds}" in cookie_attributes
    assert "secure" not in cookie_attributes
    assert "domain" not in cookie.lower()
    assert grant.token not in response.body.decode()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"

    cleared = Response()
    manager.clear_session_cookie(cleared, secure=True)
    cleared_cookie = cleared.headers["set-cookie"].lower()
    assert "max-age=0" in cleared_cookie
    assert "path=/api" in cleared_cookie
    assert "httponly" in cleared_cookie
    assert "samesite=strict" in cleared_cookie
    assert "secure" in cleared_cookie
    assert cleared.headers["cache-control"] == "no-store"


def test_public_configuration_contains_no_secret_fields() -> None:
    manager = _manager()
    configuration = manager.public_configuration()

    assert configuration == {
        "username": "review-admin",
        "using_default_credentials": False,
        "session_ttl_seconds": 900,
    }
    assert set(configuration).isdisjoint(
        {"password", "password_hash", "token", "csrf", "api_key", "credential"}
    )


def _proxy_manager(**kwargs: object) -> AdminAuthManager:
    return _manager(trusted_proxies=("127.0.0.1",), **kwargs)


def test_forwarded_for_is_ignored_without_a_configured_trusted_proxy() -> None:
    """沒設信任代理時，任何人都能自己填 X-Forwarded-For；一律不採信。"""

    manager = _manager()

    resolved = manager.client_address(
        _request(client_host="127.0.0.1", forwarded_for="203.0.113.9")
    )

    assert resolved == "127.0.0.1"


def test_forwarded_for_is_used_only_when_the_peer_is_a_trusted_proxy() -> None:
    manager = _proxy_manager()

    behind_proxy = manager.client_address(
        _request(client_host="127.0.0.1", forwarded_for="203.0.113.9")
    )
    direct = manager.client_address(
        _request(client_host="192.168.50.20", forwarded_for="203.0.113.9")
    )

    assert behind_proxy == "203.0.113.9"
    assert direct == "192.168.50.20", "直連來源不得被自己送的 header 改寫"


def test_the_rightmost_untrusted_hop_is_taken_as_the_client() -> None:
    manager = _proxy_manager()

    resolved = manager.client_address(
        _request(client_host="127.0.0.1", forwarded_for="198.51.100.7, 203.0.113.9, 127.0.0.1")
    )

    assert resolved == "203.0.113.9"


def test_an_unparsable_hop_reports_an_unknown_client_rather_than_guessing() -> None:
    manager = _proxy_manager()

    resolved = manager.client_address(
        _request(client_host="127.0.0.1", forwarded_for="not-an-address")
    )

    assert resolved is None


def test_default_credentials_stay_local_when_the_real_client_is_remote() -> None:
    """反向代理把外部訪客變成 127.0.0.1 時，預設帳密不得因此對外開放。"""

    with pytest.warns(DefaultCredentialsWarning):
        manager = AdminAuthManager.from_environment(
            {TRUSTED_PROXIES_ENV: "127.0.0.1"}, pbkdf2_iterations=1_000
        )

    manager.validate_login_request(_request(client_host="127.0.0.1"))
    with pytest.raises(DefaultCredentialsRemoteAccessDenied):
        manager.validate_login_request(
            _request(client_host="127.0.0.1", forwarded_for="203.0.113.9")
        )


def test_two_visitors_behind_one_proxy_get_separate_rate_limit_buckets() -> None:
    """代理後面的訪客在 request.client 裡全是同一個位址；分桶必須用解析後的來源。"""

    clock = FakeClock()
    manager = _manager(clock, trusted_proxies=("127.0.0.1",), login_failure_limit=2)

    def attempt(forwarded: str) -> None:
        request = _request(client_host="127.0.0.1", forwarded_for=forwarded)
        manager.login("review-admin", "wrong", client_id=manager.client_address(request))

    for _attempt in range(2):
        with pytest.raises(InvalidCredentials):
            attempt("203.0.113.9")
    with pytest.raises(LoginRateLimited):
        attempt("203.0.113.9")

    # 共用同一個代理的另一個訪客不該被前者鎖住；分錯桶時這裡會是 LoginRateLimited。
    with pytest.raises(InvalidCredentials):
        attempt("198.51.100.7")


def test_a_malformed_trusted_proxy_setting_is_refused() -> None:
    with pytest.raises(AdminAuthConfigurationError, match="不是合法的 IP 或網段"):
        _manager(trusted_proxies=("not-a-network",))
