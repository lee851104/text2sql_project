"""WAL + read-only: the service must read realtime.db while the collector writes (§10)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from realtime_support import make_config

from ingest.realtime import rebuild, store
from ingest.realtime.decisions import load_decisions
from text2sql.db import ReadOnlySQLite

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
ATTEMPT = {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "startup"}


def _create(tmp_path: Path):
    config = make_config(tmp_path)
    connection = store.connect(config.database)
    rebuild.rebuild(
        connection, config, load_decisions(config.units_csv, config.plants_csv), now=NOW
    )
    return config, connection


def test_reader_sees_committed_rows_while_a_write_is_open(tmp_path: Path) -> None:
    config, writer = _create(tmp_path)
    with store.transaction(writer):
        store.record_attempt(writer, ATTEMPT)
    reader = ReadOnlySQLite(config.database)

    writer.execute("BEGIN IMMEDIATE")
    store.record_attempt(writer, ATTEMPT)
    _columns, rows = reader.execute("SELECT COUNT(*) FROM meta_rt_attempt", ())
    writer.execute("COMMIT")

    assert rows == [(1,)]
    assert reader.execute("SELECT COUNT(*) FROM meta_rt_attempt", ())[1] == [(2,)]


def test_reader_opens_the_file_when_the_collector_is_stopped(tmp_path: Path) -> None:
    config, writer = _create(tmp_path)
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{config.database}{suffix}").unlink(missing_ok=True)

    _columns, rows = ReadOnlySQLite(config.database).execute("SELECT COUNT(*) FROM v_rt_now", ())

    assert rows == [(0,)]
