"""Build the deterministic SQLite semantic layer from validated CSV artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from align.crosswalk import CrosswalkRecord, parse_crosswalk
from align.grain import classify_category, classify_grain
from align.outage_align import align_outage_rows, summarize_outage_alignment
from align.pitfalls import generate_pitfalls
from ingest.validate import (
    DAILY_SYSTEM_COLUMNS,
    PROJECT_ROOT,
    parse_generation_cost_rows,
    parse_number,
    parse_source_date,
    parse_unit_date,
    read_csv,
    resolve_configured_paths,
    validate_files,
)

SCHEMA_VERSION = "2"


class DatabasePublishError(PermissionError):
    """Raised when a completed database cannot replace the published snapshot."""


def _text(row: dict[str, str], key: str) -> str:
    return row.get(key, "").strip()


def _load_align_limits(root: Path) -> tuple[float, float]:
    with (root / "configs/align.yaml").open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    return float(config["ratio"]["expected_min"]), float(config["ratio"]["expected_max"])


def _load_rows(paths: dict[str, Path]) -> dict[str, list[dict[str, str]]]:
    rows = {
        name: read_csv(paths[name])[1]
        for name in ("units_csv", "daily_csv", "crosswalk_csv", "daily_long_csv")
    }
    rows["outage_csv"] = read_csv(paths["outage_csv"])[1] if paths["outage_csv"].is_file() else []
    rows["generation_cost_csv"] = read_csv(paths["generation_cost_csv"])[1]
    return rows


def _insert_generation_costs(
    connection: sqlite3.Connection,
    costs: list[dict[str, str]],
) -> None:
    connection.executemany(
        """INSERT INTO fact_generation_cost
           (source_group, generation_type, year, accounting_basis, cost_per_kwh)
           VALUES (?, ?, ?, ?, ?)""",
        parse_generation_cost_rows(costs),
    )


def _insert_plants_and_units(
    connection: sqlite3.Connection, units: list[dict[str, str]]
) -> dict[str, list[int]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in units:
        grouped[_text(row, "電廠名稱")].append(row)

    unit_ids_by_name: dict[str, list[int]] = defaultdict(list)
    for plant_id, plant_name in enumerate(sorted(grouped), start=1):
        rows = grouped[plant_name]
        first = rows[0]
        fuels = sorted({_text(row, "燃料種類") for row in rows})
        address = "".join(
            _text(first, column) for column in ("地址-縣市鄉鎮", "地址-村里", "地址-街路門牌")
        )
        connection.execute(
            """INSERT INTO dim_plant
               (id, plant_name, postal_code, county, address, phone, fax, primary_fuel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plant_id,
                plant_name,
                _text(first, "郵遞區號"),
                _text(first, "地址-縣市鄉鎮"),
                address,
                _text(first, "連絡電話"),
                _text(first, "傳真電話"),
                "|".join(fuels),
            ),
        )
        for row in sorted(rows, key=lambda item: _text(item, "機組名稱")):
            unit_id = connection.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM dim_unit"
            ).fetchone()[0]
            commercial_date = _text(row, "商轉日期")
            parsed_date, date_precision = (
                parse_unit_date(commercial_date, context="units.csv")
                if commercial_date
                else (None, None)
            )
            connection.execute(
                """INSERT INTO dim_unit
                   (id, plant_id, unit_name, commercial_date, commercial_date_raw,
                    commercial_date_precision, capacity_kw, fuel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    unit_id,
                    plant_id,
                    _text(row, "機組名稱"),
                    parsed_date,
                    commercial_date or None,
                    date_precision,
                    int(_text(row, "裝置容量(瓩)")),
                    _text(row, "燃料種類"),
                ),
            )
            unit_ids_by_name[_text(row, "機組名稱")].append(unit_id)
    return unit_ids_by_name


def _insert_dates_and_system(connection: sqlite3.Connection, daily: list[dict[str, str]]) -> None:
    for row in daily:
        date = parse_source_date(row["日期"], context="daily.csv")
        year, month, day = (int(part) for part in date.split("-"))
        connection.execute("INSERT INTO dim_date VALUES (?, ?, ?, ?)", (date, year, month, day))
        values = [
            parse_number(row[column], context=column, allow_empty=True)
            for column in DAILY_SYSTEM_COLUMNS
        ]
        connection.execute(
            """INSERT INTO fact_daily_system
               (date, net_peak_supply_wankw, peak_load_wankw, operating_reserve_wankw,
                operating_reserve_rate_pct, industrial_usage_million_kwh,
                residential_usage_million_kwh)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (date, *values),
        )


