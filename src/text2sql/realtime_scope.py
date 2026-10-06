"""Which realtime units an identity may read (RT-3a spec §5).

純函式：不讀資料庫、不看 HTTP。網頁的即時面板（RT-3a）和之後的即時查詢守門（RT-3b）共用這一份，
兩條路徑的權限不會分岔。機組鍵與 `v_rt_*` 檢視的「機組鍵」相同：`機組類型|機組名稱`。

`undecided` 的機組對電廠帳號一律看不到（RT-1 規格 §8.4）：歸屬沒有人確認過，不能先算給任何一座廠。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RealtimeScope:
    kind: Literal["all", "plant"]
    plant_id: int | None = None
    plant_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("all", "plant"):
            raise ValueError(f"不支援的即時資料範圍：{self.kind!r}")
        if self.kind == "all" and (self.plant_id is not None or self.plant_name is not None):
            raise ValueError("全範圍不應該綁定電廠。")
        if self.kind == "plant" and (self.plant_id is None or not self.plant_name):
            raise ValueError("電廠範圍必須同時有電廠編號與名稱。")

    @property
    def label(self) -> str:
        return "all" if self.kind == "all" else f"plant:{self.plant_name}"


ALL_PLANTS = RealtimeScope("all")


@dataclass(frozen=True)
class UnitRow:
    key: str  # 檢視的「機組鍵」：機組類型|機組名稱
    access_scope: str  # plant / shared / undecided
    plant_id: int | None


def _owned(scope: RealtimeScope, unit: UnitRow) -> bool:
    return unit.access_scope == "plant" and unit.plant_id == scope.plant_id


def visible_unit_keys(scope: RealtimeScope, units: Iterable[UnitRow]) -> frozenset[str] | None:
    """Keys the scope may read; None means every unit, including undecided ones."""

    if scope.kind == "all":
        return None
    return frozenset(
        unit.key for unit in units if unit.access_scope == "shared" or _owned(scope, unit)
    )


def own_unit_keys(scope: RealtimeScope, units: Iterable[UnitRow]) -> frozenset[str]:
    """Keys that belong to the scope's own plant; shared rows are not counted as owned."""

    if scope.kind == "all":
        return frozenset()
    return frozenset(unit.key for unit in units if _owned(scope, unit))
