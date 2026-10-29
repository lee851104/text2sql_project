"""The realtime branch inside the pipeline, runtime and /api/query (RT-3b spec §3, §8)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from realtime_panel_support import NOW, build_standard_database, standard_units
from realtime_support import make_config

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT
from serving import runtime as runtime_module
from serving.accounts import MINIMUM_ITERATIONS, Account, hash_password
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.realtime_panel import RealtimePanel
from serving.runtime import PROVIDER_ENV, RuntimeManager, build_runtime
from text2sql.llm import LOCAL_API_KEY
from text2sql.realtime_query import RealtimeQueryEngine
from text2sql.scope_guard import ScopeCatalog

pytestmark = pytest.mark.integration

ORIGIN = "http://testserver"
PASSWORD = "realtime-query-password"
ALL_ACCOUNT = "supervisor"
PLANT_ACCOUNT = "datan"
OWN_PLANT = "大潭發電廠"
OTHER_PLANT = "明潭發電廠"


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("realtime-query") / "power.db"
    build_database(path)
    return path


@dataclass(frozen=True)
class Service:
    client: TestClient
    engine: RealtimeQueryEngine


@pytest.fixture(scope="module")
def service(database: Path, tmp_path_factory: pytest.TempPathFactory):
    plants = ScopeCatalog.from_database(database).plant_names_by_id()
    own = next(plant_id for plant_id, name in plants.items() if name == OWN_PLANT)
    other = next(plant_id for plant_id, name in plants.items() if name == OTHER_PLANT)
    config = make_config(tmp_path_factory.mktemp("realtime-query-rt"))
    build_standard_database(config, plants=plants, units=standard_units(own, other))
    engine = RealtimeQueryEngine(config, clock=lambda: NOW)
    # 資料管理工作區啟動後會用自己的資料庫副本重建 runtime，所以換的是建 engine 的地方，
    # 不是某一個 runtime 物件。
    patch = pytest.MonkeyPatch()
    patch.setattr(runtime_module, "_realtime_engine", lambda *_args: engine)
    patch.delenv(runtime_module.PROVIDER_ENV, raising=False)
    runtime = build_runtime(database=database, mode="offline")
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
        runtime,
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
        realtime_panel=RealtimePanel(config),
    )
    try:
        with TestClient(application, base_url=ORIGIN) as client:
            client.app.state.anonymous_scope = "all"
            yield Service(client, engine)
    finally:
        patch.undo()


@pytest.fixture
def client(service: Service):
    yield service.client
    service.client.cookies.clear()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/admin/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _query(client: TestClient, question: str) -> dict:
    response = client.post(
        "/api/query",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"question": question},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- /api/query -----------------------------------------------------------------------


def test_a_realtime_question_is_answered_from_realtime_db(client: TestClient) -> None:
    payload = _query(client, "現在燃氣出力多少")

    assert payload["success"] is True, payload
    data = payload["data"]
    assert data["intent"] == "rt_now_type"
    assert data["tables"] == ["v_rt_10min"]
    assert data["realtime"]["data_time"] == "2026-10-05 15:20"
    assert data["data_provenance"]["database_version"] == "realtime"
    assert data["data_provenance"]["data_sources"][0]["dataset"] == "d006001"
    # 即時 SQL 不進語料學習（RT-1 §8.5 第 12 點）。
    assert data["learning"]["reason"] == "realtime_query"


def test_energy_today_is_not_blocked_as_a_cross_day_peak_sum(client: TestClient) -> None:
    """分流在 SemanticGuard 之前：PEAK_SUM_ACROSS_DAYS 是為 v_peak 寫的（§8.5 第 9 點）。"""

    payload = _query(client, "今天燃氣發了多少度電")

    assert payload["success"] is True, payload
    assert payload["data"]["intent"] == "rt_today_energy"


def test_historical_questions_still_use_the_snapshot(client: TestClient) -> None:
    payload = _query(client, "2026年3月有哪些機組在歲修")

    assert payload["success"] is True, payload
    assert payload["data"]["tables"] == ["v_outage"]
    # 「目前」配歲修是排程問題，不能被即時路徑接走。
    assert not str(_query(client, "哪一些機組目前維修中").get("error_code", "")).startswith("RT_")


def test_a_plant_account_is_scoped_to_its_plant_and_shared_rows(client: TestClient) -> None:
    _login(client, PLANT_ACCOUNT)

    payload = _query(client, "現在燃氣出力多少")

    data = payload["data"]
    assert data["rows"][0][data["columns"].index("機組數")] == 2
    assert data["scope"] == {"plant": OWN_PLANT}
    assert "RT_SCOPE_PLANT" in [item["code"] for item in data["disclosures"]]


def test_coverage_lists_realtime_views_and_their_period(client: TestClient) -> None:
    response = client.get("/api/coverage")

    data = response.json()["data"]
    assert data["realtime"]["available"] is True
    assert data["realtime"]["first_day"] == "2026-10-04"
    assert {"v_rt_now", "v_rt_10min", "v_rt_daily"} <= {item["view"] for item in data["answerable"]}


# --- runtime ---------------------------------------------------------------------------


def test_the_project_runtime_carries_a_realtime_branch(database: Path) -> None:
    runtime = build_runtime(database=database, mode="offline")

    assert isinstance(runtime.pipeline.realtime, RealtimeQueryEngine)
    assert runtime.pipeline.realtime.config.database.name == "realtime.db"


def test_the_ollama_provider_needs_no_api_key(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 本機 runtime 要建 OpenAI 相容的 client；CI 只裝 dev extra（同 test_llm.py）。
    pytest.importorskip("openai")
    monkeypatch.setenv(PROVIDER_ENV, "ollama")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    runtime = build_runtime(database=database, root=PROJECT_ROOT, mode="local")

    assert runtime.mode == "local"
    assert runtime.provider == "ollama"
    assert runtime.model == "qwen3:8b"
    assert runtime.pipeline.llm.client.base_url.host == "127.0.0.1"
    assert runtime.pipeline.llm.client.api_key == LOCAL_API_KEY


def test_auto_uses_the_local_model_when_ollama_answers(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv(PROVIDER_ENV, "ollama")

    manager = RuntimeManager(
        database=database, root=PROJECT_ROOT, default_mode="auto", probe=lambda _url: True
    )

    assert manager.get_runtime().mode == "local"
    assert manager.status()["local_available"] is True


def test_auto_falls_back_to_offline_rules_without_ollama(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PROVIDER_ENV, raising=False)

    runtime = build_runtime(
        database=database, root=PROJECT_ROOT, mode="auto", probe=lambda _url: False
    )

    assert runtime.mode == "offline"
    assert runtime.local_llm is False


@pytest.mark.parametrize(
    ("provider", "reasoning_off", "timeout"),
    [("ollama", True, 120.0), ("ollama-reasoning", False, 600.0)],
)
def test_the_two_ollama_modes_differ_only_in_reasoning_and_timeout(
    database: Path, monkeypatch: pytest.MonkeyPatch, provider: str, reasoning_off: bool, timeout
) -> None:
    """快／準兩種模式（README「本機 LLM 評測」）：同一個模型，差在推理與逾時。"""

    pytest.importorskip("openai")
    monkeypatch.setenv(PROVIDER_ENV, provider)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    runtime = build_runtime(database=database, root=PROJECT_ROOT, mode="local")

    assert runtime.mode == "local" and runtime.model == "qwen3:8b"
    assert (runtime.pipeline.llm.extra_body == {"reasoning_effort": "none"}) is reasoning_off
    assert runtime.pipeline.llm.client.timeout == timeout
