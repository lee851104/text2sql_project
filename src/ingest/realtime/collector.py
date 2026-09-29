"""The collector process: startup sequence, fetch loop and hourly maintenance (§4).

每一輪都用 try 包起來，例外只記錄、不中止迴圈。先封存再入庫：入庫前當掉的回應，
下次啟動時由 `_reconcile_archive` 補進去。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ingest.realtime import archive, client, maintenance, parse, rebuild, store
from ingest.realtime.config import RealtimeConfig
from ingest.realtime.decisions import (
    LOAD_ERRORS,
    Decisions,
    empty_decisions,
    file_sha256,
    load_decisions,
)
from ingest.realtime.lock import AlreadyRunning, SingleInstanceLock
from ingest.realtime.schedule import SchedulerState, after_attempt, plan_next, target_slot
from ingest.realtime.timeutil import format_slot, parse_slot, to_taipei, utc_iso

LOGGER = logging.getLogger("ingest.realtime")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ALREADY_RUNNING = 3
MAINTENANCE_INTERVAL = timedelta(hours=1)
RECONCILE_DAYS = 2
_INGEST_OUTCOMES = {"new": "new", "revised": "revised", "duplicate": "stale"}


class Collector:
    def __init__(
        self,
        config: RealtimeConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        opener: client.Opener = client.default_opener,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.opener = opener
        self.monotonic = monotonic
        self.connection: sqlite3.Connection | None = None
        self.decisions: Decisions | None = None
        self.state = SchedulerState()
        self.etag: str | None = None
        self.last_modified: str | None = None

    # ---- 指令 ----------------------------------------------------------------

    def run(self) -> int:
        try:
            with SingleInstanceLock(self.config.lock_path):
                if self.config.stop_path.exists():
                    # 拿到鎖時還在的停止要求只可能是上一次留下的，不能讓它擋住這次啟動。
                    self.config.stop_path.unlink(missing_ok=True)
                    LOGGER.info("清除上一次留下的停止要求")
                self.startup()
                try:
                    self._loop()
                except KeyboardInterrupt:
                    LOGGER.info("收到 Ctrl+C，正在停止")
                finally:
                    self.shutdown()
        except AlreadyRunning as error:
            LOGGER.error("%s", error)
            return EXIT_ALREADY_RUNNING
        return EXIT_OK

    def run_once(self) -> int:
        outcome = "error"
        try:
            with SingleInstanceLock(self.config.lock_path):
                self.startup()
                try:
                    now = self.clock()
                    outcome = self.fetch(format_slot(target_slot(now, self.config.schedule)))
                    self.maintain(self.clock())
                finally:
                    self.shutdown()
        except AlreadyRunning as error:
            LOGGER.error("%s", error)
            return EXIT_ALREADY_RUNNING
        return EXIT_OK if outcome in {"new", "revised", "stale", "not_modified"} else EXIT_FAILED

    def rebuild_only(self) -> int:
        try:
            with SingleInstanceLock(self.config.lock_path):
                decisions = self._load_decisions() or empty_decisions(self.config.plants_csv)
                connection, report = self._rebuild(None, decisions, self.clock())
                connection.close()
        except AlreadyRunning as error:
            LOGGER.error("%s；請先停止收集器再重建。", error)
            return EXIT_ALREADY_RUNNING
        LOGGER.info(
            "重建完成：封存 %d 份、入庫 %d、拒收 %d、損毀 %d、無法判讀 %d",
            report.archive_files,
            report.ingested,
            report.rejected,
            report.corrupted,
            report.unparsed,
        )
        return EXIT_OK

    # ---- 啟動與停止 ------------------------------------------------------------

    def startup(self) -> None:
        now = self.clock()
        self.decisions = self._load_decisions()
        self.connection = self._open_database(now)
        if self.decisions is not None:
            try:
                with store.transaction(self.connection):
                    stale = store.sync_decisions(self.connection, self.decisions)
            except sqlite3.Error:
                LOGGER.exception("人工決定寫入資料庫失敗，記錄後繼續啟動")
                stale = []
            if stale:
                LOGGER.warning("人工決定檔有 %d 列從未出現在來源中", len(stale))
        self._reconcile_archive()
        try:
            self.maintain(now, reload_decisions=False)
        except Exception:
            LOGGER.exception("啟動時的維護發生例外，記錄後繼續；抓取照常開始")
        self.state = SchedulerState(last_adopted=store.latest_data_time(self.connection))
        self._record({"attempted_at": utc_iso(now), "kind": "startup"})

    def shutdown(self) -> None:
        if self.connection is not None:
            try:
                self._record({"attempted_at": utc_iso(self.clock()), "kind": "shutdown"})
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.connection.close()
                self.connection = None
        self.config.stop_path.unlink(missing_ok=True)

    def _load_decisions(self) -> Decisions | None:
        try:
            decisions = load_decisions(self.config.units_csv, self.config.plants_csv)
        except LOAD_ERRORS as error:
            LOGGER.warning("%s；沿用資料庫裡上一次成功套用的決定。", error)
            return None
        for warning in decisions.warnings:
            LOGGER.warning("%s", warning)
        return decisions

    def _open_database(self, now: datetime) -> sqlite3.Connection:
        path = self.config.database
        existed = path.is_file()
        connection: sqlite3.Connection | None = None
        try:
            connection = store.connect(path)
            if existed and store.quick_check(connection) != "ok":
                raise sqlite3.DatabaseError("quick_check 未通過")
            if existed and store.schema_version(connection) == store.SCHEMA_VERSION:
                return connection
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.close()
            self._move_aside(path, now)
            connection = None
        LOGGER.warning("realtime.db 不存在、版本不符或檢查失敗，從封存重建")
        decisions = self.decisions or empty_decisions(self.config.plants_csv)
        connection, report = self._rebuild(connection, decisions, now)
        LOGGER.info("重建完成：入庫 %d、拒收 %d", report.ingested, report.rejected)
        return connection

    def _rebuild(
        self, connection: sqlite3.Connection | None, decisions: Decisions, now: datetime
    ) -> tuple[sqlite3.Connection, rebuild.RebuildReport]:
        """從封存重建；原檔損毀到連重建都失敗時，移到旁邊、換新檔再重建一次。"""
        path = self.config.database
        try:
            if connection is None:
                connection = store.connect(path)
            return connection, rebuild.rebuild(connection, self.config, decisions, now=now)
        except BaseException as error:
            if connection is not None:
                connection.close()
            if not isinstance(error, sqlite3.DatabaseError):
                raise
            self._move_aside(path, now)
        connection = store.connect(path)
        try:
            return connection, rebuild.rebuild(connection, self.config, decisions, now=now)
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _move_aside(path: Path, now: datetime) -> None:
        """打不開的檔案移到旁邊再重建；封存還在，所以不會遺失資料。"""
        corrupt = path.with_name(f"{path.name}.corrupt-{to_taipei(now):%Y%m%d%H%M%S}")
        path.replace(corrupt)
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        LOGGER.error("realtime.db 無法開啟，已移到 %s", corrupt.name)

    def _reconcile_archive(self) -> None:
        """補進「已封存、還沒入庫」的回應；同一時段已有較新版本的就略過。"""
        connection = self.connection
        assert connection is not None
        latest = store.latest_data_time(connection)
        since = None
        if latest is not None:
            since = (parse_slot(latest) - timedelta(days=RECONCILE_DAYS - 1)).date().isoformat()
        times = rebuild.fetch_times(self.config)
        for archived in archive.iter_archive(self.config.archive_dir, since=since):
            try:
                raw = archive.read_payload(archived.path)
            except (OSError, EOFError, zlib.error):
                LOGGER.warning("封存檔損毀，略過：%s", archived.path)
                continue
            sha = archive.payload_sha256(raw)
            if sha[:12] != archived.sha_prefix:
                continue
            fetched_at = rebuild.archived_fetch_time(archived, times)
            existing = store.snapshot_fetched_at(connection, str(archived.data_time))
            if existing is not None and (existing[0] == sha or existing[1] >= fetched_at):
                continue
            try:
                parsed = parse.parse_payload(
                    raw,
                    now=self.clock(),
                    validation=self.config.validation,
                    slot_minutes=self.config.schedule.slot_minutes,
                )
            except parse.PayloadRejected:
                continue
            try:
                with store.transaction(connection):
                    store.ingest_snapshot(
                        connection,
                        parsed,
                        sha256=sha,
                        fetched_at=fetched_at,
                        decisions=self.decisions,
                    )
            except sqlite3.Error:
                LOGGER.exception("補入庫失敗，略過：%s", parsed.data_time)
                continue
            LOGGER.info("補入庫：%s", parsed.data_time)

    # ---- 主迴圈 ----------------------------------------------------------------

    def _loop(self) -> None:
        schedule = self.config.schedule
        last_wake = self.clock()
        last_maintenance = last_wake
        while not self.config.stop_path.exists():
            now = self.clock()
            woke_from, last_wake = last_wake, now
            wait = float(schedule.loop_max_sleep_seconds)
            try:
                if (now - woke_from).total_seconds() > schedule.resume_gap_seconds:
                    detail = json.dumps({"from": utc_iso(woke_from), "to": utc_iso(now)})
                    self._record({"attempted_at": utc_iso(now), "kind": "resume", "detail": detail})
                if now - last_maintenance >= MAINTENANCE_INTERVAL:
                    try:
                        self.maintain(now)
                    except Exception:
                        LOGGER.exception("每小時維護發生例外，記錄後繼續；下一輪照常抓取")
                    last_maintenance = now
                action = plan_next(now, self.state, schedule)
                if action.kind == "fetch":
                    self.fetch(action.target_slot)
                    continue
                wait = min(wait, max(0.0, (action.at - self.clock()).total_seconds()))
            except Exception:
                LOGGER.exception("這一輪發生例外，記錄後繼續")
            self.sleep(wait)

    def fetch(self, target: str) -> str:
        config = self.config
        result = client.fetch(
            config.source.url,
            etag=self.etag,
            last_modified=self.last_modified,
            timeout=config.source.timeout_seconds,
            max_bytes=config.source.max_bytes,
            opener=self.opener,
            monotonic=self.monotonic,
        )
        fetched_at = self.clock()
        record: dict[str, object] = {
            "attempted_at": utc_iso(fetched_at),
            "kind": "fetch",
            "target_slot": target,
            "http_status": result.status,
            "elapsed_ms": result.elapsed_ms,
        }
        data_time: str | None = None
        if result.status == 304:
            outcome = "not_modified"
        elif result.body is not None:
            outcome, data_time = self._handle_payload(result, fetched_at, record)
        else:
            outcome = "error"
            record["error_type"] = result.error_type or f"HTTP{result.status}"
        record["outcome"] = outcome
        self._record(record)
        self.state = after_attempt(
            self.state,
            now=fetched_at,
            outcome=outcome,
            data_time=data_time,
            retry_after=result.retry_after,
            config=config.schedule,
        )
        LOGGER.info("%s → %s %s", target, outcome, data_time or "")
        return outcome

    def _handle_payload(
        self, result: client.FetchResult, fetched_at: datetime, record: dict[str, object]
    ) -> tuple[str, str | None]:
        raw = result.body
        assert raw is not None and self.connection is not None
        sha = archive.payload_sha256(raw)
        try:
            archive.archive_payload(
                self.config.archive_dir,
                raw,
                source_time=parse.read_datetime(raw),
                fetched_at=fetched_at,
            )
        except OSError as error:
            # 封存都寫不進去就別入庫了：不記 ETag，下次會重新下載再試一次封存。
            record.update({"error_type": "ArchiveError", "detail": str(error)})
            return "error", None
        record.update({"sha256": sha, "bytes": len(raw), "etag": result.etag})
        try:
            parsed = parse.parse_payload(
                raw,
                now=fetched_at,
                validation=self.config.validation,
                slot_minutes=self.config.schedule.slot_minutes,
            )
        except parse.PayloadRejected as rejection:
            record.update(
                {
                    "reject_code": rejection.code,
                    "data_time": rejection.data_time,
                    "detail": rejection.detail,
                }
            )
            self._remember_validators(result)
            return "rejected", None
        record["data_time"] = parsed.data_time
        try:
            with store.transaction(self.connection):
                ingested = store.ingest_snapshot(
                    self.connection,
                    parsed,
                    sha256=sha,
                    fetched_at=utc_iso(fetched_at),
                    decisions=self.decisions,
                )
        except sqlite3.Error as error:
            # 不記 ETag：下一次會重新下載並再試入庫；封存已經寫好，重啟時也會補進去。
            record.update({"error_type": "IngestError", "detail": str(error)})
            return "error", None
        self._remember_validators(result)
        return _INGEST_OUTCOMES[ingested], parsed.data_time

    def _remember_validators(self, result: client.FetchResult) -> None:
        self.etag, self.last_modified = result.etag, result.last_modified

    def _record(self, record: dict[str, object]) -> None:
        """先寫 JSONL（真實來源），再寫資料庫鏡像；資料庫失敗時重建會補回。"""
        try:
            archive.append_attempt(self.config.attempts_dir, record)
        except OSError:
            # 例如 Excel 開著當月的 .jsonl：只記錄，資料庫鏡像照寫，抓取照常。
            LOGGER.exception("抓取紀錄寫入 JSONL 失敗；資料庫鏡像照常寫入")
        if self.connection is None:
            return
        try:
            with store.transaction(self.connection):
                store.record_attempt(self.connection, record)
        except sqlite3.Error:
            LOGGER.exception("抓取紀錄寫入資料庫失敗；JSONL 已保存，重建時會補回")

    # ---- 維護 ------------------------------------------------------------------

    def maintain(self, now: datetime, *, reload_decisions: bool = True) -> None:
        assert self.connection is not None
        if reload_decisions:
            self._reload_decisions_if_changed()
        report = maintenance.run_maintenance(self.connection, now, self.config)
        if report.rolled_up or report.purged:
            LOGGER.info("彙總 %s；清除 %s", list(report.rolled_up), list(report.purged))

    def _reload_decisions_if_changed(self) -> None:
        current = self.decisions
        if current is not None and (
            file_sha256(self.config.units_csv) == current.sha256
            and file_sha256(self.config.plants_csv) == current.plants_sha256
        ):
            return
        decisions = self._load_decisions()
        if decisions is None or self.connection is None:
            return
        with store.transaction(self.connection):
            store.sync_decisions(self.connection, decisions)
        self.decisions = decisions
        LOGGER.info("人工決定檔已更新並套用")
