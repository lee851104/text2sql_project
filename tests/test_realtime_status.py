from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from realtime_support import make_config, payload_bytes, write_project

from ingest.realtime.__main__ import main, stop
from ingest.realtime.collector import Collector
from ingest.realtime.lock import SingleInstanceLock
from ingest.realtime.status import read_status

START = datetime(2026, 9, 18, 13, 45, 20, tzinfo=UTC)  # 21:45:20 Taipei


class _Response(io.BytesIO):
    status = 200
    headers = {"ETag": '"a:0"'}


def _collect_once(config) -> None:
    Collector(
        config,
        clock=lambda: START,
        sleep=lambda _seconds: None,
        opener=lambda request, timeout: _Response(payload_bytes()),
        monotonic=lambda: 0.0,
    ).run_once()


def test_missing_database_is_unavailable(tmp_path: Path) -> None:
    report = read_status(config=make_config(tmp_path), now=START)

    assert report["state"] == "unavailable"
    assert report["collector_running"] is False


def test_status_after_one_collection(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _collect_once(config)

    report = read_status(config=config, now=START + timedelta(minutes=2))

    assert report["state"] == "stopped"  # 收集器沒在跑
    assert report["latest_data_time"] == "2026-09-18 21:40"
    assert report["lag_minutes"] == 7.3
    assert report["today"] == {"elapsed_slots": 131, "snapshots": 1}
    assert report["gaps_24h"] == {"collector_down": 143, "fetch_failed": 0, "rejected": 0}
    assert report["undecided_units"] == 204
    assert report["latest_quality"] == "ok"


def test_running_collector_is_healthy_until_data_goes_stale(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _collect_once(config)

    with SingleInstanceLock(config.lock_path):
        fresh = read_status(config=config, now=START + timedelta(minutes=2))
        stale = read_status(config=config, now=START + timedelta(minutes=45))

    assert fresh["state"] == "healthy"
    assert stale["state"] == "stale"


def test_cli_status_exit_code_and_json(tmp_path: Path) -> None:
    root = write_project(tmp_path)
    output = io.StringIO()

    with redirect_stdout(output):
        code = main(["status", "--json"], root=root)

    assert code == 2
    assert json.loads(output.getvalue())["state"] == "unavailable"


def test_stop_when_not_running(tmp_path: Path) -> None:
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(["stop"], root=write_project(tmp_path))

    assert code == 0
    assert "沒有在執行" in output.getvalue()


def test_stop_waits_for_the_lock_to_be_released(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    lock = SingleInstanceLock(config.lock_path)
    lock.acquire()

    code = stop(config, wait_seconds=3, sleep=lambda _seconds: lock.release())

    assert code == 0
    assert config.stop_path.exists()  # 真正的收集器會在停止時刪掉它


def test_stop_times_out_while_the_collector_keeps_running(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SingleInstanceLock(config.lock_path):
        code = stop(config, wait_seconds=2, sleep=lambda _seconds: None)

    assert code == 1


def test_cli_main_handles_collector_exception_cleanly(tmp_path: Path, monkeypatch) -> None:
    root = write_project(tmp_path)
    output = io.StringIO()

    def raise_error(*args, **kwargs):
        raise RuntimeError("Test error during collection")

    from ingest.realtime import collector

    monkeypatch.setattr(collector.Collector, "run_once", raise_error)

    with redirect_stdout(output):
        code = main(["once"], root=root)

    assert code == 1


def test_status_with_malformed_plants_csv(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _collect_once(config)

    # Create a malformed plants.csv without plant_id column
    config.plants_csv.write_text("plant_name\nPlant A\nPlant B\n", encoding="utf-8")

    report = read_status(config=config, now=START + timedelta(minutes=2))

    assert isinstance(report, dict)
    assert report.get("decisions_error") is not None
    assert report["state"] in ("stopped", "stale", "healthy")
