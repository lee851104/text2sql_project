"""Rebuild realtime.db in place from the archive and the attempt log (§7.3).

在原檔內以單一交易完成、不換檔：Windows 上有人開著檔案時 os.replace 會失敗，舊檔留下的 -wal
套用到新檔上還會損壞資料庫。服務端在重建期間透過 WAL 繼續讀到舊資料。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ingest.realtime.archive import (
    UNPARSED_DIR,
    ArchivedFile,
    iter_archive,
    iter_attempts,
    payload_sha256,
    read_payload,
)
from ingest.realtime.config import RealtimeConfig
from ingest.realtime.decisions import Decisions
from ingest.realtime.maintenance import (
    closed_through,
    days_needing_rollup,
    purge,
    purge_cutoff,
    rollup_day,
)
from ingest.realtime.parse import PayloadRejected, parse_payload
from ingest.realtime.store import (
    create_schema,
    drop_schema,
    ingest_snapshot,
    record_attempt,
    sync_plants,
    transaction,
    write_manifest,
)
from ingest.realtime.timeutil import utc_iso


@dataclass(frozen=True)
class RebuildReport:
    archive_files: int
    ingested: int
    rejected: int
    corrupted: int
    unparsed: int


def fetch_times(config: RealtimeConfig) -> dict[str, str]:
    """SHA-256 前 12 碼 → 第一次抓到的時間（UTC ISO），取自抓取紀錄。"""
    times: dict[str, str] = {}
    for record in iter_attempts(config.attempts_dir):
        sha, attempted_at = record.get("sha256"), record.get("attempted_at")
        if isinstance(sha, str) and isinstance(attempted_at, str):
            times.setdefault(sha[:12], attempted_at)
    return times


def archived_fetch_time(archived: ArchivedFile, times: dict[str, str]) -> str:
    """抓取時間取自抓取紀錄；紀錄缺漏時退回封存檔的修改時間。"""
    known = times.get(archived.sha_prefix)
    if known is not None:
        return known
    return utc_iso(datetime.fromtimestamp(archived.path.stat().st_mtime, UTC))


def _close_day(
    connection: sqlite3.Connection, day: str, now: datetime, config: RealtimeConfig
) -> None:
    if date.fromisoformat(day) <= closed_through(now, config):
        rollup_day(connection, day, now, config)
        purge(connection, cutoff=purge_cutoff(now, config), now=now)


def rebuild(
    connection: sqlite3.Connection, config: RealtimeConfig, decisions: Decisions, *, now: datetime
) -> RebuildReport:
    times = fetch_times(config)
    files = sorted(
        iter_archive(config.archive_dir),
        key=lambda archived: (archived.data_time or "", archived_fetch_time(archived, times)),
    )
    unparsed_dir = config.archive_dir / UNPARSED_DIR
    unparsed = len(list(unparsed_dir.glob("*.json.gz"))) if unparsed_dir.is_dir() else 0
    ingested = rejected = corrupted = 0
    with transaction(connection):
        drop_schema(connection)
        create_schema(connection)
        sync_plants(connection, decisions.plants)
        for record in iter_attempts(config.attempts_dir):
            record_attempt(connection, record)
        current_day: str | None = None
        for archived in files:
            day = str(archived.data_time)[:10]
            if current_day is not None and day != current_day:
                _close_day(connection, current_day, now, config)
            current_day = day
            raw = read_payload(archived.path)
            sha = payload_sha256(raw)
            if sha[:12] != archived.sha_prefix:
                corrupted += 1
                continue
            try:
                parsed = parse_payload(
                    raw,
                    now=now,
                    validation=config.validation,
                    slot_minutes=config.schedule.slot_minutes,
                )
            except PayloadRejected:
                rejected += 1
                continue
            ingest_snapshot(
                connection,
                parsed,
                sha256=sha,
                fetched_at=archived_fetch_time(archived, times),
                decisions=decisions,
            )
            ingested += 1
        if current_day is not None:
            _close_day(connection, current_day, now, config)
        for day in days_needing_rollup(connection, now, config):
            rollup_day(connection, day, now, config)
        purge(connection, cutoff=purge_cutoff(now, config), now=now)
        write_manifest(
            connection,
            build_kind="rebuild" if files else "create",
            archive_files=len(files),
            decisions=decisions,
            now=now,
        )
    return RebuildReport(len(files), ingested, rejected, corrupted, unparsed)
