"""Local administrator authentication primitives for management endpoints.

The service intentionally keeps its authentication state in process memory.  A
restart therefore revokes every session, just as it clears an API key supplied
through the web UI.  This module contains no FastAPI routes so applications can
compose the policy without making authentication depend on global app state.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import os
import secrets
import time
import warnings
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock, RLock
from urllib.parse import urlsplit

from fastapi import Request, Response

from serving.accounts import Account, decode_password_hash

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "PowerQuery@123"
DEFAULT_SESSION_TTL_SECONDS = 15 * 60
MINIMUM_SESSION_TTL_SECONDS = 5 * 60
MAXIMUM_SESSION_TTL_SECONDS = 60 * 60
DEFAULT_PBKDF2_ITERATIONS = 600_000
DEFAULT_COOKIE_NAME = "powerquery_admin_session"
DEFAULT_CSRF_HEADER = "X-PowerQuery-CSRF"
DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})

ADMIN_USERNAME_ENV = "POWERQUERY_ADMIN_USERNAME"
ADMIN_PASSWORD_ENV = "POWERQUERY_ADMIN_PASSWORD"
SESSION_TTL_ENV = "POWERQUERY_ADMIN_SESSION_TTL_SECONDS"
ALLOWED_HOSTS_ENV = "POWERQUERY_ADMIN_ALLOWED_HOSTS"
TRUSTED_PROXIES_ENV = "POWERQUERY_TRUSTED_PROXIES"


class DefaultCredentialsWarning(UserWarning):
    """Warn that the public demonstration credential is still active."""


class AdminAuthError(Exception):
    """Base class for safe, user-presentable authentication failures."""


class AdminAuthConfigurationError(AdminAuthError):
    """The authentication environment is incomplete or unsafe."""


class InvalidCredentials(AdminAuthError):
    """A login attempt did not match the configured account."""

    def __init__(self) -> None:
        super().__init__("帳號或密碼錯誤。")


class LoginRateLimited(AdminAuthError):
    """Too many recent login failures came from one client."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__("登入嘗試過於頻繁，請稍後再試。")


class InvalidAdminSession(AdminAuthError):
    """The supplied session is missing, unknown, or revoked."""

    def __init__(self) -> None:
        super().__init__("管理員階段已失效，請重新登入。")


class ExpiredAdminSession(InvalidAdminSession):
    """The supplied session passed its absolute expiry time."""


class RequestOriginDenied(AdminAuthError):
    """A browser mutation did not originate from this application."""

    def __init__(self) -> None:
        super().__init__("請求來源無法驗證。")


class CsrfValidationFailed(AdminAuthError):
    """A cookie-authenticated mutation omitted its session CSRF proof."""

    def __init__(self) -> None:
        super().__init__("安全驗證碼無效，請重新登入。")


class DefaultCredentialsRemoteAccessDenied(AdminAuthError):
    """Public defaults must never authenticate a non-loopback client."""

    def __init__(self) -> None:
        super().__init__("預設管理員帳密只允許本機使用；請先以環境變數覆寫帳密。")


_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse_trusted_proxies(values: Sequence[str]) -> tuple[_IPNetwork, ...]:
    networks: list[_IPNetwork] = []
    for value in values:
        candidate = value.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError as error:
            raise AdminAuthConfigurationError(
                f"{TRUSTED_PROXIES_ENV} 的「{candidate}」不是合法的 IP 或網段。"
            ) from error
    return tuple(networks)


def _parsed_address(value: str) -> _IPAddress | None:
    candidate = value.strip().strip("[]")
    if "%" in candidate:  # IPv6 zone identifier
        candidate = candidate.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    """Authenticated identity safe to pass into audit and service layers."""

    username: str
    issued_at: datetime
    expires_at: datetime
    using_default_credentials: bool
    plant_id: int | None = None
    can_review: bool = False

    @property
    def sees_every_plant(self) -> bool:
        return self.plant_id is None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "username": self.username,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "using_default_credentials": self.using_default_credentials,
            "plant_id": self.plant_id,
            "can_review": self.can_review,
        }