def _insert_columns_and_crosswalk(
    connection: sqlite3.Connection,
    daily_long: list[dict[str, str]],
    crosswalk: list[dict[str, str]],
    units: list[dict[str, str]],
    unit_ids_by_name: dict[str, list[int]],
) -> tuple[dict[str, int], list[CrosswalkRecord]]:
    crosswalk_records = parse_crosswalk(crosswalk)
    crosswalk_by_column = {record.b_column: record for record in crosswalk_records}
    fuel_by_unit = {_text(row, "機組名稱"): _text(row, "燃料種類") for row in units}
    column_metadata: dict[str, tuple[str, str]] = {}
    for row in daily_long:
        column = _text(row, "b_column")
        metadata = (_text(row, "grain"), _text(row, "category"))
        previous = column_metadata.setdefault(column, metadata)
        if previous != metadata:
            raise ValueError(f"{column} 的粒度或類別在 daily_long.csv 中不一致")

    ids: dict[str, int] = {}
    for column_id, column in enumerate(sorted(column_metadata), start=1):
        _source_grain, source_category = column_metadata[column]
        record = crosswalk_by_column.get(column)
        fuels = {fuel_by_unit[name] for name in record.units} if record else set()
        grain = classify_grain(
            column,
            n_units=record.n_units if record else 0,
            n_plants=record.n_plants if record else 0,
            is_residual=record.is_residual if record else False,
            is_bucket=record.is_bucket if record else False,
        )
        category = classify_category(fuels, source_category=source_category)
        has_master = int(record is not None)
        connection.execute(
            "INSERT INTO dim_b_column VALUES (?, ?, ?, ?, ?)",
            (column_id, column, grain, category, has_master),
        )
        ids[column] = column_id

    for bridge_id, column in enumerate(sorted(crosswalk_by_column), start=1):
        record = crosswalk_by_column[column]
        column_id = ids[column]
        connection.execute(
            """INSERT INTO bridge_b_column
               (id, b_column_id, a_plant, n_plants, n_units, cap_a_wankw, obs_max_b,
                ratio, confidence, is_residual, is_bucket, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bridge_id,
                column_id,
                "|".join(record.plants),
                record.n_plants,
                record.n_units,
                record.capacity_wankw,
                record.observed_max_wankw,
                record.ratio,
                record.confidence,
                int(record.is_residual),
                int(record.is_bucket),
                record.note,
            ),
        )
        for unit_name in record.units:
            candidates = unit_ids_by_name.get(unit_name, [])
            if len(candidates) != 1:
                raise ValueError(f"對齊機組 {unit_name!r} 無法唯一對應 dim_unit")
            connection.execute(
                "INSERT INTO bridge_b_column_unit VALUES (?, ?)", (column_id, candidates[0])
            )
    return ids, crosswalk_records


def _insert_daily_peak(
    connection: sqlite3.Connection,
    daily_long: list[dict[str, str]],
    column_ids: dict[str, int],
) -> None:
    rows = (
        (
            parse_source_date(row["日期"], context="daily_long.csv"),
            column_ids[_text(row, "b_column")],
            parse_number(row["尖峰出力_萬瓩"], context="daily_long.csv"),
        )
        for row in daily_long
    )
    connection.executemany("INSERT INTO fact_daily_peak VALUES (?, ?, ?)", rows)


def _insert_derived_pitfalls(
    connection: sqlite3.Connection,
    records: list[CrosswalkRecord],
    *,
    ratio_max: float,
) -> None:
    for pitfall in generate_pitfalls(records, ratio_max=ratio_max):
        connection.execute(
            """INSERT INTO meta_pitfall
               (pitfall_code, target_kind, target_name, severity, reason, suggestion, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                pitfall.code,
                pitfall.target_kind,
                pitfall.target_name,
                pitfall.severity,
                pitfall.reason,
                pitfall.suggestion,
                json.dumps(pitfall.evidence, ensure_ascii=False),
            ),
        )


