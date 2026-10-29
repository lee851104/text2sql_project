"""Chinese questions about realtime generation (RT-3b spec)."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from realtime_panel_support import (
    NOW,
    PLANTS,
    SNAPSHOTS,
    Unit,
    build_realtime_database,
    build_standard_database,
    standard_units,
)
from realtime_support import make_config

from ingest.realtime import store
from ingest.realtime.config import RealtimeConfig
from ingest.realtime.lock import SingleInstanceLock
from text2sql.llm import FakeLLM, parse_generated_query
from text2sql.pipeline import PipelineResponse
from text2sql.realtime_query import (
    REALTIME_ALLOWED_COLUMNS,
    RealtimeQueryEngine,
    apply_unit_scope,
    looks_realtime,
)

DATAN = (8, "大潭發電廠")


@pytest.fixture
def config(tmp_path: Path) -> RealtimeConfig:
    config = make_config(tmp_path)
    build_standard_database(config)
    return config


def _engine(config: RealtimeConfig, *, now=NOW) -> RealtimeQueryEngine:
    return RealtimeQueryEngine(config, clock=lambda: now)


def _ask(
    engine: RealtimeQueryEngine,
    question: str,
    *,
    plant: tuple[int, str] | None = None,
    llm=None,
    roster=PLANTS,
) -> PipelineResponse | None:
    return engine.try_query(
        question,
        plant=plant,
        plant_roster=roster,
        llm=llm,
        llm_enabled=llm is not None,
        max_attempts=3,
    )


def _codes(response: PipelineResponse) -> list[str]:
    assert response.data is not None
    return [item["code"] for item in response.data["disclosures"]]


def _rows(response: PipelineResponse) -> list[dict[str, object]]:
    assert response.success, response.error
    data = response.data
    return [dict(zip(data["columns"], row, strict=True)) for row in data["rows"]]


# --- 判定 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "現在風力發多少電",
        "目前燃煤出力多少",
        "今天太陽能發了幾度電",
        "昨天燃氣發電量",
        "現在哪些機組通訊異常",
        "即時各類型出力",
        "最新的風電出力",
        "過去3小時太陽能出力",
    ],
)
def test_realtime_wording_takes_the_realtime_path(question: str) -> None:
    assert looks_realtime(question)


@pytest.mark.parametrize(
    "question",
    [
        # 黃金題庫：歲修排程與資料最新日都屬於 power.db，不能被「目前／最新」搶走。
        "哪一些機組目前維修中",
        "資料最新一天的尖峰負載",
        "本資料最新日的備轉率",
        "顯示資料庫最新一日系統摘要",
        "2026年7月20日出力前五名機組",
        "今天的備轉容量率",
    ],
)
def test_historical_questions_stay_on_the_snapshot(question: str) -> None:
    assert not looks_realtime(question)


def test_a_non_realtime_question_is_left_to_the_regular_pipeline(config) -> None:
    assert _ask(_engine(config), "2026年7月20日出力前五名機組") is None


# --- 現在 ------------------------------------------------------------------------------


def test_current_output_of_one_type_sums_only_normal_values(config) -> None:
    response = _ask(_engine(config), "現在燃氣出力多少")

    rows = _rows(response)
    assert response.data["intent"] == "rt_now_type"
    # 大潭#2 通訊異常：不算進總數，但機組數與異常數要看得到。
    assert rows == [
        {
            "資料時間": "2026-10-05 15:20",
            "機組類型": "燃氣",
            "淨發電量_MW": 780.0,
            "裝置容量_MW": 1550.0,
            "機組數": 3,
            "數值異常機組數": 1,
        }
    ]
    assert "RT_DATA_TIME" in _codes(response)


def test_by_type_lists_storage_and_storage_load_separately(config) -> None:
    rows = _rows(_ask(_engine(config), "現在各類型發電量"))

    by_type = {row["機組類型"]: row["淨發電量_MW"] for row in rows}
    assert by_type["儲能"] == 40.0
    assert by_type["儲能負載"] == 25.0


def test_one_unit_by_name(config) -> None:
    response = _ask(_engine(config), "現在大潭#1出力多少")

    rows = _rows(response)
    assert response.data["intent"] == "rt_now_unit"
    assert [row["機組名稱"] for row in rows] == ["大潭#1"]
    assert rows[0]["淨發電量_MW"] == 480.0


def test_a_unit_name_never_matches_inside_a_longer_one(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    units = (Unit("燃煤", "台中#1", "shared"), Unit("燃煤", "台中#10", "shared"))
    build_realtime_database(
        config,
        plants=PLANTS,
        units=units,
        snapshots={"2026-10-05 15:20": {u.key: (100.0, 550.0, "ok") for u in units}},
    )

    rows = _rows(_ask(_engine(config), "現在台中#10出力多少"))

    assert [row["機組名稱"] for row in rows] == ["台中#10"]


def test_top_units_disclose_bucket_and_undecided_rows(config) -> None:
    response = _ask(_engine(config), "現在出力最高的前3部機組")

    rows = _rows(response)
    assert [row["淨發電量_MW"] for row in rows] == [7731.1, 480.0, 300.0]
    assert "RT_BUCKET_ROWS" in _codes(response)


def test_abnormal_units_by_value_status(config) -> None:
    rows = _rows(_ask(_engine(config), "現在哪些機組通訊異常"))

    assert [(row["機組名稱"], row["數值狀態"]) for row in rows] == [("大潭#2", "通訊異常")]


def test_plant_total_groups_by_type(config) -> None:
    response = _ask(_engine(config), "現在大潭發電廠出力多少")

    rows = _rows(response)
    assert response.data["intent"] == "rt_now_plant"
    assert {row["機組類型"] for row in rows} == {"燃氣", "儲能", "儲能負載"}


# --- 今天與某一天 ---------------------------------------------------------------------


def test_today_energy_is_an_estimate_with_its_completeness(config) -> None:
    response = _ask(_engine(config), "今天燃氣發了多少度電")

    rows = _rows(response)
    # 15:00 三部正常、15:20 兩部正常：(470+490+310+480+300) ÷ 6；昨天 23:50 不算。
    assert rows[0]["估算發電量_MWh"] == round((470 + 490 + 310 + 480 + 300) / 6, 1)
    assert rows[0]["取樣點數"] == 5
    codes = _codes(response)
    assert {"RT_ENERGY_ESTIMATE", "RT_INCOMPLETE", "RT_TODAY_PARTIAL"} <= set(codes)


def test_today_peak_of_one_unit(config) -> None:
    rows = _rows(_ask(_engine(config), "今天大潭#1最高出力"))

    assert rows == [
        {
            "機組類型": "燃氣",
            "機組名稱": "大潭#1",
            "資料時間": "2026-10-05 15:20",
            "淨發電量_MW": 480.0,
        }
    ]


def test_yesterday_falls_back_to_the_detail_until_it_is_rolled_up(config) -> None:
    response = _ask(_engine(config), "昨天燃氣發電量")

    rows = _rows(response)
    assert response.data["intent"] == "rt_day_energy"
    assert response.data["tables"] == ["v_rt_10min"]
    assert rows[0]["估算發電量_MWh"] == round(400 / 6, 1)
    assert rows[0]["應有點數"] == 144


def test_a_rolled_up_day_reads_the_daily_view(config) -> None:
    connection = store.connect(config.database)
    try:
        with store.transaction(connection):
            unit_id = connection.execute(
                "SELECT id FROM dim_rt_unit WHERE unit_name = '大潭#1'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO fact_rt_day (date, snapshots, expected, missed_collector_down,
                       missed_fetch_failed, missed_rejected, rows_at_rollup, rolled_up_at)
                   VALUES ('2026-10-04', 140, 144, 4, 0, 0, 140, '2026-10-04T16:20:00+00:00')"""
            )
            connection.execute(
                """INSERT INTO fact_rt_unit_daily (date, unit_id, energy_mwh_est, max_mw, avg_mw,
                       min_mw, samples, expected_samples)
                   VALUES ('2026-10-04', ?, 11000.0, 500.0, 471.4, 400.0, 140, 144)""",
                (unit_id,),
            )
    finally:
        connection.close()

    response = _ask(_engine(config), "昨天燃氣發電量")

    rows = _rows(response)
    assert response.data["tables"] == ["v_rt_daily"]
    assert rows[0]["估算發電量_MWh"] == 11000.0
    assert rows[0]["資料完整度"] == round(140 / 144, 4)
    assert "140／144" in response.data["disclosures"][0]["reason"]


