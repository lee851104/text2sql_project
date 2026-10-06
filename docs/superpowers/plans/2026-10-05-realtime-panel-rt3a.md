# RT-3a 即時發電面板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在網頁「資料總覽」頁加上即時發電區塊（狀態列、各類型總出力、今日趨勢），資料取自 RT-1 收集器的 `realtime.db`，權限與查詢端一致。

**Architecture:** 新增兩個模組。`src/text2sql/realtime_scope.py` 是權限純函式：給定身分範圍，算出看得到哪些機組，RT-3b 會重用。`src/serving/realtime_panel.py` 每次請求唯讀開啟 `realtime.db`，用寫死的 SQL 算出面板資料。`src/serving/app.py` 在 `/api/health` 加上 `realtime` 狀態欄位，並新增 `GET /api/realtime/overview`；「身分 → 範圍」沿用 `/api/query` 的判斷，抽成共用的小函式。前端寫在現有的 `index.html`、`app.js`、`app.css`。面板不經過 Text2SQL，查詢路徑一行都不改。

**Tech Stack:** Python ≥ 3.11 標準函式庫（sqlite3、logging）、FastAPI、PyYAML（只用來接住設定錯誤）；前端 vanilla JS，搭配 `index.html` 已載入的 Plotly（`plotly-basic-4.0.0`）；pytest、ruff。不新增相依套件。

**Spec:** `docs/superpowers/specs/2026-10-05-realtime-panel-design.md`。資料表與檢視的定義在 RT-1 規格 `docs/superpowers/specs/2026-09-24-realtime-ingest-design.md` 和 `src/ingest/realtime/store.py`。執行者請先讀 RT-3a 規格。

## 與規格的差異（寫計畫時發現，Task 6 會同步回規格）

| 規格原文 | 計畫的做法 | 為什麼 |
|---|---|---|
| §5.1 `UnitRow.key: int`，註解寫「`dim_rt_unit.id` = 檢視的『機組鍵』」 | `key: str`，值為 `機組類型\|機組名稱`；`visible_unit_keys` 回 `frozenset[str] \| None` | `store.py` 的 `v_rt_10min` 把「機組鍵」定義成 `u.unit_type \|\| '\|' \|\| u.unit_name`，不是整數 id。RT-3b 要拿這組鍵去篩檢視，型別必須和檢視一致。 |
| §4.1 `RealtimePanel(config, power_database)` | `RealtimePanel(config)`；`overview(scope, *, plants=None, now=None)`，其中 `plants` 是 power.db 的電廠對照表（`plant_id → plant_name`） | power.db 會熱抽換（`current_runtime_snapshot`），建置時存下的路徑會過時。電廠帳號本來就由目前 runtime 的 `ScopeCatalog.plant_names_by_id()` 解析，比對用同一份最一致。 |
| §4.1 設定讀取失敗時「視同無資料」 | `RealtimePanel.from_project(root)` 接住設定錯誤，記 log，回 `RealtimePanel(None)`；`config is None` 時什麼都回 unavailable | 規格 §7 的最後一列。 |
| §4.3 `today` 只有 `date`、`slots`、`series` | 另外帶 `elapsed_slots`、`snapshots`（直接取自 `read_status()` 的 `today`） | §6.1 的狀態列要顯示「今天 92／92 個時段」，需要這兩個數字。 |
| §3 寫「固定 SQL：`v_rt_now`」 | 最新時段用 `v_rt_10min WHERE "資料時間" = ?`，時間取自同一次 `read_status()` 的 `latest_data_time` | 回應裡的 `data_time`、落後分鐘數和數字出自同一個時段。如果用 `v_rt_now`，收集器剛好在兩次讀取之間寫入時，兩者會差一格。 |
| §5.3 範例 `"own_net_mw": 0.0` | 一組機組裡沒有任何「正常」的值時，該數字是 `null`（也適用 `net_mw`、`shared_net_mw`） | 「沒有本廠機組」和「本廠出力 0 MW」意思不同；這和 §4.3「缺值是 null，不是 0」的原則一致。 |
| §6.1 沒提到出處 | 面板底部加一行資料來源；ATTRIBUTION 的「開放資料顯名」加上即時資料集 | 政府資料開放授權條款要求顯名；網頁顯示即時數字之後，出處要寫在數字旁邊。 |

## Global Constraints

- Python ≥ 3.11；ruff `line-length = 100`，lint 規則 `E, F, I, UP, B, SIM`；`uv run ruff format --check .`、`uv run ruff check .` 都要通過。ruff 的 E501 算的是**顯示寬度**，中文一個字算 2 欄；`ruff format` 不會拆字串或註解，計畫裡含中文的長註解、長字串如果超過，請手動拆成兩行或兩段相連的字串，意思不變。
- `ruff format --check .` 也會格式化 Markdown 裡的 Python 區塊：本計畫和規格的程式碼區塊都已經格式化過，類別方法或內部函式的片段因此從第 0 欄開始，貼進程式時要依說明的位置補回縮排。改到任何 `.md` 裡的 Python 區塊後，也要跑一次 `uv run ruff format <檔案>`。
- 不新增相依套件（只用標準函式庫、FastAPI、PyYAML、前端已載入的 Plotly）。
- 時區一律固定 UTC+8，用 `ingest.realtime.timeutil` 的工具；**不用 `zoneinfo`**（部署機的 Windows Python 沒有 IANA 時區資料庫）。
- 給使用者看的訊息、揭露與文件用繁體中文。
- 測試不連網；資料庫建在 `tmp_path`；「現在」一律注入。
- **不改**：`src/text2sql/` 的查詢路徑（只新增 `realtime_scope.py`）、`SqlGuard`、`SemanticGuard`、`ScopeGuard`、`ALLOWED_COLUMNS`、`VIEW_TIME_SPANS`、`configs/coverage.yaml`、`corpus/`、`benchmarks/`、`src/eval/`、`src/ingest/realtime/`。
- 不 commit `data/`、`logs/`、`*.db`。
- 合併門檻只對 `src/serving/static/app.js` 跑 `node --check`，所以前端程式只寫進現有的 `app.js`，不另開 JS 檔。
- 面板不經過 Text2SQL：SQL 全部寫死，沒有任何使用者輸入進入 SQL。
- `/api/health` 絕不因為即時資料出錯；`realtime` 欄位只有狀態，不含任何發電數字。
- commit 訊息結尾附上實際使用模型的 `Co-Authored-By` 行。push 由使用者自己做。

---

## 檔案

| 檔案 | 任務 | 職責 |
|---|---|---|
| `src/text2sql/realtime_scope.py` | 1 | 新增：`RealtimeScope`、`UnitRow`、`visible_unit_keys`、`own_unit_keys` |
| `tests/test_realtime_scope.py` | 1 | 新增：權限矩陣 |
| `tests/realtime_panel_support.py` | 2 | 新增：建小型 `realtime.db` 的測試工具與標準資料 |
| `src/serving/realtime_panel.py` | 2、3 | 新增：`RealtimePanel.status()`、`overview()` |
| `tests/test_realtime_panel.py` | 2、3 | 新增 |
| `src/serving/app.py` | 4 | health 欄位、`/api/realtime/overview`、共用的身分 → 範圍函式、no-store |
| `tests/test_realtime_api.py` | 4、5 | 新增：HTTP 行為；靜態檔檢查 |
| `src/serving/static/index.html`、`app.js`、`app.css` | 5 | 即時發電區塊 |
| `docs/SERVING.md`、`README.md`、`ATTRIBUTION.md`、`docs/SYSTEM_CARD.md`、規格、`log.md` | 6 | 文件與 CP-074 |

---

### Task 1: 開分支、量基準、權限純函式

**Files:**
- Create: `src/text2sql/realtime_scope.py`
- Test: `tests/test_realtime_scope.py`

**Interfaces:**
- Consumes: 無。
- Produces:
  - `RealtimeScope(kind: Literal["all", "plant"], plant_id: int | None = None, plant_name: str | None = None)`：frozen dataclass，建構時驗證，屬性 `label -> str`（`"all"` 或 `"plant:<電廠名稱>"`）。
  - `ALL_PLANTS: RealtimeScope`，等於 `RealtimeScope("all")`。
  - `UnitRow(key: str, access_scope: str, plant_id: int | None)`：frozen dataclass。
  - `visible_unit_keys(scope: RealtimeScope, units: Iterable[UnitRow]) -> frozenset[str] | None`：`None` 代表全部看得到。
  - `own_unit_keys(scope: RealtimeScope, units: Iterable[UnitRow]) -> frozenset[str]`：`all` 範圍回空集合。

- [ ] **Step 1: 開實作分支並量基準**

worktree 是 `C:/Users/User/text2sql_project/.claude/worktrees/realtime-panel`，目前在 `docs/realtime-panel-design`（規格 commit `64018f2`，底下是 RT-1 的 `f61ed66`）。

```bash
git switch -c feat/realtime-panel
uv run pytest -q
```

記下 passed／skipped 數字，回報時附上。Task 6 寫 CP-074 要用。

- [ ] **Step 2: 寫失敗的測試**

`tests/test_realtime_scope.py`：

