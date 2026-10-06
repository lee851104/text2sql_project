from __future__ import annotations

import gzip
import io
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest
from realtime_support import PLANTS_CSV, make_config, payload_bytes

from ingest.realtime import archive, maintenance, rebuild, store
from ingest.realtime.collector import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,
    MAINTENANCE_INTERVAL,
    Collector,
)
from ingest.realtime.lock import SingleInstanceLock, is_locked
from ingest.realtime.parse import read_datetime
from ingest.realtime.timeutil import utc_iso

START = datetime(2026, 9, 18, 13, 45, 20, tzinfo=UTC)  # 21:45:20 Taipei → target 21:40
HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, etag: str):
        super().__init__(body)
        self.status = 200
        self.headers = {"ETag": etag, "Last-Modified": "Fri, 18 Sep 2026 13:45:09 GMT"}


def ok(raw: bytes, etag: str = '"a:0"'):
    return lambda request, timeout: _Response(raw, etag)


def not_modified(request, timeout):
    raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", Message(), None)


def reset(request, timeout):
    raise ConnectionResetError(10054, "reset")


class ScriptedOpener:
    """Plays one scripted response per request; `after` runs once the script is used up."""

    def __init__(self, *steps, after=None):
        self.steps = list(steps)
        self.after = after
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        step = self.steps.pop(0)
        if not self.steps and self.after is not None:
            self.after()
        return step(request, timeout)


def _collector(config, clock: FakeClock, opener) -> Collector:
    return Collector(config, clock=clock, sleep=clock.sleep, opener=opener, monotonic=lambda: 0.0)


def _attempts(config) -> list[tuple[str, str | None]]:
    return [(r["kind"], r.get("outcome")) for r in archive.iter_attempts(config.attempts_dir)]


def _db(config) -> sqlite3.Connection:
    return sqlite3.connect(config.database)


def test_run_once_archives_ingests_and_logs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)

    code = _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()

    assert code == EXIT_OK
    assert _attempts(config) == [("startup", None), ("fetch", "new"), ("shutdown", None)]
    assert _db(config).execute("SELECT data_time FROM fact_rt_snapshot").fetchall() == [
        ("2026-09-18 21:40",)
    ]
    assert len(archive.iter_archive(config.archive_dir)) == 1
    assert not is_locked(config.lock_path)


