"""Hand-written corpus examples: the same gates, a different approval rule."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT
from serving.accounts import MINIMUM_ITERATIONS, Account, hash_password
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.runtime import build_runtime

ORIGIN = "http://testserver"
PASSWORD = "corpus-submission-password"
AUTHOR = "corpus-author"
HELPER = "corpus-helper"

QUESTION = "台中發電廠有哪些機組與裝置容量"
SQL = (
    'SELECT "電廠", "機組名", "裝置容量_萬瓩" FROM v_unit '
    'WHERE "電廠" = ? ORDER BY "機組名" LIMIT 20'
)
PARAMS = ["台中發電廠"]
INTENT = "plant_unit_lookup"

# 路由答得出來，又不在 benchmark 題庫裡（在題庫裡會被洩漏關卡當場駁回）。
CAPTURED_QUESTION = "列出大林發電廠所有設備"


@pytest.fixture(scope="module")
def corpus_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("corpus-submission") / "power.db"
    build_database(database, root=PROJECT_ROOT)
    return database


@pytest.fixture(scope="module")
def corpus_client(corpus_database: Path):
    database = corpus_database
    encoded = hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)
    accounts = [
        Account(username=AUTHOR, password_hash=encoded, plant_id=None, can_review=True),
        Account(username=HELPER, password_hash=encoded, plant_id=None, can_review=True),
    ]
    application = create_app(
        build_runtime(database=database, root=PROJECT_ROOT, mode="offline"),
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
    )
    with TestClient(application, base_url=ORIGIN) as client:
        yield client


def _sign_in(client: TestClient, username: str) -> dict[str, str]:
    client.cookies.clear()
    login = client.post(
        "/api/admin/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-PowerQuery-CSRF": login.json()["data"]["csrf_token"],
    }


def _submit(client: TestClient, headers: dict[str, str], **overrides: object):
    body = {"question": QUESTION, "sql": SQL, "params": PARAMS, "intent": INTENT}
    body.update(overrides)
    return client.post("/api/corpus/entries", headers=headers, json=body)


@pytest.mark.e2e
def test_a_hand_written_example_can_be_supplied_and_self_approved(corpus_client) -> None:
    """手動提供的候選可以自審 —— 放寬的是「誰能核准」，不是「內容要不要驗」。"""

    headers = _sign_in(corpus_client, AUTHOR)

    submitted = _submit(corpus_client, headers)
    assert submitted.status_code == 200, submitted.text
    entry = submitted.json()["data"]
    assert entry["status"] == "pending_review"
    assert entry["source"] == "manual"
    assert entry["proposed_by"] == AUTHOR

    approved = corpus_client.post(
        f"/api/corpus/entries/{entry['id']}/review",
        headers=headers,
        json={"decision": "approve", "note": "wrote it myself"},
    )
    assert approved.status_code == 200, approved.text
    promoted = approved.json()["data"]
    assert promoted["status"] == "promoted"
    assert promoted["approved_by"] == AUTHOR

    # 自審放行，但六道關卡照跑，而且每一道都留下結果。
    validation = promoted["validation"]
    for gate in (
        "benchmark_leakage",
        "duplicates",
        "sql_guard",
        "semantic_question",
        "semantic_sql",
        "result_replay",
        "retrieval_regression",
    ):
        assert validation[gate]["passed"] is True, gate


@pytest.mark.e2e
def test_an_automatically_captured_candidate_still_needs_a_second_account(
    corpus_client,
) -> None:
    """手動提供是明示的提案；自動抓取可以偽裝成一般查詢，所以那條規則不放寬。"""

    headers = _sign_in(corpus_client, AUTHOR)

    # 走真正的路徑：登入後查詢，系統把成功的問答收成候選並記下是誰問的。
    answered = corpus_client.post("/api/query", json={"question": CAPTURED_QUESTION})
    assert answered.status_code == 200, answered.text
    assert answered.json()["success"] is True, answered.text

    entries = corpus_client.get("/api/corpus/entries?state=pending_review").json()
    captured = next(
        (entry for entry in entries["data"]["entries"] if entry.get("source") in {"router", "llm"}),
        None,
    )
    assert captured is not None, entries
    assert captured["proposed_by"] == AUTHOR

    refused = corpus_client.post(
        f"/api/corpus/entries/{captured['id']}/review",
        headers=headers,
        json={"decision": "approve"},
    )
    assert refused.status_code == 403
    assert "另一個帳號" in refused.json()["detail"]

    # 換一個帳號就過得了 —— 擋的是自審，不是這筆候選本身。
    other = _sign_in(corpus_client, HELPER)
    approved = corpus_client.post(
        f"/api/corpus/entries/{captured['id']}/review",
        headers=other,
        json={"decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["approved_by"] == HELPER


@pytest.mark.e2e
def test_sql_that_fails_the_guard_is_refused_before_execution(corpus_client) -> None:
    headers = _sign_in(corpus_client, AUTHOR)

    blocked = _submit(
        corpus_client,
        headers,
        question="刪掉所有機組",
        sql="DELETE FROM v_unit",
        params=[],
    )

    assert blocked.status_code == 400
    assert "安全守門" in blocked.json()["detail"]


@pytest.mark.e2e
def test_sql_selecting_a_column_outside_the_allowlist_is_refused(corpus_client) -> None:
    headers = _sign_in(corpus_client, AUTHOR)

    blocked = _submit(
        corpus_client,
        headers,
        question="機組的內部代號是什麼",
        sql='SELECT "unit_id" FROM v_unit LIMIT 5',
        params=[],
    )

    assert blocked.status_code == 400
    assert "安全守門" in blocked.json()["detail"]


@pytest.mark.e2e
def test_an_example_that_returns_nothing_is_refused(corpus_client) -> None:
    """空結果證明不了這組問答是對的，審核時的重跑比對也會變成拿空比空。"""

    headers = _sign_in(corpus_client, AUTHOR)

    empty = _submit(
        corpus_client,
        headers,
        question="不存在發電廠有哪些機組",
        params=["不存在發電廠"],
    )

    assert empty.status_code == 400
    assert "查不到任何資料" in empty.json()["detail"]


@pytest.mark.e2e
def test_a_plant_account_cannot_supply_corpus(corpus_database: Path) -> None:
    """供稿是管理功能；電廠帳號連提案都不該進得來。"""

    encoded = hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)
    accounts = [
        Account(
            username="plant-user",
            password_hash=encoded,
            plant_id=15,
            expected_plant_name="林口發電廠",
        ),
        Account(username="reviewer", password_hash=encoded, plant_id=None, can_review=True),
    ]
    application = create_app(
        build_runtime(database=corpus_database, root=PROJECT_ROOT, mode="offline"),
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
    )

    with TestClient(application, base_url=ORIGIN) as client:
        headers = _sign_in(client, "plant-user")
        refused = _submit(client, headers)

    assert refused.status_code == 403
