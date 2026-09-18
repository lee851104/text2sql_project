"""Fill the installed capacity of B columns that have no unit master.

每日供需資料有 64 個出力欄位，其中 21 個在 `units.csv`（水火力機組設備）裡找不到
對應機組：核能、民營電廠、汽電共生與風光彙總。沒有分母就算不出容量利用率，也無法
判斷某個出力值算不算滿載。

這個模組把兩份官方資料轉成「B 欄位 → 裝置容量」的對照：資料集 10858 的核能主檔，
以及資料集 8931 的各機組即時資訊。哪一欄取哪一筆由
`configs/b_column_capacity.yaml` 逐欄明寫，程式不做字串比對推測。

No I/O, no database, no network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: 8931 的容量欄在小計列會寫成 `15918.1(26.052%)`，明細列則是純數字。
_LEADING_DECIMAL = re.compile(r"^\d+(?:\.\d+)?")

#: 8931 的機組類型偶爾殘留 HTML 標籤（`儲能負載(...)</b>`）。
_HTML_TAG = re.compile(r"<[^>]*>")

SUBTOTAL_NAME = "小計"


class CapacityConfigError(ValueError):
    """Raised when the mapping names a column or source the data cannot satisfy."""


def parse_megawatts(value: str) -> float | None:
    """Read the MW figure from a 8931 capacity cell, or None when it is `-`."""
    match = _LEADING_DECIMAL.match(value.strip())
    return float(match.group()) if match else None


def megawatts_to_kilowatts(megawatts: float) -> int:
    """8931 uses MW; the unit master and `dim_unit.capacity_kw` use 瓩."""
    return round(megawatts * 1000)


@dataclass(frozen=True)
class ColumnCapacity:
    """One B column's capacity and where the number came from."""

    b_column: str
    capacity_kw: int | None
    source: str
    source_detail: str
    note: str


def _realtime_index(rows: Iterable[Mapping[str, str]]) -> dict[str, float]:
    index: dict[str, float] = {}
    for row in rows:
        name = row["機組名稱"].strip()
        if not name or SUBTOTAL_NAME in name:
            continue
        megawatts = parse_megawatts(row["裝置容量(MW)"])
        if megawatts is not None:
            index[name] = megawatts
    return index


def _subtotal_index(rows: Iterable[Mapping[str, str]]) -> dict[str, float]:
    index: dict[str, float] = {}
    for row in rows:
        if SUBTOTAL_NAME not in row["機組名稱"]:
            continue
        fuel = _HTML_TAG.sub("", row["機組類型"]).strip()
        megawatts = parse_megawatts(row["裝置容量(MW)"])
        if fuel and megawatts is not None:
            index[fuel] = megawatts
    return index


def _nuclear_index(rows: Iterable[Mapping[str, str]]) -> dict[tuple[str, str], int]:
    return {
        (row["電廠名稱"].strip(), row["機組名稱"].strip()): int(row["裝置容量MWe"]) * 1000
        for row in rows
        if row.get("裝置容量MWe", "").strip().isdigit()
    }


def build_column_capacities(
    mapping: Mapping[str, Mapping[str, object]],
    *,
    realtime_rows: Iterable[Mapping[str, str]],
    nuclear_rows: Iterable[Mapping[str, str]],
) -> list[ColumnCapacity]:
    """Resolve every configured B column against the two official sources.

    A column the configuration marks `unavailable` keeps a NULL capacity and its
    stated reason.  A column whose configured source rows are missing raises
    instead of silently falling back — a quietly empty capacity would look the
    same as a column nobody has mapped yet.
    """
    realtime = _realtime_index(realtime_rows)
    subtotals = _subtotal_index(realtime_rows)
    nuclear = _nuclear_index(nuclear_rows)

    results: list[ColumnCapacity] = []
    for column, spec in mapping.items():
        source = str(spec.get("source", ""))
        note = str(spec.get("note", ""))

        if source == "unavailable":
            results.append(ColumnCapacity(column, None, source, "", note))
            continue

        if source == "nuclear":
            key = (str(spec["plant"]), str(spec["unit"]))
            if key not in nuclear:
                raise CapacityConfigError(f"核能主檔沒有 {key[0]} {key[1]}（欄位 {column}）")
            results.append(ColumnCapacity(column, nuclear[key], source, f"{key[0]}{key[1]}", note))
            continue

        if source == "realtime":
            units = [str(name) for name in spec["units"]]  # type: ignore[union-attr]
            missing = [name for name in units if name not in realtime]
            if missing:
                raise CapacityConfigError(f"8931 快照沒有 {missing}（欄位 {column}）")
            total = sum(realtime[name] for name in units)
            results.append(
                ColumnCapacity(column, megawatts_to_kilowatts(total), source, "+".join(units), note)
            )
            continue

        if source == "realtime_subtotal":
            fuel = str(spec["fuel"])
            if fuel not in subtotals:
                raise CapacityConfigError(f"8931 快照沒有 {fuel} 小計（欄位 {column}）")
            results.append(
                ColumnCapacity(
                    column,
                    megawatts_to_kilowatts(subtotals[fuel]),
                    source,
                    f"{fuel}小計",
                    note,
                )
            )
            continue

        raise CapacityConfigError(f"不支援的來源類型：{source}（欄位 {column}）")

    return sorted(results, key=lambda item: item.b_column)


def summarize_column_capacities(
    capacities: Iterable[ColumnCapacity],
) -> dict[str, object]:
    """Counts by source plus the columns still without a capacity."""
    items = list(capacities)
    by_source: dict[str, int] = {}
    for item in items:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    filled = [item for item in items if item.capacity_kw is not None]
    return {
        "configured": len(items),
        "filled": len(filled),
        "by_source": dict(sorted(by_source.items())),
        "capacity_kw": sum(item.capacity_kw or 0 for item in filled),
        "unavailable": sorted(item.b_column for item in items if item.capacity_kw is None),
    }
