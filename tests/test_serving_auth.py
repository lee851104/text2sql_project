from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from serving.admin_auth import AdminAuthManager
from serving.app import create_app

ORIGIN = "http://testserver"
USERNAME = "review-admin"
PASSWORD = "correct horse battery staple"


class FakePipelineResponse:
    def to_dict(self) -> dict[str, object]:
        return {
            "success": False,
            "error": "demo response",
            "error_code": "DEMO",
            "severity": "refuse",
        }


class FakePipeline:
    def query(self, question: str, *, plant: str | None = None) -> FakePipelineResponse:
        del question, plant
        return FakePipelineResponse()


class FakeExecutor:
    def execute(self, sql: str, params: tuple[object, ...]) -> tuple[list[str], list[list[int]]]:
        del sql, params
        return ["count"], [[1]]


class FakeRuntime:
    def __init__(self) -> None:
        self.pipeline = FakePipeline()
        self.executor = FakeExecutor()
        self.online_llm = False
        self.data_range = ("2025-01-01", "2026-07-31")
        self.mode = "offline"
        self.provider = "offline"
        self.model = "offline-router"


class FakeRuntimeManager:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.configuration_calls: list[dict[str, object]] = []

    @staticmethod
    def status() -> dict[str, object]:
        return {
            "default_mode": "offline",
            "active_mode": "offline",
            "online_configured": False,
            "provider": "offline",
            "model": "offline-router",
            "source": "offline",
            "offline_capability": True,
        }

    def runtime_and_status(self) -> tuple[FakeRuntime, dict[str, object]]:
        return self.runtime, self.status()

    def get_runtime(self, mode: str | None = None) -> FakeRuntime:
        del mode
        return self.runtime

    def configure_and_status(self, **updates: object) -> dict[str, object]:
        self.configuration_calls.append(dict(updates))
        return self.status()


class FakeLearningService:
    def __init__(self) -> None:
        self.review_calls: list[dict[str, object]] = []

    @staticmethod
    def status() -> dict[str, object]:
        return {
            "workspace_ready": True,
            "index_synchronized": True,
            "candidate_counts": {"total": 1, "pending_review": 1},
        }

    @staticmethod
    def list_entries(*, state: str | None, limit: int) -> list[dict[str, object]]:
        del state, limit
        return [{"id": "candidate-1", "status": "pending_review"}]

    @staticmethod
    def list_events(*, limit: int) -> list[dict[str, object]]:
        del limit
        return [{"event": "candidate_pending_review", "candidate_id": "candidate-1"}]

    def review(
        self,
        candidate_id: str,
        *,
        approve: bool,
        reviewer: str,
        note: str,
        pipeline: FakePipeline,
    ) -> dict[str, object]:
        call = {
            "candidate_id": candidate_id,
            "approve": approve,
            "reviewer": reviewer,
            "note": note,
            "pipeline": pipeline,
        }
        self.review_calls.append(call)
        return {
            "id": candidate_id,
            "status": "promoted" if approve else "rejected",
            "approved_by": reviewer,
            "review_note": note,
        }


@pytest.fixture
def auth_manager() -> AdminAuthManager:
    return AdminAuthManager(
        username=USERNAME,
        password=PASSWORD,
        pbkdf2_iterations=1_000,
    )


@pytest.fixture
def client(auth_manager: AdminAuthManager):
    application = create_app(auth_manager=auth_manager)
    runtime = FakeRuntime()
    application.state.runtime_manager = FakeRuntimeManager(runtime)
    application.state.learning_service = FakeLearningService()
    application.state.learning_pipelines.add(runtime.pipeline)
    with TestClient(application, base_url=ORIGIN) as test_client:
        yield test_client


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", **extra}


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/admin/session",
        headers=_origin_headers(),
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert response.status_code == 200
    return str(response.json()["data"]["csrf_token"])


