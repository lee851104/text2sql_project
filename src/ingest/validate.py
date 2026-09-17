"""Validate the fixed Taipower CSV inputs before building the database."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNITS_REQUIRED = {
    "電廠名稱",
    "郵遞區號",
    "地址-縣市鄉鎮",
    "地址-村里",
    "地址-街路門牌",
    "連絡電話",
    "傳真電話",
    "機組名稱",
    "商轉日期",
    "裝置容量(瓩)",
    "燃料種類",
}
DAILY_SYSTEM_COLUMNS = (
    "淨尖峰供電能力(萬瓩)",
    "尖峰負載(萬瓩)",
    "備轉容量(萬瓩)",
    "備轉容量率(%)",
    "工業用電(百萬度)",
    "民生用電(百萬度)",
)
CROSSWALK_REQUIRED = {
    "b_column",
    "a_plant",
    "n_plants",
    "a_units",
    "n_units",
    "cap_a_wankw",
    "obs_max_b",
    "ratio",
    "confidence",
    "is_residual",
    "is_bucket",
    "note",
}
DAILY_LONG_REQUIRED = {
    "日期",
    "b_column",
    "尖峰出力_萬瓩",
    "a_plant",
    "a_units",
    "n_units",
    "cap_a_萬瓩",
    "grain",
    "category",
    "confidence",
}
OUTAGE_REQUIRED = {"年度", "能源別", "機組名稱", "開始日期", "結束日期", "備註"}
GENERATION_COST_LABEL = "各種發電方式之發電成本"
GENERATION_COST_COLUMNS = {
    "112年 審定決算(元/度)": (2023, "審定決算"),
    "113年 審定決算(元/度)": (2024, "審定決算"),
    "114年 自編決算(元/度)": (2025, "自編決算"),
}
GENERATION_COST_REQUIRED = {GENERATION_COST_LABEL, *GENERATION_COST_COLUMNS}


class DataValidationError(ValueError):
    """Raised when source data cannot safely be imported."""


def load_project_config(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = root / "configs/config.yaml"
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def resolve_configured_paths(root: Path = PROJECT_ROOT) -> dict[str, Path]:
    config = load_project_config(root)
    return {name: root / value for name, value in config["paths"].items()}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise DataValidationError(f"找不到資料檔：{path}")
    if path.stat().st_size == 0:
        raise DataValidationError(f"資料檔為空：{path}")
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise DataValidationError(f"資料檔缺少標頭：{path}")
            rows = list(reader)
            return reader.fieldnames, rows
    except UnicodeDecodeError as error:
        raise DataValidationError(f"資料檔不是 UTF-8：{path}") from error


def require_columns(path: Path, fieldnames: Iterable[str], required: set[str]) -> None:
    missing = required - set(fieldnames)
    if missing:
        names = "、".join(sorted(missing))
        raise DataValidationError(f"{path} 缺少欄位：{names}")


def parse_source_date(value: str, *, context: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date().isoformat()
    except ValueError as error:
        raise DataValidationError(f"{context} 日期格式錯誤：{value!r}") from error


def parse_unit_date(value: str, *, context: str) -> tuple[str, str]:
    """Return a sortable ISO date and source precision for a unit start date."""
    stripped = value.strip()
    formats = {6: ("%Y%m", "month"), 8: ("%Y%m%d", "day")}
    date_format, precision = formats.get(len(stripped), ("", ""))
    if not date_format:
        raise DataValidationError(f"{context} 商轉日期格式錯誤：{value!r}")
    try:
        parsed = datetime.strptime(stripped, date_format).date()
    except ValueError as error:
        raise DataValidationError(f"{context} 商轉日期格式錯誤：{value!r}") from error
    if precision == "month":
        parsed = parsed.replace(day=1)
    return parsed.isoformat(), precision


def parse_number(value: str, *, context: str, allow_empty: bool = False) -> float | None:
    stripped = value.strip()
    if allow_empty and not stripped:
        return None
    try:
        number = float(stripped)
    except ValueError as error:
        raise DataValidationError(f"{context} 不是數值：{value!r}") from error
    if not math.isfinite(number):
        raise DataValidationError(f"{context} 不是有限數值：{value!r}")
    return number


def parse_generation_cost_rows(
    rows: list[dict[str, str]],
) -> list[tuple[str, str, int, str, float]]:
    """Normalize the annual generation-cost matrix into queryable records."""

    records: list[tuple[str, str, int, str, float]] = []
    current_group: str | None = None
    for index, row in enumerate(rows, start=2):
        name = re.sub(r"\s+", "", row.get(GENERATION_COST_LABEL, ""))
        if not name:
            raise DataValidationError(f"generation_cost.csv:{index} 發電方式不可為空")
        values = [row.get(column, "").strip() for column in GENERATION_COST_COLUMNS]
        if not any(values):
            if name not in {"自發電力", "購入電力"}:
                raise DataValidationError(f"generation_cost.csv:{index} 缺少年度成本")
            current_group = name
            continue
        source_group = "整體" if name == "平均發購電成本" else current_group
        if source_group is None:
            raise DataValidationError(f"generation_cost.csv:{index} 缺少電力來源群組")
        for column, (year, basis) in GENERATION_COST_COLUMNS.items():
            cost = parse_number(
                row.get(column, ""), context=f"generation_cost.csv:{index}:{column}"
            )
            if cost is None or cost < 0:
                raise DataValidationError(f"generation_cost.csv:{index} 成本不可小於 0")
            records.append((source_group, name, year, basis, cost))
    _assert_unique(
        ((group, name, year) for group, name, year, _basis, _cost in records),
        context="generation_cost.csv",
    )
    return records


def _assert_unique(values: Iterable[Any], *, context: str) -> None:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        preview = "、".join(str(value) for value in sorted(duplicates)[:5])
        raise DataValidationError(f"{context} 有重複鍵：{preview}")


def validate_files(
    units_csv: Path,
    daily_csv: Path,
    crosswalk_csv: Path,
    daily_long_csv: Path,
    outage_csv: Path | None = None,
    generation_cost_csv: Path | None = None,
) -> dict[str, Any]:
    unit_fields, units = read_csv(units_csv)
    daily_fields, daily = read_csv(daily_csv)
    crosswalk_fields, crosswalk = read_csv(crosswalk_csv)
    long_fields, daily_long = read_csv(daily_long_csv)
    outage_fields: list[str] = []
    outages: list[dict[str, str]] = []
    if outage_csv is not None and outage_csv.is_file():
        outage_fields, outages = read_csv(outage_csv)
    cost_fields: list[str] = []
    cost_rows: list[dict[str, str]] = []
    if generation_cost_csv is not None and generation_cost_csv.is_file():
        cost_fields, cost_rows = read_csv(generation_cost_csv)

    require_columns(units_csv, unit_fields, UNITS_REQUIRED)
    require_columns(daily_csv, daily_fields, {"日期", *DAILY_SYSTEM_COLUMNS})
    require_columns(crosswalk_csv, crosswalk_fields, CROSSWALK_REQUIRED)
    require_columns(daily_long_csv, long_fields, DAILY_LONG_REQUIRED)
    if outages:
        require_columns(outage_csv, outage_fields, OUTAGE_REQUIRED)
    if generation_cost_csv is not None:
        require_columns(generation_cost_csv, cost_fields, GENERATION_COST_REQUIRED)
    generation_costs = parse_generation_cost_rows(cost_rows) if cost_rows else []

    _assert_unique(
        ((row["電廠名稱"].strip(), row["機組名稱"].strip()) for row in units),
        context="units.csv",
    )
    for index, row in enumerate(units, start=2):
        capacity = parse_number(row["裝置容量(瓩)"], context=f"units.csv:{index}")
        if capacity is None or capacity <= 0:
            raise DataValidationError(f"units.csv:{index} 裝置容量必須大於 0")
        if row["商轉日期"].strip():
            parse_unit_date(row["商轉日期"], context=f"units.csv:{index}")

    dates = [
        parse_source_date(row["日期"], context=f"daily.csv:{index}")
        for index, row in enumerate(daily, 2)
    ]
    _assert_unique(dates, context="daily.csv")
    for index, row in enumerate(daily, start=2):
        for column in DAILY_SYSTEM_COLUMNS:
            parse_number(row[column], context=f"daily.csv:{index}:{column}", allow_empty=True)

    generation_columns = [
        column for column in daily_fields if column not in {"日期", *DAILY_SYSTEM_COLUMNS}
    ]
    for index, row in enumerate(daily, start=2):
        for column in generation_columns:
            parse_number(row[column], context=f"daily.csv:{index}:{column}", allow_empty=True)

    _assert_unique((row["b_column"].strip() for row in crosswalk), context="crosswalk.csv")
    for index, row in enumerate(crosswalk, start=2):
        parse_number(row["ratio"], context=f"crosswalk.csv:{index}:ratio")
        if row["is_residual"] not in {"0", "1"} or row["is_bucket"] not in {"0", "1"}:
            raise DataValidationError(f"crosswalk.csv:{index} 旗標只能是 0 或 1")

    long_keys: list[tuple[str, str]] = []
    for index, row in enumerate(daily_long, start=2):
        date = parse_source_date(row["日期"], context=f"daily_long.csv:{index}")
        long_keys.append((date, row["b_column"].strip()))
        parse_number(row["尖峰出力_萬瓩"], context=f"daily_long.csv:{index}:尖峰出力")
    _assert_unique(long_keys, context="daily_long.csv")

    date_set = set(dates)
    unknown_dates = {date for date, _ in long_keys} - date_set
    if unknown_dates:
        raise DataValidationError("daily_long.csv 包含 daily.csv 以外的日期")
    expected_columns = {
        column.strip().removesuffix("(萬瓩)").strip() for column in generation_columns
    }
    unknown_columns = {column for _, column in long_keys} - expected_columns
    if unknown_columns:
        raise DataValidationError("daily_long.csv 包含 daily.csv 以外的機組欄位")
    columns_by_date: dict[str, set[str]] = {date: set() for date in dates}
    for date, column in long_keys:
        columns_by_date[date].add(column)
    incomplete_dates = [
        date for date, columns in columns_by_date.items() if columns != expected_columns
    ]
    if incomplete_dates:
        raise DataValidationError(
            f"daily_long.csv 每個日期的機組欄位集合必須與 daily.csv 一致：{incomplete_dates[0]}"
        )
    expected_long_rows = len(dates) * len(generation_columns)
    if len(daily_long) != expected_long_rows:
        raise DataValidationError(
            "daily_long.csv 列數不等於日期數 × 機組欄位數："
            f"{len(daily_long)} != {len(dates)} × {len(generation_columns)}"
        )

    warnings: list[dict[str, Any]] = []
    for index, row in enumerate(outages, start=2):
        start = parse_source_date(row["開始日期"], context=f"outage.csv:{index}")
        end = parse_source_date(row["結束日期"], context=f"outage.csv:{index}")
        if start > end:
            warnings.append(
                {
                    "code": "OUTAGE_DATE_INVALID",
                    "row": index,
                    "unit": row["機組名稱"],
                    "start_date": start,
                    "end_date": end,
                }
            )

    paths = (units_csv, daily_csv, crosswalk_csv, daily_long_csv)
    if outages and outage_csv is not None:
        paths = (*paths, outage_csv)
    if generation_cost_csv is not None:
        paths = (*paths, generation_cost_csv)
    hashes = {path.name: sha256_file(path) for path in paths}
    checksum_input = "\n".join(f"{name}:{hashes[name]}" for name in sorted(hashes))
    return {
        "status": "pass",
        "counts": {
            "plants": len({row["電廠名稱"].strip() for row in units}),
            "units": len(units),
            "dates": len(dates),
            "daily_system": len(daily),
            "generation_columns": len(generation_columns),
            "mapped_columns": len(crosswalk),
            "daily_peak": len(daily_long),
            "outages": len(outages),
            "generation_costs": len(generation_costs),
        },
        "date_range": {"min": min(dates), "max": max(dates)},
        "source_sha256": hashes,
        "data_checksum": hashlib.sha256(checksum_input.encode()).hexdigest(),
        "warnings": warnings,
    }


def validate_configured_files(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    paths = resolve_configured_paths(root)
    return validate_files(
        paths["units_csv"],
        paths["daily_csv"],
        paths["crosswalk_csv"],
        paths["daily_long_csv"],
        paths["outage_csv"],
        paths["generation_cost_csv"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出驗證結果")
    args = parser.parse_args(argv)
    try:
        report = validate_configured_files()
    except DataValidationError as error:
        parser.exit(1, f"資料驗證失敗：{error}\n")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        counts = report["counts"]
        print(
            "資料驗證通過："
            f"{counts['units']} 機組、{counts['generation_columns']} 欄位、"
            f"{counts['daily_peak']} 尖峰出力列，"
            f"期間 {report['date_range']['min']} ～ {report['date_range']['max']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