def test_an_explicit_date_in_the_realtime_period_is_answered_here(config) -> None:
    response = _ask(_engine(config), "2026年10月4日燃氣發電量是多少")

    assert response is not None and response.success
    assert response.data["intent"] == "rt_day_energy"


def test_a_date_before_collection_started_is_left_to_the_snapshot(config) -> None:
    # 不是即時用詞、日期又在即時資料之前：讓既有守門照舊回答或拒答。
    assert _ask(_engine(config), "2026年7月20日燃氣發電量是多少") is None


def test_the_day_before_the_first_collected_day_is_refused_with_the_start(config) -> None:
    response = _ask(_engine(config), "前天燃氣發電量")

    assert response.error_code == "RT_BEFORE_FIRST_DAY"
    assert response.evidence["first_day"] == "2026-10-04"


# --- 揭露 ------------------------------------------------------------------------------


def test_a_stopped_collector_is_never_described_as_now(config) -> None:
    response = _ask(_engine(config), "現在太陽能發多少電")

    assert "RT_COLLECTOR_STOPPED" in _codes(response)


def test_a_running_collector_with_old_data_is_stale(config) -> None:
    engine = _engine(config, now=NOW + timedelta(hours=2))
    with SingleInstanceLock(config.lock_path):
        response = _ask(engine, "現在燃氣出力多少")

    codes = _codes(response)
    assert "RT_STALE" in codes
    assert "RT_COLLECTOR_STOPPED" not in codes


