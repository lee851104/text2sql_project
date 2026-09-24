"""Daily rollups, gap classification and purging (§7.1, §7.2, §7.4).

清除的安全條件寫在 SQL 裡：只刪「已彙總、而且彙總之後沒有新明細寫入」的日子，
所以就算呼叫順序寫錯，也刪不到還沒彙總的資料。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ingest.realtime.config import RealtimeConfig
from ingest.realtime.store import transaction
from ingest.realtime.timeutil import TAIPEI, format_slot, parse_utc_iso, to_taipei, utc_iso


@dataclass(frozen=True)
class MaintenanceReport:
    rolled_up: tuple[str, ...]
    purged: tuple[str, ...]


def day_slots(day: str, slot_minutes: int) -> list[str]:
    start = datetime.fromisoformat(day).replace(tzinfo=TAIPEI)
    return [
        format_slot(start + timedelta(minutes=slot_minutes * index))
        for index in range(24 * 60 // slot_minutes)
    ]


def closed_through(now: datetime, config: RealtimeConfig) -> date:
    """最後一個已結束的日子：隔天 day_closed_after（預設 00:15）之後才算結束。"""
    closed_after = timedelta(
        hours=config.day_closed_after.hour, minutes=config.day_closed_after.minute
    )
    return (to_taipei(now) - closed_after).date() - timedelta(days=1)


def purge_cutoff(now: datetime, config: RealtimeConfig) -> str:
    return (to_taipei(now).date() - timedelta(days=config.raw_days)).isoformat()


def first_day(connection: sqlite3.Connection) -> date | None:
    """第一次啟動（或第一筆快照）的那天；之後每一天都要有一列 fact_rt_day。"""
    first_attempt = connection.execute("SELECT MIN(attempted_at) FROM meta_rt_attempt").fetchone()[
        0
    ]
    first_snapshot = connection.execute("SELECT MIN(data_time) FROM fact_rt_snapshot").fetchone()[0]
    days: list[date] = []
    if first_attempt:
        days.append(to_taipei(parse_utc_iso(first_attempt)).date())
    if first_snapshot:
        days.append(date.fromisoformat(first_snapshot[:10]))
    return min(days) if days else None


def _row_count(connection: sqlite3.Connection, day: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM fact_rt_unit_10min WHERE data_time >= ? AND data_time < ?",
        (f"{day} 00:00", f"{day} 24:00"),
    ).fetchone()[0]


def days_needing_rollup(
    connection: sqlite3.Connection, now: datetime, config: RealtimeConfig
) -> list[str]:
    start, end = first_day(connection), closed_through(now, config)
    if start is None or start > end:
        return []
    recorded = {
        row[0]: (row[1], row[2])
        for row in connection.execute("SELECT date, rows_at_rollup, purged_at FROM fact_rt_day")
    }
    days: list[str] = []
    current = start
    while current <= end:
        day = current.isoformat()
        entry = recorded.get(day)
        if entry is None or (entry[1] is None and entry[0] != _row_count(connection, day)):
            days.append(day)
        current += timedelta(days=1)
    return days


def classify_missing(connection: sqlite3.Connection, slots: list[str]) -> dict[str, str]:
    """{缺少的時段: 原因}，原因依序判斷：rejected → fetch_failed → collector_down。"""
    if not slots:
        return {}
    first, last = slots[0], slots[-1]
    present = {
        row[0]
        for row in connection.execute(
            "SELECT data_time FROM fact_rt_snapshot WHERE data_time >= ? AND data_time <= ?",
            (first, last),
        )
    }
    attempts = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            """SELECT target_slot, COUNT(*), SUM(outcome = 'rejected')
                 FROM meta_rt_attempt
                WHERE kind = 'fetch' AND target_slot >= ? AND target_slot <= ?
                GROUP BY target_slot""",
            (first, last),
        )
    }
    reasons: dict[str, str] = {}
    for slot in slots:
        if slot in present:
            continue
        tried, rejected = attempts.get(slot, (0, 0))
        reasons[slot] = "rejected" if rejected else "fetch_failed" if tried else "collector_down"
    return reasons


def rollup_day(
    connection: sqlite3.Connection, day: str, now: datetime, config: RealtimeConfig
) -> None:
    """重算一天的彙總與缺口。呼叫端負責交易。只算 value_status = 'ok' 的值。"""
    start, end = f"{day} 00:00", f"{day} 24:00"
    connection.execute("DELETE FROM fact_rt_unit_daily WHERE date = ?", (day,))
    connection.execute(
        """INSERT INTO fact_rt_unit_daily
               (date, unit_id, energy_mwh_est, max_mw, avg_mw, min_mw, samples,
                expected_samples, notes_seen, source)
           SELECT ?, unit_id,
                  SUM(CASE WHEN value_status = 'ok' THEN net_mw END) * ? / 60.0,
                  MAX(CASE WHEN value_status = 'ok' THEN net_mw END),
                  AVG(CASE WHEN value_status = 'ok' THEN net_mw END),
                  MIN(CASE WHEN value_status = 'ok' THEN net_mw END),
                  COUNT(CASE WHEN value_status = 'ok' THEN 1 END),
                  ?, '', 'live'
             FROM fact_rt_unit_10min
            WHERE data_time >= ? AND data_time < ?
            GROUP BY unit_id""",
        (day, config.schedule.slot_minutes, config.slots_per_day, start, end),
    )
    notes: dict[int, list[str]] = {}
    for unit_id, note, _first_seen in connection.execute(
        """SELECT unit_id, note, MIN(data_time) AS first_seen
             FROM fact_rt_unit_10min
            WHERE data_time >= ? AND data_time < ? AND note <> ''
            GROUP BY unit_id, note
            ORDER BY unit_id, first_seen, note""",
        (start, end),
    ):
        notes.setdefault(unit_id, []).append(note)
    connection.executemany(
        "UPDATE fact_rt_unit_daily SET notes_seen = ? WHERE date = ? AND unit_id = ?",
        [("|".join(values), day, unit_id) for unit_id, values in notes.items()],
    )
    reasons = Counter(
        classify_missing(connection, day_slots(day, config.schedule.slot_minutes)).values()
    )
    snapshots = connection.execute(
        "SELECT COUNT(*) FROM fact_rt_snapshot WHERE data_time >= ? AND data_time < ?",
        (start, end),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO fact_rt_day
               (date, snapshots, expected, missed_collector_down, missed_fetch_failed,
                missed_rejected, rows_at_rollup, rolled_up_at, purged_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(date) DO UPDATE SET
               snapshots = excluded.snapshots,
               expected = excluded.expected,
               missed_collector_down = excluded.missed_collector_down,
               missed_fetch_failed = excluded.missed_fetch_failed,
               missed_rejected = excluded.missed_rejected,
               rows_at_rollup = excluded.rows_at_rollup,
               rolled_up_at = excluded.rolled_up_at,
               purged_at = NULL""",
        (
            day,
            snapshots,
            config.slots_per_day,
            reasons["collector_down"],
            reasons["fetch_failed"],
            reasons["rejected"],
            _row_count(connection, day),
            utc_iso(now),
        ),
    )


