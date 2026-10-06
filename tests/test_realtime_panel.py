"""RealtimePanel: the realtime block on the overview page (RT-3a spec §4, §8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from realtime_panel_support import (
    NOW,
    PLANTS,
    Unit,
    build_realtime_database,
    build_standard_database,
)
from realtime_support import make_config, write_project

from ingest.realtime.lock import SingleInstanceLock
from serving import realtime_panel
from serving.realtime_panel import (
    PUBLIC_STATUS_FIELDS,
    RealtimePanel,
    RealtimeReadError,
    RealtimeScopeMismatch,
)
from text2sql.realtime_scope import ALL_PLANTS, RealtimeScope

UNAVAILABLE = {"available": False, "state": "unavailable"}
DATAN = RealtimeScope("plant", 8, "大潭發電廠")
MINGTAN = RealtimeScope("plant", 12, "明潭發電廠")


def _panel(tmp_path: Path, **build: object) -> RealtimePanel:
    config = make_config(tmp_path)
    build_standard_database(config, **build)
    return RealtimePanel(config)


def _running(
    panel: RealtimePanel,
    scope: RealtimeScope = ALL_PLANTS,
    *,
    now: datetime = NOW,
    plants: dict[int, str] | None = PLANTS,
) -> dict:
    """Overview while the collector holds its lock (state healthy unless the data is old)."""

    with SingleInstanceLock(panel.config.lock_path):
        return panel.overview(scope, plants=plants, now=now)


def _entry(data: dict, unit_type: str) -> dict:
    return next(entry for entry in data["by_type"] if entry["type"] == unit_type)


def _series(data: dict, unit_type: str) -> list:
    return next(item["net_mw"] for item in data["today"]["series"] if item["type"] == unit_type)


def _codes(data: dict) -> list[str]:
    return [item["code"] for item in data["disclosures"]]


# ── 狀態（/api/health 的 realtime 欄位）──────────────────────────────────


def test_status_without_a_database_is_unavailable(tmp_path: Path) -> None:
    assert RealtimePanel(make_config(tmp_path)).status(now=NOW) == UNAVAILABLE


def test_status_shows_the_state_but_no_generation_numbers(tmp_path: Path) -> None:
    panel = _panel(tmp_path)

    with SingleInstanceLock(panel.config.lock_path):
        status = panel.status(now=NOW)

    assert set(status) == set(PUBLIC_STATUS_FIELDS)
    assert status == {
        "available": True,
        "state": "healthy",
        "collector_running": True,
        "latest_data_time": "2026-10-05 15:20",
        "lag_minutes": 7.4,
    }


def test_status_reports_a_stopped_collector(tmp_path: Path) -> None:
    status = _panel(tmp_path).status(now=NOW)

    assert status["state"] == "stopped"
    assert status["collector_running"] is False


def test_status_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _panel(tmp_path)

    def broken(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(realtime_panel, "read_status", broken)

    assert panel.status(now=NOW) == UNAVAILABLE


def test_a_panel_without_settings_shows_no_data() -> None:
    assert RealtimePanel(None).status(now=NOW) == UNAVAILABLE


def test_from_project_reads_the_project_settings(tmp_path: Path) -> None:
    root = write_project(tmp_path)

    panel = RealtimePanel.from_project(root)

    assert panel.config is not None
    assert panel.config.database == root.resolve() / "data/processed/realtime.db"


def test_a_broken_realtime_config_does_not_stop_the_service(tmp_path: Path) -> None:
    root = write_project(tmp_path)
    (root / "configs/realtime.yaml").write_text("source: [\n", encoding="utf-8")

    panel = RealtimePanel.from_project(root)

    assert panel.config is None
    assert panel.status(now=NOW) == UNAVAILABLE


# ── 全範圍的數字 ───────────────────────────────────────────────────────────


def test_overview_header_fields(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path))

    assert {key: data[key] for key in ("available", "data_time", "state", "lag_minutes")} == {
        "available": True,
        "data_time": "2026-10-05 15:20",
        "state": "healthy",
        "lag_minutes": 7.4,
    }
    assert (data["quality"], data["scope"], data["disclosures"]) == ("ok", "all", [])


def test_totals_count_only_normal_values_and_report_the_rest(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path))

    # 480 + 300（歸屬未定的機組也算）；通訊異常的大潭#2 不加，但容量照加、另計一台
    assert _entry(data, "燃氣") == {
        "type": "燃氣",
        "net_mw": 780.0,
        "capacity_mw": 1550.0,
        "units": 3,
        "unreliable_units": 1,
    }


def test_storage_and_storage_load_stay_separate_and_types_sort_by_output(
    tmp_path: Path,
) -> None:
    data = _running(_panel(tmp_path))

    assert [entry["type"] for entry in data["by_type"]] == [
        "太陽能",
        "燃氣",
        "水力",
        "儲能",
        "儲能負載",
    ]
    assert _entry(data, "儲能")["net_mw"] == 40.0
    assert _entry(data, "儲能負載")["net_mw"] == 25.0


def test_missing_capacity_is_not_zero_and_an_all_unreliable_type_has_no_total(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    biogas = Unit("其它再生能源", "沼氣", "shared")
    wind = Unit("風力", "離岸#1", "shared")
    build_realtime_database(
        config,
        plants=PLANTS,
        units=(biogas, wind),
        snapshots={
            "2026-10-05 15:20": {
                biogas.key: (5.0, None, "ok"),
                wind.key: (None, 100.0, "comm_error"),
            }
        },
    )

    data = _running(RealtimePanel(config))

    assert _entry(data, "其它再生能源")["capacity_mw"] is None
    assert _entry(data, "風力") == {
        "type": "風力",
        "net_mw": None,
        "capacity_mw": 100.0,
        "units": 1,
        "unreliable_units": 1,
    }


def test_the_today_trend_marks_gaps_as_null_not_zero(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path))
    today = data["today"]

    assert today["date"] == "2026-10-05"
    assert len(today["slots"]) == 93
    assert (today["slots"][0], today["slots"][-1]) == ("00:00", "15:20")
    gas = _series(data, "燃氣")
    assert gas[:90] == [None] * 90  # 昨天 23:50 的值沒有算進今天
    assert (gas[90], gas[91], gas[92]) == (1270.0, None, 780.0)  # 15:10 沒抓到
    solar = _series(data, "太陽能")
    assert (solar[90], solar[92]) == (None, 7731.1)  # 15:00 沒有太陽能的值
    assert [item["type"] for item in today["series"]] == [
        "太陽能",
        "燃氣",
        "水力",
        "儲能",
        "儲能負載",
    ]
    assert (today["elapsed_slots"], today["snapshots"]) == (93, 2)


def test_today_follows_taiwan_time(tmp_path: Path) -> None:
    # UTC 16:30 已經是台灣隔天 00:30
    data = _running(_panel(tmp_path), now=datetime(2026, 10, 5, 16, 30, tzinfo=UTC))

    assert data["today"]["date"] == "2026-10-06"
    assert (data["today"]["slots"], data["today"]["series"]) == ([], [])
    assert "RT_NO_DATA_TODAY" in _codes(data)
    assert _entry(data, "燃氣")["net_mw"] == 780.0  # 最新一筆照常顯示


# ── 揭露 ───────────────────────────────────────────────────────────────────


def test_a_stopped_collector_is_disclosed(tmp_path: Path) -> None:
    data = _panel(tmp_path).overview(ALL_PLANTS, now=NOW)

    assert data["state"] == "stopped"
    assert _codes(data) == ["RT_COLLECTOR_STOPPED"]


def test_stale_data_is_disclosed_with_the_lag(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), now=NOW + timedelta(minutes=40))

    assert data["state"] == "stale"
    assert _codes(data) == ["RT_STALE"]
    assert "47 分鐘" in data["disclosures"][0]["reason"]


def test_a_quality_warning_is_disclosed(tmp_path: Path) -> None:
    warning = {"code": "SUBTOTAL_MISMATCH", "detail": "燃氣 明細加總與小計差 0.5 MW"}

    data = _running(_panel(tmp_path, warnings={"2026-10-05 15:20": [warning]}))

    assert data["quality"] == "warn"
    assert _codes(data) == ["RT_QUALITY_WARN"]
    assert "SUBTOTAL_MISMATCH" in data["disclosures"][0]["reason"]


def test_a_plant_scope_sees_the_warning_codes_but_no_system_wide_details(
    tmp_path: Path,
) -> None:
    warnings = [
        {"code": "VALUE_UNPARSEABLE", "detail": "淨發電量 1 列：興達#1"},
        {"code": "SUBTOTAL_MISMATCH", "detail": "燃氣：明細 780.0 MW，小計 790.0 MW"},
    ]

    data = _running(_panel(tmp_path, warnings={"2026-10-05 15:20": warnings}), DATAN)

    reasons = {item["code"]: item["reason"] for item in data["disclosures"]}
    assert reasons["RT_QUALITY_WARN"] == (
        "最新快照有驗證警告（VALUE_UNPARSEABLE、SUBTOTAL_MISMATCH）；細節請洽管理員。"
    )
    assert not any("興達" in reason or "790.0" in reason for reason in reasons.values())


def test_the_all_scope_still_sees_the_warning_details(tmp_path: Path) -> None:
    warnings = [{"code": "SUBTOTAL_MISMATCH", "detail": "燃氣：明細 780.0 MW，小計 790.0 MW"}]

    data = _running(_panel(tmp_path, warnings={"2026-10-05 15:20": warnings}))

    assert "790.0 MW" in data["disclosures"][0]["reason"]


# ── 沒有資料、讀不到 ───────────────────────────────────────────────────────


def test_overview_without_a_database_is_unavailable(tmp_path: Path) -> None:
    assert RealtimePanel(make_config(tmp_path)).overview(ALL_PLANTS, now=NOW) == UNAVAILABLE
    assert RealtimePanel(None).overview(ALL_PLANTS, now=NOW) == UNAVAILABLE


def test_a_database_without_snapshots_has_no_numbers_yet(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    build_realtime_database(config, plants=PLANTS, units=(), snapshots={})

    assert RealtimePanel(config).overview(ALL_PLANTS, now=NOW) == {
        "available": False,
        "state": "stopped",
    }


def test_an_unreadable_database_raises_a_read_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.database.parent.mkdir(parents=True)
    config.database.write_bytes(b"not a database" * 100)

    with pytest.raises(RealtimeReadError):
        RealtimePanel(config).overview(ALL_PLANTS, now=NOW)


# ── 電廠帳號 ───────────────────────────────────────────────────────────────


def test_a_plant_sees_its_own_units_and_shared_rows_split_in_two(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), DATAN)

    assert data["scope"] == "plant:大潭發電廠"
    assert [entry["type"] for entry in data["by_type"]] == ["太陽能", "燃氣", "儲能", "儲能負載"]
    assert _entry(data, "燃氣") == {
        "type": "燃氣",
        "own_net_mw": 480.0,
        "shared_net_mw": None,
        "capacity_mw": 1000.0,
        "units": 2,
        "unreliable_units": 1,
    }
    assert _entry(data, "太陽能") == {
        "type": "太陽能",
        "own_net_mw": None,
        "shared_net_mw": 7731.1,
        "capacity_mw": 15000.0,
        "units": 1,
        "unreliable_units": 0,
    }
    assert _codes(data) == ["RT_SCOPE_PLANT"]


def test_a_plant_never_sees_undecided_units_or_another_plant(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), DATAN)

    gas = _series(data, "燃氣")
    assert (gas[90], gas[92]) == (960.0, 480.0)  # 不含歸屬未定的興達#1
    assert "水力" not in [item["type"] for item in data["today"]["series"]]


def test_another_plant_sees_only_its_own_units(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), MINGTAN)

    assert [entry["type"] for entry in data["by_type"]] == ["太陽能", "水力"]
    assert _entry(data, "水力")["own_net_mw"] == 250.0


@pytest.mark.parametrize(
    "plants",
    [None, {8: "大潭發電廠", 12: "明潭電廠"}, {8: "大潭發電廠"}],
    ids=["no-roster", "renamed", "missing-plant"],
)
def test_a_plant_scope_refuses_a_roster_that_does_not_match(
    tmp_path: Path, plants: dict[int, str] | None
) -> None:
    with pytest.raises(RealtimeScopeMismatch):
        _running(_panel(tmp_path), DATAN, plants=plants)


def test_the_all_scope_does_not_need_the_roster(tmp_path: Path) -> None:
    assert _running(_panel(tmp_path), plants=None)["available"] is True