def test_second_run_sees_not_modified(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()

    code = _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    assert code == EXIT_OK
    assert _attempts(config)[-2] == ("fetch", "not_modified")


def test_payload_archived_before_a_crash_is_ingested_on_startup(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = payload_bytes()
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=START
    )

    _collector(config, FakeClock(START), ScriptedOpener(not_modified)).run_once()

    rows = _db(config).execute("SELECT data_time, sha256 FROM fact_rt_snapshot").fetchall()
    assert rows == [("2026-09-18 21:40", archive.payload_sha256(raw))]


def test_startup_skips_a_truncated_archive_file_and_ingests_the_rest(tmp_path: Path) -> None:
    """A bit-rotted/truncated .json.gz in the archive must not stop startup (Task 11 deviation)."""
    config = make_config(tmp_path)

    good_raw = payload_bytes()
    archive.archive_payload(
        config.archive_dir, good_raw, source_time=read_datetime(good_raw), fetched_at=START
    )

    truncated_raw = payload_bytes(data_time="2026-09-18T21:50:00")
    truncated_compressed = gzip.compress(truncated_raw)
    sha_prefix = archive.payload_sha256(truncated_raw)[:12]
    truncated_path = config.archive_dir / "2026" / "09" / "18" / f"2150_{sha_prefix}.json.gz"
    truncated_path.parent.mkdir(parents=True, exist_ok=True)
    truncated_path.write_bytes(truncated_compressed[: len(truncated_compressed) // 2])

    code = _collector(config, FakeClock(START), ScriptedOpener(not_modified)).run_once()

    assert code == EXIT_OK
    rows = _db(config).execute("SELECT data_time, sha256 FROM fact_rt_snapshot").fetchall()
    assert rows == [("2026-09-18 21:40", archive.payload_sha256(good_raw))]


def test_loop_retries_after_a_failure_and_stops_on_request(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    stop = lambda: config.stop_path.touch()  # noqa: E731
    opener = ScriptedOpener(reset, ok(payload_bytes()), after=stop)

    assert _collector(config, clock, opener).run() == EXIT_OK

    records = list(archive.iter_attempts(config.attempts_dir))
    fetches = [r for r in records if r["kind"] == "fetch"]
    assert [(r["outcome"], r.get("error_type")) for r in fetches] == [
        ("error", "ConnectionResetError"),
        ("new", None),
    ]
    gap = datetime.fromisoformat(fetches[1]["attempted_at"]) - datetime.fromisoformat(
        fetches[0]["attempted_at"]
    )
    assert gap == timedelta(seconds=60)
    assert records[-1]["kind"] == "shutdown"
    assert not config.stop_path.exists()


def test_waking_from_sleep_is_recorded(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    naps = iter([3 * 3600.0])

    def sleep(seconds: float) -> None:
        clock.sleep(next(naps, seconds))  # 第一次 sleep 睡了三小時（電腦睡眠）
        if clock.now > START + timedelta(hours=3, minutes=1):
            config.stop_path.touch()

    collector = Collector(
        config,
        clock=clock,
        sleep=sleep,
        opener=ScriptedOpener(ok(payload_bytes()), not_modified, not_modified, not_modified),
        monotonic=lambda: 0.0,
    )
    collector.run()

    kinds = [kind for kind, _outcome in _attempts(config)]
    assert "resume" in kinds


def test_schema_mismatch_triggers_a_rebuild_from_the_archive(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()
    with _db(config) as connection:
        connection.execute("UPDATE meta_rt_manifest SET schema_version = '0'")

    _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    connection = _db(config)
    assert connection.execute(
        "SELECT schema_version, build_kind FROM meta_rt_manifest"
    ).fetchone() == (
        store.SCHEMA_VERSION,
        "rebuild",
    )
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_snapshot").fetchone()[0] == 1


def test_changed_decisions_are_applied_during_maintenance(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    collector = _collector(config, clock, ScriptedOpener(ok(payload_bytes())))
    collector.startup()
    collector.fetch("2026-09-18 21:40")
    config.units_csv.write_text(HEADER + "燃氣,大潭CC#1,unit,plant,8,\n", encoding="utf-8")

    collector.maintain(clock())
    plant = collector.connection.execute(
        """SELECT "電廠" FROM v_rt_now WHERE "機組名稱" = '大潭CC#1'"""
    ).fetchone()[0]
    collector.shutdown()

    assert plant == "大潭發電廠"


def test_second_collector_exits_with_code_3(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SingleInstanceLock(config.lock_path):
        code = _collector(config, FakeClock(START), ScriptedOpener(not_modified)).run_once()

    assert code == EXIT_ALREADY_RUNNING


def _corrupt_units_csv(config) -> None:
    """Simulate 'saved by Excel as cp950' — invalid UTF-8, raises UnicodeDecodeError."""
    config.units_csv.write_bytes((HEADER + "燃氣,大潭CC#1,unit,plant,8,\n").encode("cp950"))


def test_startup_completes_when_the_units_csv_is_not_valid_utf8(tmp_path: Path) -> None:
    """Fix round 1 / C1(b): _load_decisions must not let a decoding error crash startup()."""
    config = make_config(tmp_path)
    _corrupt_units_csv(config)

    collector = _collector(config, FakeClock(START), ScriptedOpener(not_modified))
    collector.startup()  # must not raise
    collector.shutdown()


def test_maintenance_failure_does_not_stop_the_fetch_loop(tmp_path: Path) -> None:
    """Fix round 1 / C1(a): a maintenance error must not starve the loop of fetches forever."""
    config = make_config(tmp_path)
    clock = FakeClock(START)
    opener = ScriptedOpener(ok(payload_bytes()), *([not_modified] * 300))
    collector = Collector(
        config, clock=clock, sleep=clock.sleep, opener=opener, monotonic=lambda: 0.0
    )
    corrupted = {"done": False}
    stop_after = START + MAINTENANCE_INTERVAL + timedelta(hours=2)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if not corrupted["done"] and clock.now >= START + MAINTENANCE_INTERVAL:
            _corrupt_units_csv(config)
            corrupted["done"] = True
        if clock.now >= stop_after:
            config.stop_path.touch()

    collector.sleep = sleep

    assert collector.run() == EXIT_OK

    cutoff = MAINTENANCE_INTERVAL.total_seconds()
    fetches_after_maintenance = [
        record
        for record in archive.iter_attempts(config.attempts_dir)
        if record["kind"] == "fetch"
        and (datetime.fromisoformat(record["attempted_at"]) - START).total_seconds() > cutoff
    ]
    assert len(fetches_after_maintenance) >= 2


def test_a_failed_archive_write_is_recorded_and_does_not_remember_the_etag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fix round 1 / I1: archive_payload raising OSError must not escape fetch()."""
    config = make_config(tmp_path)
    collector = _collector(config, FakeClock(START), ScriptedOpener(ok(payload_bytes())))
    collector.startup()

    def broken_archive_payload(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(archive, "archive_payload", broken_archive_payload)

    outcome = collector.fetch("2026-09-18 21:40")
    collector.shutdown()

    assert outcome == "error"
    assert collector.etag is None
    records = [r for r in _attempts(config) if r[0] == "fetch"]
    assert records[-1] == ("fetch", "error")
    full_records = list(archive.iter_attempts(config.attempts_dir))
    fetch_record = [r for r in full_records if r["kind"] == "fetch"][-1]
    assert fetch_record["error_type"] == "ArchiveError"


def _corrupt_pages(path: Path, first_page: int, last_page: int) -> None:
    """Overwrite 4 KiB pages first_page..last_page (1-based, -1 = the last page)."""
    data = bytearray(path.read_bytes())
    pages = len(data) // 4096
    first, last = (p if p > 0 else pages + 1 + p for p in (first_page, last_page))
    data[(first - 1) * 4096 : last * 4096] = bytes([0xA5]) * ((last - first + 1) * 4096)
    path.write_bytes(bytes(data))


@pytest.mark.parametrize(
    ("pages", "opens"),
    [
        pytest.param((-1, -1), True, id="quick-check-fails"),
        pytest.param((2, -1), False, id="connect-fails"),
    ],
)
def test_a_damaged_database_is_moved_aside_and_rebuilt(tmp_path: Path, pages, opens) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()
    _corrupt_pages(config.database, *pages)
    if opens:  # 確認真的走到「檔案打得開、quick_check 不過」這條路
        probe = store.connect(config.database)
        try:
            assert store.quick_check(probe) != "ok"
        except sqlite3.DatabaseError:
            pass  # SQLite 3.45 等舊版直接拋錯，不回傳檢查結果；兩種都算檢查不過
        finally:
            probe.close()

    code = _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    assert code == EXIT_OK
    assert len(list(config.database.parent.glob("realtime.db.corrupt-*"))) == 1
    connection = _db(config)
    assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert connection.execute("SELECT data_time FROM fact_rt_snapshot").fetchall() == [
        ("2026-09-18 21:40",)
    ]


def test_rebuild_replaces_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = payload_bytes()
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=START
    )
    config.database.parent.mkdir(parents=True, exist_ok=True)
    config.database.write_bytes(b"this is not a sqlite database" * 200)

    code = _collector(config, FakeClock(START), ScriptedOpener()).rebuild_only()

    assert code == EXIT_OK
    assert len(list(config.database.parent.glob("realtime.db.corrupt-*"))) == 1
    assert _db(config).execute("SELECT COUNT(*) FROM fact_rt_snapshot").fetchone()[0] == 1


def _fetch_times(config) -> list[datetime]:
    return [
        datetime.fromisoformat(record["attempted_at"])
        for record in archive.iter_attempts(config.attempts_dir)
        if record["kind"] == "fetch"
    ]


def _run_until(config, clock: FakeClock, stop_after: datetime) -> tuple[Collector, int]:
    collector = _collector(
        config, clock, ScriptedOpener(ok(payload_bytes()), *[not_modified] * 300)
    )

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now >= stop_after:
            config.stop_path.touch()

    collector.sleep = sleep
    return collector, collector.run()


def _break_maintenance(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    calls: list[datetime] = []

    def broken(connection, now, config):
        calls.append(now)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(maintenance, "run_maintenance", broken)
    return calls


def test_a_maintenance_failure_at_startup_or_hourly_does_not_stop_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    calls = _break_maintenance(monkeypatch)

    _, code = _run_until(config, clock, START + MAINTENANCE_INTERVAL + timedelta(hours=1))

    assert code == EXIT_OK
    assert calls[0] == START and any(now >= START + MAINTENANCE_INTERVAL for now in calls)
    fetches = _fetch_times(config)
    assert fetches[0] == START
    assert any(at > START + MAINTENANCE_INTERVAL for at in fetches)


def test_startup_survives_a_failed_decisions_sync_and_reconcile_ingest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(not_modified)).run_once()  # 建好資料庫
    config.units_csv.write_text(HEADER + "燃氣,大潭CC#1,unit,plant,8,\n", encoding="utf-8")
    raw = payload_bytes()
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=START
    )

    failed: list[str] = []

    def broken(name: str):
        def fail(*args, **kwargs):
            failed.append(name)
            raise sqlite3.OperationalError("database is locked")

        return fail

    monkeypatch.setattr(store, "sync_decisions", broken("sync"))
    monkeypatch.setattr(store, "ingest_snapshot", broken("ingest"))
    collector = _collector(config, clock, ScriptedOpener(not_modified))

    collector.startup()  # must not raise
    assert collector.fetch("2026-09-18 21:40") == "not_modified"
    collector.shutdown()
    assert failed == ["sync", "ingest"]


def _break_attempt_log(monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(attempts_dir, record):
        raise PermissionError(13, "Excel has the file open")

    monkeypatch.setattr(archive, "append_attempt", locked)


def test_a_failed_attempt_log_write_still_advances_the_scheduler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    collector = _collector(config, FakeClock(START), ScriptedOpener(ok(payload_bytes())))
    collector.startup()
    _break_attempt_log(monkeypatch)

    outcome = collector.fetch("2026-09-18 21:40")

    assert outcome == "new"
    assert collector.state.last_adopted == "2026-09-18 21:40"
    mirrored = collector.connection.execute(
        "SELECT COUNT(*) FROM meta_rt_attempt WHERE kind = 'fetch'"
    ).fetchone()[0]
    collector.shutdown()
    assert mirrored == 1


def test_the_loop_keeps_running_when_the_attempt_log_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _break_attempt_log(monkeypatch)
    naps = iter([3 * 3600.0])  # 第一次 sleep 睡了三小時 → 醒來時要記 resume
    opener = ScriptedOpener(ok(payload_bytes()), *[not_modified] * 300)

    def sleep(seconds: float) -> None:
        clock.sleep(next(naps, seconds))
        if clock.now >= START + timedelta(hours=4):
            config.stop_path.touch()

    collector = Collector(config, clock=clock, sleep=sleep, opener=opener, monotonic=lambda: 0.0)

    assert collector.run() == EXIT_OK
    assert len(opener.requests) >= 3


def test_reconcile_takes_the_fetch_time_from_the_attempt_log(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(not_modified)).run_once()  # 建好空的資料庫
    raw = payload_bytes()
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=START
    )
    logged = utc_iso(START - timedelta(minutes=3))
    archive.append_attempt(
        config.attempts_dir,
        {"attempted_at": logged, "kind": "fetch", "sha256": archive.payload_sha256(raw)},
    )

    _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    rows = _db(config).execute("SELECT fetched_at FROM fact_rt_snapshot").fetchall()
    assert rows == [(logged,)]


def test_a_stop_request_left_from_an_earlier_run_is_cleared_on_start(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.stop_path.parent.mkdir(parents=True, exist_ok=True)
    config.stop_path.touch()
    stop = lambda: config.stop_path.touch()  # noqa: E731
    opener = ScriptedOpener(ok(payload_bytes()), after=stop)

    assert _collector(config, FakeClock(START), opener).run() == EXIT_OK

    assert _attempts(config) == [("startup", None), ("fetch", "new"), ("shutdown", None)]


def test_startup_completes_when_the_attempt_log_has_a_line_torn_inside_a_character(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.attempts_dir.mkdir(parents=True, exist_ok=True)
    with (config.attempts_dir / "2026-09.jsonl").open("wb") as handle:
        handle.write('{"kind":"fetch","detail":"連線失敗'.encode()[:-1])

    collector = _collector(config, FakeClock(START), ScriptedOpener(not_modified))
    collector.startup()  # must not raise
    collector.shutdown()

    assert ("startup", None) in _attempts(config)


def _healthy_database(tmp_path: Path):
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()
    return config, clock


def _corrupt_files(config) -> list[Path]:
    return list(config.database.parent.glob("realtime.db.corrupt-*"))


def test_a_locked_database_is_not_moved_aside_on_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, clock = _healthy_database(tmp_path)

    def locked(connection):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "quick_check", locked)
    with pytest.raises(sqlite3.OperationalError):
        _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    assert config.database.is_file()
    assert _corrupt_files(config) == []


def test_a_failing_rebuild_that_is_not_corruption_keeps_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, clock = _healthy_database(tmp_path)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rebuild, "rebuild", locked)
    with pytest.raises(sqlite3.OperationalError):
        _collector(config, clock, ScriptedOpener()).rebuild_only()

    assert config.database.is_file()
    assert _corrupt_files(config) == []


def test_move_aside_twice_in_one_second_keeps_both_files(tmp_path: Path) -> None:
    path = tmp_path / "realtime.db"
    for label in (b"first", b"second"):
        path.write_bytes(label)
        Collector._move_aside(path, START)

    assert sorted(p.read_bytes() for p in tmp_path.glob("realtime.db.corrupt-*")) == [
        b"first",
        b"second",
    ]


def test_move_aside_without_a_file_does_nothing(tmp_path: Path) -> None:
    Collector._move_aside(tmp_path / "realtime.db", START)

    assert list(tmp_path.iterdir()) == []


def _break_plants_roster(config) -> None:
    config.plants_csv.write_text("name\n大潭發電廠\n", encoding="utf-8")


def test_a_rebuild_with_an_unreadable_plants_roster_uses_empty_decisions(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()
    config.database.unlink()
    _break_plants_roster(config)

    collector = _collector(config, clock, ScriptedOpener(not_modified))
    code = collector.run_once()

    assert code == EXIT_OK
    connection = _db(config)
    assert connection.execute("SELECT build_kind FROM meta_rt_manifest").fetchone() == ("rebuild",)
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_snapshot").fetchone()[0] == 1

    # 名冊修好後，每小時的重新載入會把真正的決定讀回來。
    collector = _collector(config, clock, ScriptedOpener())
    collector.startup()
    assert collector.decisions is None
    config.plants_csv.write_text(PLANTS_CSV, encoding="utf-8")
    collector.maintain(clock())
    assert collector.decisions is not None and collector.decisions.plants
    collector.shutdown()


def test_rebuild_only_with_an_unreadable_plants_roster_uses_empty_decisions(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()
    config.database.unlink()
    config.plants_csv.unlink()

    code = _collector(config, clock, ScriptedOpener()).rebuild_only()

    assert code == EXIT_OK
    assert _db(config).execute("SELECT COUNT(*) FROM fact_rt_snapshot").fetchone()[0] == 1
