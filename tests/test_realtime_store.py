from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from realtime_support import make_config, payload_bytes

from ingest.realtime import store
from ingest.realtime.archive import payload_sha256
from ingest.realtime.decisions import load_decisions
from ingest.realtime.parse import parse_payload

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"


def _setup(tmp_path: Path, decisions_body: str = ""):
    config = make_config(tmp_path)
    config.units_csv.write_text(HEADER + decisions_body, encoding="utf-8")
    decisions = load_decisions(config.units_csv, config.plants_csv)
    connection = store.connect(config.database)
    with store.transaction(connection):
        store.create_schema(connection)
        store.write_manifest(
            connection, build_kind="create", archive_files=0, decisions=decisions, now=NOW
        )
        store.sync_plants(connection, decisions.plants)
    return config, decisions, connection


def _ingest(connection, config, decisions, raw: bytes) -> str:
    parsed = parse_payload(
        raw, now=NOW, validation=config.validation, slot_minutes=config.schedule.slot_minutes
    )
    with store.transaction(connection):
        return store.ingest_snapshot(
            connection,
            parsed,
            sha256=payload_sha256(raw),
            fetched_at="2026-09-18T13:45:20+00:00",
            decisions=decisions,
        )


def test_schema_and_views_exist(tmp_path: Path) -> None:
    _config, _decisions, connection = _setup(tmp_path)
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    assert {"v_rt_now", "v_rt_10min", "v_rt_daily", "fact_rt_unit_10min"} <= names
    assert store.schema_version(connection) == store.SCHEMA_VERSION
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_ingesting_the_real_snapshot(tmp_path: Path) -> None:
    config, decisions, connection = _setup(
        tmp_path,
        "燃氣,大潭CC#1,unit,plant,8,\n"
        "儲能負載,明潭#1,unit,plant,12,抽蓄機組的充電負載\n"
        "太陽能,其它購電太陽能,bucket,shared,,\n",
    )

    assert _ingest(connection, config, decisions, payload_bytes()) == "new"
    count = lambda table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
    assert count("fact_rt_unit_10min") == 204
    assert count("fact_rt_type_subtotal") == 11
    assert count("dim_rt_unit") == 204

    rows = {
        (row[0], row[1]): row[2:]
        for row in connection.execute(
            'SELECT "機組類型", "機組名稱", "電廠", "粒度", "淨發電量_MW", "數值狀態" FROM v_rt_now'
        )
    }
    assert len(rows) == 204
    assert rows[("燃氣", "大潭CC#1")] == ("大潭發電廠", "個別", 590.6, "正常")
    assert rows[("儲能負載", "明潭#1")][0] == "明潭發電廠"
    assert rows[("太陽能", "其它購電太陽能")][:2] == (None, "彙總")
    assert rows[("風力", "沃一風")][1] == "未定"
    assert rows[("風力", "龍三風(註10)")][3] == "通訊異常"


def test_same_content_is_a_duplicate_and_changed_content_a_revision(tmp_path: Path) -> None:
    config, decisions, connection = _setup(tmp_path)
    raw = payload_bytes()
    _ingest(connection, config, decisions, raw)

    assert _ingest(connection, config, decisions, raw) == "duplicate"
    revised = raw.replace(b'"590.6"', b'"591.0"')
    assert _ingest(connection, config, decisions, revised) == "revised"

    row = connection.execute("SELECT revision, sha256 FROM fact_rt_snapshot").fetchone()
    assert row == (2, payload_sha256(revised))
    net = connection.execute(
        """SELECT net_mw FROM fact_rt_unit_10min f JOIN dim_rt_unit u ON u.id = f.unit_id
            WHERE u.unit_name = '大潭CC#1'"""
    ).fetchone()[0]
    assert net == 591.0
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 204


def test_later_decisions_are_applied_to_known_units(tmp_path: Path) -> None:
    config, decisions, connection = _setup(tmp_path)
    _ingest(connection, config, decisions, payload_bytes())
    config.units_csv.write_text(
        HEADER + "燃氣,大潭CC#1,unit,plant,8,\n風力,新風場,unit,shared,,\n", encoding="utf-8"
    )
    updated = load_decisions(config.units_csv, config.plants_csv)

    with store.transaction(connection):
        stale = store.sync_decisions(connection, updated)

    assert stale == [("風力", "新風場")]
    plant = connection.execute(
        """SELECT "電廠" FROM v_rt_now WHERE "機組名稱" = '大潭CC#1'"""
    ).fetchone()[0]
    assert plant == "大潭發電廠"
    manifest = connection.execute("SELECT decisions_sha256 FROM meta_rt_manifest").fetchone()[0]
    assert manifest == updated.sha256


def test_plant_scope_requires_a_plant_id(tmp_path: Path) -> None:
    _config, _decisions, connection = _setup(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO dim_rt_unit (unit_type, unit_type_raw, unit_name, flow, grain,
                   access_scope, plant_id, first_seen, last_seen)
               VALUES ('燃氣', '燃氣', 'X', 'generation', 'unit', 'plant', NULL, 'a', 'a')"""
        )


def test_attempts_are_recorded(tmp_path: Path) -> None:
    _config, _decisions, connection = _setup(tmp_path)
    with store.transaction(connection):
        store.record_attempt(
            connection,
            {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "fetch", "outcome": "error"},
        )

    assert connection.execute("SELECT kind, outcome, detail FROM meta_rt_attempt").fetchone() == (
        "fetch",
        "error",
        "",
    )
