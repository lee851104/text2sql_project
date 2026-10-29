"""d006010 history import (RT-2 spec): parse, archive, map, store, replay and query."""

from __future__ import annotations

import codecs
import dataclasses
import gzip
import io
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from realtime_support import make_config

from ingest.realtime import history, store
from ingest.realtime.__main__ import main
from ingest.realtime.collector import EXIT_ALREADY_RUNNING, EXIT_OK, Collector
from ingest.realtime.config import HistoryConfig, RealtimeConfig
from ingest.realtime.lock import SingleInstanceLock
from ingest.realtime.status import read_status
from text2sql.realtime_query import RealtimeQueryEngine

NOW = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)  # 台灣 15:30
UNITS_CSV = (
    "unit_type,unit_name,grain,access_scope,plant_id,note\n"
    "燃氣,大潭CC#1,unit,plant,8,名稱前綴對應大潭發電廠\n"
    "燃料油,協和#3,unit,shared,,測試用\n"
    "儲能,明潭#1,unit,plant,12,名稱前綴對應明潭發電廠\n"
    "核能,核三#1,unit,shared,,只出現在 d006010\n"
)
MAPPING_CSV = (
    "history_type,history_name,unit_type,unit_name,relation,basis\n"
    "燃氣,大潭CC#1,燃氣,大潭CC#1,same_unit,類型與名稱完全相同\n"
    "燃油,協和#3,燃料油,協和#3,same_unit,類型 燃油→燃料油\n"
    "抽蓄發電,明潭#1,儲能,明潭#1,same_unit,類型 抽蓄發電→儲能\n"
    "核能,核三#1,核能,核三#1,history_only,即時資料沒有這條序列\n"
)


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _config(tmp_path: Path) -> RealtimeConfig:
    config = make_config(tmp_path)
    (tmp_path / "realtime_units.csv").write_text(UNITS_CSV, encoding="utf-8")
    mapping = tmp_path / "realtime_history_units.csv"
    mapping.write_text(MAPPING_CSV, encoding="utf-8")
    return dataclasses.replace(config, history=HistoryConfig(units_csv=mapping))


def _payload(
    start: str,
    end: str,
    series: dict[tuple[str, str], object],
    *,
    meta: dict[str, object] | None = None,
    extra: list[dict[str, str]] | None = None,
) -> bytes:
    """A d006010 file. `series` values: a number (every slot) or {slot_index: value}."""

    first, last = date.fromisoformat(start), date.fromisoformat(end)
    slots = ((last - first).days + 1) * 144
    records: list[dict[str, str]] = []
    for (fuel, name), values in series.items():
        for index in range(slots):
            if isinstance(values, dict):
                if index not in values:
                    continue
                value = values[index]
            else:
                value = values
            moment = datetime(first.year, first.month, first.day) + timedelta(minutes=10 * index)
            records.append(
                {
                    "FUEL_TYPE": fuel,
                    "UNIT_NAME": name,
                    "DATETIME": moment.strftime("%Y-%m-%dT%H:%M:%S"),
                    "NET_P": f"{value}",
                }
            )
    records += extra or []
    header = {
        "CATALOG": "UNIT_NET_P",
        "START_DATE": f"{start}T00:00:00",
        "END_DATE": f"{end}T23:50:00",
        "UNIT_OF_MEASUREMENT": "MW",
        "INTERVAL": "10MIN",
        **(meta or {}),
    }
    body = json.dumps({"records": {**header, "NET_P": records}}, ensure_ascii=False)
    return codecs.BOM_UTF8 + body.replace(", ", ",").replace(": ", ":").encode("utf-8")


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _import(config: RealtimeConfig, path: Path) -> int:
    return Collector(config, clock=Clock()).import_history(path)


def _daily(config: RealtimeConfig) -> list[tuple]:
    connection = sqlite3.connect(config.database)
    try:
        return connection.execute(
            """SELECT "日期", "機組鍵", "估算發電量_MWh", "取樣點數", "資料來源"
                 FROM v_rt_daily ORDER BY 1, 2"""
        ).fetchall()
    finally:
        connection.close()


# --- 解析 ------------------------------------------------------------------------------


