"""benchmarks/realtime_questions.json against a fixed realtime.db and a fixed "now".

即時評測要能重現（RT-1 §8.5 第 11 點）：資料是測試用的固定時段，「現在」固定在
2026-10-05 15:27（台灣時間），收集器沒有在跑。每一題都要過，沒有「準確率門檻」——
即時路徑全是規則，答錯就是規則壞了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from realtime_panel_support import NOW, PLANTS, build_standard_database
from realtime_support import make_config

from ingest.validate import PROJECT_ROOT
from text2sql.realtime_query import RealtimeQueryEngine

CASES = json.loads(
    (PROJECT_ROOT / "benchmarks" / "realtime_questions.json").read_text(encoding="utf-8")
)
PLANT_IDS = {name: plant_id for plant_id, name in PLANTS.items()}


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> RealtimeQueryEngine:
    config = make_config(Path(tmp_path_factory.mktemp("realtime-benchmark")))
    build_standard_database(config)
    return RealtimeQueryEngine(config, clock=lambda: NOW)


def test_case_ids_are_unique() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_realtime_benchmark(engine: RealtimeQueryEngine, case: dict) -> None:
    plant_name = case.get("plant")
    plant = (PLANT_IDS[plant_name], plant_name) if plant_name else None
    expect = case["expect"]

    response = engine.try_query(
        case["question"], plant=plant, plant_roster=PLANTS, llm=None, llm_enabled=False
    )

    if expect.get("route") == "snapshot":
        assert response is None, f"不該走即時路徑：{response}"
        return
    assert response is not None, "應該走即時路徑"
    if "error_code" in expect:
        assert response.error_code == expect["error_code"], response.error
        return
    assert response.success, f"{response.error_code}: {response.error}"
    data = response.data
    assert data["intent"] == expect["intent"]
    codes = {item["code"] for item in data["disclosures"]}
    assert set(expect.get("disclosures", ())) <= codes
    if "rows" in expect:
        assert data["record_count"] == expect["rows"]
