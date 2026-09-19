"""Account roster, its binding to a plant, and the scope that binding produces."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest.build_db import build_database
from serving.accounts import (
    MINIMUM_ITERATIONS,
    Account,
    AccountRosterError,
    _main,
    hash_password,
    parse_roster,
    render_roster,
    resolve_plant_names,
    verify_password,
)
from serving.admin_auth import AdminAuthConfigurationError, AdminAuthManager
from serving.app import create_app
from serving.runtime import build_runtime

ORIGIN = "http://testserver"
PASSWORD = "roster-test-password"
PLANT_ACCOUNT = "linkou"
ALL_ACCOUNT = "supervisor"
BOUND_PLANT = "林口發電廠"
OTHER_PLANT = "台中發電廠"


def _hash() -> str:
    return hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)


# ── 名冊本身 ──────────────────────────────────────────────────────────────


def test_password_hash_round_trips_and_refuses_a_wrong_password() -> None:
    encoded = _hash()

    assert verify_password(PASSWORD, encoded) is True
    assert verify_password(PASSWORD + "x", encoded) is False
    assert PASSWORD not in encoded


def test_hash_refuses_a_short_password_and_a_weak_iteration_count() -> None:
    with pytest.raises(AccountRosterError, match="密碼長度"):
        hash_password("short")
    with pytest.raises(AccountRosterError, match="迭代次數"):
        hash_password(PASSWORD, iterations=10)


def test_roster_refuses_duplicate_usernames() -> None:
    payload = {
        "accounts": [
            {"username": "same", "password": _hash(), "scope": "all"},
            {"username": "same", "password": _hash(), "scope": 15},
        ]
    }

    with pytest.raises(AccountRosterError, match="重複"):
        parse_roster(payload)


@pytest.mark.parametrize("scope", ["plant", "", 0, -3, True, None])
def test_roster_refuses_a_scope_that_is_neither_all_nor_a_plant_id(scope: object) -> None:
    payload = {"accounts": [{"username": "a", "password": _hash(), "scope": scope}]}

    with pytest.raises(AccountRosterError):
        parse_roster(payload)


def test_roster_refuses_an_all_scope_that_also_names_a_plant() -> None:
    payload = {
        "accounts": [
            {
                "username": "a",
                "password": _hash(),
                "scope": "all",
                "plant_name": BOUND_PLANT,
            }
        ]
    }

    with pytest.raises(AccountRosterError, match="不應該同時綁定"):
        parse_roster(payload)


def test_roster_refuses_a_password_that_is_not_a_supported_hash() -> None:
    payload = {"accounts": [{"username": "a", "password": PASSWORD, "scope": "all"}]}

    with pytest.raises(AccountRosterError, match="密碼雜湊格式"):
        parse_roster(payload)


# ── 綁定：編號查不到或名稱對不上就不啟動 ──────────────────────────────────


def test_binding_fails_when_the_plant_id_is_not_in_the_roster() -> None:
    account = Account(username="a", password_hash=_hash(), plant_id=999)

    with pytest.raises(AccountRosterError, match="不在 dim_plant_scope"):
        resolve_plant_names([account], {15: BOUND_PLANT})


def test_binding_fails_when_the_recorded_plant_name_no_longer_matches() -> None:
    """電廠編號漂移的第二道檢查：編號還在，但已經是另一座廠。"""

    account = Account(
        username="a",
        password_hash=_hash(),
        plant_id=15,
        expected_plant_name=BOUND_PLANT,
    )

    with pytest.raises(AccountRosterError, match="對不起來"):
        resolve_plant_names([account], {15: OTHER_PLANT})


def test_binding_succeeds_when_the_identifier_and_the_name_agree() -> None:
    account = Account(
        username="a",
        password_hash=_hash(),
        plant_id=15,
        expected_plant_name=BOUND_PLANT,
    )

    assert resolve_plant_names([account], {15: BOUND_PLANT}) == {"a": BOUND_PLANT}


# ── 驗證管理員：名冊帳號各自帶著自己的範圍 ────────────────────────────────


def test_each_roster_account_carries_its_own_scope_into_the_session() -> None:
    manager = AdminAuthManager.from_roster(
        [
            Account(username=ALL_ACCOUNT, password_hash=_hash(), plant_id=None),
            Account(username=PLANT_ACCOUNT, password_hash=_hash(), plant_id=15),
        ],
        environ={},
    )

    assert manager.login(ALL_ACCOUNT, PASSWORD).principal.plant_id is None
    assert manager.login(PLANT_ACCOUNT, PASSWORD).principal.plant_id == 15


def test_a_roster_and_a_single_account_cannot_be_configured_together() -> None:
    with pytest.raises(AdminAuthConfigurationError, match="不可同時提供"):
        AdminAuthManager(
            username="a",
            password=PASSWORD,
            accounts=[Account(username="b", password_hash=_hash(), plant_id=None)],
        )


# ── 端到端：同一個問句，兩種帳號，不同結果 ────────────────────────────────


@pytest.fixture(scope="module")
def scoped_client(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("account-scope") / "power.db"
    build_database(database)
    with sqlite3.connect(database) as connection:
        plant_id = connection.execute(
            "SELECT plant_id FROM dim_plant_scope WHERE plant_name = ?", (BOUND_PLANT,)
        ).fetchone()[0]

    accounts = [
        Account(username=ALL_ACCOUNT, password_hash=_hash(), plant_id=None),
        Account(
            username=PLANT_ACCOUNT,
            password_hash=_hash(),
            plant_id=int(plant_id),
            expected_plant_name=BOUND_PLANT,
        ),
    ]
    application = create_app(
        build_runtime(database=database),
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
    )
    with TestClient(application, base_url=ORIGIN) as client:
        yield client


def _login(client: TestClient, username: str) -> dict[str, str]:
    """Sign in and return the headers a mutation needs."""

    response = client.post(
        "/api/admin/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-PowerQuery-CSRF": response.json()["data"]["csrf_token"],
    }


def _logout(client: TestClient) -> None:
    client.cookies.clear()


def _ask(client: TestClient, question: str, **body: object) -> dict:
    response = client.post("/api/query", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
def test_one_question_gives_each_account_only_the_rows_it_may_read(
    scoped_client: TestClient,
) -> None:
    question = f"列出{OTHER_PLANT}所有設備"

    _login(scoped_client, ALL_ACCOUNT)
    unrestricted = _ask(scoped_client, question)
    _logout(scoped_client)

    _login(scoped_client, PLANT_ACCOUNT)
    restricted = _ask(scoped_client, question)
    _logout(scoped_client)

    assert unrestricted["success"] is True
    assert unrestricted["data"]["rows"], "全權限帳號應該讀得到其他電廠的機組"
    assert "scope_notice" not in unrestricted["data"]

    assert restricted["success"] is True
    assert restricted["data"]["rows"] == [], "電廠帳號不該讀到其他電廠的機組"
    assert BOUND_PLANT in restricted["data"]["scope_notice"]


@pytest.mark.integration
def test_a_plant_account_still_reads_its_own_plant(scoped_client: TestClient) -> None:
    _login(scoped_client, PLANT_ACCOUNT)
    own = _ask(scoped_client, f"列出{BOUND_PLANT}所有設備")
    _logout(scoped_client)

    assert own["success"] is True
    assert own["data"]["rows"], "電廠帳號必須讀得到自己廠的機組"


@pytest.mark.integration
def test_a_lapsed_session_is_refused_rather_than_widened_to_every_plant(
    scoped_client: TestClient,
) -> None:
    """Session 失效時權限必須往下掉，不能掉回「匿名可看全部」。"""

    _login(scoped_client, PLANT_ACCOUNT)
    manager = scoped_client.app.state.auth_manager
    assert manager.revoke(scoped_client.cookies[manager.cookie_name]) is True

    response = scoped_client.post("/api/query", json={"question": f"列出{OTHER_PLANT}所有設備"})
    _logout(scoped_client)

    assert response.status_code == 401


@pytest.mark.integration
def test_a_plant_account_cannot_reach_the_unscoped_raw_files(scoped_client: TestClient) -> None:
    _login(scoped_client, PLANT_ACCOUNT)
    raw = scoped_client.post(
        "/api/query", json={"question": "台中發電廠地址", "query_scope": "raw"}
    )
    auto = scoped_client.post(
        "/api/query", json={"question": "台中發電廠地址", "query_scope": "auto"}
    )
    _logout(scoped_client)

    assert raw.status_code == 403
    assert auto.status_code == 403


@pytest.mark.integration
def test_a_plant_account_has_no_management_rights(scoped_client: TestClient) -> None:
    """電廠帳號是資料使用者，不是系統管理者。

    範圍只收窄「看得到哪些列」是不夠的：管理端點會繞過 `ScopeGuard`，其中
    `/api/data/files/{dataset}` 直接送出所有電廠的原始來源檔。
    """

    headers = _login(scoped_client, PLANT_ACCOUNT)

    reads = [
        "/api/data/status",
        "/api/data/files",
        "/api/data/files/units_csv",
        "/api/data/changes",
        "/api/data/events",
        "/api/corpus/entries",
        "/api/corpus/events",
        "/api/runtime/llm",
        "/api/training-status",
    ]
    for path in reads:
        assert scoped_client.get(path).status_code == 403, path

    assert (
        scoped_client.post(
            "/api/data/changes/remove",
            headers=headers,
            json={"dataset": "outage_csv", "reason": "should never reach the service"},
        ).status_code
        == 403
    )
    assert (
        scoped_client.put("/api/runtime/llm", headers=headers, json={"mode": "offline"}).status_code
        == 403
    )
    assert (
        scoped_client.post(
            "/api/corpus/entries/anything/review",
            headers=headers,
            json={"decision": "approve"},
        ).status_code
        == 403
    )
    _logout(scoped_client)


@pytest.mark.integration
def test_a_plant_account_can_still_end_its_own_session(scoped_client: TestClient) -> None:
    """收窄管理權限不能把登出一起擋掉。"""

    headers = _login(scoped_client, PLANT_ACCOUNT)

    response = scoped_client.delete("/api/admin/session", headers=headers)

    assert response.status_code == 200
    assert response.json()["data"]["authenticated"] is False
    _logout(scoped_client)


@pytest.mark.integration
def test_an_all_scope_account_keeps_its_management_rights(scoped_client: TestClient) -> None:
    _login(scoped_client, ALL_ACCOUNT)

    assert scoped_client.get("/api/data/status").status_code == 200
    assert scoped_client.get("/api/corpus/entries").status_code == 200
    _logout(scoped_client)


# ── 名冊盤點指令 ──────────────────────────────────────────────────────────


def _roster_account(username: str, **kwargs: object) -> Account:
    return Account(username=username, password_hash=_hash(), **kwargs)


def test_inventory_reports_every_broken_binding_not_just_the_first() -> None:
    """盤點是診斷工具：一次看完整份名冊，不能在第一個問題就停。"""

    accounts = [
        _roster_account("admin", plant_id=None),
        _roster_account("bad-id", plant_id=999),
        _roster_account("moved", plant_id=15, expected_plant_name="台中發電廠"),
    ]

    report, code = render_roster(
        roster_path=Path("configs/accounts.yaml"),
        database=Path("power.db"),
        accounts=accounts,
        plant_names={15: BOUND_PLANT},
    )

    assert code == 1
    assert "bad-id" in report and "不在 dim_plant_scope" in report
    assert "moved" in report and "對不起來" in report
    assert "有 2 個帳號的綁定對不上資料庫" in report


def test_inventory_is_clean_when_every_binding_holds() -> None:
    accounts = [
        _roster_account("admin", plant_id=None),
        _roster_account("linkou", plant_id=15, expected_plant_name=BOUND_PLANT),
    ]

    report, code = render_roster(
        roster_path=Path("configs/accounts.yaml"),
        database=Path("power.db"),
        accounts=accounts,
        plant_names={15: BOUND_PLANT},
    )

    assert code == 0
    assert "所有綁定都對得上資料庫。" in report
    assert "✗" not in report


def test_a_missing_roster_is_reported_as_a_supported_mode() -> None:
    report, code = render_roster(
        roster_path=Path("configs/accounts.yaml"),
        database=Path("power.db"),
        accounts=None,
        plant_names=None,
    )

    assert code == 0
    assert "名冊不存在" in report


def test_an_unreadable_database_reports_unverified_rather_than_ok() -> None:
    """未驗不等於通過：驗不了綁定時離開碼必須非零。"""

    accounts = [_roster_account("linkou", plant_id=15, expected_plant_name=BOUND_PLANT)]

    report, code = render_roster(
        roster_path=Path("configs/accounts.yaml"),
        database=Path("missing.db"),
        accounts=accounts,
        plant_names=None,
    )

    assert code == 1
    assert "無法驗證綁定" in report
    assert "未驗證" in report
    assert "ok" not in report


def test_the_inventory_never_prints_password_material() -> None:
    accounts = [_roster_account("linkou", plant_id=15, expected_plant_name=BOUND_PLANT)]

    report, _code = render_roster(
        roster_path=Path("configs/accounts.yaml"),
        database=Path("power.db"),
        accounts=accounts,
        plant_names={15: BOUND_PLANT},
    )

    assert "pbkdf2" not in report
    assert accounts[0].password_hash not in report


@pytest.mark.parametrize("arguments", [["nonsense"], ["list", "extra"]])
def test_unknown_or_extra_arguments_are_refused(arguments: list[str]) -> None:
    assert _main(arguments) == 2


def test_help_is_not_an_error() -> None:
    assert _main(["--help"]) == 0
