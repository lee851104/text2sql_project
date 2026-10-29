"""Health status shared by `status` and, in RT-3, the service's /api/health (§7.5, §8.3).

唯讀開檔、不需要鎖；只為了判斷收集器有沒有在跑而試鎖一次、立刻放掉。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ingest.realtime.config import RealtimeConfig, load_config
from ingest.realtime.decisions import LOAD_ERRORS, load_decisions
from ingest.realtime.lock import is_locked
from ingest.realtime.maintenance import classify_missing, day_slots
from ingest.realtime.schedule import target_slot
from ingest.realtime.store import schema_version
from ingest.realtime.timeutil import format_slot, parse_slot, to_taipei
from ingest.validate import PROJECT_ROOT


def read_status(
    root: Path = PROJECT_ROOT,
    *,
    now: datetime | None = None,
    config: RealtimeConfig | None = None,
) -> dict[str, object]:
    config = config or load_config(root)
    now = now or datetime.now(UTC)
    base: dict[str, object] = {
        "collector_running": is_locked(config.lock_path),
        "database": str(config.database),
    }
    if not config.database.is_file():
        return {**base, "available": False, "state": "unavailable"}
    try:
        connection = sqlite3.connect(f"{config.database.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return {**base, "available": False, "state": "unavailable", "error": str(error)}
    try:
        connection.execute("PRAGMA query_only = ON")
        return {**base, **_read(connection, config, now, bool(base["collector_running"]))}
    except sqlite3.Error as error:
        return {**base, "available": False, "state": "unavailable", "error": str(error)}
    finally:
        connection.close()


def _read(
    connection: sqlite3.Connection, config: RealtimeConfig, now: datetime, running: bool
) -> dict[str, object]:
    latest = connection.execute("SELECT MAX(data_time) FROM fact_rt_snapshot").fetchone()[0]
    lag = None if latest is None else round((now - parse_slot(latest)).total_seconds() / 60, 1)
    failures = 0
    for (outcome,) in connection.execute(
        "SELECT outcome FROM meta_rt_attempt WHERE kind = 'fetch' ORDER BY id DESC LIMIT 100"
    ):
        if outcome not in ("error", "rejected"):
            break
        failures += 1
    target = format_slot(target_slot(now, config.schedule))
    today = to_taipei(now).date().isoformat()
    elapsed = [slot for slot in day_slots(today, config.schedule.slot_minutes) if slot <= target]
    snapshots_today = connection.execute(
        "SELECT COUNT(*) FROM fact_rt_snapshot WHERE data_time >= ? AND data_time < ?",
        (f"{today} 00:00", f"{today} 24:00"),
    ).fetchone()[0]
    window_start = parse_slot(target) - timedelta(days=1)
    window = [
        format_slot(window_start + timedelta(minutes=config.schedule.slot_minutes * step))
        for step in range(1, config.slots_per_day + 1)
    ]
    gaps = Counter(classify_missing(connection, window).values())
    undecided = connection.execute(
        "SELECT COUNT(*) FROM dim_rt_unit WHERE grain = 'undecided' OR access_scope = 'undecided'"
    ).fetchone()[0]
    known = {
        (row[0], row[1])
        for row in connection.execute("SELECT unit_type, unit_name FROM dim_rt_unit")
    }
    decisions_error: str | None = None
    stale_decisions = 0
    try:
        decisions = load_decisions(config.units_csv, config.plants_csv)
        stale_decisions = len(set(decisions.units) - known)
    except LOAD_ERRORS as error:
        decisions_error = str(error)
    quality_row = (
        connection.execute(
            "SELECT quality, warnings FROM fact_rt_snapshot WHERE data_time = ?", (latest,)
        ).fetchone()
        if latest is not None
        else None
    )
    if not running:
        state = "stopped"
    elif lag is None or lag > config.stale_after_minutes:
        state = "stale"
    else:
        state = "healthy"
    return {
        "available": True,
        "state": state,
        "schema_version": schema_version(connection),
        "latest_data_time": latest,
        "lag_minutes": lag,
        "consecutive_failures": failures,
        "today": {"elapsed_slots": len(elapsed), "snapshots": snapshots_today},
        "gaps_24h": {
            reason: gaps[reason] for reason in ("collector_down", "fetch_failed", "rejected")
        },
        "undecided_units": undecided,
        "stale_decisions": stale_decisions,
        "decisions_error": decisions_error,
        "latest_quality": None if quality_row is None else quality_row[0],
        "latest_warnings": [] if quality_row is None else json.loads(quality_row[1]),
    }