def test_a_well_formed_file_is_parsed_with_its_bom() -> None:
    raw = _payload("2026-03-01", "2026-03-02", {("燃氣", "大潭CC#1"): 600.0})

    parsed = history.parse_history(io.BytesIO(raw))

    assert (parsed.start_date, parsed.end_date) == ("2026-03-01", "2026-03-02")
    assert parsed.records == 288
    daily = list(parsed.daily(("燃氣", "大潭CC#1")))
    assert [item.samples for item in daily] == [144, 144]
    assert daily[0].energy_mwh_est == pytest.approx(600.0 * 24)


def test_duplicates_are_counted_and_the_later_value_wins() -> None:
    raw = _payload(
        "2026-03-01",
        "2026-03-01",
        {("燃氣", "大潭CC#1"): 100.0},
        extra=[
            {
                "FUEL_TYPE": "燃氣",
                "UNIT_NAME": "大潭CC#1",
                "DATETIME": "2026-03-01T00:00:00",
                "NET_P": "100.0",
            },
            {
                "FUEL_TYPE": "燃氣",
                "UNIT_NAME": "大潭CC#1",
                "DATETIME": "2026-03-01T00:10:00",
                "NET_P": "160.0",
            },
        ],
    )

    parsed = history.parse_history(io.BytesIO(raw))

    assert (parsed.duplicates, parsed.conflicts) == (2, 1)
    assert parsed.series[("燃氣", "大潭CC#1")].values[1] == 160.0


def test_bad_values_and_off_slot_times_are_counted_not_imported() -> None:
    raw = _payload(
        "2026-03-01",
        "2026-03-01",
        {("燃氣", "大潭CC#1"): {0: 100.0}},
        extra=[
            {
                "FUEL_TYPE": "燃氣",
                "UNIT_NAME": "大潭CC#1",
                "DATETIME": "2026-03-01T00:15:00",
                "NET_P": "1",
            },
            {
                "FUEL_TYPE": "燃氣",
                "UNIT_NAME": "大潭CC#1",
                "DATETIME": "2026-03-05T00:00:00",
                "NET_P": "1",
            },
            {
                "FUEL_TYPE": "燃氣",
                "UNIT_NAME": "大潭CC#1",
                "DATETIME": "2026-03-01T00:20:00",
                "NET_P": "N/A",
            },
        ],
    )

    parsed = history.parse_history(io.BytesIO(raw))

    assert (parsed.out_of_range, parsed.invalid_values) == (2, 1)
    assert [item.samples for item in parsed.daily(("燃氣", "大潭CC#1"))] == [1]


@pytest.mark.parametrize(
    ("meta", "code"),
    [
        ({"CATALOG": "SOMETHING_ELSE"}, "HIST_UNEXPECTED_FORMAT"),
        ({"INTERVAL": "5MIN"}, "HIST_UNEXPECTED_FORMAT"),
        ({"UNIT_OF_MEASUREMENT": "kW"}, "HIST_UNEXPECTED_FORMAT"),
        ({"END_DATE": "2025-01-01T00:00:00"}, "HIST_UNEXPECTED_FORMAT"),
    ],
)
def test_structural_problems_reject_the_whole_file(meta: dict, code: str) -> None:
    raw = _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0}, meta=meta)

    with pytest.raises(history.HistoryRejected) as error:
        history.parse_history(io.BytesIO(raw))

    assert error.value.code == code


def test_a_record_with_an_unknown_shape_rejects_the_file() -> None:
    raw = _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): {0: 1.0}})
    broken = raw.replace(b'"NET_P":"1.0"}', b'"NET_P":"1.0","EXTRA":"x"}', 1)

    with pytest.raises(history.HistoryRejected) as error:
        history.parse_history(io.BytesIO(broken))

    assert error.value.code == "HIST_RECORD_UNPARSEABLE"


def test_a_file_that_is_not_d006010_is_rejected() -> None:
    with pytest.raises(history.HistoryRejected) as error:
        history.parse_history(io.BytesIO(b'{"DateTime": "x", "aaData": []}'))

    assert error.value.code == "HIST_PAYLOAD_INVALID"


