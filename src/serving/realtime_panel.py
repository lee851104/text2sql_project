"""Realtime generation panel on the overview page (RT-3a spec §3–§4).

面板不經過 Text2SQL：SQL 全部寫死，沒有任何使用者輸入進入 SQL。每次請求唯讀開檔、讀完就關，
不常駐持有 realtime.db；收集器更新資料之後，網頁服務不必重啟。
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ingest.realtime.config import RealtimeConfig, load_config
from ingest.realtime.maintenance import day_slots
from ingest.realtime.status import read_status
from ingest.realtime.timeutil import local_date
from ingest.validate import PROJECT_ROOT
from text2sql.realtime_scope import RealtimeScope, UnitRow, own_unit_keys, visible_unit_keys

LOGGER = logging.getLogger(__name__)
PUBLIC_STATUS_FIELDS = (
    "available",
    "state",
    "collector_running",
    "latest_data_time",
    "lag_minutes",
)
UNAVAILABLE: dict[str, object] = {"available": False, "state": "unavailable"}
# load_config 可能遇到的設定錯誤：檔案不在、YAML 壞掉、缺鍵、型別不對。
# RealtimeConfigError 是 ValueError 的子類別。
CONFIG_ERRORS = (OSError, ValueError, KeyError, TypeError, yaml.YAMLError)
OK = "正常"  # v_rt_10min 的「數值狀態」；只有這種值會加進總數

# (機組鍵, 機組類型, 裝置容量_MW, 淨發電量_MW, 數值狀態)
Row = tuple[str, str, float | None, float | None, str]


class RealtimeReadError(RuntimeError):
    """realtime.db 存在但讀不出來，例如收集器正在重建。"""


class RealtimeScopeMismatch(RuntimeError):
    """realtime.db 與 power.db 的電廠名冊對不上（RT-1 規格 §8.4）。"""


class RealtimePanel:
    def __init__(self, config: RealtimeConfig | None) -> None:
        self.config = config

    @classmethod
    def from_project(cls, root: Path = PROJECT_ROOT) -> RealtimePanel:
        try:
            return cls(load_config(root))
        except CONFIG_ERRORS:
            # 設定壞掉只讓面板顯示「尚無即時資料」，整個網頁服務照常啟動（規格 §7）。
            LOGGER.exception("即時收集設定讀取失敗，即時發電面板停用")
            return cls(None)

    def status(self, *, now: datetime | None = None) -> dict[str, object]:
        """The `realtime` field of /api/health: state only, never generation numbers."""

        report = self._read_status(now or datetime.now(UTC))
        if report is None or not report.get("available"):
            return dict(UNAVAILABLE)
        return {field: report.get(field) for field in PUBLIC_STATUS_FIELDS}

    def _read_status(self, now: datetime) -> dict[str, object] | None:
        if self.config is None:
            return None
        try:
            return read_status(config=self.config, now=now)
        except Exception:
            # /api/health 絕不能因為即時資料出錯（規格 §4.2）。read_status 只接住 SQLite 錯誤，
            # 其他意外（例如鎖檔或警告欄位讀不出來）在這裡收斂成「無資料」，並留下紀錄。
            LOGGER.exception("讀取即時收集狀態失敗")
            return None

    def overview(
        self,
        scope: RealtimeScope,
        *,
        plants: Mapping[int, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Panel numbers for one scope.

        `plants` 是 power.db 的電廠對照表（plant_id → 名稱），電廠範圍必須提供，用來比對
        realtime.db 的 dim_rt_plant。realtime.db 存在但讀不出來時丟 RealtimeReadError。
        """

        now = now or datetime.now(UTC)
        if self.config is None or not self.config.database.is_file():
            return dict(UNAVAILABLE)
        report = self._read_status(now)
        if report is None or not report.get("available"):
            raise RealtimeReadError("即時收集狀態讀不出來。")
        latest = report.get("latest_data_time")
        if latest is None:
            return {"available": False, "state": report["state"]}
        try:
            uri = f"{self.config.database.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as error:
            raise RealtimeReadError(str(error)) from error
        try:
            connection.execute("PRAGMA query_only = ON")
            return self._overview(
                connection,
                scope,
                plants,
                report,
                str(latest),
                now,
                self.config.schedule.slot_minutes,
            )
        except sqlite3.Error as error:
            LOGGER.exception("讀取 realtime.db 失敗")
            raise RealtimeReadError(str(error)) from error
        finally:
            connection.close()

    def _overview(
        self,
        connection: sqlite3.Connection,
        scope: RealtimeScope,
        plants: Mapping[int, str] | None,
        report: Mapping[str, object],
        latest: str,
        now: datetime,
        slot_minutes: int,
    ) -> dict[str, object]:
        units = [
            UnitRow(key, access_scope, plant_id)
            for key, access_scope, plant_id in connection.execute(
                "SELECT unit_type || '|' || unit_name, access_scope, plant_id FROM dim_rt_unit"
            )
        ]
        if scope.kind == "plant":
            _check_plants(connection, plants)
        visible = visible_unit_keys(scope, units)
        own = own_unit_keys(scope, units) if scope.kind == "plant" else None
        # 最新時段取自同一次 read_status，數字、資料時間與落後分鐘數才會是同一格。
        rows: list[Row] = [
            row
            for row in connection.execute(
                """SELECT "機組鍵", "機組類型", "裝置容量_MW", "淨發電量_MW", "數值狀態"
                     FROM v_rt_10min WHERE "資料時間" = ?""",
                (latest,),
            )
            if visible is None or row[0] in visible
        ]
        by_type = _by_type(rows, own)
        today = local_date(now).isoformat()
        trend = _today(
            connection,
            today,
            latest,
            visible,
            [str(entry["type"]) for entry in by_type],
            slot_minutes,
        )
        counts = report["today"]
        return {
            "available": True,
            "data_time": latest,
            "state": report["state"],
            "lag_minutes": report["lag_minutes"],
            "quality": report["latest_quality"],
            "scope": scope.label,
            "by_type": by_type,
            "today": {
                **trend,
                "elapsed_slots": counts["elapsed_slots"],
                "snapshots": counts["snapshots"],
            },
            "disclosures": _disclosures(report, scope, latest, today),
        }