```python
"""Which realtime units each identity may read (RT-3a spec §5)."""

from __future__ import annotations

import pytest

from text2sql.realtime_scope import (
    ALL_PLANTS,
    RealtimeScope,
    UnitRow,
    own_unit_keys,
    visible_unit_keys,
)

DATAN_GAS = UnitRow("燃氣|大潭#1", "plant", 8)
UNDECIDED_GAS = UnitRow("燃氣|興達#1", "undecided", None)
SHARED_SOLAR = UnitRow("太陽能|太陽能購電", "shared", None)
MINGTAN_HYDRO = UnitRow("水力|明潭#1", "plant", 12)
UNITS = (DATAN_GAS, UNDECIDED_GAS, SHARED_SOLAR, MINGTAN_HYDRO)
DATAN = RealtimeScope("plant", 8, "大潭發電廠")
MINGTAN = RealtimeScope("plant", 12, "明潭發電廠")


def test_the_all_scope_sees_everything_including_undecided_units() -> None:
    assert visible_unit_keys(ALL_PLANTS, UNITS) is None
    assert own_unit_keys(ALL_PLANTS, UNITS) == frozenset()


def test_a_plant_sees_its_own_units_and_every_shared_row() -> None:
    assert visible_unit_keys(DATAN, UNITS) == {DATAN_GAS.key, SHARED_SOLAR.key}
    assert visible_unit_keys(MINGTAN, UNITS) == {MINGTAN_HYDRO.key, SHARED_SOLAR.key}


@pytest.mark.parametrize("plant_id", [8, 12, 999])
def test_undecided_units_are_never_shown_to_a_plant(plant_id: int) -> None:
    scope = RealtimeScope("plant", plant_id, "某電廠")

    assert UNDECIDED_GAS.key not in visible_unit_keys(scope, UNITS)
    assert UNDECIDED_GAS.key not in own_unit_keys(scope, UNITS)


def test_own_units_exclude_shared_rows() -> None:
    assert own_unit_keys(DATAN, UNITS) == {DATAN_GAS.key}
    assert own_unit_keys(MINGTAN, UNITS) == {MINGTAN_HYDRO.key}


def test_a_plant_with_no_units_sees_only_shared_rows() -> None:
    scope = RealtimeScope("plant", 999, "沒有機組的電廠")

    assert visible_unit_keys(scope, UNITS) == {SHARED_SOLAR.key}
    assert own_unit_keys(scope, UNITS) == frozenset()


def test_units_can_be_any_iterable() -> None:
    assert visible_unit_keys(DATAN, iter(UNITS)) == {DATAN_GAS.key, SHARED_SOLAR.key}


@pytest.mark.parametrize(
    ("kind", "plant_id", "plant_name"),
    [
        ("plant", None, None),
        ("plant", 8, None),
        ("plant", None, "大潭發電廠"),
        ("all", 8, None),
        ("all", None, "大潭發電廠"),
        ("region", None, None),
    ],
)
def test_an_inconsistent_scope_is_refused(
    kind: str, plant_id: int | None, plant_name: str | None
) -> None:
    with pytest.raises(ValueError):
        RealtimeScope(kind, plant_id, plant_name)  # type: ignore[arg-type]


def test_the_scope_label_names_the_plant() -> None:
    assert ALL_PLANTS.label == "all"
    assert DATAN.label == "plant:大潭發電廠"
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_realtime_scope.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'text2sql.realtime_scope'`

- [ ] **Step 4: 實作**

`src/text2sql/realtime_scope.py`：

```python
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
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_realtime_scope.py -q`
Expected: PASS（15 項，含 parametrize 展開）

Run: `uv run ruff format --check src/text2sql/realtime_scope.py tests/test_realtime_scope.py; uv run ruff check src/text2sql/realtime_scope.py tests/test_realtime_scope.py`
Expected: 無錯誤（格式不符就跑 `uv run ruff format <檔案>`）。

- [ ] **Step 6: Commit**

```bash
git add src/text2sql/realtime_scope.py tests/test_realtime_scope.py
git commit -m "feat: decide which realtime units each identity may read"
```

---

### Task 2: 面板狀態與測試用資料庫

**Files:**
- Create: `tests/realtime_panel_support.py`
- Create: `src/serving/realtime_panel.py`
- Test: `tests/test_realtime_panel.py`

**Interfaces:**
- Consumes:
  - `ingest.realtime.status.read_status(root=..., *, now: datetime | None, config: RealtimeConfig | None) -> dict[str, object]`：沒有資料庫時回 `{"available": False, "state": "unavailable", ...}`。有資料庫時鍵包含 `available`、`state`、`collector_running`、`latest_data_time`、`lag_minutes`、`today: {"elapsed_slots", "snapshots"}`、`latest_quality`、`latest_warnings`（`[{"code", "detail"}]`）。遇到 SQLite 錯誤時回 `available: False`，並附 `error` 鍵。
  - `ingest.realtime.config.load_config(root) -> RealtimeConfig`（`database`、`lock_path`、`schedule.slot_minutes`）。
  - `ingest.realtime.store`：`connect`、`transaction`、`create_schema`、`sync_plants`。
  - `ingest.realtime.lock.SingleInstanceLock(path)`：測試用它模擬收集器在跑。
  - 測試工具 `tests/realtime_support.py` 的 `make_config(tmp_path)`、`write_project(root)`。
- Produces:
  - `serving.realtime_panel.RealtimePanel(config: RealtimeConfig | None)`；屬性 `config`。
  - `RealtimePanel.from_project(root: Path = PROJECT_ROOT) -> RealtimePanel`。
  - `RealtimePanel.status(*, now: datetime | None = None) -> dict[str, object]`。
  - `PUBLIC_STATUS_FIELDS: tuple[str, ...]`；`RealtimeReadError`、`RealtimeScopeMismatch`（兩者都繼承 `RuntimeError`，Task 3 開始使用）。
  - 測試工具 `tests/realtime_panel_support.py`：`Unit`、`Reading`、`NOW`、`PLANTS`、`SNAPSHOTS`、`standard_units(datan=8, mingtan=12)`、`build_realtime_database(...)`、`build_standard_database(...)`。

- [ ] **Step 1: 寫測試用資料庫工具**

`tests/realtime_panel_support.py`：

```python
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
```

- [ ] **Step 2: 寫失敗的測試**

`tests/test_realtime_panel.py`：

```python
"""RealtimePanel: the realtime block on the overview page (RT-3a spec §4, §8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from realtime_panel_support import NOW, build_standard_database
from realtime_support import make_config, write_project

from ingest.realtime.lock import SingleInstanceLock
from serving import realtime_panel
from serving.realtime_panel import PUBLIC_STATUS_FIELDS, RealtimePanel

UNAVAILABLE = {"available": False, "state": "unavailable"}


def _panel(tmp_path: Path, **build: object) -> RealtimePanel:
    config = make_config(tmp_path)
    build_standard_database(config, **build)
    return RealtimePanel(config)


# ── 狀態（/api/health 的 realtime 欄位）──────────────────────────────────


def test_status_without_a_database_is_unavailable(tmp_path: Path) -> None:
    assert RealtimePanel(make_config(tmp_path)).status(now=NOW) == UNAVAILABLE


def test_status_shows_the_state_but_no_generation_numbers(tmp_path: Path) -> None:
    panel = _panel(tmp_path)

    with SingleInstanceLock(panel.config.lock_path):
        status = panel.status(now=NOW)

    assert set(status) == set(PUBLIC_STATUS_FIELDS)
    assert status == {
        "available": True,
        "state": "healthy",
        "collector_running": True,
        "latest_data_time": "2026-10-05 15:20",
        "lag_minutes": 7.4,
    }


def test_status_reports_a_stopped_collector(tmp_path: Path) -> None:
    status = _panel(tmp_path).status(now=NOW)

    assert status["state"] == "stopped"
    assert status["collector_running"] is False


def test_status_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _panel(tmp_path)

    def broken(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(realtime_panel, "read_status", broken)

    assert panel.status(now=NOW) == UNAVAILABLE


def test_a_panel_without_settings_shows_no_data() -> None:
    assert RealtimePanel(None).status(now=NOW) == UNAVAILABLE


def test_from_project_reads_the_project_settings(tmp_path: Path) -> None:
    root = write_project(tmp_path)

    panel = RealtimePanel.from_project(root)

    assert panel.config is not None
    assert panel.config.database == root.resolve() / "data/processed/realtime.db"


def test_a_broken_realtime_config_does_not_stop_the_service(tmp_path: Path) -> None:
    root = write_project(tmp_path)
    (root / "configs/realtime.yaml").write_text("source: [\n", encoding="utf-8")

    panel = RealtimePanel.from_project(root)

    assert panel.config is None
    assert panel.status(now=NOW) == UNAVAILABLE
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_realtime_panel.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'serving.realtime_panel'`

- [ ] **Step 4: 實作**

`src/serving/realtime_panel.py`：

```python
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
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_realtime_panel.py -q`
Expected: PASS（7 項）

Run: `uv run ruff format --check src/serving/realtime_panel.py tests/realtime_panel_support.py tests/test_realtime_panel.py; uv run ruff check src/serving/realtime_panel.py tests/realtime_panel_support.py tests/test_realtime_panel.py`
Expected: 無錯誤（import 排序不符就跑 `uv run ruff check --fix <檔案>`）。

