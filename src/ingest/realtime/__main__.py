"""python -m ingest.realtime {run,once,rebuild,status,stop}（§4.5）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ingest.realtime.collector import EXIT_FAILED, EXIT_OK, Collector
from ingest.realtime.config import RealtimeConfig, RealtimeConfigError, load_config
from ingest.realtime.lock import is_locked, read_holder
from ingest.realtime.status import read_status
from ingest.realtime.timeutil import TAIPEI

STATUS_EXIT = {"healthy": 0, "stale": 1, "stopped": 2, "unavailable": 2}
STOP_WAIT_SECONDS = 60


def _configure_logging(config: RealtimeConfig, *, to_file: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if to_file:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(TAIPEI).strftime("%Y%m%d-%H%M%S")
        log_path = config.log_dir / f"realtime-collector-{stamp}.log"
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _format_status(report: dict[str, object]) -> str:
    lines = [
        f"狀態：{report['state']}",
        f"收集器：{'執行中' if report['collector_running'] else '未執行'}",
    ]
    if not report.get("available"):
        lines.append("realtime.db 不存在或無法開啟")
        return "\n".join(lines)
    today = report["today"]
    assert isinstance(today, dict)
    lines += [
        f"最新時段：{report['latest_data_time']}（落後 {report['lag_minutes']} 分鐘）",
        f"連續失敗：{report['consecutive_failures']} 次",
        f"今天：已過 {today['elapsed_slots']} 個時段，拿到 {today['snapshots']} 個",
        f"最近 24 小時缺口：{report['gaps_24h']}",
        f"未定機組：{report['undecided_units']}；過期決定：{report['stale_decisions']}",
        f"最新快照品質：{report['latest_quality']}",
    ]
    if report.get("decisions_error"):
        lines.append(f"人工決定檔錯誤：{report['decisions_error']}")
    return "\n".join(lines)


def stop(
    config: RealtimeConfig,
    *,
    wait_seconds: int = STOP_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if not is_locked(config.lock_path):
        print("收集器沒有在執行。")
        return EXIT_OK
    config.stop_path.parent.mkdir(parents=True, exist_ok=True)
    config.stop_path.touch()
    for _ in range(wait_seconds):
        if not is_locked(config.lock_path):
            print("收集器已停止。")
            return EXIT_OK
        sleep(1)
    holder = read_holder(config.lock_path) or {}
    print(
        f"收集器 {wait_seconds} 秒內沒有停止（PID {holder.get('pid', '未知')}），"
        "請到它的視窗按 Ctrl+C。",
        file=sys.stderr,
    )
    return EXIT_FAILED


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.realtime", description="即時機組發電量收集器（RT-1）"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="常駐收集")
    commands.add_parser("once", help="立刻抓一次、做一次維護就結束")
    commands.add_parser("rebuild", help="從封存重建 realtime.db（收集器必須先停）")
    status_parser = commands.add_parser("status", help="健康狀態")
    status_parser.add_argument("--json", action="store_true", help="輸出 JSON")
    commands.add_parser("stop", help="要求收集器停止，最多等 60 秒")
    args = parser.parse_args(argv)
    try:
        config = load_config(root) if root is not None else load_config()
    except (OSError, RealtimeConfigError) as error:
        print(f"設定錯誤：{error}", file=sys.stderr)
        return EXIT_FAILED
    if args.command == "status":
        report = read_status(config=config)
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else _format_status(report)
        )
        return STATUS_EXIT[str(report["state"])]
    if args.command == "stop":
        return stop(config)
    _configure_logging(config, to_file=args.command == "run")
    collector = Collector(config)
    if args.command == "run":
        return collector.run()
    if args.command == "once":
        return collector.run_once()
    return collector.rebuild_only()


if __name__ == "__main__":
    raise SystemExit(main())
