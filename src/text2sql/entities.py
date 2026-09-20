"""Conservative Chinese entity extraction for routing and prompting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DateRange:
    start: str
    end: str


@dataclass(frozen=True)
class Entities:
    date_range: DateRange | None
    explicit_date: str | None
    top_n: int | None
    fuel: str | None


FUEL_ALIASES = {
    "天然氣": "天然氣",
    "輕柴油": "輕柴油",
    "重油": "重油",
    "燃煤": "煤",
    "煤": "煤",
    "燃氣": "天然氣",
    "氣": "天然氣",
    "水力": "水",
    "水": "水",
}
CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
NUMBER_TOKEN = r"[0-9零〇一二兩三四五六七八九十廿]+"


def _number(value: str) -> int:
    value = value.translate(FULLWIDTH_DIGITS)
    if value.isdigit():
        return int(value)
    if value.startswith("廿"):
        return 20 + (_number(value[1:]) if len(value) > 1 else 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return (CHINESE_DIGITS.get(tens, 1) * 10) + CHINESE_DIGITS.get(ones, 0)
    digits = [CHINESE_DIGITS[character] for character in value]
    return int("".join(str(digit) for digit in digits))


def _calendar_year(value: str) -> int:
    year = _number(value)
    return year + 1911 if year < 1911 else year


def _last_day(year: int, month: int) -> int:
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (following - date.resolution).day


def _day_range(year: int, month: int, day: int) -> tuple[DateRange | None, str | None]:
    try:
        value = date(year, month, day).isoformat()
    except ValueError:
        return None, None
    return DateRange(value, value), value


def _month_range(year: int, month: int) -> tuple[DateRange | None, str | None]:
    try:
        start = date(year, month, 1).isoformat()
        end = date(year, month, _last_day(year, month)).isoformat()
    except ValueError:
        return None, None
    return DateRange(start, end), None


def extract_date_range(
    question: str, *, reference_date: date | None = None
) -> tuple[DateRange | None, str | None]:
    """Extract one explicit day or a conservative calendar range.

    Relative dates accept an injected reference date so offline tests and replayed traces
    stay deterministic.
    """

    question = question.translate(FULLWIDTH_DIGITS)
    reference = reference_date or date.today()

    iso = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", question)
    compact = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", question)
    if match := (iso or compact):
        return _day_range(*(int(part) for part in match.groups()))

    absolute_day = re.search(
        rf"(\d{{3,4}})\s*年\s*({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*日", question
    )
    if absolute_day:
        year, month, day = absolute_day.groups()
        return _day_range(_calendar_year(year), _number(month), _number(day))

    relative_day = re.search(
        rf"(今年|去年)\s*({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*日", question
    )
    if relative_day:
        relation, month, day = relative_day.groups()
        year = reference.year - (relation == "去年")
        return _day_range(year, _number(month), _number(day))

    month_day = re.search(rf"({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*日", question)
    if month_day:
        return _day_range(reference.year, *(_number(part) for part in month_day.groups()))

    absolute_month = re.search(rf"(\d{{3,4}})\s*年\s*({NUMBER_TOKEN})\s*月", question)
    if absolute_month:
        year, month = absolute_month.groups()
        return _month_range(_calendar_year(year), _number(month))

    relative_month = re.search(rf"(今年|去年)\s*({NUMBER_TOKEN})\s*月", question)
    if relative_month:
        relation, month = relative_month.groups()
        return _month_range(reference.year - (relation == "去年"), _number(month))

    if "上個月" in question:
        year, month = reference.year, reference.month - 1
        if month == 0:
            year, month = year - 1, 12
        return _month_range(year, month)

    explicit_year = re.search(r"(?<!\d)(20\d{2}|1\d{2})\s*年?", question)
    relation = re.search(r"(今年|去年)", question)
    if explicit_year:
        year = _calendar_year(explicit_year.group(1))
    elif relation:
        year = reference.year - (relation.group(1) == "去年")
    else:
        year = None
    if year is not None:
        if "上半年" in question:
            return DateRange(f"{year}-01-01", f"{year}-06-30"), None
        if "下半年" in question:
            return DateRange(f"{year}-07-01", f"{year}-12-31"), None
        return DateRange(f"{year}-01-01", f"{year}-12-31"), None
    return None, None


# 同一句話的不同寫法。實測「資料庫有甚麼內容」完全答不出來，而「資料庫有什麼內容」
# 會正確澄清 —— 差別只在一個異體字。使用者自己就交替用這兩種寫法，比對規則沒有理由
# 因此分岔。這裡只收**只差字形、語意完全相同**的字，不碰「那些／哪些」這種會改變意思的。
QUESTION_VARIANTS = {
    "甚麼": "什麼",
    "什么": "什麼",
    "甚么": "什麼",
    "爲": "為",
    "裏": "裡",
}


def compact_question(question: str) -> str:
    """去掉空白並統一異體字，供各種比對規則使用。"""

    compact = re.sub(r"\s+", "", question)
    for variant, standard in QUESTION_VARIANTS.items():
        compact = compact.replace(variant, standard)
    return compact


def extract_top_n(question: str) -> int | None:
    question = question.translate(FULLWIDTH_DIGITS)
    match = re.search(rf"前\s*({NUMBER_TOKEN})", question)
    if not match:
        match = re.search(
            rf"(?:最大|最小|最高|最低)(?:的)?\s*({NUMBER_TOKEN})(?:名|欄|個|台)",
            question,
        )
    return _number(match.group(1)) if match else None


def extract_entities(question: str, *, reference_date: date | None = None) -> Entities:
    date_range, explicit_date = extract_date_range(question, reference_date=reference_date)
    fuel = next((formal for alias, formal in FUEL_ALIASES.items() if alias in question), None)
    return Entities(date_range, explicit_date, extract_top_n(question), fuel)