def test_a_mapping_that_sends_two_series_to_one_unit_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "map.csv"
    path.write_text(
        MAPPING_CSV + "燃氣,大潭CC#1(舊),燃氣,大潭CC#1,same_unit,重複\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="同一部機組"):
        history.load_mapping(path)


# --- 匯入 ------------------------------------------------------------------------------


def test_import_archives_maps_and_stores_daily_rollups(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write(
        tmp_path,
        "d006010.json",
        _payload(
            "2026-03-01",
            "2026-03-02",
            {
                ("燃氣", "大潭CC#1"): 600.0,
                ("燃油", "協和#3"): 60.0,
                ("抽蓄發電", "明潭#1"): {0: 120.0, 1: 120.0},
                ("風力", "沒人對照過"): 6.0,
            },
        ),
    )

    assert _import(config, source) == EXIT_OK

    archived = list(config.history_archive_dir.glob("d006010_*.json.gz"))
    assert len(archived) == 1
    assert gzip.decompress(archived[0].read_bytes()) == source.read_bytes()
    rows = _daily(config)
    assert ("2026-03-01", "燃料油|協和#3", pytest.approx(60.0 * 24), 144, "台電歷史資料") in rows
    assert ("2026-03-01", "儲能|明潭#1", pytest.approx(240 / 6), 2, "台電歷史資料") in rows
    connection = sqlite3.connect(config.database)
    try:
        unmapped = connection.execute(
            "SELECT access_scope FROM dim_rt_unit WHERE unit_name = '沒人對照過'"
        ).fetchone()
        imported = connection.execute(
            "SELECT records, series, mapped_series, unmapped FROM meta_rt_history_import"
        ).fetchone()
    finally:
        connection.close()
    # 沒列在對照檔的序列照收，但歸屬未定：電廠帳號看不到。
    assert unmapped == ("undecided",)
    assert imported[:3] == (288 * 3 + 2, 4, 3)
    assert json.loads(imported[3]) == [["風力", "沒人對照過"]]


def test_importing_the_same_file_twice_is_a_no_op(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write(
        tmp_path, "a.json", _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0})
    )
    _import(config, source)

    report = history.import_file(store.connect(config.database), source, config, None, now=NOW)

    assert report.skipped is True


def test_a_newer_file_replaces_the_days_it_covers(tmp_path: Path) -> None:
    config = _config(tmp_path)
    older = _payload("2026-03-01", "2026-03-02", {("燃氣", "大潭CC#1"): 100.0})
    newer = _payload("2026-03-02", "2026-03-03", {("燃氣", "大潭CC#1"): 200.0})
    _import(config, _write(tmp_path, "old.json", older))
    _import(config, _write(tmp_path, "new.json", newer))

    energy = {row[0]: row[2] for row in _daily(config)}

    assert energy == {
        "2026-03-01": pytest.approx(2400.0),
        "2026-03-02": pytest.approx(4800.0),
        "2026-03-03": pytest.approx(4800.0),
    }


def test_a_rejected_file_leaves_no_archive(tmp_path: Path) -> None:
    config = _config(tmp_path)
    raw = _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0}, meta={"INTERVAL": "1H"})

    assert _import(config, _write(tmp_path, "bad.json", raw)) != EXIT_OK
    assert not list(config.history_archive_dir.glob("*.gz"))


def test_import_refuses_while_the_collector_holds_the_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write(
        tmp_path, "a.json", _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0})
    )

    with SingleInstanceLock(config.lock_path):
        assert _import(config, source) == EXIT_ALREADY_RUNNING


def test_rebuild_replays_the_history_archive(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _import(
        config,
        _write(
            tmp_path, "a.json", _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0})
        ),
    )
    before = _daily(config)

    assert Collector(config, clock=Clock()).rebuild_only() == EXIT_OK

    assert _daily(config) == before


def test_startup_imports_an_archived_file_that_was_never_imported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write(
        tmp_path, "a.json", _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0})
    )
    history.archive_history(source, config.history_archive_dir)  # 封存完、匯入前當掉
    connection = store.connect(config.database)
    with store.transaction(connection):
        store.create_schema(connection)
    connection.close()
    collector = Collector(config, clock=Clock())

    with SingleInstanceLock(config.lock_path):
        collector.startup()
        collector.shutdown()

    assert [row[0] for row in _daily(config)] == ["2026-03-01"]


