"""HTTP surface of the realtime panel (RT-3a spec §4.2–§4.5, §5, §8)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from realtime_panel_support import build_standard_database, standard_units
from realtime_support import make_config

from ingest.build_db import build_database
from serving import realtime_panel
from serving.accounts import MINIMUM_ITERATIONS, Account, hash_password
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.realtime_panel import PUBLIC_STATUS_FIELDS, RealtimePanel
from serving.runtime import build_runtime
from text2sql.scope_guard import ScopeCatalog

pytestmark = pytest.mark.integration

ORIGIN = "http://testserver"
PASSWORD = "realtime-test-password"
ALL_ACCOUNT = "supervisor"
PLANT_ACCOUNT = "datan"
OWN_PLANT = "大潭發電廠"
OTHER_PLANT = "明潭發電廠"


@dataclass(frozen=True)
class Service:
    client: TestClient
    plants: dict[int, str]

    def plant_id(self, name: str) -> int:
        return next(plant_id for plant_id, plant in self.plants.items() if plant == name)


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("realtime-api") / "power.db"
    build_database(database)
    plants = ScopeCatalog.from_database(database).plant_names_by_id()
    own = next(plant_id for plant_id, plant in plants.items() if plant == OWN_PLANT)
    password_hash = hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)
    accounts = [
        Account(username=ALL_ACCOUNT, password_hash=password_hash, plant_id=None),
        Account(
            username=PLANT_ACCOUNT,
            password_hash=password_hash,
            plant_id=own,
            expected_plant_name=OWN_PLANT,
        ),
    ]
    application = create_app(
        build_runtime(database=database),
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
        realtime_panel=RealtimePanel(None),
    )
    with TestClient(application, base_url=ORIGIN) as client:
        yield Service(client, plants)


@pytest.fixture
def client(service: Service):
    client = service.client
    client.app.state.anonymous_scope = "all"
    yield client
    client.cookies.clear()
    client.app.state.anonymous_scope = "all"
    client.app.state.realtime_panel = RealtimePanel(None)


def _install(service: Service, tmp_path: Path, *, plants: dict[int, str] | None = None) -> None:
    config = make_config(tmp_path)
    build_standard_database(
        config,
        plants=plants or service.plants,
        units=standard_units(service.plant_id(OWN_PLANT), service.plant_id(OTHER_PLANT)),
    )
    service.client.app.state.realtime_panel = RealtimePanel(config)


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/admin/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _entry(data: dict, unit_type: str) -> dict:
    return next(entry for entry in data["by_type"] if entry["type"] == unit_type)


# ── /api/health ────────────────────────────────────────────────────────────


def test_health_carries_the_realtime_state_without_numbers(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    realtime = response.json()["realtime"]
    assert set(realtime) == set(PUBLIC_STATUS_FIELDS)
    assert realtime["latest_data_time"] == "2026-10-05 15:20"
    assert realtime["state"] == "stopped"  # 測試裡沒有收集器在跑


def test_health_stays_up_when_the_realtime_status_breaks(
    service: Service, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(service, tmp_path)

    def broken(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(realtime_panel, "read_status", broken)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["realtime"] == {"available": False, "state": "unavailable"}


# ── /api/realtime/overview ─────────────────────────────────────────────────


def test_overview_without_a_database_is_200_and_unavailable(client: TestClient) -> None:
    response = client.get("/api/realtime/overview")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"available": False, "state": "unavailable"},
    }
    assert "no-store" in response.headers["cache-control"]


def test_an_anonymous_visitor_sees_every_unit_when_the_service_allows_it(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)

    data = client.get("/api/realtime/overview").json()["data"]

    assert data["scope"] == "all"
    assert _entry(data, "燃氣")["net_mw"] == 780.0  # 含歸屬未定的興達#1


def test_a_denied_visitor_gets_401_while_health_still_answers(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    client.app.state.anonymous_scope = "denied"

    response = client.get("/api/realtime/overview")

    assert response.status_code == 401
    assert response.json()["detail"] == "即時發電數字需要先登入。"
    assert client.get("/api/health").json()["realtime"]["available"] is True


@pytest.mark.parametrize("username", [ALL_ACCOUNT, PLANT_ACCOUNT])
def test_a_logged_in_account_passes_a_denied_anonymous_scope(
    service: Service, client: TestClient, tmp_path: Path, username: str
) -> None:
    _install(service, tmp_path)
    client.app.state.anonymous_scope = "denied"
    _login(client, username)

    assert client.get("/api/realtime/overview").status_code == 200


def test_query_keeps_its_own_login_message(client: TestClient) -> None:
    client.app.state.anonymous_scope = "denied"

    response = client.post("/api/query", json={"question": "列出台中發電廠所有設備"})

    assert response.status_code == 401
    assert response.json()["detail"] == "此服務的查詢需要先登入。"


def test_a_lapsed_session_is_refused_rather_than_widened(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    _login(client, PLANT_ACCOUNT)
    manager = client.app.state.auth_manager
    assert manager.revoke(client.cookies[manager.cookie_name]) is True

    assert client.get("/api/realtime/overview").status_code == 401


def test_an_all_plants_account_sees_every_unit(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    _login(client, ALL_ACCOUNT)

    data = client.get("/api/realtime/overview").json()["data"]

    assert data["scope"] == "all"
    assert _entry(data, "燃氣")["net_mw"] == 780.0


def test_a_plant_account_sees_its_plant_and_shared_rows_only(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    _login(client, PLANT_ACCOUNT)

    data = client.get("/api/realtime/overview").json()["data"]

    assert data["scope"] == f"plant:{OWN_PLANT}"
    gas = _entry(data, "燃氣")
    assert (gas["own_net_mw"], gas["shared_net_mw"], gas["units"]) == (480.0, None, 2)
    assert "水力" not in [entry["type"] for entry in data["by_type"]]
    assert "RT_SCOPE_PLANT" in [item["code"] for item in data["disclosures"]]


def test_a_roster_mismatch_is_409_for_a_plant_account_only(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    renamed = dict(service.plants)
    renamed[service.plant_id(OWN_PLANT)] = "大潭電廠（舊名）"
    _install(service, tmp_path, plants=renamed)

    _login(client, PLANT_ACCOUNT)
    refused = client.get("/api/realtime/overview")
    client.cookies.clear()
    _login(client, ALL_ACCOUNT)
    allowed = client.get("/api/realtime/overview")

    assert refused.status_code == 409
    assert "RT_SCOPE_MISMATCH" in refused.json()["detail"]
    assert allowed.status_code == 200


def test_an_unreadable_database_is_503_and_health_degrades(
    client: TestClient, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    config.database.parent.mkdir(parents=True)
    config.database.write_bytes(b"not a database" * 100)
    client.app.state.realtime_panel = RealtimePanel(config)

    response = client.get("/api/realtime/overview")
    health = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "即時資料暫時讀不到。"
    assert health.status_code == 200
    assert health.json()["realtime"] == {"available": False, "state": "unavailable"}


# ── 前端靜態檔 ─────────────────────────────────────────────────────────────

STATIC = Path(__file__).resolve().parents[1] / "src" / "serving" / "static"
REALTIME_IDS = (
    "realtimePanel",
    "realtimeLight",
    "realtimeStatusText",
    "realtimeLoginNote",
    "realtimeDisclosures",
    "realtimeBody",
    "realtimeTypeHead",
    "realtimeTypeRows",
    "realtimeStorageNote",
    "realtimeTrend",
    "realtimeTrendDetails",
    "realtimeTrendTable",
)


def test_the_overview_page_has_the_realtime_block_above_the_metric_cards() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    for element_id in REALTIME_IDS:
        assert f'id="{element_id}"' in html, element_id
    assert html.index('id="realtimePanel"') < html.index('class="metric-grid"')
    assert "各機組發電量即時資訊" in html  # 資料來源顯名


def test_the_script_refreshes_the_panel_only_while_it_is_visible() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'api("/api/realtime/overview")' in script
    assert "REALTIME_REFRESH_MS = 60000" in script
    assert '"visibilitychange"' in script
    assert "connectgaps: false" in script
