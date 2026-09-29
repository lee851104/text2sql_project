"""Load the human decisions for realtime units: grain and plant scope (§5.3).

程式不做字串比對推測：沒列在檔案裡的機組一律是未定，電廠帳號看不到。
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DECISION_FIELDS = ("unit_type", "unit_name", "grain", "access_scope", "plant_id", "note")
GRAINS = frozenset({"unit", "bucket"})
SCOPES = frozenset({"plant", "shared"})


class DecisionFileError(ValueError):
    """整份人工決定檔無效（欄位標頭不符或重複列）；呼叫端沿用上一次成功載入的內容。"""


# Errors that can occur when loading unit and plant decision files
LOAD_ERRORS = (DecisionFileError, OSError, ValueError, KeyError, csv.Error)


@dataclass(frozen=True)
class UnitDecision:
    grain: str
    access_scope: str
    plant_id: int | None
    note: str


@dataclass(frozen=True)
class Decisions:
    units: Mapping[tuple[str, str], UnitDecision]
    plants: Mapping[int, str]
    warnings: tuple[str, ...]
    sha256: str
    plants_sha256: str


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def load_plants(path: Path) -> dict[int, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {int(row["plant_id"]): row["plant_name"].strip() for row in csv.DictReader(handle)}


def empty_decisions(plants_csv: Path) -> Decisions:
    """只有電廠名冊、沒有任何機組決定；決定檔無效又必須重建時用。"""
    return Decisions({}, load_plants(plants_csv), (), "", file_sha256(plants_csv))


def load_decisions(units_csv: Path, plants_csv: Path) -> Decisions:
    plants = load_plants(plants_csv)
    plants_sha = file_sha256(plants_csv)
    if not units_csv.is_file():
        warning = f"找不到 {units_csv.name}，所有機組都視為未定。"
        return Decisions({}, plants, (warning,), "", plants_sha)
    with units_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != DECISION_FIELDS:
            raise DecisionFileError(
                f"{units_csv.name} 的欄位必須是 {','.join(DECISION_FIELDS)}，"
                f"實際是 {reader.fieldnames}。"
            )
        # 少欄的列：缺的欄位填成 None，一律當成空字串。
        rows = [
            {field: (row.get(field) or "").strip() for field in DECISION_FIELDS} for row in reader
        ]
    seen: set[tuple[str, str]] = set()
    repeated: list[tuple[str, str]] = []
    for row in rows:
        key = (row["unit_type"], row["unit_name"])
        if key in seen:
            repeated.append(key)
        seen.add(key)
    if repeated:
        listed = "、".join(f"{unit_type}|{name}" for unit_type, name in repeated)
        raise DecisionFileError(f"{units_csv.name} 有重複的（類型, 名稱）：{listed}")

    units: dict[tuple[str, str], UnitDecision] = {}
    warnings: list[str] = []
    for line, row in enumerate(rows, start=2):
        key = (row["unit_type"], row["unit_name"])
        grain = row["grain"]
        scope = row["access_scope"]
        plant_text = row["plant_id"]
        note = row["note"]
        label = f"第 {line} 列 {key[0]}|{key[1]}"
        if grain not in GRAINS:
            warnings.append(f"{label}：grain={grain!r} 不合法，視為未定。")
            continue
        if scope not in SCOPES:
            warnings.append(f"{label}：access_scope={scope!r} 不合法，視為未定。")
            continue
        if scope == "plant":
            if not plant_text.isdigit() or int(plant_text) not in plants:
                warnings.append(f"{label}：plant_id={plant_text!r} 不在 plants.csv，視為未定。")
                continue
            plant_id: int | None = int(plant_text)
        else:
            if plant_text:
                warnings.append(f"{label}：shared 不能填 plant_id，視為未定。")
                continue
            plant_id = None
        units[key] = UnitDecision(grain, scope, plant_id, note)
    return Decisions(units, plants, tuple(warnings), file_sha256(units_csv), plants_sha)