def test_status_reports_the_history_period(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _import(
        config,
        _write(
            tmp_path, "a.json", _payload("2026-03-01", "2026-03-02", {("燃氣", "大潭CC#1"): 1.0})
        ),
    )

    report = read_status(config=config, now=NOW)

    assert report["history"] == {
        "imports": 1,
        "first_day": "2026-03-01",
        "last_day": "2026-03-02",
        "days": 2,
    }


def test_the_cli_routes_history_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(Collector, "import_history", lambda self, path: calls.append(path) or 0)
    monkeypatch.setattr("ingest.realtime.__main__.load_config", lambda *_: _config(tmp_path))

    assert main(["history", "import", "x.json"]) == 0
    assert calls == [Path("x.json")]


# --- 即時與歷史並存 -------------------------------------------------------------------


def _live_day(config: RealtimeConfig, day: str, unit: tuple[str, str], samples: int) -> None:
    connection = store.connect(config.database)
    try:
        with store.transaction(connection):
            unit_id = connection.execute(
                "SELECT id FROM dim_rt_unit WHERE unit_type = ? AND unit_name = ?", unit
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO fact_rt_unit_daily (date, unit_id, energy_mwh_est, max_mw, avg_mw,
                       min_mw, samples, expected_samples)
                   VALUES (?, ?, 1.0, 1.0, 1.0, 1.0, ?, 144)""",
                (day, unit_id, samples),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("live_samples", "source"),
    [(144, "即時收集"), (100, "台電歷史資料")],
)
def test_the_daily_view_keeps_the_copy_with_more_samples(
    tmp_path: Path, live_samples: int, source: str
) -> None:
    """一樣多取即時：即時有明細與備註，而且是收集器自己驗證過的。"""

    config = _config(tmp_path)
    _import(
        config,
        _write(
            tmp_path, "a.json", _payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0})
        ),
    )
    _live_day(config, "2026-03-01", ("燃氣", "大潭CC#1"), live_samples)

    rows = [row for row in _daily(config) if row[1] == "燃氣|大潭CC#1"]

    assert [row[4] for row in rows] == [source]


# --- 用中文問歷史 ---------------------------------------------------------------------


@pytest.fixture
def answered(tmp_path: Path) -> RealtimeQueryEngine:
    config = _config(tmp_path)
    _import(
        config,
        _write(
            tmp_path,
            "a.json",
            _payload(
                "2026-04-01",
                "2026-04-30",
                {("燃氣", "大潭CC#1"): 600.0, ("核能", "核三#1"): 0.0, ("燃油", "協和#3"): 60.0},
            ),
        ),
    )
    # 即時收集：只有一個時段，大潭CC#1 在跑。
    connection = store.connect(config.database)
    try:
        with store.transaction(connection):
            connection.execute(
                """INSERT INTO fact_rt_snapshot (data_time, sha256, fetched_at, detail_rows,
                       quality, warnings) VALUES ('2026-10-05 15:20', ?, ?, 1, 'ok', '[]')""",
                ("0" * 64, "2026-10-05T07:25:30+00:00"),
            )
            unit_id = connection.execute(
                "SELECT id FROM dim_rt_unit WHERE unit_name = '大潭CC#1'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO fact_rt_unit_10min (data_time, unit_id, net_mw, capacity_mw, note,
                       value_status) VALUES ('2026-10-05 15:20', ?, 500.0, 742.7, '', 'ok')""",
                (unit_id,),
            )
    finally:
        connection.close()
    return RealtimeQueryEngine(config, clock=lambda: NOW)


def _ask(engine: RealtimeQueryEngine, question: str):
    return engine.try_query(question, plant=None, plant_roster={}, llm=None, llm_enabled=False)


def _codes(response) -> list[str]:
    return [item["code"] for item in response.data["disclosures"]]


def test_a_month_of_history_is_answered_from_the_daily_view(answered) -> None:
    response = _ask(answered, "2026年4月大潭CC#1發電量")

    assert response.success, response.error
    assert response.data["intent"] == "rt_period_energy"
    row = dict(zip(response.data["columns"], response.data["rows"][0], strict=True))
    assert row["估算發電量_MWh"] == pytest.approx(600.0 * 24 * 30)
    assert row["有資料天數"] == 30
    assert "RT_HISTORY_SOURCE" in _codes(response)
    # 問過去的期間時，收集器有沒有在跑與答案無關。
    assert "RT_COLLECTOR_STOPPED" not in _codes(response)


def test_one_day_of_history(answered) -> None:
    response = _ask(answered, "2026年4月10日燃氣發電量是多少")

    assert response.success, response.error
    assert {"RT_HISTORY_SOURCE", "RT_HISTORY_PARTIAL_SCOPE"} <= set(_codes(response))


def test_a_gap_between_history_and_collection_is_explained(answered) -> None:
    response = _ask(answered, "2026年7月大潭CC#1發電量")

    assert response.error_code == "RT_NO_DATA_FOR_DAY"
    assert "2026-04-01～2026-04-30" in response.error


def test_a_history_only_unit_is_not_described_as_running_now(answered) -> None:
    response = _ask(answered, "現在核三#1出力多少")

    assert response.error_code == "RT_NOT_IN_REALTIME"
    assert "核三#1" in response.error


def test_a_history_only_type_is_not_a_current_type(answered) -> None:
    response = _ask(answered, "現在核能出力")

    assert response.error_code == "RT_NOT_IN_REALTIME"


def test_a_period_partly_without_data_says_how_many_days_are_missing(answered) -> None:
    response = _ask(answered, "2026年4月1日到2026年5月10日大潭CC#1發電量")

    assert response.success, response.error
    assert "RT_PERIOD_GAPS" in _codes(response)


def test_coverage_reports_the_history_range(answered) -> None:
    coverage = answered.coverage()

    assert coverage["history_range"] == ["2026-04-01", "2026-04-30"]
    assert coverage["first_day"] == "2026-04-01"


# --- 每日自動匯入 ---------------------------------------------------------------------


class FakeTaipower:
    """d006010 端點：HEAD 回版本標記，GET 回檔案內容；記下每一次請求。"""

    def __init__(self, payload: bytes, version: str = '"v1"'):
        self.payload = payload
        self.version = version
        self.calls: list[str] = []

    def __call__(self, request, timeout):
        method = request.get_method()
        self.calls.append(method)
        body = b"" if method == "HEAD" else self.payload
        version = self.version

        class Response(io.BytesIO):
            headers = {"ETag": version}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        return Response(body)


def _auto_config(tmp_path: Path, hours: int = 24) -> RealtimeConfig:
    config = _config(tmp_path)
    return dataclasses.replace(
        config, history=dataclasses.replace(config.history, check_hours=hours)
    )


def _maintain(config: RealtimeConfig, taipower: FakeTaipower, now: datetime) -> None:
    collector = Collector(config, clock=Clock(now), opener=taipower)
    with SingleInstanceLock(config.lock_path):
        collector.startup()
        collector.maintain(now)
        collector.shutdown()


def test_the_collector_imports_a_new_d006010_once_a_day(tmp_path: Path) -> None:
    config = _auto_config(tmp_path)
    taipower = FakeTaipower(_payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0}))

    _maintain(config, taipower, NOW)
    _maintain(config, taipower, NOW + timedelta(hours=1))  # 還沒過一天：連 HEAD 都不發

    assert taipower.calls.count("GET") == 1
    assert [row[0] for row in _daily(config)] == ["2026-03-01"]
    assert not list(config.history_download_dir.glob("*.json")), "原始下載檔匯入後要刪掉"


