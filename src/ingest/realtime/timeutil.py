"""Time helpers shared by the realtime collector.

台灣自 1979 年起沒有夏令時間，一律用固定的 UTC+8。不用 zoneinfo：部署機的 Windows Python
沒有 IANA 時區資料庫，`ZoneInfo("Asia/Taipei")` 會直接拋出 ZoneInfoNotFoundError。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
SLOT_FORMAT = "%Y-%m-%d %H:%M"


def to_taipei(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("時間必須帶時區，否則分不出是當地時間還是 UTC。")
    return moment.astimezone(TAIPEI)


def floor_slot(moment: datetime, minutes: int) -> datetime:
    """往下取整到時段開頭（UTC+8）：08:47 → 08:40。"""
    local = to_taipei(moment)
    return local.replace(minute=local.minute - local.minute % minutes, second=0, microsecond=0)


def format_slot(slot: datetime) -> str:
    return to_taipei(slot).strftime(SLOT_FORMAT)


def parse_slot(text: str) -> datetime:
    return datetime.strptime(text, SLOT_FORMAT).replace(tzinfo=TAIPEI)


def local_date(moment: datetime) -> date:
    return to_taipei(moment).date()


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_utc_iso(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)
