"""Load the collector settings: behaviour from configs/realtime.yaml, paths from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import yaml

from ingest.validate import PROJECT_ROOT, load_project_config


class RealtimeConfigError(ValueError):
    """即時收集設定不完整或不合法。"""


@dataclass(frozen=True)
class SourceConfig:
    url: str
    timeout_seconds: float
    max_bytes: int


@dataclass(frozen=True)
class ScheduleConfig:
    slot_minutes: int
    first_probe_seconds: int
    retry_seconds: int
    backoff_after_failures: tuple[int, ...]
    backoff_seconds: tuple[int, ...]
    retry_after_cap_seconds: int
    loop_max_sleep_seconds: int
    resume_gap_seconds: int


@dataclass(frozen=True)
class ValidationConfig:
    future_tolerance_seconds: int
    subtotal_tolerance_mw: float
    detail_rows_expected: tuple[int, int]
    known_types: frozenset[str]
    unreliable_notes: frozenset[str]


@dataclass(frozen=True)
class RealtimeConfig:
    source: SourceConfig
    schedule: ScheduleConfig
    validation: ValidationConfig
    raw_days: int
    day_closed_after: time
    stale_after_minutes: int
    database: Path
    root: Path
    units_csv: Path
    plants_csv: Path
    log_dir: Path

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def attempts_dir(self) -> Path:
        return self.root / "attempts"

    @property
    def lock_path(self) -> Path:
        return self.root / "collector.lock"

    @property
    def stop_path(self) -> Path:
        return self.root / "stop.request"

    @property
    def slots_per_day(self) -> int:
        return 24 * 60 // self.schedule.slot_minutes


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise RealtimeConfigError(f"configs/realtime.yaml 缺少 `{name}` 區段。")
    return value


def _clock(value: Any) -> time:
    if not isinstance(value, str):
        raise RealtimeConfigError(
            'rollup.day_closed_after 必須是加引號的 "HH:MM"；'
            "沒加引號時 YAML 會把 10:15 這類時間讀成六十進位的整數（615）。"
        )
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except ValueError as error:
        raise RealtimeConfigError(f"rollup.day_closed_after 不是 HH:MM：{value!r}") from error


def _check(config: RealtimeConfig) -> None:
    schedule = config.schedule
    if schedule.slot_minutes <= 0 or 60 % schedule.slot_minutes:
        raise RealtimeConfigError("schedule.slot_minutes 必須能整除 60。")
    if len(schedule.backoff_after_failures) != len(schedule.backoff_seconds):
        raise RealtimeConfigError("backoff_after_failures 與 backoff_seconds 的長度必須相同。")
    if list(schedule.backoff_after_failures) != sorted(set(schedule.backoff_after_failures)):
        raise RealtimeConfigError("backoff_after_failures 必須嚴格遞增。")
    low, high = config.validation.detail_rows_expected
    if low > high:
        raise RealtimeConfigError("validation.detail_rows_expected 必須是 [下限, 上限]。")
    if config.raw_days < 1:
        raise RealtimeConfigError("retention.raw_days 至少要 1 天。")


def load_config(root: Path = PROJECT_ROOT) -> RealtimeConfig:
    root = root.resolve()
    with (root / "configs/realtime.yaml").open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    paths = load_project_config(root)["paths"]
    try:
        source = _section(raw, "source")
        schedule = _section(raw, "schedule")
        validation = _section(raw, "validation")
        low, high = validation["detail_rows_expected"]
        config = RealtimeConfig(
            source=SourceConfig(
                url=str(source["url"]),
                timeout_seconds=float(source["timeout_seconds"]),
                max_bytes=int(source["max_bytes"]),
            ),
            schedule=ScheduleConfig(
                slot_minutes=int(schedule["slot_minutes"]),
                first_probe_seconds=int(schedule["first_probe_seconds"]),
                retry_seconds=int(schedule["retry_seconds"]),
                backoff_after_failures=tuple(int(v) for v in schedule["backoff_after_failures"]),
                backoff_seconds=tuple(int(v) for v in schedule["backoff_seconds"]),
                retry_after_cap_seconds=int(schedule["retry_after_cap_seconds"]),
                loop_max_sleep_seconds=int(schedule["loop_max_sleep_seconds"]),
                resume_gap_seconds=int(schedule["resume_gap_seconds"]),
            ),
            validation=ValidationConfig(
                future_tolerance_seconds=int(validation["future_tolerance_seconds"]),
                subtotal_tolerance_mw=float(validation["subtotal_tolerance_mw"]),
                detail_rows_expected=(int(low), int(high)),
                known_types=frozenset(str(v) for v in validation["known_types"]),
                unreliable_notes=frozenset(str(v) for v in validation["unreliable_notes"]),
            ),
            raw_days=int(_section(raw, "retention")["raw_days"]),
            day_closed_after=_clock(_section(raw, "rollup")["day_closed_after"]),
            stale_after_minutes=int(_section(raw, "status")["stale_after_minutes"]),
            database=root / paths["realtime_database"],
            root=root / paths["realtime_root"],
            units_csv=root / paths["realtime_units_csv"],
            plants_csv=root / paths["plants_csv"],
            log_dir=root / "logs",
        )
    except KeyError as error:
        raise RealtimeConfigError(f"即時收集設定缺少 {error}。") from error
    _check(config)
    return config
