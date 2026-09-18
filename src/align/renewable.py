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


def chinese_key_map(fieldnames: Iterable[str]) -> dict[str, str]:
    """Map the Chinese side of each bilingual header back to its raw column name."""
    return {chinese_part(name): name for name in fieldnames}


@dataclass(frozen=True)
class StationRecord:
    """One 發電站 after the 場址 rows of 17141 are aggregated.

    17141 記錄的是場址，同一個發電站可以有多個場址（彰工風力有 4 個）。
    入庫粒度是發電站，因此容量與風機數相加、型號與申設狀態以 `|` 併列。
    `source` 區分官方主檔與補充檔，兩者在查詢結果中必須可分辨。
    """

    station_name: str
    energy_type: str
    county: str
    site_count: int
    capacity_kw: int
    turbine_count: int | None
    models: str
    application_status: str
    source: str
    note: str


def _joined(values: Iterable[str]) -> str:
    return "|".join(sorted({value.strip() for value in values if value.strip()}))


def _optional_int(value: str) -> int | None:
    text = value.strip()
    return int(text) if text.isdigit() else None


def build_station_records(
    site_rows: Iterable[Mapping[str, str]],
    key_map: Mapping[str, str],
    *,
    supplement_rows: Iterable[Mapping[str, str]] = (),
) -> list[StationRecord]:
    """Aggregate 17141 場址 rows into one record per station, then append supplements.

    Subtotal rows are dropped here rather than by the caller, so no caller can
    forget and double the capacity.  A supplement whose station already exists in
    the official master is ignored — the official row wins.
    """
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for row in site_rows:
        if is_subtotal(row[key_map["發電站編號"]]):
            continue
        grouped.setdefault(normalize_station(row[key_map["發電站名稱"]]), []).append(row)

    records = [
        StationRecord(
            station_name=name,
            energy_type=_joined(chinese_part(row[key_map["能源別"]]) for row in rows),
            county=_joined(
                county for county in (parse_county(row[key_map["地址"]]) for row in rows) if county
            ),
            site_count=len(rows),
            capacity_kw=sum(int(row[key_map["裝置容量(瓩)"]]) for row in rows),
            turbine_count=sum(
                count
                for count in (_optional_int(row[key_map["風機數量"]]) for row in rows)
                if count is not None
            )
            or None,
            models=_joined(chinese_part(row[key_map["型號"]]) for row in rows),
            application_status=_joined(row[key_map["申設狀態"]] for row in rows),
            source="official",
            note="",
        )
        for name, rows in sorted(grouped.items())
    ]

    known = {record.station_name for record in records}
    records.extend(
        StationRecord(
            station_name=normalize_station(row["station_name"]),
            energy_type=row["energy_type"].strip(),
            county=row["county"].strip(),
            site_count=0,
            capacity_kw=int(row["capacity_kw"]),
            turbine_count=_optional_int(row.get("unit_count", "")),
            models="",
            application_status="",
            source="supplement",
            note=row.get("status_note", "").strip(),
        )
        for row in supplement_rows
        if normalize_station(row["station_name"]) not in known
    )
    return sorted(records, key=lambda record: record.station_name)


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
    """`matched`（兩個官方檔都有）、`supplemented`（場址主檔沒有，靠
    `re_sites_supplement.csv` 的第三方來源補上）、`site_only`、`generation_only`。"""


def align_stations(
    site_names: Iterable[str],
    generation_names: Iterable[str],
    *,
    aliases: Mapping[str, str] | None = None,
    supplement_names: Iterable[str] = (),
) -> list[StationLink]:
    """Match 17141 sites to 17140 generation rows on the normalized station key.

    `aliases` maps a **generation-side** normalized key to the **site-side** key it
    was manually confirmed to be; anything not covered stays `site_only` or
    `generation_only` with its original name preserved.

    `supplement_names` are stations the official 場址主檔 omits but a cited source
    confirms.  They match as `supplemented`, never as `matched`, so a reader can
    always tell how many stations the two official files really agreed on.
    """
    alias_map = dict(aliases or {})
    sites = {normalize_station(name): chinese_part(name) for name in site_names}
    supplement = {
        normalize_station(name): chinese_part(name)
        for name in supplement_names
        if normalize_station(name) not in sites
    }
    generation: dict[str, str] = {}
    for name in generation_names:
        generation[alias_map.get(normalize_station(name), normalize_station(name))] = chinese_part(
            name
        )

    links = [
        StationLink(
            key=key,
            site_name=sites.get(key) or supplement.get(key),
            generation_name=generation.get(key),
            status=_link_status(key, sites, supplement, generation),
        )
        for key in sorted(set(sites) | set(supplement) | set(generation))
    ]
    return links


def _link_status(
    key: str,
    sites: Mapping[str, str],
    supplement: Mapping[str, str],
    generation: Mapping[str, str],
) -> str:
    if key in generation:
        if key in sites:
            return "matched"
        if key in supplement:
            return "supplemented"
        return "generation_only"
    return "site_only"


def summarize_alignment(links: Iterable[StationLink]) -> dict[str, object]:
    """Counts plus the unmatched names, for the reproducible alignment report."""
    items = list(links)
    by_status = {
        status: sum(link.status == status for link in items)
        for status in ("matched", "supplemented", "site_only", "generation_only")
    }
    total = len(items)
    covered = by_status["matched"] + by_status["supplemented"]
    return {
        "total": total,
        **by_status,
        # 只算兩個官方檔真正對上的比率，補充檔不計入，避免把覆蓋率當成對齊率。
        "match_rate": round(by_status["matched"] / total, 4) if total else 0.0,
        "coverage_rate": round(covered / total, 4) if total else 0.0,
        "unmatched": sorted(
            link.key for link in items if link.status in {"site_only", "generation_only"}
        ),
        "supplemented_names": sorted(link.key for link in items if link.status == "supplemented"),
    }