def _check_plants(connection: sqlite3.Connection, plants: Mapping[int, str] | None) -> None:
    """Refuse a plant scope when the two plant rosters disagree (RT-1 spec §8.4).

    兩邊都來自 taipower_align/plants.csv，正常一定一致；收集器換了名冊而網頁還用舊名冊時，
    同一個編號可能已經是另一座廠，不能把別廠的機組算給這個帳號。
    """

    realtime = {
        int(plant_id): str(name)
        for plant_id, name in connection.execute("SELECT plant_id, plant_name FROM dim_rt_plant")
    }
    if plants is None or realtime != dict(plants):
        LOGGER.error(
            "即時資料與查詢資料庫的電廠名冊不一致：realtime.db %d 座，power.db %s",
            len(realtime),
            "未提供" if plants is None else f"{len(plants)} 座",
        )
        raise RealtimeScopeMismatch(
            "realtime.db 的 dim_rt_plant 與 power.db 的 dim_plant_scope 不一致。"
        )


def _sum_ok(rows: Sequence[Row]) -> float | None:
    """Sum the normal values; None when there are none (a gap is not zero)."""

    values = [row[3] for row in rows if row[4] == OK and row[3] is not None]
    return round(sum(values), 1) if values else None


def _total(entry: Mapping[str, object]) -> float:
    numbers = [
        value
        for name in ("net_mw", "own_net_mw", "shared_net_mw")
        if isinstance(value := entry.get(name), float)
    ]
    return sum(numbers) if numbers else float("-inf")