- [ ] **Step 6: Commit**

```bash
git add src/serving/realtime_panel.py tests/realtime_panel_support.py tests/test_realtime_panel.py
git commit -m "feat: report the realtime collector state for the web service"
```

---

### Task 3: 面板數字（overview）

**Files:**
- Modify: `src/serving/realtime_panel.py`
- Test: `tests/test_realtime_panel.py`（加測試、換掉 import 區塊）

**Interfaces:**
- Consumes:
  - Task 1 的 `RealtimeScope`、`ALL_PLANTS`、`UnitRow`、`visible_unit_keys`、`own_unit_keys`。
  - Task 2 的 `RealtimePanel`、`_read_status`、`UNAVAILABLE`、`RealtimeReadError`、`RealtimeScopeMismatch`，以及測試工具。
  - `ingest.realtime.maintenance.day_slots(day: str, slot_minutes: int) -> list[str]`：回傳 `"YYYY-MM-DD HH:MM"` 字串。
  - `ingest.realtime.timeutil.local_date(moment) -> date`：台灣日期。
  - 檢視 `v_rt_10min` 的欄位：`"資料時間"`、`"機組鍵"`、`"機組類型"`、`"裝置容量_MW"`、`"淨發電量_MW"`、`"數值狀態"`（值為 `正常`、`無值`、`無法解析`、`通訊異常`）。
- Produces:
  - `RealtimePanel.overview(scope: RealtimeScope, *, plants: Mapping[int, str] | None = None, now: datetime | None = None) -> dict[str, object]`：
    - 沒有設定或沒有 `realtime.db` 時，回 `{"available": False, "state": "unavailable"}`。
    - 有資料庫但還沒有快照時，回 `{"available": False, "state": <read_status 的 state>}`。
    - 有資料時，回 `available`、`data_time`、`state`、`lag_minutes`、`quality`、`scope`、`by_type`、`today`、`disclosures`。
    - `today` 包含 `date`、`slots`、`series`、`elapsed_slots`、`snapshots`。
    - `by_type` 的每一項：`all` 範圍是 `{type, net_mw, capacity_mw, units, unreliable_units}`；`plant` 範圍把 `net_mw` 換成 `own_net_mw` 與 `shared_net_mw`。
  - 例外：`realtime.db` 存在但讀不出來時丟 `RealtimeReadError`；電廠範圍的 `plants` 是 `None`、或與 `dim_rt_plant` 不完全相同時丟 `RealtimeScopeMismatch`。

- [ ] **Step 1: 換掉測試檔的 import 區塊、加上共用小工具**

把 `tests/test_realtime_panel.py` 開頭的 import 區塊到 `UNAVAILABLE = ...` 那一行整段換成：

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from realtime_panel_support import (
    NOW,
    PLANTS,
    Unit,
    build_realtime_database,
    build_standard_database,
)
from realtime_support import make_config, write_project

from ingest.realtime.lock import SingleInstanceLock
from serving import realtime_panel
from serving.realtime_panel import (
    PUBLIC_STATUS_FIELDS,
    RealtimePanel,
    RealtimeReadError,
    RealtimeScopeMismatch,
)
from text2sql.realtime_scope import ALL_PLANTS, RealtimeScope

UNAVAILABLE = {"available": False, "state": "unavailable"}
DATAN = RealtimeScope("plant", 8, "大潭發電廠")
MINGTAN = RealtimeScope("plant", 12, "明潭發電廠")
```

接著在 `_panel` 函式下方加：

```python
def _running(
    panel: RealtimePanel,
    scope: RealtimeScope = ALL_PLANTS,
    *,
    now: datetime = NOW,
    plants: dict[int, str] | None = PLANTS,
) -> dict:
    """Overview while the collector holds its lock (state healthy unless the data is old)."""

    with SingleInstanceLock(panel.config.lock_path):
        return panel.overview(scope, plants=plants, now=now)


def _entry(data: dict, unit_type: str) -> dict:
    return next(entry for entry in data["by_type"] if entry["type"] == unit_type)


def _series(data: dict, unit_type: str) -> list:
    return next(item["net_mw"] for item in data["today"]["series"] if item["type"] == unit_type)


def _codes(data: dict) -> list[str]:
    return [item["code"] for item in data["disclosures"]]
```

- [ ] **Step 2: 在檔尾加上失敗的測試**

```python
# ── 全範圍的數字 ───────────────────────────────────────────────────────────


def test_overview_header_fields(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path))

    assert {key: data[key] for key in ("available", "data_time", "state", "lag_minutes")} == {
        "available": True,
        "data_time": "2026-10-05 15:20",
        "state": "healthy",
        "lag_minutes": 7.4,
    }
    assert (data["quality"], data["scope"], data["disclosures"]) == ("ok", "all", [])


def test_totals_count_only_normal_values_and_report_the_rest(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path))

    # 480 + 300（歸屬未定的機組也算）；通訊異常的大潭#2 不加，但容量照加、另計一台
    assert _entry(data, "燃氣") == {
        "type": "燃氣",
        "net_mw": 780.0,
        "capacity_mw": 1550.0,
        "units": 3,
        "unreliable_units": 1,
    }


def test_storage_and_storage_load_stay_separate_and_types_sort_by_output(
    tmp_path: Path,
) -> None:
    data = _running(_panel(tmp_path))

    assert [entry["type"] for entry in data["by_type"]] == [
        "太陽能",
        "燃氣",
        "水力",
        "儲能",
        "儲能負載",
    ]
    assert _entry(data, "儲能")["net_mw"] == 40.0
    assert _entry(data, "儲能負載")["net_mw"] == 25.0


def test_missing_capacity_is_not_zero_and_an_all_unreliable_type_has_no_total(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    biogas = Unit("其它再生能源", "沼氣", "shared")
    wind = Unit("風力", "離岸#1", "shared")
    build_realtime_database(
        config,
        plants=PLANTS,
        units=(biogas, wind),
        snapshots={
            "2026-10-05 15:20": {
                biogas.key: (5.0, None, "ok"),
                wind.key: (None, 100.0, "comm_error"),
            }
        },
    )

    data = _running(RealtimePanel(config))

    assert _entry(data, "其它再生能源")["capacity_mw"] is None
    assert _entry(data, "風力") == {
        "type": "風力",
        "net_mw": None,
        "capacity_mw": 100.0,
        "units": 1,
        "unreliable_units": 1,
    }


def test_the_today_trend_marks_gaps_as_null_not_zero(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path))
    today = data["today"]

    assert today["date"] == "2026-10-05"
    assert len(today["slots"]) == 93
    assert (today["slots"][0], today["slots"][-1]) == ("00:00", "15:20")
    gas = _series(data, "燃氣")
    assert gas[:90] == [None] * 90  # 昨天 23:50 的值沒有算進今天
    assert (gas[90], gas[91], gas[92]) == (1270.0, None, 780.0)  # 15:10 沒抓到
    solar = _series(data, "太陽能")
    assert (solar[90], solar[92]) == (None, 7731.1)  # 15:00 沒有太陽能的值
    assert [item["type"] for item in today["series"]] == [
        "太陽能",
        "燃氣",
        "水力",
        "儲能",
        "儲能負載",
    ]
    assert (today["elapsed_slots"], today["snapshots"]) == (93, 2)


def test_today_follows_taiwan_time(tmp_path: Path) -> None:
    # UTC 16:30 已經是台灣隔天 00:30
    data = _running(_panel(tmp_path), now=datetime(2026, 10, 5, 16, 30, tzinfo=UTC))

    assert data["today"]["date"] == "2026-10-06"
    assert (data["today"]["slots"], data["today"]["series"]) == ([], [])
    assert "RT_NO_DATA_TODAY" in _codes(data)
    assert _entry(data, "燃氣")["net_mw"] == 780.0  # 最新一筆照常顯示


# ── 揭露 ───────────────────────────────────────────────────────────────────


def test_a_stopped_collector_is_disclosed(tmp_path: Path) -> None:
    data = _panel(tmp_path).overview(ALL_PLANTS, now=NOW)

    assert data["state"] == "stopped"
    assert _codes(data) == ["RT_COLLECTOR_STOPPED"]


def test_stale_data_is_disclosed_with_the_lag(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), now=NOW + timedelta(minutes=40))

    assert data["state"] == "stale"
    assert _codes(data) == ["RT_STALE"]
    assert "47 分鐘" in data["disclosures"][0]["reason"]


def test_a_quality_warning_is_disclosed(tmp_path: Path) -> None:
    warning = {"code": "SUBTOTAL_MISMATCH", "detail": "燃氣 明細加總與小計差 0.5 MW"}

    data = _running(_panel(tmp_path, warnings={"2026-10-05 15:20": [warning]}))

    assert data["quality"] == "warn"
    assert _codes(data) == ["RT_QUALITY_WARN"]
    assert "SUBTOTAL_MISMATCH" in data["disclosures"][0]["reason"]


# ── 沒有資料、讀不到 ───────────────────────────────────────────────────────