def test_a_fresh_running_collector_only_reports_the_data_time(config) -> None:
    with SingleInstanceLock(config.lock_path):
        response = _ask(_engine(config), "現在燃氣出力多少")

    assert _codes(response) == ["RT_DATA_TIME"]


def test_quality_warnings_on_the_slots_used_are_disclosed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    build_standard_database(
        config,
        warnings={"2026-10-05 15:20": [{"code": "SUBTOTAL_MISMATCH", "detail": "風力"}]},
    )

    assert "RT_QUALITY_WARN" in _codes(_ask(_engine(config), "現在燃氣出力多少"))
    plant = _ask(_engine(config), "現在燃氣出力多少", plant=DATAN)
    warning = next(item for item in plant.data["disclosures"] if item["code"] == "RT_QUALITY_WARN")
    assert "風力" not in warning["reason"]


def test_summing_storage_and_storage_load_together_is_disclosed(config) -> None:
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "sql": 'SELECT round(SUM("淨發電量_MW"), 1) AS "合計_MW" FROM v_rt_now '
                    'WHERE "機組類型" IN (?, ?) LIMIT 1',
                    "params": ["儲能", "儲能負載"],
                }
            )
        ]
    )

    response = _ask(_engine(config), "現在儲能跟儲能負載的平均", llm=llm)

    assert response.data["source"] == "llm"
    assert "RT_STORAGE_NETTING" in _codes(response)


# --- 權限 ------------------------------------------------------------------------------


def test_a_plant_account_never_sees_undecided_or_other_plants(config) -> None:
    response = _ask(_engine(config), "現在各類型發電量", plant=DATAN)

    rows = _rows(response)
    by_type = {row["機組類型"]: row for row in rows}
    # 燃氣只剩大潭兩部（興達#1 未定）；明潭的水力不見；太陽能購電是 shared。
    assert by_type["燃氣"]["淨發電量_MW"] == 480.0
    assert by_type["燃氣"]["機組數"] == 2
    assert "水力" not in by_type
    assert "太陽能" in by_type
    assert "RT_SCOPE_PLANT" in _codes(response)


def test_a_plant_account_cannot_name_its_way_to_another_plant(config) -> None:
    rows = _rows(_ask(_engine(config), "現在明潭#1出力多少", plant=DATAN))

    assert rows == []


def test_mismatched_rosters_refuse_plant_accounts(config) -> None:
    response = _ask(_engine(config), "現在燃氣出力多少", plant=DATAN, roster={8: "別的廠"})

    assert response.error_code == "RT_SCOPE_MISMATCH"


def test_an_unresolvable_plant_identity_is_refused_not_widened(config) -> None:
    response = _ask(_engine(config), "現在燃氣出力多少", plant=(-1, "不存在"), roster={})

    assert response.error_code == "RT_SCOPE_MISMATCH"


def test_scope_rewrite_restricts_every_reference_including_joins() -> None:
    sql = (
        'SELECT a."機組名稱" FROM v_rt_now AS a JOIN v_rt_daily AS b '
        'ON a."機組鍵" = b."機組鍵" WHERE b."日期" = ? LIMIT 5'
    )

    scoped, params = apply_unit_scope(sql, ("2026-10-04",), frozenset({"燃氣|大潭#1"}))

    assert scoped.count('"機組鍵" IN (?)') == 2
    assert params == ("燃氣|大潭#1", "燃氣|大潭#1", "2026-10-04")


def test_an_empty_visible_set_returns_nothing_rather_than_invalid_sql() -> None:
    scoped, params = apply_unit_scope("SELECT * FROM v_rt_now LIMIT 5", (), frozenset())

    assert "1 = 0" in scoped
    assert params == ()


# --- LLM 與守門 -----------------------------------------------------------------------


def test_an_unmodelled_question_goes_to_the_llm(config) -> None:
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "sql": 'SELECT "機組名稱" FROM v_rt_now WHERE "機組類型" = ? AND '
                    '"數值狀態" = ? ORDER BY "淨發電量_MW" ASC LIMIT 1',
                    "params": ["燃氣", "正常"],
                }
            )
        ]
    )

    response = _ask(_engine(config), "現在燃氣機組出力最低的是哪一部", llm=llm)

    assert response.data["source"] == "llm"
    assert _rows(response) == [{"機組名稱": "興達#1"}]