@dataclass(frozen=True, slots=True)
class AdminSessionGrant:
    """One-time values needed to establish a browser session.

    The opaque cookie value and CSRF value are deliberately excluded from repr
    so test failures and application diagnostics do not print credentials.
    """

    principal: AdminPrincipal
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(slots=True)
class _StoredSession:
    username: str
    issued_at: datetime
    expires_at: datetime
    issued_monotonic: float
    expires_monotonic: float
    csrf_digest: bytes = field(repr=False)
    plant_id: int | None = None
    can_review: bool = False


@dataclass(frozen=True, slots=True)
class _StoredAccount:
    """One verifiable account: the derivation parameters plus its data scope."""

    username: str
    iterations: int
    salt: bytes = field(repr=False)
    digest: bytes = field(repr=False)
    plant_id: int | None = None
    can_review: bool = False


class AdminAuthManager:
    """Verify the single local account and manage short-lived opaque sessions."""

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        accounts: Sequence[Account] | None = None,
        trusted_proxies: Sequence[str] = (),
        using_default_credentials: bool = False,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        allowed_hosts: Sequence[str] = tuple(DEFAULT_ALLOWED_HOSTS),
        cookie_name: str = DEFAULT_COOKIE_NAME,
        csrf_header: str = DEFAULT_CSRF_HEADER,
        pbkdf2_iterations: int = DEFAULT_PBKDF2_ITERATIONS,
        login_failure_limit: int = 5,
        login_failure_window_seconds: int = 5 * 60,
        maximum_sessions: int = 32,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if accounts is not None and (username is not None or password is not None):
            raise AdminAuthConfigurationError("帳號名冊與單一帳號設定不可同時提供。")
        if accounts is None:
            if username is None or password is None:
                raise AdminAuthConfigurationError("必須提供帳號名冊或單一管理員帳密。")
            clean_username = username.strip()
            if not 1 <= len(clean_username) <= 80:
                raise AdminAuthConfigurationError("管理員帳號長度必須介於 1 與 80。")
            if not 8 <= len(password) <= 512:
                raise AdminAuthConfigurationError("管理員密碼長度必須介於 8 與 512。")
        elif not accounts:
            raise AdminAuthConfigurationError("帳號名冊至少需要一個帳號。")
        if not MINIMUM_SESSION_TTL_SECONDS <= session_ttl_seconds <= MAXIMUM_SESSION_TTL_SECONDS:
            raise AdminAuthConfigurationError(
                f"管理階段有效期必須介於 {MINIMUM_SESSION_TTL_SECONDS} 與 "
                f"{MAXIMUM_SESSION_TTL_SECONDS} 秒。"
            )
        if pbkdf2_iterations < 1_000:
            raise AdminAuthConfigurationError("PBKDF2 迭代次數過低。")
        if login_failure_limit < 1 or login_failure_window_seconds < 1:
            raise AdminAuthConfigurationError("登入限速設定必須大於零。")
        if maximum_sessions < 1:
            raise AdminAuthConfigurationError("階段數量上限必須大於零。")

        normalized_hosts = frozenset(self._normalize_host(value) for value in allowed_hosts)
        if not normalized_hosts or "" in normalized_hosts:
            raise AdminAuthConfigurationError("至少需要一個可信任的管理介面主機。")
        if not cookie_name or any(character in cookie_name for character in " ;,=\r\n\t"):
            raise AdminAuthConfigurationError("Cookie 名稱無效。")
        if not csrf_header or "\r" in csrf_header or "\n" in csrf_header:
            raise AdminAuthConfigurationError("CSRF header 名稱無效。")

        self.username = clean_username if accounts is None else None
        self.using_default_credentials = using_default_credentials
        self.session_ttl_seconds = int(session_ttl_seconds)
        self.allowed_hosts = normalized_hosts
        self.trusted_proxies = _parse_trusted_proxies(trusted_proxies)
        self.cookie_name = cookie_name
        self.csrf_header = csrf_header
        self._pbkdf2_iterations = int(pbkdf2_iterations)
        self._login_failure_limit = int(login_failure_limit)
        self._login_failure_window_seconds = int(login_failure_window_seconds)
        self._maximum_sessions = int(maximum_sessions)
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._lock = RLock()
        self._login_lock = Lock()

        if accounts is None:
            salt = secrets.token_bytes(16)
            self._accounts = {
                clean_username: _StoredAccount(
                    username=clean_username,
                    iterations=self._pbkdf2_iterations,
                    salt=salt,
                    digest=self._derive_password(password or "", salt, self._pbkdf2_iterations),
                    plant_id=None,
                    can_review=True,
                )
            }
        else:
            self._accounts = {}
            for account in accounts:
                iterations, salt, digest = decode_password_hash(account.password_hash)
                self._accounts[account.username] = _StoredAccount(
                    username=account.username,
                    iterations=iterations,
                    salt=salt,
                    digest=digest,
                    plant_id=account.plant_id,
                    can_review=account.can_review,
                )
        self._dummy_password_salt = secrets.token_bytes(16)
        self._dummy_password_digest = self._derive_password(
            secrets.token_urlsafe(24), self._dummy_password_salt, self._pbkdf2_iterations
        )
        self._sessions: dict[bytes, _StoredSession] = {}
        self._login_failures: dict[bytes, deque[float]] = {}

        if self.using_default_credentials:
            warnings.warn(
                "PowerQuery TW 正在使用公開的預設管理員帳密；"
                f"請以 {ADMIN_USERNAME_ENV} 與 {ADMIN_PASSWORD_ENV} 覆寫。",
                DefaultCredentialsWarning,
                stacklevel=2,
            )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> AdminAuthManager:
        """Build a manager from an all-or-nothing account environment override."""

        source = os.environ if environ is None else environ
        has_username = ADMIN_USERNAME_ENV in source
        has_password = ADMIN_PASSWORD_ENV in source
        if has_username != has_password:
            raise AdminAuthConfigurationError(
                f"{ADMIN_USERNAME_ENV} 與 {ADMIN_PASSWORD_ENV} 必須同時設定。"
            )

        using_defaults = not has_username
        username = source[ADMIN_USERNAME_ENV] if has_username else DEFAULT_ADMIN_USERNAME
        password = source[ADMIN_PASSWORD_ENV] if has_password else DEFAULT_ADMIN_PASSWORD
        if has_username and (not username.strip() or not password):
            raise AdminAuthConfigurationError("管理員帳號與密碼不可為空。")

        raw_ttl = source.get(SESSION_TTL_ENV)
        if raw_ttl is not None:
            try:
                kwargs["session_ttl_seconds"] = int(raw_ttl)
            except ValueError as error:
                raise AdminAuthConfigurationError(f"{SESSION_TTL_ENV} 必須是整數秒數。") from error

        raw_hosts = source.get(ALLOWED_HOSTS_ENV)
        if raw_hosts is not None:
            hosts = tuple(item.strip() for item in raw_hosts.split(",") if item.strip())
            if not hosts:
                raise AdminAuthConfigurationError(f"{ALLOWED_HOSTS_ENV} 不可為空。")
            kwargs["allowed_hosts"] = hosts

        raw_proxies = source.get(TRUSTED_PROXIES_ENV)
        if raw_proxies is not None:
            kwargs["trusted_proxies"] = tuple(
                item.strip() for item in raw_proxies.split(",") if item.strip()
            )

        return cls(
            username=username,
            password=password,
            using_default_credentials=using_defaults,
            **kwargs,
        )

    @classmethod
    def from_roster(
        cls,
        accounts: Sequence[Account],
        environ: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> AdminAuthManager:
        """Build a manager from an explicit roster, reusing the environment policy knobs.

        名冊存在時，環境變數裡的單一管理員帳密不再生效：兩套帳號來源同時有效會讓
        「誰能登入」變成要看載入順序，這正是權限設定最不該有的性質。
        """

        source = os.environ if environ is None else environ
        raw_ttl = source.get(SESSION_TTL_ENV)
        if raw_ttl is not None:
            try:
                kwargs["session_ttl_seconds"] = int(raw_ttl)
            except ValueError as error:
                raise AdminAuthConfigurationError(f"{SESSION_TTL_ENV} 必須是整數秒數。") from error

        raw_hosts = source.get(ALLOWED_HOSTS_ENV)
        if raw_hosts is not None:
            hosts = tuple(item.strip() for item in raw_hosts.split(",") if item.strip())
            if not hosts:
                raise AdminAuthConfigurationError(f"{ALLOWED_HOSTS_ENV} 不可為空。")
            kwargs["allowed_hosts"] = hosts

        raw_proxies = source.get(TRUSTED_PROXIES_ENV)
        if raw_proxies is not None:
            kwargs["trusted_proxies"] = tuple(
                item.strip() for item in raw_proxies.split(",") if item.strip()
            )

        return cls(accounts=tuple(accounts), **kwargs)

    def __repr__(self) -> str:
        return (
            f"AdminAuthManager(username={self.username!r}, "
            f"using_default_credentials={self.using_default_credentials!r}, "
            f"session_ttl_seconds={self.session_ttl_seconds!r}, "
            f"active_sessions={self.active_session_count!r})"
        )

    @staticmethod
    def _normalize_host(value: str) -> str:
        host = value.strip().lower()
        if host.startswith("[") and host.endswith("]"):
            return host[1:-1]
        return host

    @staticmethod
    def _digest_text(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8", errors="strict")).digest()

    def _derive_password(self, password: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8", errors="strict"),
            salt,
            iterations,
        )

    def _now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _client_bucket_key(self, client_id: str | None) -> bytes:
        return self._digest_text(client_id or "unknown-client")

    def _prune_failure_bucket(self, key: bytes, now: float) -> deque[float]:
        failures = self._login_failures.setdefault(key, deque())
        threshold = now - self._login_failure_window_seconds
        while failures and failures[0] <= threshold:
            failures.popleft()
        if not failures:
            self._login_failures.pop(key, None)
            failures = deque()
        return failures

    def _check_login_rate_limit(self, key: bytes, now: float) -> None:
        failures = self._prune_failure_bucket(key, now)
        if len(failures) < self._login_failure_limit:
            return
        retry_after = math.ceil(self._login_failure_window_seconds - (now - failures[0]))
        raise LoginRateLimited(retry_after)

    def _record_login_failure(self, key: bytes, now: float) -> None:
        failures = self._login_failures.setdefault(key, deque())
        failures.append(now)
        if len(self._login_failures) > 1_024:
            oldest_key = min(
                self._login_failures,
                key=lambda item: (
                    self._login_failures[item][-1] if self._login_failures[item] else float("-inf")
                ),
            )
            if oldest_key != key:
                self._login_failures.pop(oldest_key, None)

    def _prune_sessions(self, now: float) -> None:
        expired = [
            digest for digest, session in self._sessions.items() if session.expires_monotonic <= now
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def _issue_session(self, now: float, account: _StoredAccount) -> AdminSessionGrant:
        self._prune_sessions(now)
        if len(self._sessions) >= self._maximum_sessions:
            oldest = min(
                self._sessions,
                key=lambda digest: self._sessions[digest].issued_monotonic,
            )
            self._sessions.pop(oldest, None)

        for _attempt in range(8):
            token = secrets.token_urlsafe(32)
            token_digest = self._digest_text(token)
            if token_digest not in self._sessions:
                break
        else:  # pragma: no cover - cryptographically implausible collision loop
            raise RuntimeError("無法產生唯一的管理階段。")

        csrf_token = secrets.token_urlsafe(32)
        issued_at = self._now()
        expires_at = issued_at + timedelta(seconds=self.session_ttl_seconds)
        stored = _StoredSession(
            username=account.username,
            issued_at=issued_at,
            expires_at=expires_at,
            issued_monotonic=now,
            expires_monotonic=now + self.session_ttl_seconds,
            csrf_digest=self._digest_text(csrf_token),
            plant_id=account.plant_id,
            can_review=account.can_review,
        )
        self._sessions[token_digest] = stored
        principal = self._principal(stored)
        return AdminSessionGrant(principal=principal, token=token, csrf_token=csrf_token)

    def login(
        self,
        username: str,
        password: str,
        *,
        client_id: str | None = None,
    ) -> AdminSessionGrant:
        """Validate credentials and create a fresh, absolute-expiry session."""

        # Serialize only credential verification.  Session reads remain
        # concurrent, while parallel guesses cannot all slip past the same
        # pre-verification rate-limit snapshot.
        with self._login_lock:
            now = self._monotonic_clock()
            bucket_key = self._client_bucket_key(client_id)
            with self._lock:
                self._check_login_rate_limit(bucket_key, now)

            clean_username = username.strip() if isinstance(username, str) else ""
            supplied_password = password if isinstance(password, str) else ""
            account = self._accounts.get(clean_username)
            password_shape_ok = 1 <= len(supplied_password) <= 512
            candidate_password = (
                supplied_password if password_shape_ok else "invalid-password-shape"
            )
            salt = account.salt if account is not None else self._dummy_password_salt
            expected = account.digest if account is not None else self._dummy_password_digest
            iterations = account.iterations if account is not None else self._pbkdf2_iterations
            candidate = self._derive_password(candidate_password, salt, iterations)
            password_ok = hmac.compare_digest(candidate, expected)

            with self._lock:
                if not (account is not None and password_shape_ok and password_ok):
                    self._record_login_failure(bucket_key, now)
                    raise InvalidCredentials
                self._login_failures.pop(bucket_key, None)
                return self._issue_session(now, account)

    def _principal(self, session: _StoredSession) -> AdminPrincipal:
        return AdminPrincipal(
            username=session.username,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
            using_default_credentials=self.using_default_credentials,
            plant_id=session.plant_id,
            can_review=session.can_review,
        )

    def _lookup_session(self, token: str, now: float) -> _StoredSession:
        token_digest = self._digest_text(token if isinstance(token, str) else "")
        session = self._sessions.get(token_digest)
        if session is None:
            raise InvalidAdminSession
        if session.expires_monotonic <= now:
            self._sessions.pop(token_digest, None)
            raise ExpiredAdminSession
        return session

    def authenticate(self, token: str | None) -> AdminPrincipal:
        """Authenticate an opaque cookie without extending its absolute TTL."""

        if not token:
            raise InvalidAdminSession
        with self._lock:
            session = self._lookup_session(token, self._monotonic_clock())
            return self._principal(session)

    def revoke(self, token: str | None) -> bool:
        """Revoke one session and report whether it had still existed."""

        if not token:
            return False
        token_digest = self._digest_text(token)
        with self._lock:
            return self._sessions.pop(token_digest, None) is not None

    def validate_csrf(self, token: str | None, csrf_token: str | None) -> AdminPrincipal:
        """Authenticate a session and compare its CSRF proof in constant time."""

        if not token:
            raise InvalidAdminSession
        if not csrf_token:
            raise CsrfValidationFailed
        with self._lock:
            session = self._lookup_session(token, self._monotonic_clock())
            candidate = self._digest_text(csrf_token)
            if not hmac.compare_digest(candidate, session.csrf_digest):
                raise CsrfValidationFailed
            return self._principal(session)

    def rotate_csrf(self, token: str | None) -> tuple[AdminPrincipal, str]:
        """Replace a live session's CSRF proof without extending its lifetime.

        This supports restoring a page after refresh: the HttpOnly cookie is
        still present, while the previous JavaScript-memory CSRF value is gone.
        Only the new digest is retained server-side, so the old proof stops
        working as soon as this method returns.
        """

        if not token:
            raise InvalidAdminSession
        with self._lock:
            session = self._lookup_session(token, self._monotonic_clock())
            csrf_token = secrets.token_urlsafe(32)
            session.csrf_digest = self._digest_text(csrf_token)
            return self._principal(session), csrf_token

    @property
    def active_session_count(self) -> int:
        with self._lock:
            self._prune_sessions(self._monotonic_clock())
            return len(self._sessions)

    def public_configuration(self) -> dict[str, object]:
        """Return UI-safe policy metadata; no credential material is included."""

        return {
            "username": self.username,
            "using_default_credentials": self.using_default_credentials,
            "session_ttl_seconds": self.session_ttl_seconds,
        }

    @staticmethod
    def _canonical_origin(scheme: str, hostname: str, port: int | None) -> str:
        normalized_scheme = scheme.lower()
        normalized_host = hostname.lower()
        default_port = 443 if normalized_scheme == "https" else 80
        effective_port = port or default_port
        return f"{normalized_scheme}://{normalized_host}:{effective_port}"

    def validate_request_origin(self, request: Request) -> None:
        """Require an exact, trusted same-origin browser mutation."""

        raw_host = request.headers.get("host", "")
        if not raw_host or any(character in raw_host for character in "@/?#\\\r\n\t "):
            raise RequestOriginDenied
        request_host = self._normalize_host(request.url.hostname or "")
        if (
            request.url.username is not None
            or request.url.password is not None
            or request_host not in self.allowed_hosts
        ):
            raise RequestOriginDenied

        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site.lower() != "same-origin":
            raise RequestOriginDenied

        raw_origin = request.headers.get("origin")
        if not raw_origin:
            raise RequestOriginDenied
        try:
            parsed = urlsplit(raw_origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            supplied = self._canonical_origin(parsed.scheme, parsed.hostname, parsed.port)
            expected = self._canonical_origin(
                request.url.scheme,
                request_host,
                request.url.port,
            )
        except (TypeError, ValueError):
            raise RequestOriginDenied from None
        if not hmac.compare_digest(supplied, expected):
            raise RequestOriginDenied

    def _is_trusted_proxy(self, address: _IPAddress) -> bool:
        return any(address in network for network in self.trusted_proxies)

    def client_address(self, request: Request) -> str | None:
        """Resolve the real client, honouring X-Forwarded-For only behind a trusted proxy.

        直連位址就是對端位址。只有當對端本身是設定過的信任代理時，才往
        `X-Forwarded-For` 裡找：由右往左跳過所有信任代理，第一個不是代理的位址就是
        真正的來源。沒有設定信任代理時完全忽略這個 header —— 否則任何人都能自己填一
        個來源位址，同時繞過登入限速與「預設帳密只准本機」這兩道。

        反向代理（例如 Tailscale Funnel → 127.0.0.1）若未設定，所有外部訪客在服務眼中
        都是同一個 loopback 位址：限速會變成全域共用一桶，loopback 判斷也會誤放。
        """

        peer = request.client.host if request.client is not None else None
        if peer is None:
            return None
        peer_address = _parsed_address(peer)
        if peer_address is None or not self._is_trusted_proxy(peer_address):
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [item.strip() for item in forwarded.split(",") if item.strip()]
        for raw in reversed(hops):
            address = _parsed_address(raw)
            if address is None:
                # 這一段無法解析就不猜來源；回報未知，讓限速與 loopback 判斷從嚴。
                return None
            if not self._is_trusted_proxy(address):
                return str(address)
        return peer

    @staticmethod
    def _is_loopback_client(client_host: str | None) -> bool:
        if not client_host:
            return False
        normalized = client_host.strip().strip("[]").lower()
        if normalized in {"localhost", "testclient"}:
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    def validate_login_request(self, request: Request) -> None:
        """Validate login origin and forbid public defaults over the network."""

        self.validate_request_origin(request)
        if self.using_default_credentials and not self._is_loopback_client(
            self.client_address(request)
        ):
            raise DefaultCredentialsRemoteAccessDenied

    def authenticate_request(
        self,
        request: Request,
        *,
        require_csrf: bool = False,
    ) -> AdminPrincipal:
        """Authenticate the management cookie, with origin/CSRF for mutations."""

        if require_csrf:
            self.validate_request_origin(request)
        token = request.cookies.get(self.cookie_name)
        if require_csrf:
            return self.validate_csrf(token, request.headers.get(self.csrf_header))
        return self.authenticate(token)

    def revoke_request(self, request: Request) -> bool:
        return self.revoke(request.cookies.get(self.cookie_name))

    def set_session_cookie(
        self,
        response: Response,
        grant: AdminSessionGrant,
        *,
        secure: bool,
    ) -> None:
        """Attach the opaque session to a host-only HttpOnly cookie."""

        response.set_cookie(
            key=self.cookie_name,
            value=grant.token,
            max_age=self.session_ttl_seconds,
            expires=grant.principal.expires_at,
            path="/api",
            secure=secure,
            httponly=True,
            samesite="strict",
        )
        self.mark_no_store(response)

    @staticmethod
    def cookie_should_be_secure(request: Request) -> bool:
        """Use Secure cookies whenever the management page itself uses HTTPS."""

        return request.url.scheme.lower() == "https"

    def clear_session_cookie(self, response: Response, *, secure: bool) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            path="/api",
            secure=secure,
            httponly=True,
            samesite="strict",
        )
        self.mark_no_store(response)

    @staticmethod
    def mark_no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