def _by_type(rows: Sequence[Row], own: frozenset[str] | None) -> list[dict[str, object]]:
    """One entry per unit type; storage and storage load are separate types, never netted."""

    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        groups[row[1]].append(row)
    entries: list[dict[str, object]] = []
    for unit_type, members in groups.items():
        entry: dict[str, object] = {"type": unit_type}
        if own is None:
            entry["net_mw"] = _sum_ok(members)
        else:
            entry["own_net_mw"] = _sum_ok([row for row in members if row[0] in own])
            entry["shared_net_mw"] = _sum_ok([row for row in members if row[0] not in own])
        capacities = [row[2] for row in members if row[2] is not None]
        entry["capacity_mw"] = round(sum(capacities), 1) if capacities else None
        entry["units"] = len(members)
        entry["unreliable_units"] = sum(1 for row in members if row[4] != OK)
        entries.append(entry)
    entries.sort(key=lambda entry: (-_total(entry), str(entry["type"])))
    return entries


def _today(
    connection: sqlite3.Connection,
    today: str,
    latest: str,
    visible: frozenset[str] | None,
    order: Sequence[str],
    slot_minutes: int,
) -> dict[str, object]:
    """Today's per-type series from 00:00 to the latest slot; a missing slot is None."""

    if latest[:10] != today:
        return {"date": today, "slots": [], "series": []}
    slots = [slot for slot in day_slots(today, slot_minutes) if slot <= latest]
    position = {slot: index for index, slot in enumerate(slots)}
    totals: dict[str, list[float | None]] = {}
    for key, unit_type, data_time, net in connection.execute(
        """SELECT "機組鍵", "機組類型", "資料時間", "淨發電量_MW" FROM v_rt_10min
            WHERE "資料時間" BETWEEN ? AND ? AND "數值狀態" = ?""",
        (f"{today} 00:00", latest, OK),
    ):
        if net is None or (visible is not None and key not in visible):
            continue
        index = position.get(data_time)
        if index is None:
            continue
        values = totals.setdefault(unit_type, [None] * len(slots))
        values[index] = (values[index] or 0.0) + net
    types = [unit_type for unit_type in order if unit_type in totals]
    types += sorted(set(totals) - set(types))
    return {
        "date": today,
        "slots": [slot[11:] for slot in slots],
        "series": [
            {
                "type": unit_type,
                "net_mw": [
                    None if value is None else round(value, 1) for value in totals[unit_type]
                ],
            }
            for unit_type in types
        ],
    }


def _disclosures(
    report: Mapping[str, object], scope: RealtimeScope, latest: str, today: str
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    state = report["state"]
    if state == "stale":
        lag = report["lag_minutes"]
        minutes = round(lag) if isinstance(lag, int | float) else "?"
        items.append(
            {"code": "RT_STALE", "reason": f"資料落後 {minutes} 分鐘（最新時段 {latest}）。"}
        )
    if state == "stopped":
        items.append(
            {
                "code": "RT_COLLECTOR_STOPPED",
                "reason": f"收集器目前沒有在執行；數字是停止前的最後一筆（{latest}）。",
            }
        )
    if report.get("latest_quality") == "warn":
        warnings = report.get("latest_warnings") or []
        detail = "；".join(
            f"{item.get('code')}：{item.get('detail')}"
            for item in warnings
            if isinstance(item, Mapping)
        )
        items.append({"code": "RT_QUALITY_WARN", "reason": f"最新快照有驗證警告：{detail}"})
    if scope.kind == "plant":
        items.append(
            {
                "code": "RT_SCOPE_PLANT",
                "reason": (
                    f"只含{scope.plant_name}的機組與跨廠共用（shared）的列；歸屬未定的機組不列入。"
                ),
            }
        )
    if latest[:10] != today:
        items.append(
            {
                "code": "RT_NO_DATA_TODAY",
                "reason": f"最新資料是 {latest}，不是今天（{today}）；今日趨勢是空的。",
            }
        )
    return items
