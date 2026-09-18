"""Pure cleaning and alignment for the two renewable open-data files.

Covers 資料集 17141（再生能源各場址）與 17140（自建各類再生能源發電量）。
No I/O, no database, no network — every function takes and returns plain data so
the rules stay testable and the decisions stay auditable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from align.naming import normalize_name

#: 兩份檔案的欄位名稱與部分值都是 `中文/English`，取中文側。
BILINGUAL_SEPARATOR = "/"

#: 官方在明細中混入小計列，`發電站編號` 寫成「陸域風力小計」這類字串。
SUBTOTAL_MARKER = "小計"

#: 場址主檔的發電站名稱帶「發電站」後綴，發電量檔沒有。
STATION_SUFFIX = "發電站"

#: 缺值標記；官方以單一連字號表示該月無資料。
MISSING_MARKERS = frozenset({"", "-", "－", "N/A"})

_TRAILING_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9 ,.'()&-]*$")
_COUNTY = re.compile(r"^(.{1,2}[縣市])")
_LEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)")


def chinese_part(value: str) -> str:
    """Take the Chinese side of a `中文/English` string.

    One official value（`彰工風力Changgong Wind Power`）遺漏了分隔符，因此在沒有
    分隔符時額外剝除結尾的拉丁字母串。中文側本身不含拉丁字母，所以這個剝除不會
    誤傷正常值。
    """
    head = value.split(BILINGUAL_SEPARATOR, 1)[0].strip()
    return _TRAILING_LATIN.sub("", head).strip()


def is_subtotal(station_id: str) -> bool:
    """True when the 17141 row is an aggregate, not a real site."""
    return SUBTOTAL_MARKER in station_id


def normalize_station(name: str) -> str:
    """Canonical station key shared by both files."""
    return normalize_name(chinese_part(name)).removesuffix(STATION_SUFFIX)


def parse_county(address: str) -> str | None:
    """Leading 縣/市 of an address, or None when the address is not a real one."""
    match = _COUNTY.match(chinese_part(address).strip())
    return match.group(1) if match else None


@dataclass(frozen=True)
class GenerationValue:
    """One `發電量(度)` cell with its parse outcome.

    `status` is one of:

    - ``ok``       —— 乾淨的整數，可直接加總
    - ``missing``  —— 官方標記為無資料，存 NULL 而不是 0
    - ``suspect``  —— 前綴看得出數字但尾隨無法解釋的字元，**不列入加總**，
      除非 `configs/renewable_overrides.yaml` 有附依據的人工修復
    - ``invalid``  —— 完全無法解析
    """

    raw: str
    kwh: int | None
    status: str


def parse_generation(value: str) -> GenerationValue:
    """Parse one 發電量 cell without guessing at damaged content."""
    raw = value.strip()
    if raw in MISSING_MARKERS:
        return GenerationValue(raw=raw, kwh=None, status="missing")

    candidate = raw.replace(",", "")
    if candidate.isdigit():
        return GenerationValue(raw=raw, kwh=int(candidate), status="ok")

    match = _LEADING_NUMBER.match(candidate)
    if match is None:
        return GenerationValue(raw=raw, kwh=None, status="invalid")

    number = float(match.group(1))
    if number.is_integer() and candidate == match.group(1):
        return GenerationValue(raw=raw, kwh=int(number), status="ok")
    return GenerationValue(raw=raw, kwh=None, status="suspect")


def repair_generation(value: GenerationValue, repairs: Mapping[str, int]) -> GenerationValue:
    """Apply a human-confirmed repair to a suspect cell, keyed by its raw string."""
    if value.status != "suspect" or value.raw not in repairs:
        return value
    return GenerationValue(raw=value.raw, kwh=repairs[value.raw], status="repaired")


@dataclass(frozen=True)
class StationLink:
    """One station name seen in either file and how it aligned."""

    key: str
    site_name: str | None
    generation_name: str | None
    status: str


def align_stations(
    site_names: Iterable[str],
    generation_names: Iterable[str],
    *,
    aliases: Mapping[str, str] | None = None,
) -> list[StationLink]:
    """Match 17141 sites to 17140 generation rows on the normalized station key.

    `aliases` maps a **generation-side** normalized key to the **site-side** key it
    was manually confirmed to be; anything not covered stays `site_only` or
    `generation_only` with its original name preserved.
    """
    alias_map = dict(aliases or {})
    sites = {normalize_station(name): chinese_part(name) for name in site_names}
    generation: dict[str, str] = {}
    for name in generation_names:
        generation[alias_map.get(normalize_station(name), normalize_station(name))] = chinese_part(
            name
        )

    links = [
        StationLink(
            key=key,
            site_name=sites.get(key),
            generation_name=generation.get(key),
            status=(
                "matched"
                if key in sites and key in generation
                else "site_only"
                if key in sites
                else "generation_only"
            ),
        )
        for key in sorted(set(sites) | set(generation))
    ]
    return links


def summarize_alignment(links: Iterable[StationLink]) -> dict[str, object]:
    """Counts plus the unmatched names, for the reproducible alignment report."""
    items = list(links)
    by_status = {
        status: sum(link.status == status for link in items)
        for status in ("matched", "site_only", "generation_only")
    }
    total = len(items)
    return {
        "total": total,
        **by_status,
        "match_rate": round(by_status["matched"] / total, 4) if total else 0.0,
        "unmatched": sorted(link.key for link in items if link.status != "matched"),
    }
