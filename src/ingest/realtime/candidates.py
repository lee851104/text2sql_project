"""Propose rows for taipower_align/realtime_units.csv from one d006001 snapshot.

只產生「候選」給人逐列審查，收集器執行期間不會用到這支程式。候選規則：

- 名稱帶「其它／其他／購電／小水力」、等於「汽電共生」或以「電池」開頭 → bucket
- 風力、太陽能、其它再生能源、汽電共生 → shared（不屬於 34 座電廠，比照 v_re_generation）
- 其餘類型：名稱以某座電廠的別名開頭且只命中一座 → plant；只有名稱中間出現別名 → plant 並註明；
  都沒命中或命中多座 → access_scope 留空、註明「待人工確認」，載入時會被當成未定

用法：uv run python -m ingest.realtime.candidates [snapshot.json] > candidates.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from ingest.realtime.decisions import DECISION_FIELDS
from ingest.realtime.parse import clean_type
from ingest.validate import PROJECT_ROOT

BUCKET_HINTS = ("其它", "其他", "購電", "小水力")
SHARED_TYPES = frozenset({"風力", "太陽能", "其它再生能源", "汽電共生"})


def plant_aliases(plants_csv: Path) -> dict[int, tuple[str, ...]]:
    aliases: dict[int, tuple[str, ...]] = {}
    with plants_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            names = {part.strip() for part in row["aliases"].split("|") if part.strip()}
            names.add(row["plant_name"].removesuffix("發電廠").removesuffix("電廠"))
            aliases[int(row["plant_id"])] = tuple(sorted(names, key=len, reverse=True))
    return aliases


def _is_bucket(name: str) -> bool:
    return (
        any(hint in name for hint in BUCKET_HINTS) or name == "汽電共生" or name.startswith("電池")
    )


def propose(
    snapshot: dict[str, object], aliases: dict[int, tuple[str, ...]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in snapshot["aaData"]:  # type: ignore[union-attr]
        name = str(raw["機組名稱"]).strip()
        if name.startswith("小計"):
            continue
        unit_type = clean_type(str(raw["機組類型"]))
        row = {
            "unit_type": unit_type,
            "unit_name": name,
            "grain": "bucket" if _is_bucket(name) else "unit",
            "access_scope": "",
            "plant_id": "",
            "note": "",
        }
        if unit_type in SHARED_TYPES or row["grain"] == "bucket":
            row.update(access_scope="shared", note="候選：不屬於單一電廠")
            rows.append(row)
            continue
        prefix = sorted({pid for pid, names in aliases.items() if name.startswith(names)})
        inner = sorted({pid for pid, names in aliases.items() if any(n in name for n in names)})
        if len(prefix) == 1:
            row.update(access_scope="plant", plant_id=str(prefix[0]), note="候選：名稱前綴命中")
        elif not prefix and len(inner) == 1:
            row.update(
                access_scope="plant", plant_id=str(inner[0]), note="候選：名稱中間命中，請確認"
            )
        else:
            row["note"] = "待人工確認：沒有唯一命中的電廠"
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "snapshot",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "taipower_align/units_generation.json",
    )
    parser.add_argument("--plants", type=Path, default=PROJECT_ROOT / "taipower_align/plants.csv")
    args = parser.parse_args(argv)
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
    writer = csv.DictWriter(sys.stdout, fieldnames=DECISION_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(propose(snapshot, plant_aliases(args.plants)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
