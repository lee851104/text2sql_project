from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from realtime_support import make_config, tiny_payload

from ingest.realtime import maintenance, store
from ingest.realtime.archive import payload_sha256
from ingest.realtime.decisions import load_decisions
from ingest.realtime.parse import parse_payload

LIVE_NOW = datetime(2026, 9, 20, 0, 45, tzinfo=UTC)  # 08:45 in Taipei
CLOSED_NOW = datetime(2026, 9, 20, 16, 20, tzinfo=UTC)  # 2026-09-21 00:20 in Taipei


def _db(tmp_path: Path):
    config = make_config(tmp_path)
    decisions = load_decisions(config.units_csv, config.plants_csv)
    connection = store.connect(config.database)
    with store.transaction(connection):
        store.create_schema(connection)
        store.sync_plants(connection, decisions.plants)
        store.record_attempt(
            connection, {"attempted_at": "2026-09-19T23:00:00+00:00", "kind": "startup"}
        )
    return config, connection


def _ingest(connection, config, raw: bytes) -> None:
    parsed = parse_payload(
        raw, now=LIVE_NOW, validation=config.validation, slot_minutes=config.schedule.slot_minutes
    )
    with store.transaction(connection):
        store.ingest_snapshot(
            connection, parsed, sha256=payload_sha256(raw), fetched_at="x", decisions=None
        )


def _attempt(connection, slot: str, outcome: str) -> None:
    with store.transaction(connection):
        store.record_attempt(
            connection,
            {
                "attempted_at": "2026-09-20T00:40:00+00:00",
                "kind": "fetch",
                "target_slot": slot,
                "outcome": outcome,
            },
        )


def _load_day(connection, config) -> None:
    rows = {
        "08:00": [("燃氣", "A", "100", "60", ""), ("儲能負載", "C", "-", "-30", "")],
        "08:10": [("燃氣", "A", "100", "120", "歲修"), ("儲能負載", "C", "-", "-30", "")],
        "08:20": [("燃氣", "A", "100", "N/A", "")],
    }
    for clock, units in rows.items():
        units = [*units, ("風力", "B", "50", "0.0", "通訊異常")]
        _ingest(connection, config, tiny_payload(f"2026-09-20T{clock}:00", units))
    _attempt(connection, "2026-09-20 08:30", "error")
    _attempt(connection, "2026-09-20 08:40", "rejected")


def _daily(connection) -> dict[str, tuple]:
    return {
        row[0]: row[1:]
        for row in connection.execute(
            """SELECT u.unit_name, d.energy_mwh_est, d.max_mw, d.avg_mw, d.min_mw, d.samples,
                      d.expected_samples, d.notes_seen
                 FROM fact_rt_unit_daily d JOIN dim_rt_unit u ON u.id = d.unit_id"""
        )
    }


def test_rollup_counts_only_trusted_values(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    report = maintenance.run_maintenance(connection, CLOSED_NOW, config)

    assert report.rolled_up == ("2026-09-20",)
    daily = _daily(connection)
    assert daily["A"] == (30.0, 120.0, 90.0, 60.0, 2, 144, "歲修")
    assert daily["B"] == (None, None, None, None, 0, 144, "通訊異常")
    assert daily["C"][0] == -10.0


def test_gaps_are_classified_and_add_up_to_144(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    maintenance.run_maintenance(connection, CLOSED_NOW, config)

    row = connection.execute(
        """SELECT snapshots, missed_collector_down, missed_fetch_failed, missed_rejected, expected
             FROM fact_rt_day WHERE date = '2026-09-20'"""
    ).fetchone()
    assert row == (3, 139, 1, 1, 144)


def test_day_closes_at_0015_the_next_morning(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    before = datetime(2026, 9, 20, 16, 14, tzinfo=UTC)  # 2026-09-21 00:14 in Taipei
    after = datetime(2026, 9, 20, 16, 16, tzinfo=UTC)

    assert maintenance.closed_through(before, config) == date(2026, 9, 19)
    assert maintenance.closed_through(after, config) == date(2026, 9, 20)


def test_purge_only_removes_rolled_up_days_past_retention(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)
    maintenance.run_maintenance(connection, CLOSED_NOW, config)
    much_later = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)

    report = maintenance.run_maintenance(connection, much_later, config)

    assert "2026-09-20" in report.purged
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_daily").fetchone()[0] == 3
    purged_at = connection.execute(
        "SELECT purged_at FROM fact_rt_day WHERE date = '2026-09-20'"
    ).fetchone()[0]
    assert purged_at == "2026-10-10T04:00:00+00:00"


def test_day_without_rollup_is_never_purged(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    with store.transaction(connection):
        purged = maintenance.purge(connection, cutoff="2026-12-31", now=CLOSED_NOW)

    assert purged == []
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 8


def test_rows_written_after_rollup_block_purge_until_recomputed(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)
    maintenance.run_maintenance(connection, CLOSED_NOW, config)
    _ingest(
        connection, config, tiny_payload("2026-09-20T08:30:00", [("燃氣", "A", "100", "90", "")])
    )

    with store.transaction(connection):
        purged = maintenance.purge(connection, cutoff="2026-12-31", now=CLOSED_NOW)
    assert purged == []
    assert maintenance.days_needing_rollup(connection, CLOSED_NOW, config) == ["2026-09-20"]


def test_failed_rollup_leaves_every_row_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(maintenance, "rollup_day", broken)
    with pytest.raises(RuntimeError):
        maintenance.run_maintenance(connection, datetime(2026, 12, 31, tzinfo=UTC), config)

    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 8
    with store.transaction(connection):
        assert maintenance.purge(connection, cutoff="2026-12-31", now=CLOSED_NOW) == []