def test_without_an_llm_an_unmodelled_question_asks_for_another_wording(config) -> None:
    response = _ask(_engine(config), "現在燃氣機組出力最低的是哪一部")

    assert response.error_code == "RT_CLARIFY"
    assert response.suggestions


def test_guard_failures_are_retried_with_the_previous_attempt(config) -> None:
    bad = json.dumps({"sql": "SELECT * FROM v_rt_now", "params": []})  # 沒有 LIMIT
    good = json.dumps({"sql": 'SELECT "機組名稱" FROM v_rt_now LIMIT 1', "params": []})
    llm = FakeLLM([bad, good])

    response = _ask(_engine(config), "現在燃氣機組出力最低的是哪一部", llm=llm)

    assert response.success
    stages = [item["stage"] for item in response.data["trace"]]
    assert stages.count("realtime_generate") == 2


def test_mixing_realtime_and_snapshot_views_is_refused(config) -> None:
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "sql": 'SELECT a."機組名稱" FROM v_rt_now AS a JOIN v_unit AS b '
                    'ON a."機組名稱" = b."機組名" LIMIT 5',
                    "params": [],
                }
            )
        ]
    )

    response = _ask(_engine(config), "現在燃氣機組出力最低的是哪一部", llm=llm)

    assert response.error_code == "RT_CROSS_DATABASE"


def test_the_realtime_guard_only_knows_realtime_views() -> None:
    assert set(REALTIME_ALLOWED_COLUMNS) == {"v_rt_now", "v_rt_10min", "v_rt_daily"}


def test_the_guard_allowlist_matches_the_realtime_schema(config) -> None:
    """不變式（RT-1 §8.5 第 15 點）：realtime.db 的檢視＝即時守門的 allowlist，欄位一致。"""

    connection = sqlite3.connect(config.database)
    try:
        views = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
        for view in views:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({view})")}
            assert columns == REALTIME_ALLOWED_COLUMNS[view], view
    finally:
        connection.close()
    assert views == set(REALTIME_ALLOWED_COLUMNS)


def test_no_realtime_database_is_reported_not_guessed(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    response = _ask(_engine(config), "現在風力發多少電")

    assert response.error_code == "RT_UNAVAILABLE"
    # 日期問句沒有即時資料時不是我們的題目。
    assert _ask(_engine(config), "2026年10月4日燃氣發電量是多少") is None


def test_llm_output_with_a_reasoning_preamble_is_parsed() -> None:
    output = (
        "<think>先想一下</think>\n說明如下：\n"
        '```json\n{"sql": "SELECT 1 LIMIT 1", "params": []}\n```'
    )

    assert parse_generated_query(output).sql == "SELECT 1 LIMIT 1"


def test_standard_fixture_is_what_these_tests_assume() -> None:
    assert "2026-10-04 23:50" in SNAPSHOTS
    assert any(unit.access_scope == "undecided" for unit in standard_units())


# --- 名稱對不上 -----------------------------------------------------------------------


def test_a_unit_that_is_not_in_realtime_is_named_not_answered_with_a_total(config) -> None:
    response = _ask(_engine(config), "現在大潭#9出力多少")

    assert response.error_code == "RT_NOT_IN_REALTIME"
    assert response.evidence["name"] == "大潭#9"
    assert response.evidence["similar"] == ["大潭#1", "大潭#2", "大潭儲能"]


def test_a_type_that_is_not_in_realtime_is_named(config) -> None:
    response = _ask(_engine(config), "現在核能出力")

    assert response.error_code == "RT_NOT_IN_REALTIME"
    assert "核能" in response.error


def test_ordinal_unit_names_are_understood(config) -> None:
    rows = _rows(_ask(_engine(config), "現在大潭1號機出力多少"))

    assert [row["機組名稱"] for row in rows] == ["大潭#1"]


def test_a_specific_note_does_not_widen_to_the_generic_one(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    units = (Unit("燃煤", "甲#1", "shared"), Unit("風力", "乙風", "shared"))
    build_realtime_database(
        config,
        plants=PLANTS,
        units=units,
        snapshots={"2026-10-05 15:20": {u.key: (0.0, 100.0, "ok") for u in units}},
    )
    connection = sqlite3.connect(config.database)
    with connection:
        connection.execute("UPDATE fact_rt_unit_10min SET note = '環保停機' WHERE unit_id = 1")
        connection.execute("UPDATE fact_rt_unit_10min SET note = '計劃停機' WHERE unit_id = 2")
    connection.close()

    rows = _rows(_ask(_engine(config), "現在哪些機組環保停機"))

    assert [row["備註"] for row in rows] == ["環保停機"]