def test_overview_without_a_database_is_unavailable(tmp_path: Path) -> None:
    assert RealtimePanel(make_config(tmp_path)).overview(ALL_PLANTS, now=NOW) == UNAVAILABLE
    assert RealtimePanel(None).overview(ALL_PLANTS, now=NOW) == UNAVAILABLE


def test_a_database_without_snapshots_has_no_numbers_yet(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    build_realtime_database(config, plants=PLANTS, units=(), snapshots={})

    assert RealtimePanel(config).overview(ALL_PLANTS, now=NOW) == {
        "available": False,
        "state": "stopped",
    }


def test_an_unreadable_database_raises_a_read_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.database.parent.mkdir(parents=True)
    config.database.write_bytes(b"not a database" * 100)

    with pytest.raises(RealtimeReadError):
        RealtimePanel(config).overview(ALL_PLANTS, now=NOW)


# ── 電廠帳號 ───────────────────────────────────────────────────────────────


def test_a_plant_sees_its_own_units_and_shared_rows_split_in_two(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), DATAN)

    assert data["scope"] == "plant:大潭發電廠"
    assert [entry["type"] for entry in data["by_type"]] == ["太陽能", "燃氣", "儲能", "儲能負載"]
    assert _entry(data, "燃氣") == {
        "type": "燃氣",
        "own_net_mw": 480.0,
        "shared_net_mw": None,
        "capacity_mw": 1000.0,
        "units": 2,
        "unreliable_units": 1,
    }
    assert _entry(data, "太陽能") == {
        "type": "太陽能",
        "own_net_mw": None,
        "shared_net_mw": 7731.1,
        "capacity_mw": 15000.0,
        "units": 1,
        "unreliable_units": 0,
    }
    assert _codes(data) == ["RT_SCOPE_PLANT"]


def test_a_plant_never_sees_undecided_units_or_another_plant(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), DATAN)

    gas = _series(data, "燃氣")
    assert (gas[90], gas[92]) == (960.0, 480.0)  # 不含歸屬未定的興達#1
    assert "水力" not in [item["type"] for item in data["today"]["series"]]


def test_another_plant_sees_only_its_own_units(tmp_path: Path) -> None:
    data = _running(_panel(tmp_path), MINGTAN)

    assert [entry["type"] for entry in data["by_type"]] == ["太陽能", "水力"]
    assert _entry(data, "水力")["own_net_mw"] == 250.0


@pytest.mark.parametrize(
    "plants",
    [None, {8: "大潭發電廠", 12: "明潭電廠"}, {8: "大潭發電廠"}],
    ids=["no-roster", "renamed", "missing-plant"],
)
def test_a_plant_scope_refuses_a_roster_that_does_not_match(
    tmp_path: Path, plants: dict[int, str] | None
) -> None:
    with pytest.raises(RealtimeScopeMismatch):
        _running(_panel(tmp_path), DATAN, plants=plants)


def test_the_all_scope_does_not_need_the_roster(tmp_path: Path) -> None:
    assert _running(_panel(tmp_path), plants=None)["available"] is True
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_realtime_panel.py -q`
Expected: Task 2 的 7 項通過，新增的測試失敗，錯誤為 `AttributeError: 'RealtimePanel' object has no attribute 'overview'`。

- [ ] **Step 4: 實作**

在 `src/serving/realtime_panel.py`：

把 import 區塊換成：

```python
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
```

在 `CONFIG_ERRORS = ...` 下方加：

```python
OK = "正常"  # v_rt_10min 的「數值狀態」；只有這種值會加進總數