def _mutation_headers(csrf_token: str, *, origin: str = ORIGIN) -> dict[str, str]:
    return {
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin" if origin == ORIGIN else "cross-site",
        "X-PowerQuery-CSRF": csrf_token,
    }


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/training-status", None),
        ("get", "/api/runtime/llm", None),
        ("get", "/api/corpus/entries", None),
        ("get", "/api/corpus/events", None),
        ("put", "/api/runtime/llm", {"mode": "offline"}),
        (
            "post",
            "/api/corpus/entries/candidate-1/review",
            {"decision": "approve"},
        ),
    ],
)
def test_management_endpoints_require_session(
    client: TestClient,
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    response = client.request(
        method,
        path,
        headers=_origin_headers(),
        json=json_body,
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "cookie" not in response.text.lower()


def test_query_and_public_status_endpoints_remain_available_without_login(
    client: TestClient,
) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/examples").status_code == 200
    query = client.post("/api/query", json={"question": "公開展示查詢"})
    assert query.status_code == 200
    assert query.json()["error_code"] == "DEMO"

    session = client.get("/api/admin/session")
    assert session.status_code == 200
    assert session.json()["data"] == {
        "authenticated": False,
        "using_default_credentials": False,
        "session_ttl_seconds": 900,
    }
    assert session.headers["cache-control"] == "no-store"


def test_login_cookie_session_refresh_rotation_and_logout(client: TestClient) -> None:
    login = client.post(
        "/api/admin/session",
        headers=_origin_headers(),
        json={"username": USERNAME, "password": PASSWORD},
    )

    assert login.status_code == 200
    login_data = login.json()["data"]
    first_csrf = login_data["csrf_token"]
    assert login_data["authenticated"] is True
    assert login_data["username"] == USERNAME
    assert PASSWORD not in login.text
    assert "token" not in {key.lower() for key in login_data if key != "csrf_token"}
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api" in cookie
    assert "domain=" not in cookie
    assert login.headers["cache-control"] == "no-store"

    assert client.get("/api/runtime/llm").status_code == 200
    refreshed = client.get("/api/admin/session")
    assert refreshed.status_code == 200
    second_csrf = refreshed.json()["data"]["csrf_token"]
    assert second_csrf != first_csrf

    stale = client.put(
        "/api/runtime/llm",
        headers=_mutation_headers(first_csrf),
        json={"mode": "offline"},
    )
    assert stale.status_code == 403
    configured = client.put(
        "/api/runtime/llm",
        headers=_mutation_headers(second_csrf),
        json={"mode": "offline"},
    )
    assert configured.status_code == 200

    logout = client.delete(
        "/api/admin/session",
        headers=_mutation_headers(second_csrf),
    )
    assert logout.status_code == 200
    assert logout.json()["data"]["authenticated"] is False
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert client.get("/api/admin/session").json()["data"]["authenticated"] is False
    assert client.get("/api/runtime/llm").status_code == 401


def test_login_failures_are_sanitized_and_rate_limited() -> None:
    auth = AdminAuthManager(
        username=USERNAME,
        password=PASSWORD,
        pbkdf2_iterations=1_000,
        login_failure_limit=1,
        login_failure_window_seconds=60,
    )
    with TestClient(create_app(auth_manager=auth), base_url=ORIGIN) as test_client:
        secret = "not-the-password-secret"
        failed = test_client.post(
            "/api/admin/session",
            headers=_origin_headers(),
            json={"username": USERNAME, "password": secret},
        )
        limited = test_client.post(
            "/api/admin/session",
            headers=_origin_headers(),
            json={"username": USERNAME, "password": PASSWORD},
        )

    assert failed.status_code == 401
    assert failed.json()["detail"] == "帳號或密碼錯誤。"
    assert secret not in failed.text
    assert failed.headers["cache-control"] == "no-store"
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.headers["cache-control"] == "no-store"


def test_validation_error_does_not_reflect_malformed_login_secret(client: TestClient) -> None:
    secret = "malformed-login-secret"
    response = client.post(
        "/api/admin/session",
        headers=_origin_headers(),
        json={"username": USERNAME, "password": [secret]},
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert all("input" not in item for item in response.json()["detail"])
    assert response.headers["cache-control"] == "no-store"


def test_management_mutations_require_same_origin_and_current_csrf(client: TestClient) -> None:
    csrf_token = _login(client)

    missing_csrf = client.put(
        "/api/runtime/llm",
        headers=_origin_headers(),
        json={"mode": "offline"},
    )
    wrong_csrf = client.put(
        "/api/runtime/llm",
        headers=_mutation_headers("wrong-csrf"),
        json={"mode": "offline"},
    )
    cross_site = client.put(
        "/api/runtime/llm",
        headers=_mutation_headers(csrf_token, origin="http://attacker.example"),
        json={"mode": "offline"},
    )

    assert missing_csrf.status_code == 403
    assert wrong_csrf.status_code == 403
    assert cross_site.status_code == 403
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (missing_csrf, wrong_csrf, cross_site)
    )
    # Read-only management endpoints require the cookie but not an Origin or
    # JavaScript-held CSRF value.
    assert client.get("/api/runtime/llm").status_code == 200


def test_corpus_review_identity_comes_only_from_authenticated_principal(
    client: TestClient,
) -> None:
    csrf_token = _login(client)
    learning: FakeLearningService = client.app.state.learning_service

    assert client.get("/api/training-status").status_code == 200
    assert client.get("/api/corpus/entries").status_code == 200
    assert client.get("/api/corpus/events").status_code == 200

    spoofed = client.post(
        "/api/corpus/entries/candidate-1/review",
        headers=_mutation_headers(csrf_token),
        json={
            "decision": "approve",
            "reviewer": "forged-reviewer",
            "note": "looks good",
        },
    )
    assert spoofed.status_code == 422
    assert learning.review_calls == []

    reviewed = client.post(
        "/api/corpus/entries/candidate-1/review",
        headers=_mutation_headers(csrf_token),
        json={"decision": "approve", "note": "  looks good  "},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["approved_by"] == USERNAME
    assert learning.review_calls == [
        {
            "candidate_id": "candidate-1",
            "approve": True,
            "reviewer": USERNAME,
            "note": "looks good",
            "pipeline": client.app.state.runtime_manager.runtime.pipeline,
        }
    ]
    assert reviewed.headers["cache-control"] == "no-store"


def test_invalid_cookie_is_cleared_by_session_status(client: TestClient) -> None:
    client.cookies.set("powerquery_admin_session", "invalid-cookie", path="/api")

    response = client.get("/api/admin/session")

    assert response.status_code == 200
    assert response.json()["data"]["authenticated"] is False
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/api/runtime/llm").status_code == 401


def test_auth_responses_never_contain_manager_secrets(
    client: TestClient,
    auth_manager: AdminAuthManager,
) -> None:
    csrf_token = _login(client)
    responses: list[Any] = [
        client.get("/api/admin/session"),
        client.get("/api/runtime/llm"),
        client.put(
            "/api/runtime/llm",
            headers=_mutation_headers(csrf_token),
            json={"mode": "offline"},
        ),
    ]
    for response in responses:
        assert PASSWORD not in response.text
        assert "password_hash" not in response.text
        assert auth_manager.cookie_name not in response.text