def _load_outage_overrides(root: Path) -> dict[str, str]:
    with (root / "configs/outage_overrides.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle).get("overrides", {})


def _insert_outages(
    connection: sqlite3.Connection,
    outages: list[dict[str, str]],
    units: list[dict[str, str]],
    unit_ids_by_name: dict[str, list[int]],
    *,
    overrides: dict[str, str],
) -> dict[str, object]:
    results = align_outage_rows(outages, units, overrides=overrides)
    for outage_id, (row, result) in enumerate(zip(outages, results, strict=True), start=1):
        unit_id = unit_ids_by_name[result.unit_name][0] if result.unit_name else None
        start_date = parse_source_date(row["開始日期"], context="outage.csv")
        end_date = parse_source_date(row["結束日期"], context="outage.csv")
        connection.execute(
            """INSERT INTO dim_outage
               (id, unit_id, source_unit_name, fuel, start_date, end_date, reason,
                date_status, alignment_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                outage_id,
                unit_id,
                row["機組名稱"].strip(),
                row["能源別"].strip(),
                start_date,
                end_date,
                row["備註"].strip(),
                "valid" if start_date <= end_date else "invalid_range",
                result.status,
            ),
        )
    return summarize_outage_alignment(results)


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "dim_plant",
        "dim_unit",
        "dim_date",
        "dim_b_column",
        "bridge_b_column",
        "bridge_b_column_unit",
        "fact_daily_peak",
        "fact_daily_system",
        "dim_outage",
        "fact_generation_cost",
        "meta_pitfall",
    )
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def _database_content_checksum(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in (
        "dim_plant",
        "dim_unit",
        "dim_date",
        "dim_b_column",
        "bridge_b_column",
        "bridge_b_column_unit",
        "fact_daily_peak",
        "fact_daily_system",
        "dim_outage",
        "fact_generation_cost",
        "meta_pitfall",
    ):
        digest.update(table.encode())
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1, 2'):
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
    return digest.hexdigest()


def build_database(
    target: Path,
    *,
    root: Path = PROJECT_ROOT,
    report_path: Path | None = None,
    source_paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    paths = resolve_configured_paths(root)
    if source_paths is not None:
        allowed_sources = {
            "units_csv",
            "daily_csv",
            "crosswalk_csv",
            "daily_long_csv",
            "outage_csv",
            "generation_cost_csv",
        }
        unknown_sources = set(source_paths) - allowed_sources
        if unknown_sources:
            raise ValueError(f"不支援的資料來源：{sorted(unknown_sources)}")
        paths.update({name: Path(path).resolve() for name, path in source_paths.items()})
    validation = validate_files(
        paths["units_csv"],
        paths["daily_csv"],
        paths["crosswalk_csv"],
        paths["daily_long_csv"],
        paths["outage_csv"],
        paths["generation_cost_csv"],
    )
    rows = _load_rows(paths)
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lower_ratio, upper_ratio = _load_align_limits(root)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}-", suffix=".db", dir=target.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript((root / "src/ingest/schema.sql").read_text(encoding="utf-8"))
            unit_ids = _insert_plants_and_units(connection, rows["units_csv"])
            _insert_dates_and_system(connection, rows["daily_csv"])
            column_ids, crosswalk_records = _insert_columns_and_crosswalk(
                connection,
                rows["daily_long_csv"],
                rows["crosswalk_csv"],
                rows["units_csv"],
                unit_ids,
            )
            _insert_daily_peak(connection, rows["daily_long_csv"], column_ids)
            outage_summary = _insert_outages(
                connection,
                rows["outage_csv"],
                rows["units_csv"],
                unit_ids,
                overrides=_load_outage_overrides(root),
            )
            _insert_generation_costs(connection, rows["generation_cost_csv"])
            _insert_derived_pitfalls(connection, crosswalk_records, ratio_max=upper_ratio)

            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise ValueError(f"外鍵驗證失敗：{foreign_key_errors[:3]}")
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"SQLite quick_check 失敗：{integrity}")

            counts = _table_counts(connection)
            content_checksum = _database_content_checksum(connection)
            source_hashes = json.dumps(validation["source_sha256"], sort_keys=True)
            connection.execute(
                """INSERT INTO meta_manifest
                   (id, schema_version, data_version, data_start, data_end, built_at,
                    source_sha256, table_counts, data_checksum)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    SCHEMA_VERSION,
                    validation["data_checksum"][:16],
                    validation["date_range"]["min"],
                    validation["date_range"]["max"],
                    datetime.now(UTC).isoformat(),
                    source_hashes,
                    json.dumps(counts, sort_keys=True),
                    content_checksum,
                ),
            )
            connection.commit()
        try:
            os.replace(temporary_path, target)
        except PermissionError as exc:
            raise DatabasePublishError(
                f"無法更新資料庫 {target}。檔案可能正被 PowerQuery 服務、SQLite "
                "檢視器或同步程式占用；請先在服務視窗按 Ctrl+C 停止服務，確認目錄可寫，"
                "再重新執行建庫指令。"
            ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)

    report = {
        **validation,
        "schema_version": SCHEMA_VERSION,
        "database": str(target),
        "table_counts": counts,
        "database_content_checksum": content_checksum,
        "ratio_thresholds": {"expected_min": lower_ratio, "expected_max": upper_ratio},
        "outage_alignment": outage_summary,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    paths = resolve_configured_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=paths["database"])
    parser.add_argument("--report", type=Path, default=paths["data_quality_report"])
    args = parser.parse_args(argv)
    try:
        report = build_database(args.output, report_path=args.report)
    except DatabasePublishError as exc:
        parser.exit(1, f"錯誤：{exc}\n")
    counts = report["table_counts"]
    print(
        f"已建立 {args.output}：{counts['dim_unit']} 機組、"
        f"{counts['bridge_b_column']} 對應欄位、{counts['fact_daily_peak']} 尖峰出力列。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