# (機組鍵, 機組類型, 裝置容量_MW, 淨發電量_MW, 數值狀態)
Row = tuple[str, str, float | None, float | None, str]
```

在 `RealtimePanel` 類別裡、`status` 之後加（兩個方法，整段縮排 4 格放進類別）：

```python
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
        connection = sqlite3.connect(f"{self.config.database.resolve().as_uri()}?mode=ro", uri=True)
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
```

在檔尾（類別之外）加：

```python
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
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_realtime_panel.py tests/test_realtime_scope.py -q`
Expected: PASS（panel 26 項：Task 2 的 7 項＋本任務 19 項，含 parametrize 展開；scope 15 項）

Run: `uv run ruff format --check src/serving/realtime_panel.py tests/test_realtime_panel.py; uv run ruff check src/serving/realtime_panel.py tests/test_realtime_panel.py`
Expected: 無錯誤

- [ ] **Step 6: Commit**

```bash
git add src/serving/realtime_panel.py tests/test_realtime_panel.py
git commit -m "feat: compute the realtime panel totals, today's trend and disclosures"
```

---

### Task 4: 接上網頁服務

**Files:**
- Modify: `src/serving/app.py`（imports、`create_app` 參數與 state、`security_headers`、`health`、`/api/query` 的匿名檢查、新增兩個共用小函式與 `/api/realtime/overview`）
- Test: `tests/test_realtime_api.py`

**Interfaces:**
- Consumes:
  - Task 1 的 `ALL_PLANTS`、`RealtimeScope`。
  - Task 2、3 的 `RealtimePanel`、`.status()`、`.overview(scope, *, plants)`、`RealtimeReadError`、`RealtimeScopeMismatch`、`PUBLIC_STATUS_FIELDS`。
  - `app.py` 裡已經有的：`_query_principal`（沒帶 cookie 回 `None`，帶了但失效回 401）、`resolve_account_plant(principal, service) -> str | None`、`current_runtime()`、`application.state.anonymous_scope`（`all` 或 `denied`）、`application.state.auth_manager.mark_no_store(response)`。
  - `service.pipeline.scope_guard.catalog.plant_names_by_id() -> dict[int, str]`。
- Produces:
  - `create_app(..., realtime_panel: RealtimePanel | None = None)`；`application.state.realtime_panel`。
  - `GET /api/health` 多一個 `realtime` 欄位。
  - `GET /api/realtime/overview` → `{"success": True, "data": <overview>}`。錯誤時回 401、409（detail 含 `RT_SCOPE_MISMATCH`）或 503。

- [ ] **Step 1: 寫失敗的測試**

`tests/test_realtime_api.py`：

```python
"""HTTP surface of the realtime panel (RT-3a spec §4.2–§4.5, §5, §8)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from realtime_panel_support import build_standard_database, standard_units
from realtime_support import make_config

from ingest.build_db import build_database
from serving import realtime_panel
from serving.accounts import MINIMUM_ITERATIONS, Account, hash_password
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.realtime_panel import PUBLIC_STATUS_FIELDS, RealtimePanel
from serving.runtime import build_runtime
from text2sql.scope_guard import ScopeCatalog

pytestmark = pytest.mark.integration

ORIGIN = "http://testserver"
PASSWORD = "realtime-test-password"
ALL_ACCOUNT = "supervisor"
PLANT_ACCOUNT = "datan"
OWN_PLANT = "大潭發電廠"
OTHER_PLANT = "明潭發電廠"


@dataclass(frozen=True)
class Service:
    client: TestClient
    plants: dict[int, str]

    def plant_id(self, name: str) -> int:
        return next(plant_id for plant_id, plant in self.plants.items() if plant == name)


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("realtime-api") / "power.db"
    build_database(database)
    plants = ScopeCatalog.from_database(database).plant_names_by_id()
    own = next(plant_id for plant_id, plant in plants.items() if plant == OWN_PLANT)
    password_hash = hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)
    accounts = [
        Account(username=ALL_ACCOUNT, password_hash=password_hash, plant_id=None),
        Account(
            username=PLANT_ACCOUNT,
            password_hash=password_hash,
            plant_id=own,
            expected_plant_name=OWN_PLANT,
        ),
    ]
    application = create_app(
        build_runtime(database=database),
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
        realtime_panel=RealtimePanel(None),
    )
    with TestClient(application, base_url=ORIGIN) as client:
        yield Service(client, plants)


@pytest.fixture
def client(service: Service):
    client = service.client
    client.app.state.anonymous_scope = "all"
    yield client
    client.cookies.clear()
    client.app.state.anonymous_scope = "all"
    client.app.state.realtime_panel = RealtimePanel(None)


def _install(service: Service, tmp_path: Path, *, plants: dict[int, str] | None = None) -> None:
    config = make_config(tmp_path)
    build_standard_database(
        config,
        plants=plants or service.plants,
        units=standard_units(service.plant_id(OWN_PLANT), service.plant_id(OTHER_PLANT)),
    )
    service.client.app.state.realtime_panel = RealtimePanel(config)


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/admin/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _entry(data: dict, unit_type: str) -> dict:
    return next(entry for entry in data["by_type"] if entry["type"] == unit_type)


# ── /api/health ────────────────────────────────────────────────────────────


def test_health_carries_the_realtime_state_without_numbers(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    realtime = response.json()["realtime"]
    assert set(realtime) == set(PUBLIC_STATUS_FIELDS)
    assert realtime["latest_data_time"] == "2026-10-05 15:20"
    assert realtime["state"] == "stopped"  # 測試裡沒有收集器在跑


def test_health_stays_up_when_the_realtime_status_breaks(
    service: Service, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(service, tmp_path)

    def broken(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(realtime_panel, "read_status", broken)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["realtime"] == {"available": False, "state": "unavailable"}


# ── /api/realtime/overview ─────────────────────────────────────────────────


def test_overview_without_a_database_is_200_and_unavailable(client: TestClient) -> None:
    response = client.get("/api/realtime/overview")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"available": False, "state": "unavailable"},
    }
    assert "no-store" in response.headers["cache-control"]


def test_an_anonymous_visitor_sees_every_unit_when_the_service_allows_it(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)

    data = client.get("/api/realtime/overview").json()["data"]

    assert data["scope"] == "all"
    assert _entry(data, "燃氣")["net_mw"] == 780.0  # 含歸屬未定的興達#1


def test_a_denied_visitor_gets_401_while_health_still_answers(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    client.app.state.anonymous_scope = "denied"

    response = client.get("/api/realtime/overview")

    assert response.status_code == 401
    assert response.json()["detail"] == "即時發電數字需要先登入。"
    assert client.get("/api/health").json()["realtime"]["available"] is True


def test_query_keeps_its_own_login_message(client: TestClient) -> None:
    client.app.state.anonymous_scope = "denied"

    response = client.post("/api/query", json={"question": "列出台中發電廠所有設備"})

    assert response.status_code == 401
    assert response.json()["detail"] == "此服務的查詢需要先登入。"


def test_a_lapsed_session_is_refused_rather_than_widened(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    _login(client, PLANT_ACCOUNT)
    manager = client.app.state.auth_manager
    assert manager.revoke(client.cookies[manager.cookie_name]) is True

    assert client.get("/api/realtime/overview").status_code == 401


def test_an_all_plants_account_sees_every_unit(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    _login(client, ALL_ACCOUNT)

    data = client.get("/api/realtime/overview").json()["data"]

    assert data["scope"] == "all"
    assert _entry(data, "燃氣")["net_mw"] == 780.0


def test_a_plant_account_sees_its_plant_and_shared_rows_only(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    _install(service, tmp_path)
    _login(client, PLANT_ACCOUNT)

    data = client.get("/api/realtime/overview").json()["data"]

    assert data["scope"] == f"plant:{OWN_PLANT}"
    gas = _entry(data, "燃氣")
    assert (gas["own_net_mw"], gas["shared_net_mw"], gas["units"]) == (480.0, None, 2)
    assert "水力" not in [entry["type"] for entry in data["by_type"]]
    assert "RT_SCOPE_PLANT" in [item["code"] for item in data["disclosures"]]


def test_a_roster_mismatch_is_409_for_a_plant_account_only(
    service: Service, client: TestClient, tmp_path: Path
) -> None:
    renamed = dict(service.plants)
    renamed[service.plant_id(OWN_PLANT)] = "大潭電廠（舊名）"
    _install(service, tmp_path, plants=renamed)

    _login(client, PLANT_ACCOUNT)
    refused = client.get("/api/realtime/overview")
    client.cookies.clear()
    _login(client, ALL_ACCOUNT)
    allowed = client.get("/api/realtime/overview")

    assert refused.status_code == 409
    assert "RT_SCOPE_MISMATCH" in refused.json()["detail"]
    assert allowed.status_code == 200


def test_an_unreadable_database_is_503_and_health_degrades(
    client: TestClient, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    config.database.parent.mkdir(parents=True)
    config.database.write_bytes(b"not a database" * 100)
    client.app.state.realtime_panel = RealtimePanel(config)

    response = client.get("/api/realtime/overview")
    health = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "即時資料暫時讀不到。"
    assert health.status_code == 200
    assert health.json()["realtime"] == {"available": False, "state": "unavailable"}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_realtime_api.py -q`
Expected: FAIL，`TypeError: create_app() got an unexpected keyword argument 'realtime_panel'`

- [ ] **Step 3: 實作：imports 與 `create_app`**

`src/serving/app.py` 的 import 區塊加兩行，放在 ruff isort 要求的位置（`serving.raw_data` 之後是 `serving.realtime_panel`；`text2sql` 自成一段，放在 `serving.*` 之後）：

```python
from serving.realtime_panel import RealtimePanel, RealtimeReadError, RealtimeScopeMismatch
from text2sql.realtime_scope import ALL_PLANTS, RealtimeScope
```

`create_app` 的參數在 `raw_data_service` 之後加上：

```text
    realtime_panel: RealtimePanel | None = None,
```

在 `application.state.raw_data_service = ...` 那段之後加上：

```python
    application.state.realtime_panel = (
        realtime_panel if realtime_panel is not None else RealtimePanel.from_project(PROJECT_ROOT)
    )
```

- [ ] **Step 4: 實作：no-store、health**

`security_headers` 裡的 `protected_path` 判斷加一行：

```python
            or protected_path.startswith("/api/realtime/")
```

電廠帳號拿到的回應依帳號而不同，不能被快取。

`health()` 的回傳 dict 最後加一個欄位：

```python
            "realtime": application.state.realtime_panel.status(),
```

- [ ] **Step 5: 實作：抽出共用的身分 → 範圍函式**

在 `resolve_account_plant` 函式之後、`@application.post("/api/query")` 之前加（兩個 `create_app` 內的函式，整段縮排 4 格，與 `resolve_account_plant` 對齊）：

```python
def require_query_access(principal: AdminPrincipal | None, detail: str) -> None:
    """Anonymous callers read data only when POWERQUERY_ANONYMOUS_QUERY_SCOPE allows it."""

    if principal is None and application.state.anonymous_scope == "denied":
        raise HTTPException(status_code=401, detail=detail)


def realtime_scope_for(
    principal: AdminPrincipal | None,
) -> tuple[RealtimeScope, dict[int, str] | None]:
    """Same identity rule as /api/query, plus the roster a plant scope is checked against.

    全電廠帳號與匿名訪客不需要 power.db，所以只有電廠帳號才取目前的 runtime。
    """

    if principal is None or principal.plant_id is None:
        return ALL_PLANTS, None
    service = current_runtime()
    plant = resolve_account_plant(principal, service)
    catalog = service.pipeline.scope_guard.catalog
    return RealtimeScope("plant", principal.plant_id, plant), catalog.plant_names_by_id()
```

`/api/query` 裡的：

```python
        if principal is None and application.state.anonymous_scope == "denied":
            raise HTTPException(status_code=401, detail="此服務的查詢需要先登入。")
```

換成：

```python
        require_query_access(principal, "此服務的查詢需要先登入。")
```

`resolve_account_plant` 在 `scope_guard` 不存在時會先丟 503，所以 `realtime_scope_for` 取 `catalog` 時 guard 一定在。

- [ ] **Step 6: 實作：端點**

在 `/api/query` 函式之後、`application.mount("/static", ...)` 之前加：

```python
    @application.get("/api/realtime/overview")
    def realtime_overview(
        principal: Annotated[AdminPrincipal | None, Depends(_query_principal)],
    ) -> dict[str, object]:
        require_query_access(principal, "即時發電數字需要先登入。")
        scope, plants = realtime_scope_for(principal)
        try:
            data = application.state.realtime_panel.overview(scope, plants=plants)
        except RealtimeScopeMismatch as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "電廠對照不一致（RT_SCOPE_MISMATCH）：即時資料與查詢資料庫的電廠名冊"
                    "對不上，請聯絡管理員。"
                ),
            ) from error
        except RealtimeReadError as error:
            raise HTTPException(status_code=503, detail="即時資料暫時讀不到。") from error
        return {"success": True, "data": data}
```

- [ ] **Step 7: 跑測試確認通過，現有測試不受影響**

Run: `uv run pytest tests/test_realtime_api.py -q`
Expected: PASS（11 項）

Run: `uv run pytest tests/test_serving.py tests/test_serving_auth.py tests/test_account_scope.py tests/test_admin_auth.py -q`
Expected: 全部 PASS（`/api/query` 抽出函式後行為不變）

Run: `uv run ruff format --check .; uv run ruff check .`
Expected: 無錯誤

- [ ] **Step 8: Commit**

```bash
git add src/serving/app.py tests/test_realtime_api.py
git commit -m "feat: serve the realtime panel with the same scope rules as queries"
```

---

### Task 5: 前端即時發電區塊

**Files:**
- Modify: `src/serving/static/index.html`（`overviewView` 的 `</header>` 與 `<div class="metric-grid"` 之間）
- Modify: `src/serving/static/app.js`（頂端變數、新函式、`showView`、初始化）
- Modify: `src/serving/static/app.css`（檔尾）
- Test: `tests/test_realtime_api.py`（檔尾加靜態檔檢查）

**Interfaces:**
- Consumes:
  - Task 4 的 `GET /api/realtime/overview`、`GET /api/health` 的 `realtime` 欄位。
  - `app.js` 既有的 `byId`、`element(tag, className, text)`、`api(path)`（非 2xx 時丟出的 Error 帶 `status`）、`businessData(payload)`、`renderTable(columns, rows)`、`loadStats`、`showView`。
  - CSS 既有的 `.surface`、`.content-heading`、`.eyebrow`、`.callout`、`.table-wrap`、`.chart`、`.plotly-chart`。CSP 的 `style-src 'self'` 擋掉了 Plotly 自己注入的樣式，`.plotly-chart` 那組規則是替代品，所以趨勢圖要用這個 class。
- Produces: 元素 id `realtimePanel`、`realtimeLight`、`realtimeStatusText`、`realtimeLoginNote`、`realtimeDisclosures`、`realtimeBody`、`realtimeTypeHead`、`realtimeTypeRows`、`realtimeStorageNote`、`realtimeTrend`、`realtimeTrendDetails`、`realtimeTrendTable`；JS 函式 `loadRealtime()`。

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_realtime_api.py` 檔尾加：

```python
# ── 前端靜態檔 ─────────────────────────────────────────────────────────────

STATIC = Path(__file__).resolve().parents[1] / "src" / "serving" / "static"
REALTIME_IDS = (
    "realtimePanel",
    "realtimeLight",
    "realtimeStatusText",
    "realtimeLoginNote",
    "realtimeDisclosures",
    "realtimeBody",
    "realtimeTypeHead",
    "realtimeTypeRows",
    "realtimeStorageNote",
    "realtimeTrend",
    "realtimeTrendDetails",
    "realtimeTrendTable",
)


def test_the_overview_page_has_the_realtime_block_above_the_metric_cards() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    for element_id in REALTIME_IDS:
        assert f'id="{element_id}"' in html, element_id
    assert html.index('id="realtimePanel"') < html.index('class="metric-grid"')
    assert "各機組發電量即時資訊" in html  # 資料來源顯名


def test_the_script_refreshes_the_panel_only_while_it_is_visible() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'api("/api/realtime/overview")' in script
    assert "REALTIME_REFRESH_MS = 60000" in script
    assert '"visibilitychange"' in script
    assert "connectgaps: false" in script
```

Run: `uv run pytest tests/test_realtime_api.py -q -k "overview_page or script_refreshes"`
Expected: FAIL（找不到 `id="realtimePanel"`）

- [ ] **Step 2: `index.html`**

在 `overviewView` 裡、`</header>`（`refreshOverview` 按鈕所在的 header）之後，`<div class="metric-grid" aria-label="資料涵蓋統計">` 之前插入：

```html
          <section class="surface realtime-panel" id="realtimePanel" aria-labelledby="realtimeTitle">
            <div class="content-heading">
              <div><p class="eyebrow">REALTIME</p><h3 id="realtimeTitle">即時發電</h3></div>
              <p class="realtime-status"><span class="rt-light" id="realtimeLight" aria-hidden="true"></span><span id="realtimeStatusText">讀取中…</span></p>
            </div>
            <p class="realtime-note" id="realtimeLoginNote" hidden>登入後可查看即時發電數字。</p>
            <div id="realtimeDisclosures"></div>
            <div id="realtimeBody" hidden>
              <div class="table-wrap" tabindex="0" aria-label="最新時段各類型總出力，可左右捲動">
                <table class="realtime-table"><thead id="realtimeTypeHead"></thead><tbody id="realtimeTypeRows"></tbody></table>
              </div>
              <p class="realtime-note" id="realtimeStorageNote" hidden>儲能是放電、儲能負載是充電，兩者分列、不相抵。</p>
              <figure class="chart realtime-chart" aria-label="今日各類型出力趨勢（台灣時間）"><div class="plotly-chart" id="realtimeTrend" tabindex="0"></div></figure>
              <details id="realtimeTrendDetails"><summary>今日趨勢數字表</summary><div id="realtimeTrendTable"></div></details>
            </div>
            <p class="realtime-note">資料來源：台灣電力公司「各機組發電量即時資訊」（政府資料開放授權條款－第 1 版），每 10 分鐘更新；可能落後或中斷，不能作為供電或調度決策依據。</p>
          </section>
```

狀態文字每 60 秒會更新一次，所以刻意**不加** `role="status"`，免得螢幕閱讀器每分鐘都唸一次。

- [ ] **Step 3: `app.js` 頂端變數**

在檔案開頭的變數區、`var dataChangeDialogReturnFocus = null;` 之後加：

```js
  var REALTIME_REFRESH_MS = 60000;
  var REALTIME_STATE_LABELS = { healthy: "正常", stale: "資料落後", stopped: "收集器未執行", unavailable: "尚無即時資料" };
  var realtimeRequest = null;
```

- [ ] **Step 4: `app.js` 新函式**

在 `function loadStats(announceResult) { ... }` 結束之後（`function loadExamples()` 之前）加：

```js
  function realtimeNumber(value) {
    if (value == null) return "—";
    return new Intl.NumberFormat("zh-Hant-TW", { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(value);
  }

  function realtimeVisible() {
    return !document.hidden && !byId("overviewView").hidden;
  }

  function setRealtimeStatus(state, text) {
    var light = state === "healthy" ? "healthy" : state === "stale" ? "stale" : "stopped";
    byId("realtimeLight").className = "rt-light " + light;
    byId("realtimeStatusText").textContent = text;
  }

  function realtimeStatusText(info) {
    if (!info || !info.available || !info.latest_data_time) return "尚無即時資料";
    var parts = ["最新時段 " + info.latest_data_time + "（台灣時間）"];
    if (info.lag_minutes != null) parts.push(Math.round(info.lag_minutes) + " 分鐘前");
    if (info.today && info.today.elapsed_slots) parts.push("今天 " + info.today.snapshots + "／" + info.today.elapsed_slots + " 個時段");
    if (info.state && info.state !== "healthy") parts.push(REALTIME_STATE_LABELS[info.state] || info.state);
    return parts.join(" · ");
  }

  function renderRealtimeDisclosures(items) {
    var holder = byId("realtimeDisclosures");
    holder.textContent = "";
    (Array.isArray(items) ? items : []).forEach(function (item) {
      holder.appendChild(element("div", "callout", item.reason || item.code));
    });
  }

  function renderRealtimeTypes(data) {
    var plant = data.scope !== "all";
    var head = byId("realtimeTypeHead");
    var body = byId("realtimeTypeRows");
    var headRow = element("tr");
    var storage = false;
    head.textContent = "";
    body.textContent = "";
    var labels = plant
      ? ["類型", "本廠（MW）", "共用（MW）", "裝置容量（MW）", "通訊異常機組"]
      : ["類型", "淨發電量（MW）", "裝置容量（MW）", "通訊異常機組"];
    labels.forEach(function (label) {
      var th = element("th", "", label);
      th.scope = "col";
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    (Array.isArray(data.by_type) ? data.by_type : []).forEach(function (entry) {
      var row = element("tr");
      var label = entry.type;
      if (entry.type === "儲能") { label += "（放電）"; storage = true; }
      if (entry.type === "儲能負載") { label += "（充電）"; storage = true; }
      var th = element("th", "", label);
      th.scope = "row";
      row.appendChild(th);
      var values = plant ? [entry.own_net_mw, entry.shared_net_mw] : [entry.net_mw];
      values.concat([entry.capacity_mw]).forEach(function (value) {
        row.appendChild(element("td", "numeric", realtimeNumber(value)));
      });
      row.appendChild(element("td", "numeric", valueText(entry.unreliable_units)));
      body.appendChild(row);
    });
    byId("realtimeStorageNote").hidden = !storage;
  }

  function renderRealtimeTrend(today) {
    var chart = byId("realtimeTrend");
    var holder = byId("realtimeTrendTable");
    var slots = today && Array.isArray(today.slots) ? today.slots : [];
    var series = today && Array.isArray(today.series) ? today.series : [];
    var plotly = window.Plotly && typeof window.Plotly.react === "function" ? window.Plotly : null;
    holder.textContent = "";
    byId("realtimeTrendDetails").hidden = !slots.length;
    if (slots.length) {
      var columns = ["時刻"].concat(series.map(function (item) { return item.type; }));
      var rows = slots.map(function (slot, index) {
        return [slot].concat(series.map(function (item) { return item.net_mw[index]; }));
      });
      var table = renderTable(columns, rows);
      if (table) holder.appendChild(table);
    }
    if (!slots.length || !plotly) {
      if (plotly) plotly.purge(chart);
      chart.textContent = slots.length ? "圖表元件未載入，請展開下方數字表。" : "今天還沒有資料。";
      return;
    }
    if (!chart.classList.contains("js-plotly-plot")) chart.textContent = "";
    var traces = series.map(function (item) {
      // 缺值是 null：斷線，不畫成 0。
      return { type: "scatter", mode: "lines", name: item.type, x: slots, y: item.net_mw, connectgaps: false };
    });
    Promise.resolve().then(function () {
      return plotly.react(chart, traces, {
        margin: { t: 16, r: 16, b: 52, l: 64 },
        xaxis: { title: { text: "時刻（台灣時間）" } },
        yaxis: { title: { text: "MW" } },
        legend: { orientation: "h" },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { family: "system-ui, sans-serif", color: "#263547" },
        autosize: true
      }, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["sendDataToCloud", "lasso2d", "select2d"] });
    }).catch(function () {
      plotly.purge(chart);
      chart.textContent = "圖表無法顯示，請展開下方數字表。";
    });
  }

  function loadRealtime() {
    if (realtimeRequest) return realtimeRequest;
    realtimeRequest = api("/api/realtime/overview").then(function (payload) {
      var data = businessData(payload);
      byId("realtimeLoginNote").hidden = true;
      if (!data.available) {
        setRealtimeStatus(data.state, "尚無即時資料");
        renderRealtimeDisclosures([]);
        byId("realtimeBody").hidden = true;
        return;
      }
      setRealtimeStatus(data.state, realtimeStatusText({
        available: true,
        latest_data_time: data.data_time,
        lag_minutes: data.lag_minutes,
        state: data.state,
        today: data.today
      }));
      renderRealtimeDisclosures(data.disclosures);
      renderRealtimeTypes(data);
      byId("realtimeBody").hidden = false;
      renderRealtimeTrend(data.today);
    }).catch(function (error) {
      byId("realtimeBody").hidden = true;
      renderRealtimeDisclosures([]);
      if (error.status === 401) {
        // 訪客權限是 denied 或登入已失效：只顯示狀態，不顯示數字。
        byId("realtimeLoginNote").hidden = false;
        return api("/api/health").then(function (health) {
          var info = health.realtime || {};
          setRealtimeStatus(info.state, realtimeStatusText(info));
        }).catch(function () { setRealtimeStatus("unavailable", "無法讀取即時狀態"); });
      }
      byId("realtimeLoginNote").hidden = true;
      if (error.status === 409) setRealtimeStatus("unavailable", "電廠對照不一致，請聯絡管理員。");
      else if (error.status === 503) setRealtimeStatus("unavailable", "即時資料暫時讀不到，稍後會自動再試。");
      else setRealtimeStatus("unavailable", "無法讀取即時資料：" + error.message);
    }).finally(function () { realtimeRequest = null; });
    return realtimeRequest;
  }
```

`renderRealtimeTrend` 放在 `byId("realtimeBody").hidden = false;` 之後才呼叫：容器要先顯示出來，Plotly 才量得到寬度。

- [ ] **Step 5: `app.js` 接上 `showView` 與初始化**

`showView` 裡 `if (name === "data") enterDataManagement();` 下一行加：

```js
    if (name === "overview") loadRealtime();
```

初始化區的：

```js
  byId("refreshOverview").addEventListener("click", function () { loadStats(true); });
```

換成：

```js
  byId("refreshOverview").addEventListener("click", function () { loadStats(true); loadRealtime(); });
```

在 `window.addEventListener("resize", ...)` 那一行之後加：

```js
  // 總覽頁開著、且分頁在前景時才更新；資料本身每 10 分鐘才變一次。
  window.setInterval(function () { if (realtimeVisible()) loadRealtime(); }, REALTIME_REFRESH_MS);
  document.addEventListener("visibilitychange", function () { if (realtimeVisible()) loadRealtime(); });
```

- [ ] **Step 6: `app.css`**

檔尾加：

```css
.realtime-panel { margin-bottom: 18px; padding: 20px; }
.realtime-panel .content-heading { flex-wrap: wrap; }
.realtime-status { display: flex; align-items: center; gap: 8px; margin: 0; color: var(--muted-strong); font-size: .8rem; }
.rt-light { flex: 0 0 auto; width: 10px; height: 10px; border-radius: 50%; background: #9aa5b1; box-shadow: 0 0 0 4px rgba(154,165,177,.16); }
.rt-light.healthy { background: #5dcc78; box-shadow: 0 0 0 4px rgba(93,204,120,.14); }
.rt-light.stale { background: #d89b35; box-shadow: 0 0 0 4px rgba(216,155,53,.14); }
.realtime-table { width: 100%; border-collapse: collapse; }
.realtime-table th, .realtime-table td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; font-size: .8rem; }
.realtime-table td.numeric { text-align: right; font-variant-numeric: tabular-nums; }
.realtime-note { margin: 10px 0 0; color: var(--muted); font-size: .75rem; }
.realtime-panel details { margin-top: 8px; font-size: .8rem; }
```

- [ ] **Step 7: 檢查**

Run: `node --check src/serving/static/app.js`
Expected: 無輸出、結束碼 0

Run: `uv run pytest tests/test_realtime_api.py tests/test_serving.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/serving/static/index.html src/serving/static/app.js src/serving/static/app.css tests/test_realtime_api.py
git commit -m "feat: show realtime generation on the overview page"
```

---

### Task 6: 對外文件、規格同步、CP-074

**Files:**
- Modify: `docs/SERVING.md`、`README.md`（第 244 行）、`ATTRIBUTION.md`（「開放資料顯名」與第 27 行）、`docs/SYSTEM_CARD.md`、`docs/superpowers/specs/2026-10-05-realtime-panel-design.md`、`log.md`

**Interfaces:**
- Consumes: Task 1–5 的行為；本計畫「與規格的差異」一節。
- Produces: 文件；儲存點 CP-074。

- [ ] **Step 1: `docs/SERVING.md`**

「網頁工作台」一節裡，「資料總覽」那一行改成：

```markdown
- **資料總覽**：即時發電（收集器狀態、各類型總出力、今日趨勢）、資料期間、尖峰紀錄、機組與歲修筆數，以及主題式分析入口。
```

在「## 網頁工作台」這一節的結尾（「## 資料管理登入」之前）加一節：

```markdown
## 即時發電面板

「資料總覽」頁最上方的「即時發電」區塊，顯示即時收集器（見「即時資料收集器」一節）收進 `realtime.db` 的資料：

- **狀態列**：燈號綠色＝收集器在跑、資料是新的；黃色＝資料落後超過 30 分鐘（`configs/realtime.yaml` 的 `status.stale_after_minutes`）；灰色＝收集器沒在跑，或還沒有資料。文字列出最新時段（台灣時間）、落後幾分鐘、今天收到幾個時段。
- **各類型總出力**：最新時段各發電類型的淨發電量、裝置容量與通訊異常的機組數。只加總「正常」的值；儲能（放電）與儲能負載（充電）分列，不相抵。
- **今日趨勢**：今天每 10 分鐘各類型的出力；沒抓到的時段會斷線，不畫成 0。圖下可以展開數字表。

頁面開著、且分頁在前景時，每 60 秒更新一次；切到其他頁或分頁在背景就暫停。按「重新整理」會立刻更新。資料本身每 10 分鐘才變一次。收集器更新資料後，不必重啟網頁服務。

權限與查詢一致：

| 身分 | 看得到 |
|---|---|
| 管理員、全電廠帳號 | 全部機組，包括歸屬未定的 |
| 電廠帳號 | 自己電廠的機組與跨廠共用（shared）的列，分「本廠」「共用」兩欄；歸屬未定的機組看不到 |
| 訪客（`POWERQUERY_ANONYMOUS_QUERY_SCOPE=all`） | 全部 |
| 訪客（`denied`） | 只有狀態列，沒有數字 |

- 面板不經過 Text2SQL；查詢中心目前**還查不到**即時資料。
- 收集器沒在跑時，面板照常顯示最後一筆，並標出「收集器目前沒有在執行」。網頁服務不會啟動或停止收集器。
- API：`GET /api/health` 的 `realtime` 欄位只有狀態，任何人都看得到；`GET /api/realtime/overview` 回傳面板數字，套用上表的權限。電廠帳號遇到兩邊電廠名冊不一致時回 409 `RT_SCOPE_MISMATCH`；`realtime.db` 讀不出來時回 503。
```

- [ ] **Step 2: `README.md` 第 244 行**

把這一句：

```markdown
本專案使用的是固定時間取得的**快照，不是即時資料服務**，請勿作為供電或調度決策依據。
```

換成：

```markdown
分析用的 `power.db` 是固定時間取得的**快照**；「資料總覽」頁的即時發電區塊每 10 分鐘取自台電 `d006001`，可能落後或中斷，頁面會標出資料時間。兩者都**不能作為供電或調度決策依據**。
```

前半句「台電資料的顯名不代表…背書。」保持不變。

- [ ] **Step 3: `ATTRIBUTION.md`**

「## 開放資料顯名」清單的最後一項（`- 授權條款：https://data.gov.tw/license`）之後加：

```markdown
- 即時資料：「各機組發電量即時資訊（含外購電力）」（政府資料開放平臺資料集 8931，台電代號 `d006001`），由即時收集器每 10 分鐘取得，不在上述快照內；網頁「資料總覽」的即時發電區塊會標示這個出處。
```

第 27 行「本資料為固定時間取得的快照……」整段換成：

```markdown
分析用的 `power.db` 與下載資料是固定時間取得的快照，**不是即時資料服務**。名稱含「今日」或「即時」的資料，也只代表抓取當時保存的版本。例外是「資料總覽」頁的即時發電區塊：它每 10 分鐘取自台電 `d006001`，可能落後或中斷，頁面會標出資料時間。不論是快照還是即時區塊，都請以台電最新公告及官方服務作為供電、運轉調度或其他正式決策依據。
```

- [ ] **Step 4: `docs/SYSTEM_CARD.md`**

在「## 當前狀態」之前加：

```markdown
## 即時資料

- 「資料總覽」頁的即時發電區塊顯示台電 `d006001` 每 10 分鐘的機組發電量，由獨立的收集器寫進 `realtime.db`。查詢中心目前**查不到**即時資料。
- 即時資料**不經過四眼審核**：每一筆快照由自動驗證放行。結構有問題的整份拒收；數值有問題的照收、標記品質，並在頁面上揭露。會改變結果的規則，也就是解析程式、`taipower_align/realtime_units.csv` 的人工決定與 `configs/realtime.yaml`，變更時才走 PR 審查。
- 面板只加總狀態「正常」的值。資料可能落後或中斷，頁面會標出資料時間與落後分鐘數，不能作為供電或調度決策依據。
- 權限與查詢一致：電廠帳號只看到自己電廠的機組與跨廠共用的列，歸屬未定的機組看不到。
```

- [ ] **Step 5: 同步規格**

修改 `docs/superpowers/specs/2026-10-05-realtime-panel-design.md`：

1. 開頭的狀態改成 `> 狀態：已實作（feat/realtime-panel，CP-074）`。
2. §4.1 的程式碼區塊換成：

   ```python
   class RealtimePanel:
       def __init__(self, config: RealtimeConfig | None) -> None: ...
       @classmethod
       def from_project(cls, root: Path = PROJECT_ROOT) -> RealtimePanel: ...
       def status(self, *, now: datetime | None = None) -> dict[str, object]: ...
       def overview(
           self,
           scope: RealtimeScope,
           *,
           plants: Mapping[int, str] | None = None,
           now: datetime | None = None,
       ) -> dict[str, object]: ...
   ```

   並在下方清單加兩點：
   - 「`plants` 是 power.db 的電廠對照表（`ScopeCatalog.plant_names_by_id()`），電廠範圍必須提供。power.db 會熱抽換，所以不在建置時記路徑，而是每次請求取目前 runtime 的對照表，和電廠帳號解析用的是同一份。」
   - 「`from_project` 讀不到 RT-1 設定時記 log，回 `RealtimePanel(None)`，此時一律視同無資料（§7）。」
3. §3 架構圖裡的「固定 SQL：v_rt_now、v_rt_10min（今天）」改成「固定 SQL：v_rt_10min（最新時段＝read_status 的 latest_data_time；今天）」。
4. §4.3 的 JSON 範例 `"today"` 加上 `"elapsed_slots": 93, "snapshots": 92`；清單加一點：「一組機組裡沒有任何『正常』的值時，`net_mw`（以及電廠帳號的 `own_net_mw`、`shared_net_mw`）是 `null`，不是 0。」
5. §5.1 的 `UnitRow.key` 改成 `key: str  # 檢視的「機組鍵」：機組類型|機組名稱`；兩個函式的回傳型別改成 `frozenset[str] | None` 與 `frozenset[str]`；在 `RealtimeScope` 補上 `label` 屬性與 `ALL_PLANTS`。
6. §5.3 範例的 `"own_net_mw": 0.0` 改成 `"own_net_mw": null`。
7. §6.1 第 3 點之後加一點：「4. **資料來源**：區塊底部一行顯名——台灣電力公司『各機組發電量即時資訊』、授權條款、更新頻率與『不能作為決策依據』。」
8. §9 的 ATTRIBUTION 那一點補上「『開放資料顯名』加上即時資料集 8931／`d006001`」。

- [ ] **Step 6: 全部檢查，記下數字**

Run: `uv run ruff format --check .; uv run ruff check .; uv run pytest -q; node --check src/serving/static/app.js`
Expected: 全部通過；passed 數 = Task 1 的基準 + 新增的測試數，skipped 數不變。

- [ ] **Step 7: `log.md` 寫 CP-074**

在 `log.md` 的第一個 `## CP-` 之前加。時間用寫入當下的台灣時間；測試數字用 Step 6 的實際結果：

```markdown
## CP-074 — 即時發電面板 RT-3a：總覽頁看得到即時資料，權限與查詢一致

- 時間：<YYYY-MM-DD HH:MM> +08:00
- 狀態：已完成（RT-3a；實機驗收見下方。RT-3b 自然語言查詢即時資料尚未開始）
- 分支：`feat/realtime-panel`（從 `docs/realtime-panel-design` 開，底下是 RT-1 的 `feat/realtime-collector`）
- 起點：依 `docs/superpowers/specs/2026-10-05-realtime-panel-design.md` 與
  `docs/superpowers/plans/2026-10-05-realtime-panel-rt3a.md` 實作。

### 做了什麼

- `src/text2sql/realtime_scope.py`：權限純函式（全範圍看全部；電廠帳號看本廠＋shared；未定的機組一律不給電廠帳號），
  RT-3b 重用。
- `src/serving/realtime_panel.py`：每次請求唯讀開 `realtime.db`，寫死的 SQL；只加總「正常」的值，缺值是 null；
  儲能與儲能負載分列；電廠帳號拆「本廠」「共用」，並比對兩邊的電廠名冊；揭露 `RT_STALE`、`RT_COLLECTOR_STOPPED`、
  `RT_QUALITY_WARN`、`RT_SCOPE_PLANT`、`RT_NO_DATA_TODAY`。
- `src/serving/app.py`：`/api/health` 多 `realtime` 狀態欄位（不含數字，絕不因此出錯）；`GET /api/realtime/overview`
  （401／409／503）；「匿名可否讀取」抽成 `require_query_access`，`/api/query` 行為不變。
- 前端：總覽頁的即時發電區塊（狀態列、各類型表、今日趨勢圖＋數字表），前景時每 60 秒更新。
- 文件：SERVING 新增「即時發電面板」；README、ATTRIBUTION 的「快照，不是即時資料服務」改寫並加上即時資料集顯名；
  SYSTEM_CARD 新增「即時資料」（不經四眼審核的說明）；規格依實作同步。

### 刻意沒做的

- 查詢端仍查不到即時資料，`configs/coverage.yaml` 不變（RT-3b）。
- 機組明細表、從網頁啟停收集器。

- 驗收：`uv run ruff format --check .`、`uv run ruff check .` 通過；`uv run pytest -q` → <N> passed、<M> skipped
  （基準 <N0> passed）；`node --check app.js` 通過。
```

`<…>` 要換成實際值，不能留著。

- [ ] **Step 8: Commit**

```bash
git add docs/SERVING.md README.md ATTRIBUTION.md docs/SYSTEM_CARD.md docs/superpowers/specs/2026-10-05-realtime-panel-design.md log.md
git commit -m "docs: describe the realtime panel and record CP-074"
```

---

### Task 7: 實機驗收（controller 與使用者一起做，不派給 subagent）

**Files:** 無程式變更；結果補進 `log.md` 的 CP-074。

- [ ] **Step 1: 準備資料**

panel worktree 的 `data/` 是空的。
- `power.db` 用 `uv run python -m ingest.build_db` 建（離線，不連網）。如果指令名稱不同，就看 `src/ingest/build_db.py` 的 `__main__`。
- `realtime.db` 有兩個選項，**先問使用者選哪個**：
  - (a) 在這個 worktree 用 `即時收集啟動.bat` 啟動收集器。這會連線台電，每次連線都要使用者同意。
  - (b) 暫停 realtime-collector worktree 的收集器，把它的 `data/processed/realtime.db` 與 `data/realtime/` 複製過來，再在這裡啟動收集器。
  - 不論哪個選項，**不得刪除** realtime-collector worktree 的 `data/`。

- [ ] **Step 2: 啟動服務**

用 `.claude/launch.json` 加一筆設定，透過 preview 工具啟動網頁服務，不用 Bash 直接跑。指令沿用 `docs/SERVING.md`「手動啟動」的寫法。

- [ ] **Step 3: 三種身分各看一次（規格 §11 第 3 點）**

1. 管理員：看得到全部類型。
2. 電廠帳號：只看到本廠＋共用，表格有兩欄，並出現 `RT_SCOPE_PLANT` 揭露。需要一份暫時的帳號名冊；不要改使用者既有的名冊檔，用完要移除。
3. 以 `POWERQUERY_ANONYMOUS_QUERY_SCOPE=denied` 啟動：訪客只看到狀態列和「登入後可查看即時發電數字」。

每一種截圖一張。

- [ ] **Step 4: 收集器狀態（規格 §11 第 4 點）**

- 收集器在跑時燈號是綠色。
- 用 `停止即時收集.bat` 停掉收集器，60 秒內燈號變灰，並出現 `RT_COLLECTOR_STOPPED`。
- 再啟動收集器，等下一個時段進來；不重啟網頁服務，數字就會更新。

- [ ] **Step 5: 確認沒有動到查詢端（規格 §11 第 5 點）**

```bash
git diff --stat 64018f2 -- src/text2sql src/eval benchmarks corpus configs src/ingest
```

Expected: 只有 `src/text2sql/realtime_scope.py` 是新增的，其他路徑沒有變動。

- [ ] **Step 6: 把驗收結果補進 CP-074，commit**

在 CP-074 末尾加一個「### 實機驗收」小節，寫日期、三種身分的結果、燈號變化。然後：

```bash
git add log.md
git commit -m "docs: record the RT-3a manual acceptance"
```

完成後由 controller 跑 pre-merge-check（規格 §11 第 2 點），並請使用者 push 開 PR。PR #18 還沒合併時，這個 PR 會包含 RT-1 的 commit；在 PR 說明裡註明「base 是 #18」，或等 #18 合併後再 rebase 到 main。
