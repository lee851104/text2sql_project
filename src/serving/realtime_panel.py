"""Realtime generation panel on the overview page (RT-3a spec §3–§4).

面板不經過 Text2SQL：SQL 全部寫死，沒有任何使用者輸入進入 SQL。每次請求唯讀開檔、讀完就關，
不常駐持有 realtime.db；收集器更新資料之後，網頁服務不必重啟。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ingest.realtime.config import RealtimeConfig, load_config
from ingest.realtime.status import read_status
from ingest.validate import PROJECT_ROOT

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
