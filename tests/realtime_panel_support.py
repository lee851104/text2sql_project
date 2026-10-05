"""Small realtime.db files for the panel tests (not a test module).

直接寫進 RT-1 的資料表，不經過解析器：每個測試都能精確控制機組的歸屬、數值狀態與缺值時段。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ingest.realtime import store
from ingest.realtime.config import RealtimeConfig

NOW = datetime(2026, 10, 5, 7, 27, 24, tzinfo=UTC)  # 台灣 15:27:24，最新時段 15:20 落後 7.4 分鐘
PLANTS = {8: "大潭發電廠", 12: "明潭發電廠"}

# (淨發電量 MW, 裝置容量 MW, value_status)
Reading = tuple[float | None, float | None, str]


@dataclass(frozen=True)
class Unit:
    unit_type: str
    unit_name: str
    access_scope: str  # plant / shared / undecided
    plant_id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.unit_type}|{self.unit_name}"


def standard_units(datan: int = 8, mingtan: int = 12) -> tuple[Unit, ...]:
    return (
        Unit("燃氣", "大潭#1", "plant", datan),
        Unit("燃氣", "大潭#2", "plant", datan),
        Unit("燃氣", "興達#1", "undecided"),
        Unit("太陽能", "太陽能購電", "shared"),
        Unit("水力", "明潭#1", "plant", mingtan),
        Unit("儲能", "大潭儲能", "plant", datan),
        Unit("儲能負載", "大潭儲能", "plant", datan),
    )


# 15:10 整個時段沒抓到；15:00 只有燃氣；昨天 23:50 的值不能算進今天。
SNAPSHOTS: dict[str, dict[str, Reading]] = {
    "2026-10-04 23:50": {"燃氣|大潭#1": (400.0, 500.0, "ok")},
    "2026-10-05 15:00": {
        "燃氣|大潭#1": (470.0, 500.0, "ok"),
        "燃氣|大潭#2": (490.0, 500.0, "ok"),
        "燃氣|興達#1": (310.0, 550.0, "ok"),
    },
    "2026-10-05 15:20": {
        "燃氣|大潭#1": (480.0, 500.0, "ok"),
        "燃氣|大潭#2": (None, 500.0, "comm_error"),
        "燃氣|興達#1": (300.0, 550.0, "ok"),
        "太陽能|太陽能購電": (7731.1, 15000.0, "ok"),
        "水力|明潭#1": (250.0, 270.0, "ok"),
        "儲能|大潭儲能": (40.0, 60.0, "ok"),
        "儲能負載|大潭儲能": (25.0, 60.0, "ok"),
    },
}


def build_realtime_database(
    config: RealtimeConfig,
    *,
    plants: Mapping[int, str],
    units: Sequence[Unit],
    snapshots: Mapping[str, Mapping[str, Reading]],
    warnings: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> None:
    """Create config.database with the RT-1 schema and exactly these rows."""

    warnings = warnings or {}
    connection = store.connect(config.database)
    try:
        with store.transaction(connection):
            store.create_schema(connection)
            store.sync_plants(connection, plants)
            ids: dict[str, int] = {}
            for unit in units:
                cursor = connection.execute(
                    """INSERT INTO dim_rt_unit
                           (unit_type, unit_type_raw, unit_name, flow, grain, access_scope,
                            plant_id, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        unit.unit_type,
                        unit.unit_type,
                        unit.unit_name,
                        "storage_load" if unit.unit_type == "儲能負載" else "generation",
                        "undecided" if unit.access_scope == "undecided" else "unit",
                        unit.access_scope,
                        unit.plant_id,
                        "2026-10-04 00:00",
                        "2026-10-05 15:20",
                    ),
                )
                ids[unit.key] = int(cursor.lastrowid)
            for data_time, readings in snapshots.items():
                notes = list(warnings.get(data_time, ()))
                connection.execute(
                    """INSERT INTO fact_rt_snapshot
                           (data_time, sha256, fetched_at, detail_rows, quality, warnings)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        data_time,
                        "0" * 64,
                        "2026-10-05T07:25:30+00:00",
                        len(readings),
                        "warn" if notes else "ok",
                        json.dumps(notes, ensure_ascii=False),
                    ),
                )
                for key, (net, capacity, value_status) in readings.items():
                    connection.execute(
                        """INSERT INTO fact_rt_unit_10min
                               (data_time, unit_id, net_mw, capacity_mw, note, value_status)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            data_time,
                            ids[key],
                            net,
                            capacity,
                            "通訊異常" if value_status == "comm_error" else "",
                            value_status,
                        ),
                    )
    finally:
        connection.close()


def build_standard_database(
    config: RealtimeConfig,
    *,
    plants: Mapping[int, str] = PLANTS,
    units: Sequence[Unit] | None = None,
    warnings: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> None:
    build_realtime_database(
        config,
        plants=plants,
        units=units if units is not None else standard_units(),
        snapshots=SNAPSHOTS,
        warnings=warnings,
    )
