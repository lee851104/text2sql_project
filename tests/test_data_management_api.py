from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT, resolve_configured_paths
from serving.accounts import MINIMUM_ITERATIONS, Account, hash_password
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.runtime import build_runtime

ORIGIN = "http://testserver"
UPLOADER = "data-uploader"
REVIEWER = "data-reviewer"
PASSWORD = "fast test administrator password"


def _origin_headers(**extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", **extra}


@pytest.fixture(scope="module")
def data_api(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("data-management-api")
    database = root / "power.db"
    build_database(database, root=PROJECT_ROOT)
    runtime = build_runtime(database=database, root=PROJECT_ROOT, mode="offline")
    encoded = hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)
    accounts = [
        Account(username=UPLOADER, password_hash=encoded, plant_id=None),
        Account(
            username=REVIEWER,
            password_hash=encoded,
            plant_id=None,
            can_review=True,
        ),
    ]
    application = create_app(
        runtime,
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
    )
    outage_path = resolve_configured_paths(PROJECT_ROOT)["outage_csv"]
    outage_payload = outage_path.read_bytes()

    with TestClient(application, base_url=ORIGIN) as client:
        yield client, outage_payload, outage_path.name


def _sign_in(client: TestClient, username: str) -> dict[str, str]:
    """Switch the client to one account and return its mutation headers."""

    client.cookies.clear()
    login = client.post(
        "/api/admin/session",
        headers=_origin_headers(),
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return _origin_headers(**{"X-PowerQuery-CSRF": login.json()["data"]["csrf_token"]})


@pytest.mark.e2e
def test_authenticated_data_hot_unplug_upload_review_and_audit(data_api) -> None:
    client, outage_payload, outage_filename = data_api

    anonymous = client.get("/api/data/status")
    assert anonymous.status_code == 401
    assert anonymous.headers["cache-control"] == "no-store"

    uploader_headers = _sign_in(client, UPLOADER)

    initial_status = client.get("/api/data/status")
    assert initial_status.status_code == 200
    initial = initial_status.json()["data"]
    baseline_version = initial["active_version"]
    assert initial["sources"]["outage_csv"]["present"] is True
    assert initial["audit_chain_valid"] is True
    assert client.get("/api/stats").json()["data"]["outage_records"] == 138

    required_remove = client.post(
        "/api/data/changes/remove",
        headers=uploader_headers,
        json={"dataset": "units_csv", "reason": "must remain required"},
    )
    assert required_remove.status_code == 400
    assert client.get("/api/stats").json()["data"]["outage_records"] == 138

    staged_remove_response = client.post(
        "/api/data/changes/remove",
        headers=uploader_headers,
        json={"dataset": "outage_csv", "reason": "hot-unplug demonstration"},
    )
    assert staged_remove_response.status_code == 200
    staged_remove = staged_remove_response.json()["data"]
    assert staged_remove["status"] == "pending_review"
    assert staged_remove["created_by"] == UPLOADER
    assert staged_remove["request_reason"] == "hot-unplug demonstration"
    assert client.get("/api/stats").json()["data"]["outage_records"] == 138

    # 提案人沒有審核權，而且就算有也不能核准自己的變更；兩道都不得讓資料上線。
    self_approval = client.post(
        f"/api/data/changes/{staged_remove['id']}/review",
        headers=uploader_headers,
        json={"decision": "approve", "note": "approving my own request"},
    )
    assert self_approval.status_code == 403
    assert "審核權" in self_approval.json()["detail"]
    assert client.get("/api/stats").json()["data"]["outage_records"] == 138

    reviewer_headers = _sign_in(client, REVIEWER)
    remove_review = client.post(
        f"/api/data/changes/{staged_remove['id']}/review",
        headers=reviewer_headers,
        json={"decision": "approve", "note": "source removal reviewed"},
    )
    assert remove_review.status_code == 200
    approved_remove = remove_review.json()["data"]
    assert approved_remove["status"] == "approved"
    assert approved_remove["reviewed_by"] == REVIEWER
    assert approved_remove["self_approved"] is False
    assert approved_remove["review_reason"] == "source removal reviewed"
    assert client.get("/api/stats").json()["data"]["outage_records"] == 0

    removed_files = client.get("/api/data/files")
    assert removed_files.status_code == 200
    assert removed_files.json()["data"]["files"]["outage_csv"]["present"] is False

    uploader_headers = _sign_in(client, UPLOADER)
    staged_upload_response = client.post(
        "/api/data/changes/upload",
        headers=uploader_headers,
        json={
            "dataset": "outage_csv",
            "filename": outage_filename,
            "content_base64": base64.b64encode(outage_payload).decode("ascii"),
            "reason": "restore reviewed Taipower outage source",
        },
    )
    assert staged_upload_response.status_code == 200
    staged_upload = staged_upload_response.json()["data"]
    assert staged_upload["status"] == "pending_review"
    assert staged_upload["created_by"] == UPLOADER
    assert staged_upload["candidate_version"] == baseline_version
    assert client.get("/api/stats").json()["data"]["outage_records"] == 0

    reviewer_headers = _sign_in(client, REVIEWER)
    spoofed_reviewer = client.post(
        f"/api/data/changes/{staged_upload['id']}/review",
        headers=reviewer_headers,
        json={"decision": "approve", "reviewer": "spoofed-user"},
    )
    assert spoofed_reviewer.status_code == 422
    assert client.get("/api/stats").json()["data"]["outage_records"] == 0

    upload_review = client.post(
        f"/api/data/changes/{staged_upload['id']}/review",
        headers=reviewer_headers,
        json={"decision": "approve", "note": "uploaded source reviewed"},
    )
    assert upload_review.status_code == 200
    approved_upload = upload_review.json()["data"]
    assert approved_upload["status"] == "approved"
    assert approved_upload["reviewed_by"] == REVIEWER
    assert approved_upload["self_approved"] is False
    assert approved_upload["review_reason"] == "uploaded source reviewed"
    assert client.get("/api/stats").json()["data"]["outage_records"] == 138

    files = client.get("/api/data/files").json()["data"]
    assert files["version"] == baseline_version
    assert files["files"]["outage_csv"]["present"] is True
    downloaded = client.get("/api/data/files/outage_csv")
    assert downloaded.status_code == 200
    assert downloaded.content == outage_payload
    assert "outage.csv" in downloaded.headers["content-disposition"]

    changes = client.get("/api/data/changes").json()["data"]["changes"]
    by_id = {change["id"]: change for change in changes}
    assert by_id[staged_remove["id"]]["status"] == "approved"
    assert by_id[staged_upload["id"]]["status"] == "approved"
    assert by_id[staged_remove["id"]]["reviewed_by"] == REVIEWER
    assert by_id[staged_upload["id"]]["reviewed_by"] == REVIEWER

    versions_response = client.get("/api/data/versions")
    assert versions_response.status_code == 200
    versions = versions_response.json()["data"]["versions"]
    assert len(versions) == 2
    assert {version["version"] for version in versions} == {
        baseline_version,
        staged_remove["candidate_version"],
    }
    assert next(version for version in versions if version["active"])["version"] == baseline_version

    audit_response = client.get("/api/data/events")
    assert audit_response.status_code == 200
    events = audit_response.json()["data"]["events"]
    approved_events = [event for event in events if event["event"] == "change_approved"]
    staged_events = [event for event in events if event["event"] == "change_staged"]
    assert len(approved_events) == 2
    assert len(staged_events) == 2
    # 提案與發布在稽核鏈上是兩個不同的名字，被擋下的那次自審也留了紀錄。
    assert all(event["actor"] == UPLOADER for event in staged_events)
    assert all(event["actor"] == REVIEWER for event in approved_events)
    assert all(event["details"]["self_approved"] is False for event in approved_events)
    assert all(len(event["event_hash"]) == 64 for event in events)

    final_status = client.get("/api/data/status").json()["data"]
    assert final_status["active_version"] == baseline_version
    assert final_status["sources"]["outage_csv"]["present"] is True
    assert final_status["changes"]["approved"] == 2
    assert final_status["audit_chain_valid"] is True


@pytest.mark.e2e
def test_the_two_refusals_are_distinct_and_only_one_reaches_the_journal(data_api) -> None:
    """審核權與四眼是兩道不同的檢查，順序也不同。

    沒有審核權的帳號在 API 層就被擋下，請求不會進到資料管理服務，所以稽核鏈上沒有
    紀錄 —— 那是 HTTP 存取層的事。有審核權但核准自己提案的帳號會走到服務層，由四眼
    規則擋下並留痕。
    """

    client, _payload, _filename = data_api

    uploader_headers = _sign_in(client, UPLOADER)
    staged = client.post(
        "/api/data/changes/remove",
        headers=uploader_headers,
        json={"dataset": "outage_csv", "reason": "capability versus four eyes"},
    ).json()["data"]

    without_capability = client.post(
        f"/api/data/changes/{staged['id']}/review",
        headers=uploader_headers,
        json={"decision": "approve"},
    )
    assert without_capability.status_code == 403
    assert "審核權" in without_capability.json()["detail"]

    reviewer_headers = _sign_in(client, REVIEWER)
    own = client.post(
        "/api/data/changes/remove",
        headers=reviewer_headers,
        json={"dataset": "outage_csv", "reason": "reviewer proposes something"},
    )
    # 同一個資料槽已有待審變更時服務會擋下；改用既有的那筆驗證四眼即可。
    if own.status_code != 200:
        client.post(
            f"/api/data/changes/{staged['id']}/review",
            headers=reviewer_headers,
            json={"decision": "reject", "note": "clearing the queue"},
        )
        own = client.post(
            "/api/data/changes/remove",
            headers=reviewer_headers,
            json={"dataset": "outage_csv", "reason": "reviewer proposes something"},
        )
    assert own.status_code == 200, own.text
    own_change = own.json()["data"]

    self_approval = client.post(
        f"/api/data/changes/{own_change['id']}/review",
        headers=reviewer_headers,
        json={"decision": "approve"},
    )
    assert self_approval.status_code == 403
    assert "自己的變更" in self_approval.json()["detail"]

    events = client.get("/api/data/events?limit=200").json()["data"]["events"]
    refused = [event for event in events if event["event"] == "self_approval_refused"]
    assert len(refused) == 1
    assert refused[0]["actor"] == REVIEWER

    client.post(
        f"/api/data/changes/{own_change['id']}/review",
        headers=reviewer_headers,
        json={"decision": "reject", "note": "withdrawing"},
    )
    client.cookies.clear()
