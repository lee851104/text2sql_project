"""Decide when to fetch next — pure functions, no clock and no I/O (§4.1).

每個時段抓到就停：失敗時的重試次數與每分鐘輪詢一樣多，正常時請求數少 5–10 倍。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ingest.realtime.config import ScheduleConfig
from ingest.realtime.timeutil import floor_slot, format_slot, parse_slot

ADOPTED = frozenset({"new", "revised"})
QUIET = frozenset({"not_modified", "stale"})


@dataclass(frozen=True)
class SchedulerState:
    last_adopted: str | None = None  # 已採用的最新時段 'YYYY-MM-DD HH:MM'
    consecutive_failures: int = 0
    not_before: datetime | None = None  # 下一次請求最早的時間（重試、退避或 Retry-After）


@dataclass(frozen=True)
class Action:
    kind: str  # 'fetch' 或 'wait'
    at: datetime
    target_slot: str


def target_slot(now: datetime, config: ScheduleConfig) -> datetime:
    """最新一個「應該已經發布」的時段：now 往前推 first_probe 再往下取整。"""
    return floor_slot(now - timedelta(seconds=config.first_probe_seconds), config.slot_minutes)


def plan_next(now: datetime, state: SchedulerState, config: ScheduleConfig) -> Action:
    target = target_slot(now, config)
    if state.last_adopted is not None and parse_slot(state.last_adopted) >= target:
        upcoming = target + timedelta(minutes=config.slot_minutes)
        due = upcoming + timedelta(seconds=config.first_probe_seconds)
        return Action("wait", due, format_slot(upcoming))
    if state.not_before is not None and state.not_before > now:
        return Action("wait", state.not_before, format_slot(target))
    return Action("fetch", now, format_slot(target))


def after_attempt(
    state: SchedulerState,
    *,
    now: datetime,
    outcome: str,
    data_time: str | None,
    retry_after: float | None,
    config: ScheduleConfig,
) -> SchedulerState:
    if outcome in ADOPTED:
        latest = state.last_adopted
        if data_time is not None and (latest is None or data_time > latest):
            latest = data_time
        return SchedulerState(latest, 0, None)
    if outcome in QUIET:
        return SchedulerState(state.last_adopted, 0, now + timedelta(seconds=config.retry_seconds))
    failures = state.consecutive_failures + 1
    delay = float(config.retry_seconds)
    for threshold, seconds in zip(
        config.backoff_after_failures, config.backoff_seconds, strict=True
    ):
        if failures >= threshold:
            delay = float(seconds)
    if retry_after is not None:
        delay = max(delay, min(retry_after, float(config.retry_after_cap_seconds)))
    return SchedulerState(state.last_adopted, failures, now + timedelta(seconds=delay))
