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
# 日期分隔符。教材列的六種髒寫法裡就有 `114/05` —— 同一天的各種寫法必須落在同一個值。
# 實測本專案原本只認「20xx 開頭配 - 或 /」，於是 `115/7/20`、`2026.7.20`、`2026-07`
# 全部掉到最後一條「只認得年份」，變成**靜默查整年**：問一天拿到一年，畫面上沒有異狀。
# 全形／與．一併收，因為中文輸入法很容易打出來（數字本身已由 FULLWIDTH_DIGITS 轉半形）。
DATE_SEPARATOR = r"[-/.／．]"
# 年份：西元四碼或民國三碼，由 _calendar_year() 統一換算。
DATE_YEAR = r"20\d{2}|1\d{2}"
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


def _relative_year(reference_year: int, word: str) -> int:
    """今年／去年／明年 換算成實際年份。

    三個判斷點（年月日、年月、只有年）共用這一份，避免補一邊漏一邊 —— 下面那段
    「同一件事的判斷散在兩個地方」的註解講的就是這種半殘狀態。
    """

    return reference_year + {"明年": 1, "去年": -1}.get(word, 0)


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

    separated = re.search(
        rf"(?<!\d)({DATE_YEAR})\s*{DATE_SEPARATOR}\s*(\d{{1,2}})"
        rf"\s*{DATE_SEPARATOR}\s*(\d{{1,2}})(?!\d)",
        question,
    )
    # 空白分隔。原本只開放西元四碼，理由是「民國三碼配空白與一般數字難分辨」—— 那是
    # 推測。實測四份題庫 185 題沒有任何誤判，而不收的代價很具體：「115 7 20」會被下面
    # 「只認得年份」那條抓成民國 115 年整年，又是一次靜默擴大。
    spaced = re.search(rf"(?<!\d)({DATE_YEAR})\s+(\d{{1,2}})\s+(\d{{1,2}})(?!\d)", question)
    compact = re.search(r"(?<!\d)(20\d{2}|1\d{2})(\d{2})(\d{2})(?!\d)", question)
    if match := (separated or spaced or compact):
        year, month, day = match.groups()
        return _day_range(_calendar_year(year), int(month), int(day))

    absolute_day = re.search(
        rf"(\d{{3,4}})\s*年\s*({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*日", question
    )
    if absolute_day:
        year, month, day = absolute_day.groups()
        return _day_range(_calendar_year(year), _number(month), _number(day))

    relative_day = re.search(
        rf"(今年|去年|明年)\s*({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*日", question
    )
    if relative_day:
        relation, month, day = relative_day.groups()
        year = _relative_year(reference.year, relation)
        return _day_range(year, _number(month), _number(day))

    month_day = re.search(rf"({NUMBER_TOKEN})\s*月\s*({NUMBER_TOKEN})\s*日", question)
    if month_day:
        return _day_range(reference.year, *(_number(part) for part in month_day.groups()))

    absolute_month = re.search(rf"(\d{{3,4}})\s*年\s*({NUMBER_TOKEN})\s*月", question)
    if absolute_month:
        year, month = absolute_month.groups()
        return _month_range(_calendar_year(year), _number(month))

    relative_month = re.search(rf"(今年|去年|明年)\s*({NUMBER_TOKEN})\s*月", question)
    if relative_month:
        relation, month = relative_month.groups()
        return _month_range(_relative_year(reference.year, relation), _number(month))

    if "上個月" in question:
        year, month = reference.year, reference.month - 1
        if month == 0:
            year, month = year - 1, 12
        return _month_range(year, month)

    if "下個月" in question:
        year, month = reference.year, reference.month + 1
        if month == 13:
            year, month = year + 1, 1
        return _month_range(year, month)

    # 「未來兩年」是從今天起算的滾動區間，不是某個日曆年 —— 大修排程幾乎只用這個講法。
    if "未來兩年" in question or "未來2年" in question:
        try:
            end = reference.replace(year=reference.year + 2)
        except ValueError:  # 2/29 起算
            end = reference.replace(year=reference.year + 2, day=28)
        return DateRange(reference.isoformat(), end.isoformat()), None

    # 純數字的年月：`2026-07`、`115/07`、`202607`、`11507`。少了這段，它們會掉到下面
    # 「只認得年份」那條，靜默擴大成整年。
    numeric_month = re.search(
        rf"(?<!\d)({DATE_YEAR})\s*{DATE_SEPARATOR}\s*(\d{{1,2}})(?!\d)", question
    )
    if numeric_month:
        year, month = numeric_month.groups()
        return _month_range(_calendar_year(year), int(month))
    compact_month = re.search(r"(?<!\d)(20\d{2}|1\d{2})(\d{2})(?!\d)", question)
    if compact_month:
        year, month = compact_month.groups()
        return _month_range(_calendar_year(year), int(month))

    explicit_year = re.search(r"(?<!\d)(20\d{2}|1\d{2})\s*年?", question)
    relation = re.search(r"(今年|去年|明年)", question)
    if explicit_year:
        year = _calendar_year(explicit_year.group(1))
    elif relation:
        year = _relative_year(reference.year, relation.group(1))
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


# 看起來在指定日期、卻解析不出來的片段。這件事必須與「根本沒提日期」分開：
# 後者退回完整資料範圍是對的（「台中#1最高出力」問的就是全部期間），前者退回完整範圍
# 就成了**靜默擴大** —— 實測「2026.7.20台中#1最高出力」會查 2026-01-01～2026-07-31
# 並且照樣回答成功，使用者問一天卻拿到一整年，畫面上沒有任何異狀。
# 由長到短：訊息要指到真正看不懂的那一段。「2026年13月40日」回報成「看不懂 2026年」
# 只會讓人去改對的那一半。
DATE_LIKE_PATTERNS: tuple[str, ...] = (
    r"\d{1,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?",
    r"\d{1,4}\s*年\s*\d{1,2}\s*月",
    r"\d{1,2}\s*月\s*\d{1,2}\s*日?",
    rf"\d{{1,4}}\s*{DATE_SEPARATOR}\s*\d{{1,2}}\s*{DATE_SEPARATOR}\s*\d{{1,2}}",
    r"\d{1,4}\s+\d{1,2}\s+\d{1,2}",
    r"\d{1,4}\s*年",
    r"\d{1,2}\s*月",
    r"\d{1,2}\s*日(?!期)",
)


def unparsed_date(question: str, entities: Entities) -> str | None:
    """Return the date-looking fragment this question has but the parser could not read."""

    if entities.explicit_date is not None or entities.date_range is not None:
        return None
    normalized = question.translate(FULLWIDTH_DIGITS)
    for pattern in DATE_LIKE_PATTERNS:
        if match := re.search(pattern, normalized):
            return match.group(0).strip()
    return None


def extract_entities(question: str, *, reference_date: date | None = None) -> Entities:
    date_range, explicit_date = extract_date_range(question, reference_date=reference_date)
    fuel = next((formal for alias, formal in FUEL_ALIASES.items() if alias in question), None)
    return Entities(date_range, explicit_date, extract_top_n(question), fuel)