def purge(connection: sqlite3.Connection, *, cutoff: str, now: datetime) -> list[str]:
    """刪掉 cutoff（'YYYY-MM-DD'）之前、已彙總且彙總後沒有新列的日子的明細。呼叫端負責交易。"""
    connection.execute("DROP TABLE IF EXISTS temp.purgeable")
    connection.execute(
        """CREATE TEMP TABLE purgeable AS
           SELECT d.date
             FROM fact_rt_day AS d
            WHERE d.date < ?
              AND d.purged_at IS NULL
              AND d.rows_at_rollup = (SELECT COUNT(*) FROM fact_rt_unit_10min AS f
                                       WHERE f.data_time >= d.date || ' 00:00'
                                         AND f.data_time < d.date || ' 24:00')""",
        (cutoff,),
    )
    days = [row[0] for row in connection.execute("SELECT date FROM temp.purgeable ORDER BY date")]
    connection.execute(
        "DELETE FROM fact_rt_unit_10min "
        "WHERE substr(data_time, 1, 10) IN (SELECT date FROM temp.purgeable)"
    )
    connection.execute(
        "DELETE FROM fact_rt_type_subtotal "
        "WHERE substr(data_time, 1, 10) IN (SELECT date FROM temp.purgeable)"
    )
    connection.execute(
        "UPDATE fact_rt_day SET purged_at = ? WHERE date IN (SELECT date FROM temp.purgeable)",
        (utc_iso(now),),
    )
    connection.execute("DROP TABLE temp.purgeable")
    return days


def run_maintenance(
    connection: sqlite3.Connection, now: datetime, config: RealtimeConfig
) -> MaintenanceReport:
    """每一天各自一個交易地補彙總，最後清除；某天失敗不影響其他天，也不會被清除。"""
    rolled: list[str] = []
    for day in days_needing_rollup(connection, now, config):
        with transaction(connection):
            rollup_day(connection, day, now, config)
        rolled.append(day)
    with transaction(connection):
        purged = purge(connection, cutoff=purge_cutoff(now, config), now=now)
    return MaintenanceReport(tuple(rolled), tuple(purged))