def test_an_unchanged_version_is_not_downloaded_again(tmp_path: Path) -> None:
    config = _auto_config(tmp_path)
    taipower = FakeTaipower(_payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0}))
    _maintain(config, taipower, NOW)

    _maintain(config, taipower, NOW + timedelta(days=1, minutes=1))

    assert taipower.calls == ["HEAD", "GET", "HEAD"]


def test_a_new_version_the_next_month_extends_the_history(tmp_path: Path) -> None:
    config = _auto_config(tmp_path)
    taipower = FakeTaipower(_payload("2026-03-01", "2026-03-01", {("燃氣", "大潭CC#1"): 1.0}))
    _maintain(config, taipower, NOW)
    taipower.payload = _payload("2026-03-01", "2026-03-02", {("燃氣", "大潭CC#1"): 1.0})
    taipower.version = '"v2"'

    _maintain(config, taipower, NOW + timedelta(days=30))

    assert [row[0] for row in _daily(config)] == ["2026-03-01", "2026-03-02"]


def test_auto_import_is_off_unless_configured(tmp_path: Path) -> None:
    config = _auto_config(tmp_path, hours=0)
    taipower = FakeTaipower(b"")

    _maintain(config, taipower, NOW)

    assert taipower.calls == []


def test_a_failed_download_waits_a_day_instead_of_retrying_every_hour(tmp_path: Path) -> None:
    config = _auto_config(tmp_path)
    taipower = FakeTaipower(b'{"not": "d006010"}')

    _maintain(config, taipower, NOW)  # 匯入失敗只記錄，收集照常
    _maintain(config, taipower, NOW + timedelta(hours=1))

    assert taipower.calls == ["HEAD", "GET"]
    assert not list(config.history_archive_dir.glob("*.gz"))
