from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from realtime_support import make_config

from ingest.realtime.schedule import SchedulerState, after_attempt, plan_next, target_slot
from ingest.realtime.timeutil import TAIPEI, format_slot


def _schedule(tmp_path: Path):
    return make_config(tmp_path).schedule


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-24T{clock}").replace(tzinfo=TAIPEI)


def test_target_is_the_latest_slot_that_should_be_published(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)

    assert format_slot(target_slot(_at("08:55:19"), schedule)) == "2026-09-24 08:40"
    assert format_slot(target_slot(_at("08:55:20"), schedule)) == "2026-09-24 08:50"


def test_without_data_fetch_now(tmp_path: Path) -> None:
    action = plan_next(_at("08:57:00"), SchedulerState(), _schedule(tmp_path))

    assert (action.kind, action.target_slot) == ("fetch", "2026-09-24 08:50")


def test_after_adopting_the_target_wait_for_the_next_publication(tmp_path: Path) -> None:
    state = SchedulerState(last_adopted="2026-09-24 08:50")

    action = plan_next(_at("08:57:00"), state, _schedule(tmp_path))

    assert action.kind == "wait"
    assert action.at == _at("09:05:20")
    assert action.target_slot == "2026-09-24 09:00"


def test_quiet_answers_retry_in_a_minute(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    now = _at("08:55:20")

    state = after_attempt(
        SchedulerState(),
        now=now,
        outcome="not_modified",
        data_time=None,
        retry_after=None,
        config=schedule,
    )

    assert state.not_before == now + timedelta(seconds=60)
    assert plan_next(now + timedelta(seconds=30), state, schedule).kind == "wait"
    assert plan_next(now + timedelta(seconds=60), state, schedule).kind == "fetch"


def test_failures_back_off_after_10_and_20_attempts(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    now = _at("08:55:20")
    state = SchedulerState()
    delays = []
    for _ in range(21):
        state = after_attempt(
            state, now=now, outcome="error", data_time=None, retry_after=None, config=schedule
        )
        delays.append((state.not_before - now).total_seconds())

    assert delays[8] == 60  # 第 9 次
    assert delays[9] == 120  # 第 10 次
    assert delays[19] == 300  # 第 20 次
    assert state.consecutive_failures == 21


def test_retry_after_is_honoured_up_to_the_cap(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    now = _at("08:55:20")

    polite = after_attempt(
        SchedulerState(), now=now, outcome="error", data_time=None, retry_after=900, config=schedule
    )
    capped = after_attempt(
        SchedulerState(),
        now=now,
        outcome="error",
        data_time=None,
        retry_after=9999,
        config=schedule,
    )

    assert polite.not_before == now + timedelta(seconds=900)
    assert capped.not_before == now + timedelta(seconds=1800)


def test_success_resets_failures_and_a_late_slot_keeps_the_chase_going(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    failing = SchedulerState(consecutive_failures=12, not_before=_at("09:00:00"))
    now = _at("09:06:00")  # target 已是 09:00，但台電晚發布，拿到的是 08:50

    state = after_attempt(
        failing,
        now=now,
        outcome="new",
        data_time="2026-09-24 08:50",
        retry_after=None,
        config=schedule,
    )

    assert state == SchedulerState("2026-09-24 08:50", 0, None)
    assert plan_next(now, state, schedule).kind == "fetch"


def test_an_older_revision_never_moves_the_latest_slot_back(tmp_path: Path) -> None:
    state = after_attempt(
        SchedulerState(last_adopted="2026-09-24 09:00"),
        now=datetime(2026, 9, 24, 1, 6, tzinfo=UTC),
        outcome="revised",
        data_time="2026-09-24 08:50",
        retry_after=None,
        config=_schedule(tmp_path),
    )

    assert state.last_adopted == "2026-09-24 09:00"
