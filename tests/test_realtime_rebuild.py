from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from realtime_support import make_config, payload_bytes, tiny_payload

from ingest.realtime import archive, maintenance, rebuild, store
from ingest.realtime.decisions import load_decisions
from ingest.realtime.parse import PayloadRejected, parse_payload, read_datetime
from ingest.realtime.timeutil import utc_iso

HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"
NOW = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)  # 2026-09-21 12:00 in Taipei


def _setup(tmp_path: Path):
    config = make_config(tmp_path)
    config.units_csv.write_text(HEADER + "燃氣,大潭CC#1,unit,plant,8,\n", encoding="utf-8")
    return config, load_decisions(config.units_csv, config.plants_csv)


def _live_ingest(connection, config, decisions, raw: bytes, fetched_at: datetime) -> str:
    """與收集器相同的順序：先封存，再解析入庫，最後寫抓取紀錄。"""
    sha = archive.payload_sha256(raw)
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=fetched_at
    )
    record = {
        "attempted_at": utc_iso(fetched_at),
        "kind": "fetch",
        "target_slot": None,
        "sha256": sha,
    }
    try:
        parsed = parse_payload(
            raw,
            now=fetched_at,
            validation=config.validation,
            slot_minutes=config.schedule.slot_minutes,
        )
    except PayloadRejected as rejection:
        record.update(outcome="rejected", reject_code=rejection.code)
    else:
        with store.transaction(connection):
            ingested = store.ingest_snapshot(
                connection, parsed, sha256=sha, fetched_at=utc_iso(fetched_at), decisions=decisions
            )
        outcome = {"new": "new", "revised": "revised", "duplicate": "stale"}[ingested]
        record.update(outcome=outcome, target_slot=parsed.data_time, data_time=parsed.data_time)
    archive.append_attempt(config.attempts_dir, record)
    with store.transaction(connection):
        store.record_attempt(connection, record)
    return str(record["outcome"])


def _contents(connection) -> dict[str, list[tuple]]:
    tables = [t for t in store.TABLES_IN_DROP_ORDER if t != "meta_rt_manifest"]
    return {
        table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        for table in tables
    }


def _payloads() -> list[tuple[bytes, datetime]]:
    first = datetime(2026, 9, 19, 15, 45, 20, tzinfo=UTC)  # 2026-09-19 23:45:20 Taipei
    return [
        (payload_bytes(data_time="2026-09-19T23:40:00"), first),
        (payload_bytes(data_time="2026-09-19T23:50:00"), first + timedelta(minutes=10)),
        (
            tiny_payload("2026-09-20T00:00:00", [("燃氣", "大潭CC#1", "742.7", "500.0", "")]),
            first + timedelta(minutes=20),
        ),
    ]


def test_rebuild_reproduces_the_incrementally_built_database(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    live = store.connect(config.database)
    rebuild.rebuild(live, config, decisions, now=NOW)  # 空封存：等於建立空資料庫
    for raw, fetched_at in _payloads():
        assert _live_ingest(live, config, decisions, raw, fetched_at) == "new"
    maintenance.run_maintenance(live, NOW, config)

    replayed = store.connect(tmp_path / "replayed.db")
    report = rebuild.rebuild(replayed, config, decisions, now=NOW)

    assert report.ingested == 3
    assert _contents(replayed) == _contents(live)
    manifest = replayed.execute("SELECT build_kind, archive_files FROM meta_rt_manifest").fetchone()
    assert manifest == ("rebuild", 3)


def test_first_start_with_an_empty_archive_creates_the_schema(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    connection = store.connect(config.database)

    report = rebuild.rebuild(connection, config, decisions, now=NOW)

    assert report == rebuild.RebuildReport(0, 0, 0, 0, 0)
    assert store.schema_version(connection) == store.SCHEMA_VERSION
    assert connection.execute("SELECT build_kind FROM meta_rt_manifest").fetchone()[0] == "create"


def test_payload_rejected_live_comes_back_after_rebuild(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    live = store.connect(config.database)
    rebuild.rebuild(live, config, decisions, now=NOW)
    raw = payload_bytes(data_time="2026-09-21T13:00:00")
    too_early = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)  # 12:00 Taipei: 13:00 is in the future

    assert _live_ingest(live, config, decisions, raw, too_early) == "rejected"
    later = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    report = rebuild.rebuild(live, config, decisions, now=later)

    assert report.ingested == 1
    assert store.latest_data_time(live) == "2026-09-21 13:00"


def test_corrupted_archive_file_is_skipped(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    bad = config.archive_dir / "2026/09/19/2340_000000000000.json.gz"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(gzip.compress(payload_bytes(data_time="2026-09-19T23:40:00")))
    connection = store.connect(config.database)

    report = rebuild.rebuild(connection, config, decisions, now=NOW)

    assert (report.corrupted, report.ingested) == (1, 0)


def test_revisions_are_replayed_in_fetch_order(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    live = store.connect(config.database)
    rebuild.rebuild(live, config, decisions, now=NOW)
    original = payload_bytes(data_time="2026-09-19T23:40:00")
    revised = original.replace(b'"590.6"', b'"591.0"')
    fetched = datetime(2026, 9, 19, 15, 45, 20, tzinfo=UTC)
    _live_ingest(live, config, decisions, original, fetched)
    _live_ingest(live, config, decisions, revised, fetched + timedelta(minutes=3))

    replayed = store.connect(tmp_path / "replayed.db")
    rebuild.rebuild(replayed, config, decisions, now=NOW)

    row = replayed.execute("SELECT revision, sha256 FROM fact_rt_snapshot").fetchone()
    assert row == (2, archive.payload_sha256(revised))
    assert json.loads(replayed.execute("SELECT warnings FROM fact_rt_snapshot").fetchone()[0]) == [
        {"code": "SUBTOTAL_MISMATCH", "detail": "燃氣：明細 16521.8 MW，小計 16521.4 MW"}
    ]
