"""Shared helpers for the realtime collector tests (not a test module)."""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import time
from pathlib import Path

from ingest.realtime.config import RealtimeConfig, ScheduleConfig, SourceConfig, ValidationConfig

FIXTURES = Path(__file__).parent / "fixtures" / "realtime"
SNAPSHOT = FIXTURES / "d006001_2026-09-18T2140.json"
KNOWN_TYPES = frozenset(
    {
        "燃氣",
        "民營電廠-燃氣",
        "燃煤",
        "民營電廠-燃煤",
        "汽電共生",
        "燃料油",
        "太陽能",
        "風力",
        "水力",
        "儲能",
        "其它再生能源",
        "儲能負載",
        "核能",
    }
)
PLANTS_CSV = (
    "plant_id,plant_name,plant_type,fuel_types,ownership,aliases,source_urls\n"
    "8,大潭發電廠,火力,天然氣,台電,大潭,https://data.gov.tw/dataset/8934\n"
    "12,明潭發電廠,水力,水,台電,明潭,https://data.gov.tw/dataset/8934\n"
)


def make_config(tmp_path: Path, **overrides: object) -> RealtimeConfig:
    """A complete config whose paths all live under tmp_path."""
    plants = tmp_path / "plants.csv"
    if not plants.exists():
        plants.write_text(PLANTS_CSV, encoding="utf-8")
    config = RealtimeConfig(
        source=SourceConfig(
            url="https://example.invalid/d006001/001.json", timeout_seconds=20, max_bytes=1_000_000
        ),
        schedule=ScheduleConfig(
            slot_minutes=10,
            first_probe_seconds=320,
            retry_seconds=60,
            backoff_after_failures=(10, 20),
            backoff_seconds=(120, 300),
            retry_after_cap_seconds=1800,
            loop_max_sleep_seconds=30,
            resume_gap_seconds=120,
        ),
        validation=ValidationConfig(
            future_tolerance_seconds=900,
            subtotal_tolerance_mw=0.1,
            detail_rows_expected=(100, 400),
            known_types=KNOWN_TYPES,
            unreliable_notes=frozenset({"通訊異常"}),
        ),
        raw_days=14,
        day_closed_after=time(0, 15),
        stale_after_minutes=30,
        database=tmp_path / "processed" / "realtime.db",
        root=tmp_path / "realtime",
        units_csv=tmp_path / "realtime_units.csv",
        plants_csv=plants,
        log_dir=tmp_path / "logs",
    )
    return dataclasses.replace(config, **overrides) if overrides else config


REALTIME_YAML = """\
source:
  url: https://example.invalid/d006001/001.json
  timeout_seconds: 20
  max_bytes: 1000000
schedule:
  slot_minutes: 10
  first_probe_seconds: 320
  retry_seconds: 60
  backoff_after_failures: [10, 20]
  backoff_seconds: [120, 300]
  retry_after_cap_seconds: 1800
  loop_max_sleep_seconds: 30
  resume_gap_seconds: 120
validation:
  future_tolerance_seconds: 900
  subtotal_tolerance_mw: 0.1
  detail_rows_expected: [100, 400]
  known_types: [燃氣, 民營電廠-燃氣, 燃煤, 民營電廠-燃煤, 汽電共生, 燃料油,
                太陽能, 風力, 水力, 儲能, 其它再生能源, 儲能負載, 核能]
  unreliable_notes: [通訊異常]
retention:
  raw_days: 14
rollup:
  day_closed_after: "00:15"
status:
  stale_after_minutes: 30
"""


def write_project(root: Path) -> Path:
    """A minimal project root that `load_config(root)` can read."""
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "configs/realtime.yaml").write_text(REALTIME_YAML, encoding="utf-8")
    (root / "configs/config.yaml").write_text(
        "paths:\n"
        "  plants_csv: taipower_align/plants.csv\n"
        "  realtime_database: data/processed/realtime.db\n"
        "  realtime_root: data/realtime\n"
        "  realtime_units_csv: taipower_align/realtime_units.csv\n",
        encoding="utf-8",
    )
    (root / "taipower_align").mkdir(exist_ok=True)
    (root / "taipower_align/plants.csv").write_text(PLANTS_CSV, encoding="utf-8")
    return root


def snapshot_payload() -> dict[str, object]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8-sig"))


def payload_bytes(
    payload: dict[str, object] | None = None, *, data_time: str | None = None
) -> bytes:
    """Serialise the real snapshot (or a modified copy); data_time like '2026-09-24T08:50:00'."""
    data = copy.deepcopy(payload if payload is not None else snapshot_payload())
    if data_time is not None:
        data["DateTime"] = data_time
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def tiny_payload(data_time: str, rows: list[tuple[str, str, str, str, str]]) -> bytes:
    """A minimal payload; each row is (機組類型, 機組名稱, 裝置容量, 淨發電量, 備註)."""
    return json.dumps(
        {
            "DateTime": data_time,
            "aaData": [
                {
                    "機組類型": unit_type,
                    "機組名稱": name,
                    "裝置容量(MW)": capacity,
                    "淨發電量(MW)": net,
                    "淨發電量/裝置容量比(%)": "-",
                    "備註": note,
                }
                for unit_type, name, capacity, net, note in rows
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
