# RT-1 即時機組發電量收集器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立常駐收集器，把台電 `d006001` 每 10 分鐘的機組發電量快照先封存、再寫進可重建的 `data/processed/realtime.db`，並提供每日彙總、安全清除、從封存重建與健康狀態。

**Architecture:** `src/ingest/realtime/` 下 15 個單一職責模組。純函式（`timeutil`、`parse`、`schedule`）不碰檔案與時鐘；檔案層（`archive`、`decisions`、`lock`）；資料庫層（`store`、`maintenance`、`rebuild`、`status`）；網路層（`client`）；由 `collector` 串起，`__main__` 提供指令列，`candidates` 是給人審的候選產生器。原始回應永久 gzip 封存為真實來源，資料庫可隨時從封存重建。RT-1 不動任何查詢端程式。

**Tech Stack:** Python ≥ 3.11 標準函式庫（sqlite3、urllib、gzip、msvcrt／fcntl）、PyYAML、certifi；pytest；ruff。不新增相依套件。

**Spec:** `docs/superpowers/specs/2026-09-24-realtime-ingest-design.md`（資料本身的分析見 `docs/REALTIME_INGEST.md`）。執行者請先讀規格，本計畫的每個任務都以規格的章節為依據。

## 實作後記：與計畫的差異

> 以下程式碼區塊是寫計畫當時的版本。凡是與 repo 裡的程式碼和測試不同的地方，**以 repo 為準**；
> 本節只記下差在哪裡、為什麼。規格已在 `bbbf4c6` 依實作同步。

### 各任務的偏離

| 任務 | 改了什麼 | 為什麼 | Commit |
|---|---|---|---|
| Task 1 | `tests/test_foundation.py` 的必備設定檔清單加上 `realtime.yaml`（計畫的 Files 與 Step 8 沒列） | 該測試釘住 `configs/` 下全部 YAML，不加的話計畫自己的 Step 7 過不了；規格附錄 A 要求 `configs/realtime.yaml` | `ce4a144` |
| Task 1 | 不執行 Step 1 的 `git fetch`／`git switch -c` | 分支已在 worktree 從 `bd91fbf` 開好，基點相同 | — |
| Task 4 | 讀檔時每列正規化成 `(row.get(field) or "").strip()`，新增 `test_short_row_is_skipped_with_a_warning` | 少欄的列原本讓 `.strip()` 拋 `AttributeError`，一個打錯的列就讓收集停下；規格 §5.3「該列視為未定＋警告」與 §3.1 原則 3 | `c153bd9` |
| Task 7 | 重建時讀不出來的封存檔（`OSError`／`EOFError`／`zlib.error`）計為 `corrupted` 並略過；新增截斷 gzip 與「重建中途失敗會回復舊資料庫」兩個測試 | 規格 §3.1（封存是真實來源）、§9（損壞自動重建）、§7.3（單一交易） | `0004959` |
| Task 11 | `_reconcile_archive` 同樣略過讀不出來的封存檔，加回歸測試 | 同上：一個壞檔不能擋住啟動 | `d2cd957` |
| Task 11 | 每小時維護有自己的 try，失敗也推進 `last_maintenance`；`_load_decisions` 擴大捕捉範圍（含非 UTF-8 的 `UnicodeDecodeError`）；封存寫入失敗記成 `ArchiveError`、不記 ETag | 原碼一次維護失敗（例如 cp950 的決定檔）就讓抓取全停，封存失敗會繞過退避；規格 §3.1 原則 3、§5.3、§9 | `c029738` |
| Task 12 | `main()` 對 `run`／`once`／`rebuild` 捕捉未預期例外：log 留 traceback、stderr 印中文、結束碼 1；新增共用的 `decisions.LOAD_ERRORS`，收集器與 `status` 都用它，壞掉的 `plants.csv` 不再讓 `read_status()` 當掉 | 規格 §4.5（1＝失敗）、繁中訊息的全域約束、§8.3（健康狀態要降級而不是當掉） | `d7b042a` |
| Task 13 | Steps 1–6 與 Steps 7–9 分兩次做，中間停下等使用者逐列審查；使用者的決定改了候選檔 33 列（含 README 對照、松林→19、尖山／塔山／珠山歸電廠、烏來＆桂山＆粗坑為 bucket＋shared、6 列未查證的水力列為 shared），其餘 168 列照候選 | 計畫規定的人工關卡；最後 204 個決定、0 警告、無缺漏 | `c748749`、`bccea3a` |
| Task 14 | 批次檔不重導輸出，`run` 自己寫 `logs/realtime-collector-<日期時間>.log`（計畫本來就這樣寫，與規格 §4.6 原文不同，已改規格） | 規格 §4.6 | `aeb2257`、`bbbf4c6` |
| Task 15 | 實機 `once` 發現 `SUBTOTAL_MISMATCH` 把剛好差 0.1 MW 判成超過（浮點 0.10000000000002）：改成 `round(abs(diff), 6) > 0.1`，加「差 0.1 不警告、差 0.2 警告」兩個測試；明細加總維持小數 3 位。`4958074` 曾改成 1 位、`e5611be` 改回 3 位（`4958074` 的訊息仍寫著 1 位） | 規格 §6.3「差超過 0.1 MW」；來源淨發電量有 2–3 位小數 | `4958074`、`e5611be` |
| Task 15 | 文件 commit 只含 SERVING 與 lineage，log.md 另外寫；儲存點由 CP-072 改為 CP-073（見下） | main 已先用掉 CP-071 | `29d8b53`、`d8ad8d8`、`47604ec`、`8753e7e` |

### 最終審查與之後的修正

| 項目 | 改了什麼 | 為什麼 | Commit |
|---|---|---|---|
| 損毀的 `realtime.db` | `quick_check` 失敗、開檔或重建遇到損毀時，把檔案移到旁邊再從封存重建（`rebuild` 指令也一樣）；`store.connect` 在 PRAGMA 失敗時關閉連線，Windows 上才移得開 | 原本每次重啟都撞「database disk image is malformed」而無限重啟；規格 §7.3、§9 | `49ce80d` |
| 啟動步驟與抓取紀錄 | 啟動時的維護、人工決定同步、補入庫的每一筆失敗都只記錄；JSONL 寫不進去（例如 Excel 開著）時照寫資料庫鏡像、排程照樣推進；`resume` 紀錄移進每輪的 try | 規格 §4.2、§9 | `c7dc14c` |
| 補入庫與停止檔 | 補入庫的抓取時間改用抓取紀錄（`rebuild.fetch_times`），與重建一致；拿到鎖後立刻刪掉上一次留下的 `stop.request` | 規格 §4.2、§7.3 | `5b35c07` |
| 文件 | SERVING.md 說明結束碼 3、`once` 失敗回 1、設定錯誤每個指令都回 1；lineage 03 的 `realtime_units.csv` 大小改成磁碟上的大小 | 與程式一致 | `49202e5` |
| 殘行 | `iter_attempts` 以 `errors="replace"` 讀檔，切在多位元組字元中間的殘行解不出 JSON 就略過 | 審查後發現：否則每次啟動都 `UnicodeDecodeError` 而無限重啟；規格 §9 | `bdcf0e0` |
| 電廠名冊讀不到的重建 | 新增 `Collector._fallback_decisions`：`empty_decisions` 讀 `plants.csv` 失敗（`LOAD_ERRORS`）時警告，改用空的決定重建；`run`／`once` 與 `rebuild_only` 兩處都用它，各加一個測試 | 重建的後備路徑沒接住 `LOAD_ERRORS`，`plants.csv` 缺檔或格式壞掉時啟動失敗、啟動器每 60 秒重試同一個錯；違反規格 §3.1／§5.3／§9「壞檔只警告、不停止收集」 | `28fac81` |
| 只在真的損毀時移檔 | 只有 `SQLITE_CORRUPT`／`SQLITE_NOTADB`（或 `quick_check` 不是 ok）才移到旁邊，鎖住等暫時性錯誤往上拋、結束碼 1、由批次檔重試；移開的檔名不重複；檔案不存在時不做事 | 避免把健康的檔案移走；規格 §4.2、§9 | `0341c0a` |
| CI 上的舊版 SQLite | 損毀資料庫測試的前置探測也接受 `quick_check` 直接拋例外（SQLite 3.45.1，CI 的 Ubuntu）；程式本身原本就兩種都處理 | PR #17 的 CI 失敗 | `99ef404` |

審查中決定延後、不在 RT-1 修的：`RECONCILE_DAYS = 2`；啟動重建期間按 Ctrl+C 會被批次檔重啟；
重建以重建當下的 `now` 解析；Windows 上別的行程開著資料庫時移不開損毀檔（RT-3）。寫在規格 §12.1。

### 測試數字

計畫寫的「15 個測試檔共 99 項測試」是寫計畫時的數字，留著不改。實際：

- 基準（`bd91fbf`）：750 passed、1 skipped。
- RT-1 全部修正後、合併 main 前（`0341c0a`／`d8ad8d8`）：874 passed、2 skipped（openai extra 未安裝、
  連接埠 8765 被占用，都是環境因素）。
- 合併 origin/main 後（`47604ec`）：1051 passed、2 skipped（Windows）。
- 加上 `99ef404` 後：Linux／SQLite 3.45.1（與 CI 相同版本）1043 passed，PR #17 的 test CI 通過。

### 實機驗證

- 2026-09-29，經使用者同意的一次 `once`：結束碼 0，時段 11:20 `new`，封存與當月 JSONL 都寫入；未定機組 0、
  過期決定 0。首次品質 `warn` 正是上面的浮點問題，修正後重新解析同一份封存為 `ok`。
- 2026-10-05，使用者以 `即時收集啟動.bat` 連續執行：10:30–11:10 共 5 個時段全部 `new`、0 失敗、品質 ok；
  第二次啟動時清掉遺留的 `stop.request`；睡眠喚醒寫出 `resume`（03:15:20Z → 03:18:55Z）；
  `停止即時收集.bat` 寫出 `shutdown`。記在 log.md CP-073。

### 儲存點編號

計畫原本把設計記為 CP-071、收集器記為 CP-072。合併時 main 已用掉 CP-071，因此設計順延為 **CP-072**、
收集器為 **CP-073**（`47604ec`）；本計畫內文的編號已一併改正。

### Commit trailer

全域約束要求 `Co-Authored-By: Claude Opus 5.5`，但有 12 個 RT-1 commit 的 trailer 是 Haiku 或 Sonnet
（實作者的模型）：`847466b`、`9f76abd`、`b69e22e`、`b29b103`、`69956b6`、`9d0d322`、`a16e42d`、
`0004959`、`2019458`、`c748749`、`aeb2257`（Haiku 4.5），`bccea3a`（Sonnet 5.5）。已推上 PR，未改寫歷史。

## Global Constraints

- 不新增相依套件：只用標準函式庫、PyYAML、certifi（`pyproject.toml` 已有）。
- 時區一律固定 UTC+8（`timezone(timedelta(hours=8))`）；**禁止 `zoneinfo`**，部署機的 Windows Python 沒有 IANA 時區資料庫。
- 語法下限 Python 3.11（`requires-python = ">=3.11"`、ruff `target-version = "py311"`）。
- ruff：行寬 100，規則 E、F、I、UP、B、SIM。每個任務結束前 `uv run ruff format --check .` 與 `uv run ruff check .` 都要通過。
- 測試不連網：網路一律經可注入的 opener；時間一律經可注入的 clock／sleep；資料庫一律建在 `tmp_path`。
- Windows 專屬測試加 `pytestmark = pytest.mark.skipif(os.name != "nt", reason=...)`；CI 跑在 Ubuntu。
- 絕不提交 `data/`、`logs/` 或任何 `*.db`（合併門檻 BLOCK）。
- `.bat` 檔必須是 **CRLF 行尾**、UTF-8 無 BOM，而且 `chcp 65001 >nul` 之後才出現中文。
- 使用者看得到的訊息用繁體中文；識別字用英文；實體表英文、`v_` 檢視中文，與 `power.db` 一致。
- 缺值存 NULL，不是 0；只有 `value_status = 'ok'` 的值算進估算發電量。
- RT-1 **不修改** `src/text2sql/`、`src/serving/`、`src/eval/`、`benchmarks/`、`corpus/`、`configs/guard.yaml`、`configs/coverage.yaml`、README、ATTRIBUTION、SYSTEM_CARD（屬 RT-3，規格 §11.2）。
- 分支：從 `docs/realtime-ingest-design`（含規格與本計畫）開 `feat/realtime-collector`；若該分支已併入 main，改從 `origin/main` 開。
- Commit：Conventional Commits，訊息結尾加一行 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。log.md 的儲存點（CP-073）在最後一個任務一次寫入。
- 所有程式碼都已在與本 repo 相同的目錄結構下實跑驗證：15 個測試檔共 99 項測試全數通過，ruff check 與 ruff format 皆無問題。照抄即可；若環境不同導致失敗，先查原因再改，不要改測試去遷就實作。

## File Structure

| 檔案 | 職責 | Task |
|---|---|---|
| `src/ingest/realtime/__init__.py` | 套件說明 | 1 |
| `src/ingest/realtime/timeutil.py` | 固定 UTC+8、時段取整與格式 | 1 |
| `src/ingest/realtime/config.py` | 讀 `configs/realtime.yaml` 與 `config.yaml` 的路徑 | 1 |
| `configs/realtime.yaml` | 收集器行為參數（規格附錄 A） | 1 |
| `configs/config.yaml` | 新增三個路徑 | 1 |
| `tests/realtime_support.py` | 測試共用：設定、真實快照、迷你回應、專案根目錄 | 1 |
| `tests/fixtures/realtime/d006001_2026-09-18T2140.json` | 真實快照（repo 內 `taipower_align/units_generation.json` 的副本） | 1 |
| `src/ingest/realtime/parse.py` | 純函式：原始位元組 → 解析結果或拒收 | 2 |
| `src/ingest/realtime/archive.py` | 封存與抓取紀錄 | 3 |
| `src/ingest/realtime/decisions.py` | 人工決定檔與電廠名冊 | 4 |
| `src/ingest/realtime/store.py` | schema、入庫、維度同步、抓取紀錄鏡像 | 5 |
| `src/ingest/realtime/maintenance.py` | 日彙總、缺口分類、清除 | 6 |
| `src/ingest/realtime/rebuild.py` | 從封存原地重建 | 7 |
| `src/ingest/realtime/schedule.py` | 純函式：下一次什麼時候抓 | 8 |
| `src/ingest/realtime/client.py` | 條件式請求 | 9 |
| `src/ingest/realtime/lock.py` | 單一實例鎖 | 10 |
| `src/ingest/realtime/collector.py` | 啟動順序、主迴圈、維護 | 11 |
| `src/ingest/realtime/status.py` | 健康狀態（`status` 與 RT-3 共用） | 12 |
| `src/ingest/realtime/__main__.py` | `run`／`once`／`rebuild`／`status`／`stop` | 12 |
| `src/ingest/realtime/candidates.py` | 人工決定檔的候選產生器（執行期不用） | 13 |
| `taipower_align/realtime_units.csv` | 人工審核後的決定 | 13 |
| `即時收集啟動.bat`、`開機自動啟動-即時收集.bat`、`停止即時收集.bat` | 啟動、登入後自動啟動、停止 | 14 |
| `.gitattributes` | `*.bat` 固定 CRLF | 14 |
| `docs/SERVING.md`、`docs/lineage/*`、`log.md` | 操作說明、資料血緣、儲存點 | 15 |

相對於最初的規格，寫計畫時多了 `timeutil.py`（時段運算被 parse、schedule、maintenance、status 共用）、`candidates.py`（規格 §5.3「程式依名稱前綴產生候選」的那支程式）、`.gitattributes`，以及 `__main__` 的 `stop` 子指令（讓停止批次檔的「等 60 秒、逾時顯示 PID」在 Python 裡實作並可測試）。這幾處已同步寫回規格 §4.5、§4.6、§11.1。

---

### Task 1: 設定、時間工具與測試基礎

**Files:**
- Create: `src/ingest/realtime/__init__.py`、`src/ingest/realtime/timeutil.py`、`src/ingest/realtime/config.py`、`configs/realtime.yaml`
- Modify: `configs/config.yaml`（`paths` 區段末尾加三行）
- Create: `tests/realtime_support.py`、`tests/fixtures/realtime/d006001_2026-09-18T2140.json`
- Test: `tests/test_realtime_config.py`

**Interfaces:**
- Produces: `ingest.realtime.timeutil` — `TAIPEI`、`SLOT_FORMAT`、`to_taipei(dt) -> datetime`、`floor_slot(dt, minutes) -> datetime`、`format_slot(dt) -> str`、`parse_slot(text) -> datetime`、`local_date(dt) -> date`、`utc_iso(dt) -> str`、`parse_utc_iso(text) -> datetime`
- Produces: `ingest.realtime.config` — `RealtimeConfigError`、`SourceConfig`、`ScheduleConfig`、`ValidationConfig`、`RealtimeConfig`（屬性 `archive_dir`、`attempts_dir`、`lock_path`、`stop_path`、`slots_per_day`）、`load_config(root=PROJECT_ROOT) -> RealtimeConfig`
- Produces（測試用）: `tests/realtime_support.py` — `make_config(tmp_path, **overrides)`、`snapshot_payload()`、`payload_bytes(payload=None, *, data_time=None)`、`tiny_payload(data_time, rows)`、`write_project(root)`、`SNAPSHOT`、`KNOWN_TYPES`、`PLANTS_CSV`、`REALTIME_YAML`

- [ ] **Step 1: 開分支並複製測試用的真實快照**

```bash
git fetch origin
git switch -c feat/realtime-collector docs/realtime-ingest-design
uv run python -c "import json, pathlib, shutil; target = pathlib.Path('tests/fixtures/realtime/d006001_2026-09-18T2140.json'); target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile('taipower_align/units_generation.json', target); data = json.loads(target.read_text(encoding='utf-8-sig')); print(data['DateTime'], len(data['aaData']))"
```

Expected: 印出 `2026-09-18T21:40:00 215`。比對內容而不是雜湊：`core.autocrlf` 會改變工作目錄裡的行尾，同一份內容在不同機器上的位元組不同。印出的不是這兩個值，代表 `taipower_align/units_generation.json` 已被更新；改用 `git show fd9fc77:taipower_align/units_generation.json > tests/fixtures/realtime/d006001_2026-09-18T2140.json` 取出這一版，測試的預期值都以這一版為準。

- [ ] **Step 2: 寫測試共用模組 `tests/realtime_support.py`**

```python
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
```

- [ ] **Step 3: 寫會失敗的測試 `tests/test_realtime_config.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from ingest.realtime.config import RealtimeConfigError, load_config
from ingest.realtime.timeutil import TAIPEI, floor_slot, format_slot, parse_slot, to_taipei

REALTIME_YAML = """\
source:
  url: https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json
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
  known_types: [燃氣, 儲能負載]
  unreliable_notes: [通訊異常]
retention:
  raw_days: 14
rollup:
  day_closed_after: "00:15"
status:
  stale_after_minutes: 30
"""

CONFIG_YAML = """\
paths:
  plants_csv: taipower_align/plants.csv
  realtime_database: data/processed/realtime.db
  realtime_root: data/realtime
  realtime_units_csv: taipower_align/realtime_units.csv
"""


def _write_root(root: Path, realtime_yaml: str = REALTIME_YAML) -> Path:
    (root / "configs").mkdir(parents=True)
    (root / "configs/realtime.yaml").write_text(realtime_yaml, encoding="utf-8")
    (root / "configs/config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    return root


def test_slots_are_utc_plus_8_and_floor_to_ten_minutes() -> None:
    moment = datetime(2026, 9, 24, 0, 47, 31, tzinfo=UTC)  # 08:47:31 in Taipei
    assert format_slot(floor_slot(moment, 10)) == "2026-09-24 08:40"
    assert parse_slot("2026-09-24 08:40") == datetime(2026, 9, 24, 8, 40, tzinfo=TAIPEI)


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="時區"):
        to_taipei(datetime(2026, 9, 24, 8, 40))


def test_load_config_reads_both_files(tmp_path: Path) -> None:
    config = load_config(_write_root(tmp_path))

    assert config.schedule.backoff_after_failures == (10, 20)
    assert config.validation.known_types == frozenset({"燃氣", "儲能負載"})
    assert config.day_closed_after == time(0, 15)
    assert config.database == tmp_path.resolve() / "data/processed/realtime.db"
    assert config.archive_dir == tmp_path.resolve() / "data/realtime/archive"
    assert config.slots_per_day == 144


def test_unquoted_closing_time_is_explained(tmp_path: Path) -> None:
    # PyYAML 把沒加引號的 12:30 讀成六十進位整數 750；00:15 則剛好還是字串。
    yaml_text = REALTIME_YAML.replace('day_closed_after: "00:15"', "day_closed_after: 12:30")
    with pytest.raises(RealtimeConfigError, match="加引號"):
        load_config(_write_root(tmp_path, yaml_text))


def test_malformed_closing_time_is_refused(tmp_path: Path) -> None:
    yaml_text = REALTIME_YAML.replace('day_closed_after: "00:15"', 'day_closed_after: "late"')
    with pytest.raises(RealtimeConfigError, match="HH:MM"):
        load_config(_write_root(tmp_path, yaml_text))


def test_backoff_lists_must_pair_up(tmp_path: Path) -> None:
    yaml_text = REALTIME_YAML.replace("backoff_seconds: [120, 300]", "backoff_seconds: [120]")
    with pytest.raises(RealtimeConfigError, match="長度必須相同"):
        load_config(_write_root(tmp_path, yaml_text))


def test_missing_key_names_the_key(tmp_path: Path) -> None:
    yaml_text = REALTIME_YAML.replace("  retry_seconds: 60\n", "")
    with pytest.raises(RealtimeConfigError, match="retry_seconds"):
        load_config(_write_root(tmp_path, yaml_text))
```

- [ ] **Step 4: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_config.py -q`
Expected: 收集階段失敗，`ModuleNotFoundError: No module named 'ingest.realtime'`

- [ ] **Step 5: 寫實作**

`src/ingest/realtime/__init__.py`：

```python
"""即時機組發電量收集器（RT-1）。

設計見 docs/superpowers/specs/2026-09-24-realtime-ingest-design.md。
"""
```

`src/ingest/realtime/timeutil.py`：

```python
"""Time helpers shared by the realtime collector.

台灣自 1979 年起沒有夏令時間，一律用固定的 UTC+8。不用 zoneinfo：部署機的 Windows Python
沒有 IANA 時區資料庫，`ZoneInfo("Asia/Taipei")` 會直接拋出 ZoneInfoNotFoundError。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
SLOT_FORMAT = "%Y-%m-%d %H:%M"


def to_taipei(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("時間必須帶時區，否則分不出是當地時間還是 UTC。")
    return moment.astimezone(TAIPEI)


def floor_slot(moment: datetime, minutes: int) -> datetime:
    """往下取整到時段開頭（UTC+8）：08:47 → 08:40。"""
    local = to_taipei(moment)
    return local.replace(minute=local.minute - local.minute % minutes, second=0, microsecond=0)


def format_slot(slot: datetime) -> str:
    return to_taipei(slot).strftime(SLOT_FORMAT)


def parse_slot(text: str) -> datetime:
    return datetime.strptime(text, SLOT_FORMAT).replace(tzinfo=TAIPEI)


def local_date(moment: datetime) -> date:
    return to_taipei(moment).date()


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_utc_iso(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)
```

`src/ingest/realtime/config.py`：

```python
"""Load the collector settings: behaviour from configs/realtime.yaml, paths from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import yaml

from ingest.validate import PROJECT_ROOT, load_project_config


class RealtimeConfigError(ValueError):
    """即時收集設定不完整或不合法。"""


@dataclass(frozen=True)
class SourceConfig:
    url: str
    timeout_seconds: float
    max_bytes: int


@dataclass(frozen=True)
class ScheduleConfig:
    slot_minutes: int
    first_probe_seconds: int
    retry_seconds: int
    backoff_after_failures: tuple[int, ...]
    backoff_seconds: tuple[int, ...]
    retry_after_cap_seconds: int
    loop_max_sleep_seconds: int
    resume_gap_seconds: int


@dataclass(frozen=True)
class ValidationConfig:
    future_tolerance_seconds: int
    subtotal_tolerance_mw: float
    detail_rows_expected: tuple[int, int]
    known_types: frozenset[str]
    unreliable_notes: frozenset[str]


@dataclass(frozen=True)
class RealtimeConfig:
    source: SourceConfig
    schedule: ScheduleConfig
    validation: ValidationConfig
    raw_days: int
    day_closed_after: time
    stale_after_minutes: int
    database: Path
    root: Path
    units_csv: Path
    plants_csv: Path
    log_dir: Path

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def attempts_dir(self) -> Path:
        return self.root / "attempts"

    @property
    def lock_path(self) -> Path:
        return self.root / "collector.lock"

    @property
    def stop_path(self) -> Path:
        return self.root / "stop.request"

    @property
    def slots_per_day(self) -> int:
        return 24 * 60 // self.schedule.slot_minutes


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise RealtimeConfigError(f"configs/realtime.yaml 缺少 `{name}` 區段。")
    return value


def _clock(value: Any) -> time:
    if not isinstance(value, str):
        raise RealtimeConfigError(
            'rollup.day_closed_after 必須是加引號的 "HH:MM"；'
            "沒加引號時 YAML 會把 10:15 這類時間讀成六十進位的整數（615）。"
        )
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except ValueError as error:
        raise RealtimeConfigError(f"rollup.day_closed_after 不是 HH:MM：{value!r}") from error


def _check(config: RealtimeConfig) -> None:
    schedule = config.schedule
    if schedule.slot_minutes <= 0 or 60 % schedule.slot_minutes:
        raise RealtimeConfigError("schedule.slot_minutes 必須能整除 60。")
    if len(schedule.backoff_after_failures) != len(schedule.backoff_seconds):
        raise RealtimeConfigError("backoff_after_failures 與 backoff_seconds 的長度必須相同。")
    if list(schedule.backoff_after_failures) != sorted(set(schedule.backoff_after_failures)):
        raise RealtimeConfigError("backoff_after_failures 必須嚴格遞增。")
    low, high = config.validation.detail_rows_expected
    if low > high:
        raise RealtimeConfigError("validation.detail_rows_expected 必須是 [下限, 上限]。")
    if config.raw_days < 1:
        raise RealtimeConfigError("retention.raw_days 至少要 1 天。")


def load_config(root: Path = PROJECT_ROOT) -> RealtimeConfig:
    root = root.resolve()
    with (root / "configs/realtime.yaml").open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    paths = load_project_config(root)["paths"]
    try:
        source = _section(raw, "source")
        schedule = _section(raw, "schedule")
        validation = _section(raw, "validation")
        low, high = validation["detail_rows_expected"]
        config = RealtimeConfig(
            source=SourceConfig(
                url=str(source["url"]),
                timeout_seconds=float(source["timeout_seconds"]),
                max_bytes=int(source["max_bytes"]),
            ),
            schedule=ScheduleConfig(
                slot_minutes=int(schedule["slot_minutes"]),
                first_probe_seconds=int(schedule["first_probe_seconds"]),
                retry_seconds=int(schedule["retry_seconds"]),
                backoff_after_failures=tuple(int(v) for v in schedule["backoff_after_failures"]),
                backoff_seconds=tuple(int(v) for v in schedule["backoff_seconds"]),
                retry_after_cap_seconds=int(schedule["retry_after_cap_seconds"]),
                loop_max_sleep_seconds=int(schedule["loop_max_sleep_seconds"]),
                resume_gap_seconds=int(schedule["resume_gap_seconds"]),
            ),
            validation=ValidationConfig(
                future_tolerance_seconds=int(validation["future_tolerance_seconds"]),
                subtotal_tolerance_mw=float(validation["subtotal_tolerance_mw"]),
                detail_rows_expected=(int(low), int(high)),
                known_types=frozenset(str(v) for v in validation["known_types"]),
                unreliable_notes=frozenset(str(v) for v in validation["unreliable_notes"]),
            ),
            raw_days=int(_section(raw, "retention")["raw_days"]),
            day_closed_after=_clock(_section(raw, "rollup")["day_closed_after"]),
            stale_after_minutes=int(_section(raw, "status")["stale_after_minutes"]),
            database=root / paths["realtime_database"],
            root=root / paths["realtime_root"],
            units_csv=root / paths["realtime_units_csv"],
            plants_csv=root / paths["plants_csv"],
            log_dir=root / "logs",
        )
    except KeyError as error:
        raise RealtimeConfigError(f"即時收集設定缺少 {error}。") from error
    _check(config)
    return config
```

`configs/realtime.yaml`：

```yaml
# 即時機組發電量收集器（RT-1）的行為參數；路徑在 configs/config.yaml 的 paths。
# 設計見 docs/superpowers/specs/2026-09-24-realtime-ingest-design.md。
source:
  url: https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json
  timeout_seconds: 20
  max_bytes: 1000000
schedule:
  slot_minutes: 10
  first_probe_seconds: 320          # 時段開始後 5:20；實測發布延遲 5:08–5:09
  retry_seconds: 60
  backoff_after_failures: [10, 20]  # 連續失敗達到這些次數時，改用下一行對應的間隔
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
  day_closed_after: "00:15"         # 一定要加引號：沒加引號的 10:15 會被 YAML 讀成整數 615
status:
  stale_after_minutes: 30
```

`configs/config.yaml`：在 `paths:` 區段最後（`data_quality_report` 那一行之後）加入三行：

```yaml
  realtime_database: data/processed/realtime.db
  realtime_root: data/realtime
  realtime_units_csv: taipower_align/realtime_units.csv
```

資料中心只取資料槽那幾個 key（`src/serving/app.py` 的 `build_managed_service`），新增的 key 不影響受控版本。

- [ ] **Step 6: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_config.py -q`
Expected: `7 passed`

另外確認正式設定檔讀得起來：

Run: `uv run python -c "from ingest.realtime.config import load_config; c = load_config(); print(c.database, c.slots_per_day)"`
Expected: 印出 `...\data\processed\realtime.db 144`

- [ ] **Step 7: Lint 與全套測試**

Run: `uv run ruff format --check . && uv run ruff check . && uv run pytest -q`
Expected: ruff 無問題；pytest 全數通過（新增的 key 不影響既有測試）

- [ ] **Step 8: Commit**

```bash
git add src/ingest/realtime/__init__.py src/ingest/realtime/timeutil.py src/ingest/realtime/config.py configs/realtime.yaml configs/config.yaml tests/realtime_support.py tests/fixtures/realtime/d006001_2026-09-18T2140.json tests/test_realtime_config.py
git commit -m "feat: add the realtime collector config and time helpers" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 解析與驗證

依據規格 §6：結構有問題整份拒收（`PayloadRejected`），數值有問題逐列標記後照收（`ParseWarning`）。

**Files:**
- Create: `src/ingest/realtime/parse.py`
- Test: `tests/test_realtime_parse.py`

**Interfaces:**
- Consumes: `ValidationConfig`（Task 1）、`TAIPEI`、`format_slot`（Task 1）
- Produces: `PayloadRejected(code, detail, *, data_time=None)`（屬性 `code`、`detail`、`data_time`）、`ParseWarning(code, detail)`、`DetailRow(unit_type, unit_type_raw, unit_name, flow, net_mw, capacity_mw, load_ratio, note, value_status)`、`SubtotalRow(unit_type, subtotal_name, net_mw, net_share_pct, capacity_mw, capacity_share_pct, detail_net_mw)`、`QuarantinedRow(row_index, reason, raw_row)`、`ParsedSnapshot(data_time, details, subtotals, quarantined, warnings)`（屬性 `quality`）、`clean_type`、`parse_number`、`parse_ratio`、`parse_with_share`、`read_datetime(raw) -> datetime | None`、`parse_payload(raw, *, now, validation, slot_minutes) -> ParsedSnapshot`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from realtime_support import make_config, payload_bytes, snapshot_payload, tiny_payload

from ingest.realtime.parse import (
    PayloadRejected,
    clean_type,
    parse_number,
    parse_payload,
    parse_ratio,
    parse_with_share,
    read_datetime,
)

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)  # 22:00 in Taipei, after the 21:40 snapshot


def _parse(raw: bytes, tmp_path: Path, **overrides: object):
    config = make_config(tmp_path, **overrides)
    return parse_payload(
        raw, now=NOW, validation=config.validation, slot_minutes=config.schedule.slot_minutes
    )


def _details(parsed) -> dict[tuple[str, str], object]:
    return {(row.unit_type, row.unit_name): row for row in parsed.details}


def test_real_snapshot_parses_cleanly(tmp_path: Path) -> None:
    parsed = _parse(payload_bytes(), tmp_path)

    assert parsed.data_time == "2026-09-18 21:40"
    assert len(parsed.details) == 204
    assert len(parsed.subtotals) == 11
    assert parsed.quarantined == ()
    assert parsed.quality == "ok", parsed.warnings


def test_names_repeat_across_types_and_stay_separate(tmp_path: Path) -> None:
    details = _details(_parse(payload_bytes(), tmp_path))

    discharge = details[("儲能", "明潭#1")]
    charge = details[("儲能負載", "明潭#1")]
    assert discharge.flow == "generation"
    assert charge.flow == "storage_load"
    assert charge.unit_type_raw == "儲能負載(Energy Storage System Load)</b>"
    assert details[("儲能負載", "電池(註16)")].net_mw == -19.4
    assert ("太陽能", "其它台電自有") in details
    assert ("風力", "其它台電自有") in details


def test_subtotal_with_a_footnote_is_still_a_subtotal(tmp_path: Path) -> None:
    parsed = _parse(payload_bytes(), tmp_path)
    subtotals = {row.unit_type: row for row in parsed.subtotals}

    assert subtotals["風力"].subtotal_name == "小計(註5)"
    assert subtotals["風力"].net_mw == 2561.4
    assert subtotals["風力"].detail_net_mw == 2561.4
    assert subtotals["燃氣"].net_share_pct == 50.877
    assert "儲能負載" not in subtotals
    assert not any(row.unit_name.startswith("小計") for row in parsed.details)


def test_values_become_null_not_zero(tmp_path: Path) -> None:
    details = _details(_parse(payload_bytes(), tmp_path))

    island = details[("燃料油", "離島其它(註4)")]
    assert island.net_mw is None
    assert island.value_status == "missing"
    trial = details[("燃氣", "台中CC#1(註10)")]
    assert trial.capacity_mw is None
    assert trial.load_ratio is None
    assert details[("燃氣", "大潭CC#1")].load_ratio == 0.79521
    assert details[("燃氣", "大潭CC#1")].note == ""


def test_communication_failure_marks_the_value_untrusted(tmp_path: Path) -> None:
    row = _details(_parse(payload_bytes(), tmp_path))[("風力", "龍三風(註10)")]

    assert row.net_mw == 0.0
    assert row.value_status == "comm_error"


# 參數用短 id：pytest 會把測試 id 放進環境變數，整份回應當 id 會超過 Windows 的 32,767 字元上限。
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param(b"not json", "PAYLOAD_INVALID", id="not-json"),
        pytest.param(json.dumps({"aaData": []}).encode(), "PAYLOAD_INVALID", id="no-datetime"),
        pytest.param(
            payload_bytes(data_time="2026-09-18T21:45:00"), "DATETIME_OFF_SLOT", id="off-slot"
        ),
        pytest.param(
            payload_bytes(data_time="2026-09-18T23:00:00"), "DATETIME_IN_FUTURE", id="future"
        ),
    ],
)
def test_structural_problems_reject_the_whole_payload(
    raw: bytes, code: str, tmp_path: Path
) -> None:
    with pytest.raises(PayloadRejected) as caught:
        _parse(raw, tmp_path)
    assert caught.value.code == code


def test_missing_field_rejects(tmp_path: Path) -> None:
    payload = snapshot_payload()
    del payload["aaData"][0]["淨發電量(MW)"]

    with pytest.raises(PayloadRejected) as caught:
        _parse(payload_bytes(payload), tmp_path)
    assert caught.value.code == "FIELD_MISSING"
    assert caught.value.data_time == "2026-09-18 21:40"


def test_duplicate_key_rejects(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["aaData"].append(dict(payload["aaData"][0]))

    with pytest.raises(PayloadRejected) as caught:
        _parse(payload_bytes(payload), tmp_path)
    assert caught.value.code == "DUPLICATE_KEY"


def test_only_subtotals_rejects(tmp_path: Path) -> None:
    raw = tiny_payload("2026-09-18T21:40:00", [("燃氣", "小計", "1(1%)", "1(1%)", "")])

    with pytest.raises(PayloadRejected) as caught:
        _parse(raw, tmp_path)
    assert caught.value.code == "NO_DETAIL_ROWS"


def test_subtotal_mismatch_is_flagged_not_rejected(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["aaData"][0]["淨發電量(MW)"] = "600.6"  # 大潭CC#1 was 590.6

    parsed = _parse(payload_bytes(payload), tmp_path)
    assert parsed.quality == "warn"
    assert [w.code for w in parsed.warnings] == ["SUBTOTAL_MISMATCH"]
    assert "燃氣" in parsed.warnings[0].detail


def test_renamed_aggregate_is_quarantined(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["aaData"].insert(
        1,
        {
            "機組類型": "燃氣",
            "機組名稱": "合計",
            "裝置容量(MW)": "15918.1(26.052%)",
            "淨發電量(MW)": "16521.4(50.877%)",
            "淨發電量/裝置容量比(%)": "",
            "備註": "",
        },
    )

    parsed = _parse(payload_bytes(payload), tmp_path)
    assert [row.reason for row in parsed.quarantined] == ["UNRECOGNIZED_AGGREGATE"]
    assert parsed.quarantined[0].row_index == 1
    assert "UNRECOGNIZED_AGGREGATE" in [w.code for w in parsed.warnings]
    assert ("燃氣", "合計") not in _details(parsed)


def test_value_and_shape_warnings(tmp_path: Path) -> None:
    raw = tiny_payload(
        "2026-09-18T21:40:00",
        [("核融合", "新機組", "100", "abc", ""), ("燃氣", "大潭CC#1", "742.7", "590.6", "")],
    )
    payload = json.loads(raw)
    payload["aaData"][1]["新欄位"] = "x"

    parsed = _parse(json.dumps(payload, ensure_ascii=False).encode(), tmp_path)
    codes = [w.code for w in parsed.warnings]
    assert codes == ["UNKNOWN_TYPE", "ROW_COUNT_UNUSUAL", "EXTRA_FIELD", "VALUE_UNPARSEABLE"]
    assert _details(parsed)[("核融合", "新機組")].value_status == "invalid"


def test_cleaning_helpers() -> None:
    assert clean_type("儲能負載(Energy Storage System Load)</b>") == "儲能負載"
    assert clean_type("民營電廠-燃氣") == "民營電廠-燃氣"
    assert parse_number("1,234.5") == (1234.5, "ok")
    assert parse_number(" ") == (None, "missing")
    assert parse_number("N/A") == (None, "missing")
    assert parse_ratio("38.70066%") == (0.3870066, "ok")
    assert parse_with_share("16521.4(50.877%)") == (16521.4, 50.877)


def test_read_datetime_is_lenient() -> None:
    assert read_datetime(payload_bytes()) == datetime.fromisoformat("2026-09-18T21:40:00+08:00")
    assert read_datetime(b"garbage") is None
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_parse.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.parse'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/parse.py`**

```python
"""Parse one d006001 payload into validated rows (pure: no files, no database).

結構有問題就整份拒收（PayloadRejected）；數值有問題就逐列標記後照收（ParseWarning）。
拒收不等於遺失：原始回應在解析前就已經封存，修好規則後重建即可補回。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from ingest.realtime.config import ValidationConfig
from ingest.realtime.timeutil import TAIPEI, format_slot

REQUIRED_FIELDS = (
    "機組類型",
    "機組名稱",
    "裝置容量(MW)",
    "淨發電量(MW)",
    "淨發電量/裝置容量比(%)",
    "備註",
)
STORAGE_LOAD_TYPE = "儲能負載"
MISSING_TOKENS = frozenset({"", "-", "N/A"})

_TAG = re.compile(r"<[^>]+>")
_TRAILING_ENGLISH = re.compile(r"\s*\([A-Za-z0-9 .,&/'-]+\)\s*$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_NUMBER_WITH_SHARE = re.compile(r"(-?\d+(?:\.\d+)?)\((-?\d+(?:\.\d+)?)%\)")
_PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)%")


class PayloadRejected(ValueError):
    """整份不收。`data_time` 在讀得出 DateTime 時才有值。"""

    def __init__(self, code: str, detail: str, *, data_time: str | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.data_time = data_time


@dataclass(frozen=True)
class ParseWarning:
    code: str
    detail: str


@dataclass(frozen=True)
class DetailRow:
    unit_type: str
    unit_type_raw: str
    unit_name: str
    flow: str
    net_mw: float | None
    capacity_mw: float | None
    load_ratio: float | None
    note: str
    value_status: str


@dataclass(frozen=True)
class SubtotalRow:
    unit_type: str
    subtotal_name: str
    net_mw: float | None
    net_share_pct: float | None
    capacity_mw: float | None
    capacity_share_pct: float | None
    detail_net_mw: float


@dataclass(frozen=True)
class QuarantinedRow:
    row_index: int
    reason: str
    raw_row: str


@dataclass(frozen=True)
class ParsedSnapshot:
    data_time: str
    details: tuple[DetailRow, ...]
    subtotals: tuple[SubtotalRow, ...]
    quarantined: tuple[QuarantinedRow, ...]
    warnings: tuple[ParseWarning, ...]

    @property
    def quality(self) -> str:
        return "warn" if self.warnings else "ok"


def clean_type(raw: str) -> str:
    """`儲能負載(Energy Storage System Load)</b>` → `儲能負載`。"""
    return _TRAILING_ENGLISH.sub("", _TAG.sub("", raw).strip()).strip()


def parse_number(text: str) -> tuple[float | None, str]:
    """回傳（數值, 'ok'｜'missing'｜'invalid'）；`-`、`N/A`、空字串是缺值，不是 0。"""
    value = text.strip().replace(",", "")
    if value in MISSING_TOKENS:
        return None, "missing"
    if _NUMBER.fullmatch(value):
        return float(value), "ok"
    return None, "invalid"


def parse_ratio(text: str) -> tuple[float | None, str]:
    """`79.521%` → 0.79521。"""
    value = text.strip().replace(",", "")
    if value in MISSING_TOKENS:
        return None, "missing"
    match = _PERCENT.fullmatch(value)
    if match:
        return round(float(match.group(1)) / 100, 8), "ok"
    return None, "invalid"


def parse_with_share(text: str) -> tuple[float | None, float | None]:
    """小計的值：`16521.4(50.877%)` → (16521.4, 50.877)。"""
    value = text.strip().replace(",", "")
    match = _NUMBER_WITH_SHARE.fullmatch(value)
    if match:
        return float(match.group(1)), float(match.group(2))
    number, _status = parse_number(value)
    return number, None


def _looks_aggregated(text: object) -> bool:
    return bool(_NUMBER_WITH_SHARE.fullmatch(str(text).strip().replace(",", "")))


def _source_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("DateTime 不是字串")
    moment = datetime.fromisoformat(value.strip())
    return moment.replace(tzinfo=TAIPEI) if moment.tzinfo is None else moment.astimezone(TAIPEI)


def read_datetime(raw: bytes) -> datetime | None:
    """只讀出 DateTime（UTC+8），不驗證其他內容；讀不到就回 None。決定封存路徑用。"""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        return _source_datetime(payload["DateTime"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def parse_payload(
    raw: bytes, *, now: datetime, validation: ValidationConfig, slot_minutes: int
) -> ParsedSnapshot:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadRejected("PAYLOAD_INVALID", f"不是可解析的 JSON：{error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("aaData"), list):
        raise PayloadRejected("PAYLOAD_INVALID", "頂層必須是含 aaData 陣列的物件。")
    try:
        moment = _source_datetime(payload.get("DateTime"))
    except (TypeError, ValueError) as error:
        raise PayloadRejected(
            "PAYLOAD_INVALID", f"DateTime 無法解析：{payload.get('DateTime')!r}"
        ) from error
    data_time = format_slot(moment)
    if moment.minute % slot_minutes or moment.second or moment.microsecond:
        raise PayloadRejected(
            "DATETIME_OFF_SLOT",
            f"{moment.isoformat()} 不在 {slot_minutes} 分鐘整點。",
            data_time=data_time,
        )
    if moment > now + timedelta(seconds=validation.future_tolerance_seconds):
        raise PayloadRejected(
            "DATETIME_IN_FUTURE", f"{moment.isoformat()} 比現在晚太多。", data_time=data_time
        )

    details: list[DetailRow] = []
    subtotal_rows: list[tuple[str, dict[str, object]]] = []
    quarantined: list[QuarantinedRow] = []
    extra_fields: set[str] = set()
    unparseable: dict[str, list[str]] = defaultdict(list)
    for index, row in enumerate(payload["aaData"]):
        if not isinstance(row, dict):
            raise PayloadRejected(
                "PAYLOAD_INVALID", f"aaData 第 {index} 列不是物件。", data_time=data_time
            )
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            raise PayloadRejected(
                "FIELD_MISSING", f"第 {index} 列缺少欄位：{'、'.join(missing)}", data_time=data_time
            )
        extra_fields.update(str(key) for key in row if key not in REQUIRED_FIELDS)
        type_raw = str(row["機組類型"]).strip()
        unit_type = clean_type(type_raw)
        name = str(row["機組名稱"]).strip()
        if name.startswith("小計"):
            subtotal_rows.append((unit_type, row))
            continue
        if _looks_aggregated(row["淨發電量(MW)"]) or _looks_aggregated(row["裝置容量(MW)"]):
            raw_row = json.dumps(row, ensure_ascii=False)
            quarantined.append(QuarantinedRow(index, "UNRECOGNIZED_AGGREGATE", raw_row))
            continue
        net, net_status = parse_number(str(row["淨發電量(MW)"]))
        capacity, capacity_status = parse_number(str(row["裝置容量(MW)"]))
        ratio, ratio_status = parse_ratio(str(row["淨發電量/裝置容量比(%)"]))
        for field, status in (
            ("淨發電量(MW)", net_status),
            ("裝置容量(MW)", capacity_status),
            ("淨發電量/裝置容量比(%)", ratio_status),
        ):
            if status == "invalid":
                unparseable[field].append(name)
        note = str(row["備註"]).strip()
        value_status = net_status
        if net_status == "ok" and note in validation.unreliable_notes:
            value_status = "comm_error"
        flow = "storage_load" if unit_type == STORAGE_LOAD_TYPE else "generation"
        details.append(
            DetailRow(unit_type, type_raw, name, flow, net, capacity, ratio, note, value_status)
        )

    if not details:
        raise PayloadRejected("NO_DETAIL_ROWS", "沒有任何明細列。", data_time=data_time)
    keys = [(detail.unit_type, detail.unit_name) for detail in details]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    subtotal_types = [unit_type for unit_type, _row in subtotal_rows]
    duplicates += sorted({(t, "小計") for t in subtotal_types if subtotal_types.count(t) > 1})
    if duplicates:
        listed = "、".join(f"{unit_type}|{name}" for unit_type, name in duplicates)
        raise PayloadRejected(
            "DUPLICATE_KEY", f"重複的（類型, 名稱）：{listed}", data_time=data_time
        )

    warnings: list[ParseWarning] = []
    detail_sums: dict[str, float] = defaultdict(float)
    for detail in details:
        if detail.net_mw is not None:
            detail_sums[detail.unit_type] += detail.net_mw
    subtotals: list[SubtotalRow] = []
    for unit_type, row in subtotal_rows:
        net, net_share = parse_with_share(str(row["淨發電量(MW)"]))
        capacity, capacity_share = parse_with_share(str(row["裝置容量(MW)"]))
        detail_net = round(detail_sums.get(unit_type, 0.0), 3)
        name = str(row["機組名稱"]).strip()
        subtotals.append(
            SubtotalRow(unit_type, name, net, net_share, capacity, capacity_share, detail_net)
        )
        if net is not None and abs(detail_net - net) > validation.subtotal_tolerance_mw:
            warnings.append(
                ParseWarning(
                    "SUBTOTAL_MISMATCH", f"{unit_type}：明細 {detail_net:.1f} MW，小計 {net:.1f} MW"
                )
            )
    for row in quarantined:
        warnings.append(ParseWarning("UNRECOGNIZED_AGGREGATE", f"第 {row.row_index} 列已隔離"))
    for unit_type in sorted({detail.unit_type for detail in details} - validation.known_types):
        warnings.append(ParseWarning("UNKNOWN_TYPE", unit_type))
    low, high = validation.detail_rows_expected
    if not low <= len(details) <= high:
        warnings.append(
            ParseWarning("ROW_COUNT_UNUSUAL", f"明細 {len(details)} 列，預期 {low}–{high} 列")
        )
    if extra_fields:
        warnings.append(ParseWarning("EXTRA_FIELD", "、".join(sorted(extra_fields))))
    for field, names in unparseable.items():
        listed = "、".join(names[:5])
        warnings.append(ParseWarning("VALUE_UNPARSEABLE", f"{field} {len(names)} 列：{listed}"))
    return ParsedSnapshot(
        data_time, tuple(details), tuple(subtotals), tuple(quarantined), tuple(warnings)
    )
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_parse.py -q`
Expected: `17 passed`。其中 `test_real_snapshot_parses_cleanly` 證明真實快照零警告：204 列明細、11 列小計。

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/parse.py tests/test_realtime_parse.py
git commit -m "feat: parse and validate d006001 payloads" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 原始回應封存與抓取紀錄

依據規格 §5.1：每份內容不同的回應存一個檔，原樣 gzip、原子寫入，檔名帶 SHA-256 前 12 碼；抓取紀錄每次嘗試一行 JSON，當機時寫到一半的殘行不能連累下一筆。

**Files:**
- Create: `src/ingest/realtime/archive.py`
- Test: `tests/test_realtime_archive.py`

**Interfaces:**
- Consumes: `to_taipei`（Task 1）；測試用 `read_datetime`（Task 2）
- Produces: `UNPARSED_DIR`、`ArchivedFile(path, sha_prefix, data_time)`、`payload_sha256(raw) -> str`、`archive_payload(archive_dir, raw, *, source_time, fetched_at) -> ArchivedFile`、`read_payload(path) -> bytes`、`iter_archive(archive_dir, *, since=None) -> list[ArchivedFile]`、`append_attempt(attempts_dir, record)`、`iter_attempts(attempts_dir) -> Iterator[dict]`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import gzip
import os
from datetime import UTC, datetime
from pathlib import Path

from realtime_support import payload_bytes

from ingest.realtime.archive import (
    append_attempt,
    archive_payload,
    iter_archive,
    iter_attempts,
    payload_sha256,
    read_payload,
)
from ingest.realtime.parse import read_datetime

FETCHED = datetime(2026, 9, 18, 13, 45, 20, tzinfo=UTC)


def test_payload_is_archived_verbatim_under_its_slot(tmp_path: Path) -> None:
    raw = payload_bytes()
    archived = archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)

    digest = payload_sha256(raw)
    assert archived.path == tmp_path / "2026/09/18" / f"2140_{digest[:12]}.json.gz"
    assert archived.data_time == "2026-09-18 21:40"
    assert archived.sha_prefix == digest[:12]
    assert read_payload(archived.path) == raw
    assert gzip.decompress(archived.path.read_bytes()) == raw


def test_same_content_is_stored_once(tmp_path: Path) -> None:
    raw = payload_bytes()
    first = archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)
    stamp = first.path.stat().st_mtime_ns
    second = archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)

    assert second.path == first.path
    assert second.path.stat().st_mtime_ns == stamp
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1


def test_unreadable_datetime_goes_to_unparsed(tmp_path: Path) -> None:
    archived = archive_payload(tmp_path, b"garbage", source_time=None, fetched_at=FETCHED)

    assert archived.path.parent == tmp_path / "_unparsed"
    assert archived.path.name.startswith("20260918T134520Z_")
    assert archived.data_time is None
    assert iter_archive(tmp_path) == []


def test_iter_archive_orders_by_slot_then_write_time(tmp_path: Path) -> None:
    later = payload_bytes(data_time="2026-09-18T21:50:00")
    first = payload_bytes(data_time="2026-09-18T21:40:00")
    revised = first.replace(b"590.6", b"591.0")
    for raw in (later, first, revised):
        archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)
    older_revision = next(p for p in tmp_path.rglob("2140_*") if read_payload(p) == first)
    os.utime(older_revision, ns=(1, 1))

    found = iter_archive(tmp_path)
    assert [f.data_time for f in found] == [
        "2026-09-18 21:40",
        "2026-09-18 21:40",
        "2026-09-18 21:50",
    ]
    assert read_payload(found[0].path) == first
    assert iter_archive(tmp_path, since="2026-09-19") == []


def test_attempt_log_round_trips_and_survives_a_torn_line(tmp_path: Path) -> None:
    append_attempt(
        tmp_path, {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "fetch", "etag": None}
    )
    with (tmp_path / "2026-09.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"attempted_at": "2026-09-18T13:5')  # 當機時寫到一半
    append_attempt(tmp_path, {"attempted_at": "2026-09-18T13:55:20+00:00", "kind": "resume"})

    records = list(iter_attempts(tmp_path))
    assert records == [
        {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "fetch"},
        {"attempted_at": "2026-09-18T13:55:20+00:00", "kind": "resume"},
    ]
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_archive.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.archive'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/archive.py`**

```python
"""Raw payload archive and fetch-attempt log: the source of truth for rebuilds (§5.1).

封存檔解壓後與台電回傳的內容逐位元組相同，檔名帶 SHA-256 前 12 碼，所以內容可以自我驗證。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ingest.realtime.timeutil import to_taipei

UNPARSED_DIR = "_unparsed"


@dataclass(frozen=True)
class ArchivedFile:
    path: Path
    sha_prefix: str
    data_time: str | None  # 'YYYY-MM-DD HH:MM'；_unparsed 底下的檔案為 None


def payload_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def archive_payload(
    archive_dir: Path, raw: bytes, *, source_time: datetime | None, fetched_at: datetime
) -> ArchivedFile:
    """原樣 gzip 封存；同一時段內容相同只存一次。DateTime 讀不出來時放進 _unparsed/。"""
    digest = payload_sha256(raw)
    if source_time is None:
        stamp = fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = archive_dir / UNPARSED_DIR / f"{stamp}_{digest[:12]}.json.gz"
        data_time = None
    else:
        local = to_taipei(source_time)
        folder = archive_dir / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}"
        path = folder / f"{local:%H%M}_{digest[:12]}.json.gz"
        data_time = f"{local:%Y-%m-%d %H:%M}"
    if not path.exists():
        _atomic_write(path, gzip.compress(raw, compresslevel=9, mtime=0))
    return ArchivedFile(path, digest[:12], data_time)


def read_payload(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def iter_archive(archive_dir: Path, *, since: str | None = None) -> list[ArchivedFile]:
    """依（資料時段, 寫入時間）排序的封存檔；since 為 'YYYY-MM-DD' 時只列該日（含）之後。"""
    if not archive_dir.is_dir():
        return []
    found: list[tuple[str, int, str, ArchivedFile]] = []
    for path in archive_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.json.gz"):
        year, month, day = path.parts[-4:-1]
        hhmm, _, prefix = path.name.removesuffix(".json.gz").partition("_")
        if len(hhmm) != 4 or not hhmm.isdigit() or len(prefix) != 12:
            continue
        data_time = f"{year}-{month}-{day} {hhmm[:2]}:{hhmm[2:]}"
        if since is not None and data_time[:10] < since:
            continue
        archived = ArchivedFile(path, prefix, data_time)
        found.append((data_time, path.stat().st_mtime_ns, path.name, archived))
    found.sort(key=lambda item: item[:3])
    return [item[3] for item in found]


def _ensure_trailing_newline(path: Path) -> None:
    """當機時可能寫到一半；下一筆不能接在殘行後面，否則兩筆會一起壞掉。"""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
    if last != b"\n":
        with path.open("ab") as handle:
            handle.write(b"\n")


def append_attempt(attempts_dir: Path, record: Mapping[str, object]) -> None:
    """每次嘗試一行 JSON，依 attempted_at 的 UTC 月份分檔；沒有值的欄位省略。"""
    path = attempts_dir / f"{str(record['attempted_at'])[:7]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_trailing_newline(path)
    line = json.dumps(
        {key: value for key, value in record.items() if value is not None},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def iter_attempts(attempts_dir: Path) -> Iterator[dict[str, object]]:
    if not attempts_dir.is_dir():
        return
    for path in sorted(attempts_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue  # 當機時寫到一半的殘行
                if isinstance(record, dict):
                    yield record
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_archive.py -q`
Expected: `5 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/archive.py tests/test_realtime_archive.py
git commit -m "feat: archive raw payloads and the fetch attempt log" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 人工決定檔

依據規格 §5.3：沒列在檔案裡的機組一律未定；標頭不符或重複列讓整份無效（呼叫端沿用上一次成功套用的內容）；單列不合法只讓那一列變成未定。

**Files:**
- Create: `src/ingest/realtime/decisions.py`
- Test: `tests/test_realtime_decisions.py`

**Interfaces:**
- Produces: `DECISION_FIELDS`、`GRAINS`、`SCOPES`、`DecisionFileError`、`UnitDecision(grain, access_scope, plant_id, note)`、`Decisions(units, plants, warnings, sha256, plants_sha256)`、`file_sha256(path) -> str`、`load_plants(path) -> dict[int, str]`、`empty_decisions(plants_csv) -> Decisions`、`load_decisions(units_csv, plants_csv) -> Decisions`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

from pathlib import Path

import pytest
from realtime_support import make_config

from ingest.realtime.decisions import DecisionFileError, UnitDecision, load_decisions

HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"


def _load(tmp_path: Path, body: str):
    config = make_config(tmp_path)
    config.units_csv.write_text(HEADER + body, encoding="utf-8")
    return load_decisions(config.units_csv, config.plants_csv)


def test_valid_rows_become_decisions(tmp_path: Path) -> None:
    decisions = _load(
        tmp_path,
        "燃氣,大潭CC#1,unit,plant,8,\n"
        "太陽能,其它購電太陽能,bucket,shared,,購電太陽能全部併在這一列\n",
    )

    assert decisions.units[("燃氣", "大潭CC#1")] == UnitDecision("unit", "plant", 8, "")
    assert decisions.units[("太陽能", "其它購電太陽能")].access_scope == "shared"
    assert decisions.plants[12] == "明潭發電廠"
    assert decisions.warnings == ()
    assert len(decisions.sha256) == 64


def test_missing_file_means_everything_undecided(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    decisions = load_decisions(config.units_csv, config.plants_csv)

    assert decisions.units == {}
    assert "未定" in decisions.warnings[0]
    assert decisions.sha256 == ""


def test_wrong_header_invalidates_the_whole_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.units_csv.write_text("type,name\n燃氣,大潭CC#1\n", encoding="utf-8")

    with pytest.raises(DecisionFileError, match="欄位必須是"):
        load_decisions(config.units_csv, config.plants_csv)


def test_duplicate_rows_invalidate_the_whole_file(tmp_path: Path) -> None:
    with pytest.raises(DecisionFileError, match="重複"):
        _load(tmp_path, "燃氣,大潭CC#1,unit,plant,8,\n燃氣,大潭CC#1,unit,shared,,\n")


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ("燃氣,大潭CC#1,unit,plant,99,", "plant_id"),
        ("燃氣,大潭CC#1,single,plant,8,", "grain"),
        ("燃氣,大潭CC#1,unit,everyone,,", "access_scope"),
        ("燃氣,大潭CC#1,unit,shared,8,", "shared"),
    ],
)
def test_invalid_row_is_skipped_with_a_warning(tmp_path: Path, row: str, reason: str) -> None:
    decisions = _load(tmp_path, row + "\n")

    assert decisions.units == {}
    assert reason in decisions.warnings[0]
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_decisions.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.decisions'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/decisions.py`**

```python
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
        rows = list(reader)
    seen: set[tuple[str, str]] = set()
    repeated: list[tuple[str, str]] = []
    for row in rows:
        key = (row["unit_type"].strip(), row["unit_name"].strip())
        if key in seen:
            repeated.append(key)
        seen.add(key)
    if repeated:
        listed = "、".join(f"{unit_type}|{name}" for unit_type, name in repeated)
        raise DecisionFileError(f"{units_csv.name} 有重複的（類型, 名稱）：{listed}")

    units: dict[tuple[str, str], UnitDecision] = {}
    warnings: list[str] = []
    for line, row in enumerate(rows, start=2):
        key = (row["unit_type"].strip(), row["unit_name"].strip())
        grain = row["grain"].strip()
        scope = row["access_scope"].strip()
        plant_text = (row["plant_id"] or "").strip()
        note = (row["note"] or "").strip()
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
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_decisions.py -q`
Expected: `8 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/decisions.py tests/test_realtime_decisions.py
git commit -m "feat: load the human decisions for realtime units" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: realtime.db schema 與入庫

依據規格 §5.2、§5.4：自然鍵是（清洗後類型, 名稱）；同一時段內容相同是 `duplicate`，不同是 `revised`；小計不進明細；三個 `v_rt_` 檢視。連線用 autocommit，交易由 `transaction()` 明確開啟，DDL 也能放進交易。

**Files:**
- Create: `src/ingest/realtime/store.py`
- Test: `tests/test_realtime_store.py`

**Interfaces:**
- Consumes: `Decisions`、`UnitDecision`（Task 4）、`ParsedSnapshot`（Task 2）、`utc_iso`（Task 1）
- Produces: `SCHEMA_VERSION = "1"`、`SCHEMA`、`TABLES_IN_DROP_ORDER`、`VIEWS_IN_DROP_ORDER`、`ATTEMPT_COLUMNS`、`connect(path) -> sqlite3.Connection`、`transaction(connection)`（context manager）、`quick_check`、`schema_version`、`create_schema`、`drop_schema`、`write_manifest(connection, *, build_kind, archive_files, decisions, now)`、`sync_plants(connection, plants)`、`sync_decisions(connection, decisions) -> list[tuple[str, str]]`、`ingest_snapshot(connection, parsed, *, sha256, fetched_at, decisions) -> str`、`record_attempt(connection, record)`、`latest_data_time(connection) -> str | None`、`snapshot_fetched_at(connection, data_time) -> tuple[str, str] | None`
- 契約：呼叫 `ingest_snapshot` 前必須先 `sync_plants`（外鍵）；`ingest_snapshot` 不自己開交易。

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from realtime_support import make_config, payload_bytes

from ingest.realtime import store
from ingest.realtime.archive import payload_sha256
from ingest.realtime.decisions import load_decisions
from ingest.realtime.parse import parse_payload

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"


def _setup(tmp_path: Path, decisions_body: str = ""):
    config = make_config(tmp_path)
    config.units_csv.write_text(HEADER + decisions_body, encoding="utf-8")
    decisions = load_decisions(config.units_csv, config.plants_csv)
    connection = store.connect(config.database)
    with store.transaction(connection):
        store.create_schema(connection)
        store.write_manifest(
            connection, build_kind="create", archive_files=0, decisions=decisions, now=NOW
        )
        store.sync_plants(connection, decisions.plants)
    return config, decisions, connection


def _ingest(connection, config, decisions, raw: bytes) -> str:
    parsed = parse_payload(
        raw, now=NOW, validation=config.validation, slot_minutes=config.schedule.slot_minutes
    )
    with store.transaction(connection):
        return store.ingest_snapshot(
            connection,
            parsed,
            sha256=payload_sha256(raw),
            fetched_at="2026-09-18T13:45:20+00:00",
            decisions=decisions,
        )


def test_schema_and_views_exist(tmp_path: Path) -> None:
    _config, _decisions, connection = _setup(tmp_path)
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    assert {"v_rt_now", "v_rt_10min", "v_rt_daily", "fact_rt_unit_10min"} <= names
    assert store.schema_version(connection) == store.SCHEMA_VERSION
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_ingesting_the_real_snapshot(tmp_path: Path) -> None:
    config, decisions, connection = _setup(
        tmp_path,
        "燃氣,大潭CC#1,unit,plant,8,\n"
        "儲能負載,明潭#1,unit,plant,12,抽蓄機組的充電負載\n"
        "太陽能,其它購電太陽能,bucket,shared,,\n",
    )

    assert _ingest(connection, config, decisions, payload_bytes()) == "new"
    count = lambda table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
    assert count("fact_rt_unit_10min") == 204
    assert count("fact_rt_type_subtotal") == 11
    assert count("dim_rt_unit") == 204

    rows = {
        (row[0], row[1]): row[2:]
        for row in connection.execute(
            'SELECT "機組類型", "機組名稱", "電廠", "粒度", "淨發電量_MW", "數值狀態" FROM v_rt_now'
        )
    }
    assert len(rows) == 204
    assert rows[("燃氣", "大潭CC#1")] == ("大潭發電廠", "個別", 590.6, "正常")
    assert rows[("儲能負載", "明潭#1")][0] == "明潭發電廠"
    assert rows[("太陽能", "其它購電太陽能")][:2] == (None, "彙總")
    assert rows[("風力", "沃一風")][1] == "未定"
    assert rows[("風力", "龍三風(註10)")][3] == "通訊異常"


def test_same_content_is_a_duplicate_and_changed_content_a_revision(tmp_path: Path) -> None:
    config, decisions, connection = _setup(tmp_path)
    raw = payload_bytes()
    _ingest(connection, config, decisions, raw)

    assert _ingest(connection, config, decisions, raw) == "duplicate"
    revised = raw.replace(b'"590.6"', b'"591.0"')
    assert _ingest(connection, config, decisions, revised) == "revised"

    row = connection.execute("SELECT revision, sha256 FROM fact_rt_snapshot").fetchone()
    assert row == (2, payload_sha256(revised))
    net = connection.execute(
        """SELECT net_mw FROM fact_rt_unit_10min f JOIN dim_rt_unit u ON u.id = f.unit_id
            WHERE u.unit_name = '大潭CC#1'"""
    ).fetchone()[0]
    assert net == 591.0
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 204


def test_later_decisions_are_applied_to_known_units(tmp_path: Path) -> None:
    config, decisions, connection = _setup(tmp_path)
    _ingest(connection, config, decisions, payload_bytes())
    config.units_csv.write_text(
        HEADER + "燃氣,大潭CC#1,unit,plant,8,\n風力,新風場,unit,shared,,\n", encoding="utf-8"
    )
    updated = load_decisions(config.units_csv, config.plants_csv)

    with store.transaction(connection):
        stale = store.sync_decisions(connection, updated)

    assert stale == [("風力", "新風場")]
    plant = connection.execute(
        """SELECT "電廠" FROM v_rt_now WHERE "機組名稱" = '大潭CC#1'"""
    ).fetchone()[0]
    assert plant == "大潭發電廠"
    manifest = connection.execute("SELECT decisions_sha256 FROM meta_rt_manifest").fetchone()[0]
    assert manifest == updated.sha256


def test_plant_scope_requires_a_plant_id(tmp_path: Path) -> None:
    _config, _decisions, connection = _setup(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO dim_rt_unit (unit_type, unit_type_raw, unit_name, flow, grain,
                   access_scope, plant_id, first_seen, last_seen)
               VALUES ('燃氣', '燃氣', 'X', 'generation', 'unit', 'plant', NULL, 'a', 'a')"""
        )


def test_attempts_are_recorded(tmp_path: Path) -> None:
    _config, _decisions, connection = _setup(tmp_path)
    with store.transaction(connection):
        store.record_attempt(
            connection,
            {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "fetch", "outcome": "error"},
        )

    assert connection.execute("SELECT kind, outcome, detail FROM meta_rt_attempt").fetchone() == (
        "fetch",
        "error",
        "",
    )
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_store.py -q`
Expected: `ImportError: cannot import name 'store' from 'ingest.realtime'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/store.py`**

```python
"""realtime.db: schema, writes and small reads (§5.2, §5.4).

只有收集器會寫入。連線用 autocommit（isolation_level=None），交易一律由 `transaction()` 明確開啟，
這樣 DDL 也能放進同一個交易，重建失敗時整份回滾。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ingest.realtime.decisions import Decisions, UnitDecision
from ingest.realtime.parse import ParsedSnapshot
from ingest.realtime.timeutil import utc_iso

SCHEMA_VERSION = "1"

VIEWS_IN_DROP_ORDER = ("v_rt_now", "v_rt_daily", "v_rt_10min")
TABLES_IN_DROP_ORDER = (
    "meta_rt_attempt",
    "fact_rt_day",
    "fact_rt_unit_daily",
    "fact_rt_quarantine",
    "fact_rt_type_subtotal",
    "fact_rt_unit_10min",
    "fact_rt_snapshot",
    "dim_rt_unit",
    "dim_rt_plant",
    "meta_rt_manifest",
)
ATTEMPT_COLUMNS = (
    "attempted_at",
    "kind",
    "target_slot",
    "outcome",
    "http_status",
    "error_type",
    "reject_code",
    "data_time",
    "sha256",
    "bytes",
    "elapsed_ms",
    "etag",
    "detail",
)

_GRAIN_LABEL = "CASE u.grain WHEN 'unit' THEN '個別' WHEN 'bucket' THEN '彙總' ELSE '未定' END"

SCHEMA = (
    """CREATE TABLE meta_rt_manifest (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version   TEXT    NOT NULL,
        built_at         TEXT    NOT NULL,
        build_kind       TEXT    NOT NULL CHECK (build_kind IN ('create', 'rebuild')),
        archive_files    INTEGER NOT NULL,
        decisions_sha256 TEXT    NOT NULL,
        plants_sha256    TEXT    NOT NULL
    )""",
    """CREATE TABLE dim_rt_plant (
        plant_id   INTEGER PRIMARY KEY,
        plant_name TEXT    NOT NULL UNIQUE
    )""",
    """CREATE TABLE dim_rt_unit (
        id            INTEGER PRIMARY KEY,
        unit_type     TEXT NOT NULL,
        unit_type_raw TEXT NOT NULL,
        unit_name     TEXT NOT NULL,
        flow          TEXT NOT NULL CHECK (flow IN ('generation', 'storage_load')),
        grain         TEXT NOT NULL CHECK (grain IN ('unit', 'bucket', 'undecided')),
        access_scope  TEXT NOT NULL CHECK (access_scope IN ('plant', 'shared', 'undecided')),
        plant_id      INTEGER REFERENCES dim_rt_plant(plant_id),
        decision_note TEXT NOT NULL DEFAULT '',
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL,
        UNIQUE (unit_type, unit_name),
        CHECK ((access_scope = 'plant') = (plant_id IS NOT NULL))
    )""",
    """CREATE TABLE fact_rt_snapshot (
        data_time   TEXT    PRIMARY KEY,
        sha256      TEXT    NOT NULL,
        fetched_at  TEXT    NOT NULL,
        revision    INTEGER NOT NULL DEFAULT 1,
        detail_rows INTEGER NOT NULL,
        quality     TEXT    NOT NULL CHECK (quality IN ('ok', 'warn')),
        warnings    TEXT    NOT NULL DEFAULT '[]'
    )""",
    """CREATE TABLE fact_rt_unit_10min (
        data_time    TEXT    NOT NULL REFERENCES fact_rt_snapshot(data_time),
        unit_id      INTEGER NOT NULL REFERENCES dim_rt_unit(id),
        net_mw       REAL,
        capacity_mw  REAL,
        load_ratio   REAL,
        note         TEXT    NOT NULL DEFAULT '',
        value_status TEXT    NOT NULL
                     CHECK (value_status IN ('ok', 'missing', 'invalid', 'comm_error')),
        PRIMARY KEY (data_time, unit_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_rt_unit_10min_unit ON fact_rt_unit_10min(unit_id, data_time)",
    """CREATE TABLE fact_rt_type_subtotal (
        data_time          TEXT NOT NULL REFERENCES fact_rt_snapshot(data_time),
        unit_type          TEXT NOT NULL,
        subtotal_name      TEXT NOT NULL,
        net_mw             REAL,
        net_share_pct      REAL,
        capacity_mw        REAL,
        capacity_share_pct REAL,
        detail_net_mw      REAL NOT NULL,
        PRIMARY KEY (data_time, unit_type)
    ) WITHOUT ROWID""",
    """CREATE TABLE fact_rt_quarantine (
        data_time TEXT    NOT NULL REFERENCES fact_rt_snapshot(data_time),
        row_index INTEGER NOT NULL,
        reason    TEXT    NOT NULL,
        raw_row   TEXT    NOT NULL,
        PRIMARY KEY (data_time, row_index)
    ) WITHOUT ROWID""",
    """CREATE TABLE fact_rt_unit_daily (
        date             TEXT    NOT NULL,
        unit_id          INTEGER NOT NULL REFERENCES dim_rt_unit(id),
        energy_mwh_est   REAL,
        max_mw           REAL,
        avg_mw           REAL,
        min_mw           REAL,
        samples          INTEGER NOT NULL,
        expected_samples INTEGER NOT NULL,
        notes_seen       TEXT    NOT NULL DEFAULT '',
        source           TEXT    NOT NULL DEFAULT 'live' CHECK (source IN ('live')),
        PRIMARY KEY (date, unit_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE fact_rt_day (
        date                  TEXT    PRIMARY KEY,
        snapshots             INTEGER NOT NULL,
        expected              INTEGER NOT NULL,
        missed_collector_down INTEGER NOT NULL,
        missed_fetch_failed   INTEGER NOT NULL,
        missed_rejected       INTEGER NOT NULL,
        rows_at_rollup        INTEGER NOT NULL,
        rolled_up_at          TEXT    NOT NULL,
        purged_at             TEXT,
        CHECK (snapshots + missed_collector_down + missed_fetch_failed + missed_rejected
               = expected)
    )""",
    """CREATE TABLE meta_rt_attempt (
        id           INTEGER PRIMARY KEY,
        attempted_at TEXT NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('fetch', 'startup', 'shutdown', 'resume')),
        target_slot  TEXT,
        outcome      TEXT CHECK (outcome IN
                     ('new', 'revised', 'not_modified', 'stale', 'error', 'rejected')),
        http_status  INTEGER,
        error_type   TEXT,
        reject_code  TEXT,
        data_time    TEXT,
        sha256       TEXT,
        bytes        INTEGER,
        elapsed_ms   INTEGER,
        etag         TEXT,
        detail       TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX idx_rt_attempt_slot ON meta_rt_attempt(target_slot)",
    f"""CREATE VIEW v_rt_10min AS
        SELECT f.data_time                        AS "資料時間",
               substr(f.data_time, 1, 10)         AS "日期",
               substr(f.data_time, 12, 5)         AS "時刻",
               u.unit_type || '|' || u.unit_name  AS "機組鍵",
               u.unit_type                        AS "機組類型",
               u.unit_name                        AS "機組名稱",
               {_GRAIN_LABEL}                     AS "粒度",
               p.plant_name                       AS "電廠",
               f.capacity_mw                      AS "裝置容量_MW",
               f.net_mw                           AS "淨發電量_MW",
               f.load_ratio                       AS "出力比",
               f.note                             AS "備註",
               CASE f.value_status WHEN 'ok' THEN '正常' WHEN 'missing' THEN '無值'
                                   WHEN 'invalid' THEN '無法解析' ELSE '通訊異常' END
                                                  AS "數值狀態"
          FROM fact_rt_unit_10min AS f
          JOIN dim_rt_unit AS u ON u.id = f.unit_id
          LEFT JOIN dim_rt_plant AS p ON p.plant_id = u.plant_id""",
    """CREATE VIEW v_rt_now AS
        SELECT * FROM v_rt_10min
         WHERE "資料時間" = (SELECT MAX(data_time) FROM fact_rt_snapshot)""",
    f"""CREATE VIEW v_rt_daily AS
        SELECT d.date                                         AS "日期",
               u.unit_type || '|' || u.unit_name              AS "機組鍵",
               u.unit_type                                    AS "機組類型",
               u.unit_name                                    AS "機組名稱",
               {_GRAIN_LABEL}                                 AS "粒度",
               p.plant_name                                   AS "電廠",
               d.energy_mwh_est                               AS "估算發電量_MWh",
               d.max_mw                                       AS "最高出力_MW",
               d.avg_mw                                       AS "平均出力_MW",
               d.min_mw                                       AS "最低出力_MW",
               d.samples                                      AS "取樣點數",
               d.expected_samples                             AS "應有點數",
               round(1.0 * d.samples / d.expected_samples, 4) AS "資料完整度",
               d.notes_seen                                   AS "當日備註",
               CASE d.source WHEN 'live' THEN '即時收集' ELSE d.source END AS "資料來源"
          FROM fact_rt_unit_daily AS d
          JOIN dim_rt_unit AS u ON u.id = d.unit_id
          LEFT JOIN dim_rt_plant AS p ON p.plant_id = u.plant_id""",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def quick_check(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA quick_check").fetchone()[0])


def schema_version(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute(
            "SELECT schema_version FROM meta_rt_manifest WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else str(row[0])


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def drop_schema(connection: sqlite3.Connection) -> None:
    for view in VIEWS_IN_DROP_ORDER:
        connection.execute(f'DROP VIEW IF EXISTS "{view}"')
    for table in TABLES_IN_DROP_ORDER:
        connection.execute(f'DROP TABLE IF EXISTS "{table}"')


def write_manifest(
    connection: sqlite3.Connection,
    *,
    build_kind: str,
    archive_files: int,
    decisions: Decisions,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO meta_rt_manifest
               (id, schema_version, built_at, build_kind, archive_files,
                decisions_sha256, plants_sha256)
           VALUES (1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               schema_version = excluded.schema_version,
               built_at = excluded.built_at,
               build_kind = excluded.build_kind,
               archive_files = excluded.archive_files,
               decisions_sha256 = excluded.decisions_sha256,
               plants_sha256 = excluded.plants_sha256""",
        (
            SCHEMA_VERSION,
            utc_iso(now),
            build_kind,
            archive_files,
            decisions.sha256,
            decisions.plants_sha256,
        ),
    )


def sync_plants(connection: sqlite3.Connection, plants: Mapping[int, str]) -> None:
    connection.executemany(
        """INSERT INTO dim_rt_plant (plant_id, plant_name) VALUES (?, ?)
           ON CONFLICT(plant_id) DO UPDATE SET plant_name = excluded.plant_name""",
        sorted(plants.items()),
    )


def _decision_values(decision: UnitDecision | None) -> tuple[str, str, int | None, str]:
    if decision is None:
        return "undecided", "undecided", None, ""
    return decision.grain, decision.access_scope, decision.plant_id, decision.note


def sync_decisions(connection: sqlite3.Connection, decisions: Decisions) -> list[tuple[str, str]]:
    """把人工決定套到每一條序列；回傳檔案裡有、但來源從沒出現過的（過期）決定。"""
    sync_plants(connection, decisions.plants)
    known = {
        (unit_type, unit_name): unit_id
        for unit_id, unit_type, unit_name in connection.execute(
            "SELECT id, unit_type, unit_name FROM dim_rt_unit"
        )
    }
    for key, unit_id in known.items():
        grain, scope, plant_id, note = _decision_values(decisions.units.get(key))
        connection.execute(
            """UPDATE dim_rt_unit
                  SET grain = ?, access_scope = ?, plant_id = ?, decision_note = ?
                WHERE id = ?""",
            (grain, scope, plant_id, note, unit_id),
        )
    connection.execute(
        "UPDATE meta_rt_manifest SET decisions_sha256 = ?, plants_sha256 = ? WHERE id = 1",
        (decisions.sha256, decisions.plants_sha256),
    )
    return sorted(set(decisions.units) - set(known))


def _ensure_units(
    connection: sqlite3.Connection, parsed: ParsedSnapshot, decisions: Decisions | None
) -> dict[tuple[str, str], int]:
    ids: dict[tuple[str, str], int] = {}
    for detail in parsed.details:
        key = (detail.unit_type, detail.unit_name)
        row = connection.execute(
            "SELECT id FROM dim_rt_unit WHERE unit_type = ? AND unit_name = ?", key
        ).fetchone()
        if row is not None:
            ids[key] = row[0]
            connection.execute(
                """UPDATE dim_rt_unit
                      SET unit_type_raw = CASE WHEN ? >= last_seen THEN ? ELSE unit_type_raw END,
                          first_seen = min(first_seen, ?),
                          last_seen = max(last_seen, ?)
                    WHERE id = ?""",
                (
                    parsed.data_time,
                    detail.unit_type_raw,
                    parsed.data_time,
                    parsed.data_time,
                    row[0],
                ),
            )
            continue
        decision = decisions.units.get(key) if decisions is not None else None
        grain, scope, plant_id, note = _decision_values(decision)
        cursor = connection.execute(
            """INSERT INTO dim_rt_unit
                   (unit_type, unit_type_raw, unit_name, flow, grain, access_scope, plant_id,
                    decision_note, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                detail.unit_type,
                detail.unit_type_raw,
                detail.unit_name,
                detail.flow,
                grain,
                scope,
                plant_id,
                note,
                parsed.data_time,
                parsed.data_time,
            ),
        )
        ids[key] = int(cursor.lastrowid)
    return ids


def ingest_snapshot(
    connection: sqlite3.Connection,
    parsed: ParsedSnapshot,
    *,
    sha256: str,
    fetched_at: str,
    decisions: Decisions | None,
) -> str:
    """寫入一個時段，回傳 'new'、'revised' 或 'duplicate'。呼叫端負責交易，並先同步電廠名冊。"""
    existing = connection.execute(
        "SELECT sha256 FROM fact_rt_snapshot WHERE data_time = ?", (parsed.data_time,)
    ).fetchone()
    if existing is not None and existing[0] == sha256:
        return "duplicate"
    warnings = json.dumps(
        [{"code": w.code, "detail": w.detail} for w in parsed.warnings], ensure_ascii=False
    )
    if existing is not None:
        for table in ("fact_rt_unit_10min", "fact_rt_type_subtotal", "fact_rt_quarantine"):
            connection.execute(f"DELETE FROM {table} WHERE data_time = ?", (parsed.data_time,))
        connection.execute(
            """UPDATE fact_rt_snapshot
                  SET sha256 = ?, fetched_at = ?, revision = revision + 1,
                      detail_rows = ?, quality = ?, warnings = ?
                WHERE data_time = ?""",
            (sha256, fetched_at, len(parsed.details), parsed.quality, warnings, parsed.data_time),
        )
        outcome = "revised"
    else:
        connection.execute(
            """INSERT INTO fact_rt_snapshot
                   (data_time, sha256, fetched_at, revision, detail_rows, quality, warnings)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (parsed.data_time, sha256, fetched_at, len(parsed.details), parsed.quality, warnings),
        )
        outcome = "new"
    ids = _ensure_units(connection, parsed, decisions)
    connection.executemany(
        """INSERT INTO fact_rt_unit_10min
               (data_time, unit_id, net_mw, capacity_mw, load_ratio, note, value_status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                parsed.data_time,
                ids[(d.unit_type, d.unit_name)],
                d.net_mw,
                d.capacity_mw,
                d.load_ratio,
                d.note,
                d.value_status,
            )
            for d in parsed.details
        ],
    )
    connection.executemany(
        """INSERT INTO fact_rt_type_subtotal
               (data_time, unit_type, subtotal_name, net_mw, net_share_pct, capacity_mw,
                capacity_share_pct, detail_net_mw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                parsed.data_time,
                s.unit_type,
                s.subtotal_name,
                s.net_mw,
                s.net_share_pct,
                s.capacity_mw,
                s.capacity_share_pct,
                s.detail_net_mw,
            )
            for s in parsed.subtotals
        ],
    )
    connection.executemany(
        "INSERT INTO fact_rt_quarantine (data_time, row_index, reason, raw_row) "
        "VALUES (?, ?, ?, ?)",
        [(parsed.data_time, q.row_index, q.reason, q.raw_row) for q in parsed.quarantined],
    )
    return outcome


def record_attempt(connection: sqlite3.Connection, record: Mapping[str, object]) -> None:
    values = [record.get(column) for column in ATTEMPT_COLUMNS]
    values[-1] = values[-1] or ""
    placeholders = ", ".join("?" * len(ATTEMPT_COLUMNS))
    connection.execute(
        f"INSERT INTO meta_rt_attempt ({', '.join(ATTEMPT_COLUMNS)}) VALUES ({placeholders})",
        values,
    )


def latest_data_time(connection: sqlite3.Connection) -> str | None:
    return connection.execute("SELECT MAX(data_time) FROM fact_rt_snapshot").fetchone()[0]


def snapshot_fetched_at(connection: sqlite3.Connection, data_time: str) -> tuple[str, str] | None:
    """回傳（sha256, fetched_at）；該時段還沒有快照時回 None。"""
    row = connection.execute(
        "SELECT sha256, fetched_at FROM fact_rt_snapshot WHERE data_time = ?", (data_time,)
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_store.py -q`
Expected: `6 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/store.py tests/test_realtime_store.py
git commit -m "feat: create realtime.db and ingest snapshots" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 日彙總、缺口分類與清除

依據規格 §7.1、§7.2、§7.4：只算 `ok` 值；沒有 `ok` 值時是 NULL；每一天的快照數加三種缺口等於 144；清除的安全條件寫在 SQL 裡，彙總失敗或彙總後又有新列的日子不會被刪。

**Files:**
- Create: `src/ingest/realtime/maintenance.py`
- Test: `tests/test_realtime_maintenance.py`

**Interfaces:**
- Consumes: `RealtimeConfig`（Task 1）、`transaction`（Task 5）、`TAIPEI`、`format_slot`、`parse_utc_iso`、`to_taipei`、`utc_iso`（Task 1）
- Produces: `MaintenanceReport(rolled_up, purged)`、`day_slots(day, slot_minutes) -> list[str]`、`closed_through(now, config) -> date`、`purge_cutoff(now, config) -> str`、`first_day(connection) -> date | None`、`days_needing_rollup(connection, now, config) -> list[str]`、`classify_missing(connection, slots) -> dict[str, str]`、`rollup_day(connection, day, now, config)`、`purge(connection, *, cutoff, now) -> list[str]`、`run_maintenance(connection, now, config) -> MaintenanceReport`
- 契約：`rollup_day`、`purge` 不自己開交易；`run_maintenance` 每天一個交易、清除一個交易。

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from realtime_support import make_config, tiny_payload

from ingest.realtime import maintenance, store
from ingest.realtime.archive import payload_sha256
from ingest.realtime.decisions import load_decisions
from ingest.realtime.parse import parse_payload

LIVE_NOW = datetime(2026, 9, 20, 0, 45, tzinfo=UTC)  # 08:45 in Taipei
CLOSED_NOW = datetime(2026, 9, 20, 16, 20, tzinfo=UTC)  # 2026-09-21 00:20 in Taipei


def _db(tmp_path: Path):
    config = make_config(tmp_path)
    decisions = load_decisions(config.units_csv, config.plants_csv)
    connection = store.connect(config.database)
    with store.transaction(connection):
        store.create_schema(connection)
        store.sync_plants(connection, decisions.plants)
        store.record_attempt(
            connection, {"attempted_at": "2026-09-19T23:00:00+00:00", "kind": "startup"}
        )
    return config, connection


def _ingest(connection, config, raw: bytes) -> None:
    parsed = parse_payload(
        raw, now=LIVE_NOW, validation=config.validation, slot_minutes=config.schedule.slot_minutes
    )
    with store.transaction(connection):
        store.ingest_snapshot(
            connection, parsed, sha256=payload_sha256(raw), fetched_at="x", decisions=None
        )


def _attempt(connection, slot: str, outcome: str) -> None:
    with store.transaction(connection):
        store.record_attempt(
            connection,
            {
                "attempted_at": "2026-09-20T00:40:00+00:00",
                "kind": "fetch",
                "target_slot": slot,
                "outcome": outcome,
            },
        )


def _load_day(connection, config) -> None:
    rows = {
        "08:00": [("燃氣", "A", "100", "60", ""), ("儲能負載", "C", "-", "-30", "")],
        "08:10": [("燃氣", "A", "100", "120", "歲修"), ("儲能負載", "C", "-", "-30", "")],
        "08:20": [("燃氣", "A", "100", "N/A", "")],
    }
    for clock, units in rows.items():
        units = [*units, ("風力", "B", "50", "0.0", "通訊異常")]
        _ingest(connection, config, tiny_payload(f"2026-09-20T{clock}:00", units))
    _attempt(connection, "2026-09-20 08:30", "error")
    _attempt(connection, "2026-09-20 08:40", "rejected")


def _daily(connection) -> dict[str, tuple]:
    return {
        row[0]: row[1:]
        for row in connection.execute(
            """SELECT u.unit_name, d.energy_mwh_est, d.max_mw, d.avg_mw, d.min_mw, d.samples,
                      d.expected_samples, d.notes_seen
                 FROM fact_rt_unit_daily d JOIN dim_rt_unit u ON u.id = d.unit_id"""
        )
    }


def test_rollup_counts_only_trusted_values(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    report = maintenance.run_maintenance(connection, CLOSED_NOW, config)

    assert report.rolled_up == ("2026-09-20",)
    daily = _daily(connection)
    assert daily["A"] == (30.0, 120.0, 90.0, 60.0, 2, 144, "歲修")
    assert daily["B"] == (None, None, None, None, 0, 144, "通訊異常")
    assert daily["C"][0] == -10.0


def test_gaps_are_classified_and_add_up_to_144(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    maintenance.run_maintenance(connection, CLOSED_NOW, config)

    row = connection.execute(
        """SELECT snapshots, missed_collector_down, missed_fetch_failed, missed_rejected, expected
             FROM fact_rt_day WHERE date = '2026-09-20'"""
    ).fetchone()
    assert row == (3, 139, 1, 1, 144)


def test_day_closes_at_0015_the_next_morning(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    before = datetime(2026, 9, 20, 16, 14, tzinfo=UTC)  # 2026-09-21 00:14 in Taipei
    after = datetime(2026, 9, 20, 16, 16, tzinfo=UTC)

    assert maintenance.closed_through(before, config) == date(2026, 9, 19)
    assert maintenance.closed_through(after, config) == date(2026, 9, 20)


def test_purge_only_removes_rolled_up_days_past_retention(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)
    maintenance.run_maintenance(connection, CLOSED_NOW, config)
    much_later = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)

    report = maintenance.run_maintenance(connection, much_later, config)

    assert "2026-09-20" in report.purged
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_daily").fetchone()[0] == 3
    purged_at = connection.execute(
        "SELECT purged_at FROM fact_rt_day WHERE date = '2026-09-20'"
    ).fetchone()[0]
    assert purged_at == "2026-10-10T04:00:00+00:00"


def test_day_without_rollup_is_never_purged(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    with store.transaction(connection):
        purged = maintenance.purge(connection, cutoff="2026-12-31", now=CLOSED_NOW)

    assert purged == []
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 8


def test_rows_written_after_rollup_block_purge_until_recomputed(tmp_path: Path) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)
    maintenance.run_maintenance(connection, CLOSED_NOW, config)
    _ingest(
        connection, config, tiny_payload("2026-09-20T08:30:00", [("燃氣", "A", "100", "90", "")])
    )

    with store.transaction(connection):
        purged = maintenance.purge(connection, cutoff="2026-12-31", now=CLOSED_NOW)
    assert purged == []
    assert maintenance.days_needing_rollup(connection, CLOSED_NOW, config) == ["2026-09-20"]


def test_failed_rollup_leaves_every_row_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, connection = _db(tmp_path)
    _load_day(connection, config)

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(maintenance, "rollup_day", broken)
    with pytest.raises(RuntimeError):
        maintenance.run_maintenance(connection, datetime(2026, 12, 31, tzinfo=UTC), config)

    assert connection.execute("SELECT COUNT(*) FROM fact_rt_unit_10min").fetchone()[0] == 8
    with store.transaction(connection):
        assert maintenance.purge(connection, cutoff="2026-12-31", now=CLOSED_NOW) == []
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_maintenance.py -q`
Expected: `ImportError: cannot import name 'maintenance' from 'ingest.realtime'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/maintenance.py`**

```python
"""Daily rollups, gap classification and purging (§7.1, §7.2, §7.4).

清除的安全條件寫在 SQL 裡：只刪「已彙總、而且彙總之後沒有新明細寫入」的日子，
所以就算呼叫順序寫錯，也刪不到還沒彙總的資料。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ingest.realtime.config import RealtimeConfig
from ingest.realtime.store import transaction
from ingest.realtime.timeutil import TAIPEI, format_slot, parse_utc_iso, to_taipei, utc_iso


@dataclass(frozen=True)
class MaintenanceReport:
    rolled_up: tuple[str, ...]
    purged: tuple[str, ...]


def day_slots(day: str, slot_minutes: int) -> list[str]:
    start = datetime.fromisoformat(day).replace(tzinfo=TAIPEI)
    return [
        format_slot(start + timedelta(minutes=slot_minutes * index))
        for index in range(24 * 60 // slot_minutes)
    ]


def closed_through(now: datetime, config: RealtimeConfig) -> date:
    """最後一個已結束的日子：隔天 day_closed_after（預設 00:15）之後才算結束。"""
    closed_after = timedelta(
        hours=config.day_closed_after.hour, minutes=config.day_closed_after.minute
    )
    return (to_taipei(now) - closed_after).date() - timedelta(days=1)


def purge_cutoff(now: datetime, config: RealtimeConfig) -> str:
    return (to_taipei(now).date() - timedelta(days=config.raw_days)).isoformat()


def first_day(connection: sqlite3.Connection) -> date | None:
    """第一次啟動（或第一筆快照）的那天；之後每一天都要有一列 fact_rt_day。"""
    first_attempt = connection.execute("SELECT MIN(attempted_at) FROM meta_rt_attempt").fetchone()[
        0
    ]
    first_snapshot = connection.execute("SELECT MIN(data_time) FROM fact_rt_snapshot").fetchone()[0]
    days: list[date] = []
    if first_attempt:
        days.append(to_taipei(parse_utc_iso(first_attempt)).date())
    if first_snapshot:
        days.append(date.fromisoformat(first_snapshot[:10]))
    return min(days) if days else None


def _row_count(connection: sqlite3.Connection, day: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM fact_rt_unit_10min WHERE data_time >= ? AND data_time < ?",
        (f"{day} 00:00", f"{day} 24:00"),
    ).fetchone()[0]


def days_needing_rollup(
    connection: sqlite3.Connection, now: datetime, config: RealtimeConfig
) -> list[str]:
    start, end = first_day(connection), closed_through(now, config)
    if start is None or start > end:
        return []
    recorded = {
        row[0]: (row[1], row[2])
        for row in connection.execute("SELECT date, rows_at_rollup, purged_at FROM fact_rt_day")
    }
    days: list[str] = []
    current = start
    while current <= end:
        day = current.isoformat()
        entry = recorded.get(day)
        if entry is None or (entry[1] is None and entry[0] != _row_count(connection, day)):
            days.append(day)
        current += timedelta(days=1)
    return days


def classify_missing(connection: sqlite3.Connection, slots: list[str]) -> dict[str, str]:
    """{缺少的時段: 原因}，原因依序判斷：rejected → fetch_failed → collector_down。"""
    if not slots:
        return {}
    first, last = slots[0], slots[-1]
    present = {
        row[0]
        for row in connection.execute(
            "SELECT data_time FROM fact_rt_snapshot WHERE data_time >= ? AND data_time <= ?",
            (first, last),
        )
    }
    attempts = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            """SELECT target_slot, COUNT(*), SUM(outcome = 'rejected')
                 FROM meta_rt_attempt
                WHERE kind = 'fetch' AND target_slot >= ? AND target_slot <= ?
                GROUP BY target_slot""",
            (first, last),
        )
    }
    reasons: dict[str, str] = {}
    for slot in slots:
        if slot in present:
            continue
        tried, rejected = attempts.get(slot, (0, 0))
        reasons[slot] = "rejected" if rejected else "fetch_failed" if tried else "collector_down"
    return reasons


def rollup_day(
    connection: sqlite3.Connection, day: str, now: datetime, config: RealtimeConfig
) -> None:
    """重算一天的彙總與缺口。呼叫端負責交易。只算 value_status = 'ok' 的值。"""
    start, end = f"{day} 00:00", f"{day} 24:00"
    connection.execute("DELETE FROM fact_rt_unit_daily WHERE date = ?", (day,))
    connection.execute(
        """INSERT INTO fact_rt_unit_daily
               (date, unit_id, energy_mwh_est, max_mw, avg_mw, min_mw, samples,
                expected_samples, notes_seen, source)
           SELECT ?, unit_id,
                  SUM(CASE WHEN value_status = 'ok' THEN net_mw END) * ? / 60.0,
                  MAX(CASE WHEN value_status = 'ok' THEN net_mw END),
                  AVG(CASE WHEN value_status = 'ok' THEN net_mw END),
                  MIN(CASE WHEN value_status = 'ok' THEN net_mw END),
                  COUNT(CASE WHEN value_status = 'ok' THEN 1 END),
                  ?, '', 'live'
             FROM fact_rt_unit_10min
            WHERE data_time >= ? AND data_time < ?
            GROUP BY unit_id""",
        (day, config.schedule.slot_minutes, config.slots_per_day, start, end),
    )
    notes: dict[int, list[str]] = {}
    for unit_id, note, _first_seen in connection.execute(
        """SELECT unit_id, note, MIN(data_time) AS first_seen
             FROM fact_rt_unit_10min
            WHERE data_time >= ? AND data_time < ? AND note <> ''
            GROUP BY unit_id, note
            ORDER BY unit_id, first_seen, note""",
        (start, end),
    ):
        notes.setdefault(unit_id, []).append(note)
    connection.executemany(
        "UPDATE fact_rt_unit_daily SET notes_seen = ? WHERE date = ? AND unit_id = ?",
        [("|".join(values), day, unit_id) for unit_id, values in notes.items()],
    )
    reasons = Counter(
        classify_missing(connection, day_slots(day, config.schedule.slot_minutes)).values()
    )
    snapshots = connection.execute(
        "SELECT COUNT(*) FROM fact_rt_snapshot WHERE data_time >= ? AND data_time < ?",
        (start, end),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO fact_rt_day
               (date, snapshots, expected, missed_collector_down, missed_fetch_failed,
                missed_rejected, rows_at_rollup, rolled_up_at, purged_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(date) DO UPDATE SET
               snapshots = excluded.snapshots,
               expected = excluded.expected,
               missed_collector_down = excluded.missed_collector_down,
               missed_fetch_failed = excluded.missed_fetch_failed,
               missed_rejected = excluded.missed_rejected,
               rows_at_rollup = excluded.rows_at_rollup,
               rolled_up_at = excluded.rolled_up_at,
               purged_at = NULL""",
        (
            day,
            snapshots,
            config.slots_per_day,
            reasons["collector_down"],
            reasons["fetch_failed"],
            reasons["rejected"],
            _row_count(connection, day),
            utc_iso(now),
        ),
    )


def purge(connection: sqlite3.Connection, *, cutoff: str, now: datetime) -> list[str]:
    """刪掉 cutoff（'YYYY-MM-DD'）之前、已彙總且彙總後沒有新列的日子的明細。呼叫端負責交易。"""
    connection.execute("DROP TABLE IF EXISTS temp.purgeable")
    connection.execute(
        """CREATE TEMP TABLE purgeable AS
           SELECT d.date
             FROM fact_rt_day AS d
            WHERE d.date < ?
              AND d.purged_at IS NULL
              AND d.rows_at_rollup = (SELECT COUNT(*) FROM fact_rt_unit_10min AS f
                                       WHERE f.data_time >= d.date || ' 00:00'
                                         AND f.data_time < d.date || ' 24:00')""",
        (cutoff,),
    )
    days = [row[0] for row in connection.execute("SELECT date FROM temp.purgeable ORDER BY date")]
    connection.execute(
        "DELETE FROM fact_rt_unit_10min "
        "WHERE substr(data_time, 1, 10) IN (SELECT date FROM temp.purgeable)"
    )
    connection.execute(
        "DELETE FROM fact_rt_type_subtotal "
        "WHERE substr(data_time, 1, 10) IN (SELECT date FROM temp.purgeable)"
    )
    connection.execute(
        "UPDATE fact_rt_day SET purged_at = ? WHERE date IN (SELECT date FROM temp.purgeable)",
        (utc_iso(now),),
    )
    connection.execute("DROP TABLE temp.purgeable")
    return days


def run_maintenance(
    connection: sqlite3.Connection, now: datetime, config: RealtimeConfig
) -> MaintenanceReport:
    """每一天各自一個交易地補彙總，最後清除；某天失敗不影響其他天，也不會被清除。"""
    rolled: list[str] = []
    for day in days_needing_rollup(connection, now, config):
        with transaction(connection):
            rollup_day(connection, day, now, config)
        rolled.append(day)
    with transaction(connection):
        purged = purge(connection, cutoff=purge_cutoff(now, config), now=now)
    return MaintenanceReport(tuple(rolled), tuple(purged))
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_maintenance.py -q`
Expected: `7 passed`。`test_failed_rollup_leaves_every_row_in_place` 是規格 §10 要求的故障注入。

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/maintenance.py tests/test_realtime_maintenance.py
git commit -m "feat: roll up realtime days and purge only what was rolled up" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 從封存重建，以及 WAL 唯讀

依據規格 §7.3、§10：在原檔內以單一交易重建、不換檔；同一時段依抓取時間重放；重建結果必須與逐筆入庫的內容完全相同。WAL 測試驗證服務端用 `ReadOnlySQLite` 在寫入進行中、以及收集器停止後（`-wal`／`-shm` 不存在）都讀得到——這是規格 §12 列的第一個風險，已在 Windows 上實測通過。

**Files:**
- Create: `src/ingest/realtime/rebuild.py`
- Test: `tests/test_realtime_rebuild.py`、`tests/test_realtime_wal.py`

**Interfaces:**
- Consumes: Task 2–6 全部；`text2sql.db.ReadOnlySQLite`（既有，只在測試中使用）
- Produces: `RebuildReport(archive_files, ingested, rejected, corrupted, unparsed)`、`fetch_times(config) -> dict[str, str]`、`archived_fetch_time(archived, times) -> str`、`rebuild(connection, config, decisions, *, now) -> RebuildReport`
- 契約：封存為空時同一流程等於建立空資料庫，`build_kind = 'create'`。

- [ ] **Step 1: 寫會失敗的測試**

`tests/test_realtime_rebuild.py`：

```python
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from realtime_support import make_config, payload_bytes, tiny_payload

from ingest.realtime import archive, maintenance, rebuild, store
from ingest.realtime.decisions import load_decisions
from ingest.realtime.parse import PayloadRejected, parse_payload, read_datetime
from ingest.realtime.timeutil import utc_iso

HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"
NOW = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)  # 2026-09-21 12:00 in Taipei


def _setup(tmp_path: Path):
    config = make_config(tmp_path)
    config.units_csv.write_text(HEADER + "燃氣,大潭CC#1,unit,plant,8,\n", encoding="utf-8")
    return config, load_decisions(config.units_csv, config.plants_csv)


def _live_ingest(connection, config, decisions, raw: bytes, fetched_at: datetime) -> str:
    """與收集器相同的順序：先封存，再解析入庫，最後寫抓取紀錄。"""
    sha = archive.payload_sha256(raw)
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=fetched_at
    )
    record = {
        "attempted_at": utc_iso(fetched_at),
        "kind": "fetch",
        "target_slot": None,
        "sha256": sha,
    }
    try:
        parsed = parse_payload(
            raw,
            now=fetched_at,
            validation=config.validation,
            slot_minutes=config.schedule.slot_minutes,
        )
    except PayloadRejected as rejection:
        record.update(outcome="rejected", reject_code=rejection.code)
    else:
        with store.transaction(connection):
            ingested = store.ingest_snapshot(
                connection, parsed, sha256=sha, fetched_at=utc_iso(fetched_at), decisions=decisions
            )
        outcome = {"new": "new", "revised": "revised", "duplicate": "stale"}[ingested]
        record.update(outcome=outcome, target_slot=parsed.data_time, data_time=parsed.data_time)
    archive.append_attempt(config.attempts_dir, record)
    with store.transaction(connection):
        store.record_attempt(connection, record)
    return str(record["outcome"])


def _contents(connection) -> dict[str, list[tuple]]:
    tables = [t for t in store.TABLES_IN_DROP_ORDER if t != "meta_rt_manifest"]
    return {
        table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        for table in tables
    }


def _payloads() -> list[tuple[bytes, datetime]]:
    first = datetime(2026, 9, 19, 15, 45, 20, tzinfo=UTC)  # 2026-09-19 23:45:20 Taipei
    return [
        (payload_bytes(data_time="2026-09-19T23:40:00"), first),
        (payload_bytes(data_time="2026-09-19T23:50:00"), first + timedelta(minutes=10)),
        (
            tiny_payload("2026-09-20T00:00:00", [("燃氣", "大潭CC#1", "742.7", "500.0", "")]),
            first + timedelta(minutes=20),
        ),
    ]


def test_rebuild_reproduces_the_incrementally_built_database(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    live = store.connect(config.database)
    rebuild.rebuild(live, config, decisions, now=NOW)  # 空封存：等於建立空資料庫
    for raw, fetched_at in _payloads():
        assert _live_ingest(live, config, decisions, raw, fetched_at) == "new"
    maintenance.run_maintenance(live, NOW, config)

    replayed = store.connect(tmp_path / "replayed.db")
    report = rebuild.rebuild(replayed, config, decisions, now=NOW)

    assert report.ingested == 3
    assert _contents(replayed) == _contents(live)
    manifest = replayed.execute("SELECT build_kind, archive_files FROM meta_rt_manifest").fetchone()
    assert manifest == ("rebuild", 3)


def test_first_start_with_an_empty_archive_creates_the_schema(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    connection = store.connect(config.database)

    report = rebuild.rebuild(connection, config, decisions, now=NOW)

    assert report == rebuild.RebuildReport(0, 0, 0, 0, 0)
    assert store.schema_version(connection) == store.SCHEMA_VERSION
    assert connection.execute("SELECT build_kind FROM meta_rt_manifest").fetchone()[0] == "create"


def test_payload_rejected_live_comes_back_after_rebuild(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    live = store.connect(config.database)
    rebuild.rebuild(live, config, decisions, now=NOW)
    raw = payload_bytes(data_time="2026-09-21T13:00:00")
    too_early = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)  # 12:00 Taipei: 13:00 is in the future

    assert _live_ingest(live, config, decisions, raw, too_early) == "rejected"
    later = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    report = rebuild.rebuild(live, config, decisions, now=later)

    assert report.ingested == 1
    assert store.latest_data_time(live) == "2026-09-21 13:00"


def test_corrupted_archive_file_is_skipped(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    bad = config.archive_dir / "2026/09/19/2340_000000000000.json.gz"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(gzip.compress(payload_bytes(data_time="2026-09-19T23:40:00")))
    connection = store.connect(config.database)

    report = rebuild.rebuild(connection, config, decisions, now=NOW)

    assert (report.corrupted, report.ingested) == (1, 0)


def test_revisions_are_replayed_in_fetch_order(tmp_path: Path) -> None:
    config, decisions = _setup(tmp_path)
    live = store.connect(config.database)
    rebuild.rebuild(live, config, decisions, now=NOW)
    original = payload_bytes(data_time="2026-09-19T23:40:00")
    revised = original.replace(b'"590.6"', b'"591.0"')
    fetched = datetime(2026, 9, 19, 15, 45, 20, tzinfo=UTC)
    _live_ingest(live, config, decisions, original, fetched)
    _live_ingest(live, config, decisions, revised, fetched + timedelta(minutes=3))

    replayed = store.connect(tmp_path / "replayed.db")
    rebuild.rebuild(replayed, config, decisions, now=NOW)

    row = replayed.execute("SELECT revision, sha256 FROM fact_rt_snapshot").fetchone()
    assert row == (2, archive.payload_sha256(revised))
    assert json.loads(replayed.execute("SELECT warnings FROM fact_rt_snapshot").fetchone()[0]) == [
        {"code": "SUBTOTAL_MISMATCH", "detail": "燃氣：明細 16521.8 MW，小計 16521.4 MW"}
    ]
```

`tests/test_realtime_wal.py`：

```python
"""WAL + read-only: the service must read realtime.db while the collector writes (§10)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from realtime_support import make_config

from ingest.realtime import rebuild, store
from ingest.realtime.decisions import load_decisions
from text2sql.db import ReadOnlySQLite

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
ATTEMPT = {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "startup"}


def _create(tmp_path: Path):
    config = make_config(tmp_path)
    connection = store.connect(config.database)
    rebuild.rebuild(
        connection, config, load_decisions(config.units_csv, config.plants_csv), now=NOW
    )
    return config, connection


def test_reader_sees_committed_rows_while_a_write_is_open(tmp_path: Path) -> None:
    config, writer = _create(tmp_path)
    with store.transaction(writer):
        store.record_attempt(writer, ATTEMPT)
    reader = ReadOnlySQLite(config.database)

    writer.execute("BEGIN IMMEDIATE")
    store.record_attempt(writer, ATTEMPT)
    _columns, rows = reader.execute("SELECT COUNT(*) FROM meta_rt_attempt", ())
    writer.execute("COMMIT")

    assert rows == [(1,)]
    assert reader.execute("SELECT COUNT(*) FROM meta_rt_attempt", ())[1] == [(2,)]


def test_reader_opens_the_file_when_the_collector_is_stopped(tmp_path: Path) -> None:
    config, writer = _create(tmp_path)
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{config.database}{suffix}").unlink(missing_ok=True)

    _columns, rows = ReadOnlySQLite(config.database).execute("SELECT COUNT(*) FROM v_rt_now", ())

    assert rows == [(0,)]
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_rebuild.py tests/test_realtime_wal.py -q`
Expected: `ImportError: cannot import name 'rebuild' from 'ingest.realtime'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/rebuild.py`**

```python
"""Rebuild realtime.db in place from the archive and the attempt log (§7.3).

在原檔內以單一交易完成、不換檔：Windows 上有人開著檔案時 os.replace 會失敗，舊檔留下的 -wal
套用到新檔上還會損壞資料庫。服務端在重建期間透過 WAL 繼續讀到舊資料。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ingest.realtime.archive import (
    UNPARSED_DIR,
    ArchivedFile,
    iter_archive,
    iter_attempts,
    payload_sha256,
    read_payload,
)
from ingest.realtime.config import RealtimeConfig
from ingest.realtime.decisions import Decisions
from ingest.realtime.maintenance import (
    closed_through,
    days_needing_rollup,
    purge,
    purge_cutoff,
    rollup_day,
)
from ingest.realtime.parse import PayloadRejected, parse_payload
from ingest.realtime.store import (
    create_schema,
    drop_schema,
    ingest_snapshot,
    record_attempt,
    sync_plants,
    transaction,
    write_manifest,
)
from ingest.realtime.timeutil import utc_iso


@dataclass(frozen=True)
class RebuildReport:
    archive_files: int
    ingested: int
    rejected: int
    corrupted: int
    unparsed: int


def fetch_times(config: RealtimeConfig) -> dict[str, str]:
    """SHA-256 前 12 碼 → 第一次抓到的時間（UTC ISO），取自抓取紀錄。"""
    times: dict[str, str] = {}
    for record in iter_attempts(config.attempts_dir):
        sha, attempted_at = record.get("sha256"), record.get("attempted_at")
        if isinstance(sha, str) and isinstance(attempted_at, str):
            times.setdefault(sha[:12], attempted_at)
    return times


def archived_fetch_time(archived: ArchivedFile, times: dict[str, str]) -> str:
    """抓取時間取自抓取紀錄；紀錄缺漏時退回封存檔的修改時間。"""
    known = times.get(archived.sha_prefix)
    if known is not None:
        return known
    return utc_iso(datetime.fromtimestamp(archived.path.stat().st_mtime, UTC))


def _close_day(
    connection: sqlite3.Connection, day: str, now: datetime, config: RealtimeConfig
) -> None:
    if date.fromisoformat(day) <= closed_through(now, config):
        rollup_day(connection, day, now, config)
        purge(connection, cutoff=purge_cutoff(now, config), now=now)


def rebuild(
    connection: sqlite3.Connection, config: RealtimeConfig, decisions: Decisions, *, now: datetime
) -> RebuildReport:
    times = fetch_times(config)
    files = sorted(
        iter_archive(config.archive_dir),
        key=lambda archived: (archived.data_time or "", archived_fetch_time(archived, times)),
    )
    unparsed_dir = config.archive_dir / UNPARSED_DIR
    unparsed = len(list(unparsed_dir.glob("*.json.gz"))) if unparsed_dir.is_dir() else 0
    ingested = rejected = corrupted = 0
    with transaction(connection):
        drop_schema(connection)
        create_schema(connection)
        sync_plants(connection, decisions.plants)
        for record in iter_attempts(config.attempts_dir):
            record_attempt(connection, record)
        current_day: str | None = None
        for archived in files:
            day = str(archived.data_time)[:10]
            if current_day is not None and day != current_day:
                _close_day(connection, current_day, now, config)
            current_day = day
            raw = read_payload(archived.path)
            sha = payload_sha256(raw)
            if sha[:12] != archived.sha_prefix:
                corrupted += 1
                continue
            try:
                parsed = parse_payload(
                    raw,
                    now=now,
                    validation=config.validation,
                    slot_minutes=config.schedule.slot_minutes,
                )
            except PayloadRejected:
                rejected += 1
                continue
            ingest_snapshot(
                connection,
                parsed,
                sha256=sha,
                fetched_at=archived_fetch_time(archived, times),
                decisions=decisions,
            )
            ingested += 1
        if current_day is not None:
            _close_day(connection, current_day, now, config)
        for day in days_needing_rollup(connection, now, config):
            rollup_day(connection, day, now, config)
        purge(connection, cutoff=purge_cutoff(now, config), now=now)
        write_manifest(
            connection,
            build_kind="rebuild" if files else "create",
            archive_files=len(files),
            decisions=decisions,
            now=now,
        )
    return RebuildReport(len(files), ingested, rejected, corrupted, unparsed)
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_rebuild.py tests/test_realtime_wal.py -q`
Expected: `7 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/rebuild.py tests/test_realtime_rebuild.py tests/test_realtime_wal.py
git commit -m "feat: rebuild realtime.db in place from the archive" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: 排程

依據規格 §4.1：時段開始後 5:20 第一次請求；304 或舊資料 60 秒後再試；連續失敗第 10 次改 2 分鐘、第 20 次改 5 分鐘；`Retry-After` 最多等 30 分鐘；台電晚發布的舊時段照樣採用並繼續追。

**Files:**
- Create: `src/ingest/realtime/schedule.py`
- Test: `tests/test_realtime_schedule.py`

**Interfaces:**
- Consumes: `ScheduleConfig`（Task 1）、`floor_slot`、`format_slot`、`parse_slot`（Task 1）
- Produces: `ADOPTED`、`QUIET`、`SchedulerState(last_adopted=None, consecutive_failures=0, not_before=None)`、`Action(kind, at, target_slot)`、`target_slot(now, config) -> datetime`、`plan_next(now, state, config) -> Action`、`after_attempt(state, *, now, outcome, data_time, retry_after, config) -> SchedulerState`
- 結果代碼：`new`、`revised`（採用）；`not_modified`、`stale`（安靜）；`error`、`rejected`（失敗，會退避）。

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from realtime_support import make_config

from ingest.realtime.schedule import SchedulerState, after_attempt, plan_next, target_slot
from ingest.realtime.timeutil import TAIPEI, format_slot


def _schedule(tmp_path: Path):
    return make_config(tmp_path).schedule


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-24T{clock}").replace(tzinfo=TAIPEI)


def test_target_is_the_latest_slot_that_should_be_published(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)

    assert format_slot(target_slot(_at("08:55:19"), schedule)) == "2026-09-24 08:40"
    assert format_slot(target_slot(_at("08:55:20"), schedule)) == "2026-09-24 08:50"


def test_without_data_fetch_now(tmp_path: Path) -> None:
    action = plan_next(_at("08:57:00"), SchedulerState(), _schedule(tmp_path))

    assert (action.kind, action.target_slot) == ("fetch", "2026-09-24 08:50")


def test_after_adopting_the_target_wait_for_the_next_publication(tmp_path: Path) -> None:
    state = SchedulerState(last_adopted="2026-09-24 08:50")

    action = plan_next(_at("08:57:00"), state, _schedule(tmp_path))

    assert action.kind == "wait"
    assert action.at == _at("09:05:20")
    assert action.target_slot == "2026-09-24 09:00"


def test_quiet_answers_retry_in_a_minute(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    now = _at("08:55:20")

    state = after_attempt(
        SchedulerState(),
        now=now,
        outcome="not_modified",
        data_time=None,
        retry_after=None,
        config=schedule,
    )

    assert state.not_before == now + timedelta(seconds=60)
    assert plan_next(now + timedelta(seconds=30), state, schedule).kind == "wait"
    assert plan_next(now + timedelta(seconds=60), state, schedule).kind == "fetch"


def test_failures_back_off_after_10_and_20_attempts(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    now = _at("08:55:20")
    state = SchedulerState()
    delays = []
    for _ in range(21):
        state = after_attempt(
            state, now=now, outcome="error", data_time=None, retry_after=None, config=schedule
        )
        delays.append((state.not_before - now).total_seconds())

    assert delays[8] == 60  # 第 9 次
    assert delays[9] == 120  # 第 10 次
    assert delays[19] == 300  # 第 20 次
    assert state.consecutive_failures == 21


def test_retry_after_is_honoured_up_to_the_cap(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    now = _at("08:55:20")

    polite = after_attempt(
        SchedulerState(), now=now, outcome="error", data_time=None, retry_after=900, config=schedule
    )
    capped = after_attempt(
        SchedulerState(),
        now=now,
        outcome="error",
        data_time=None,
        retry_after=9999,
        config=schedule,
    )

    assert polite.not_before == now + timedelta(seconds=900)
    assert capped.not_before == now + timedelta(seconds=1800)


def test_success_resets_failures_and_a_late_slot_keeps_the_chase_going(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    failing = SchedulerState(consecutive_failures=12, not_before=_at("09:00:00"))
    now = _at("09:06:00")  # target 已是 09:00，但台電晚發布，拿到的是 08:50

    state = after_attempt(
        failing,
        now=now,
        outcome="new",
        data_time="2026-09-24 08:50",
        retry_after=None,
        config=schedule,
    )

    assert state == SchedulerState("2026-09-24 08:50", 0, None)
    assert plan_next(now, state, schedule).kind == "fetch"


def test_an_older_revision_never_moves_the_latest_slot_back(tmp_path: Path) -> None:
    state = after_attempt(
        SchedulerState(last_adopted="2026-09-24 09:00"),
        now=datetime(2026, 9, 24, 1, 6, tzinfo=UTC),
        outcome="revised",
        data_time="2026-09-24 08:50",
        retry_after=None,
        config=_schedule(tmp_path),
    )

    assert state.last_adopted == "2026-09-24 09:00"
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_schedule.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.schedule'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/schedule.py`**

```python
"""Decide when to fetch next — pure functions, no clock and no I/O (§4.1).

每個時段抓到就停：失敗時的重試次數與每分鐘輪詢一樣多，正常時請求數少 5–10 倍。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ingest.realtime.config import ScheduleConfig
from ingest.realtime.timeutil import floor_slot, format_slot, parse_slot

ADOPTED = frozenset({"new", "revised"})
QUIET = frozenset({"not_modified", "stale"})


@dataclass(frozen=True)
class SchedulerState:
    last_adopted: str | None = None  # 已採用的最新時段 'YYYY-MM-DD HH:MM'
    consecutive_failures: int = 0
    not_before: datetime | None = None  # 下一次請求最早的時間（重試、退避或 Retry-After）


@dataclass(frozen=True)
class Action:
    kind: str  # 'fetch' 或 'wait'
    at: datetime
    target_slot: str


def target_slot(now: datetime, config: ScheduleConfig) -> datetime:
    """最新一個「應該已經發布」的時段：now 往前推 first_probe 再往下取整。"""
    return floor_slot(now - timedelta(seconds=config.first_probe_seconds), config.slot_minutes)


def plan_next(now: datetime, state: SchedulerState, config: ScheduleConfig) -> Action:
    target = target_slot(now, config)
    if state.last_adopted is not None and parse_slot(state.last_adopted) >= target:
        upcoming = target + timedelta(minutes=config.slot_minutes)
        due = upcoming + timedelta(seconds=config.first_probe_seconds)
        return Action("wait", due, format_slot(upcoming))
    if state.not_before is not None and state.not_before > now:
        return Action("wait", state.not_before, format_slot(target))
    return Action("fetch", now, format_slot(target))


def after_attempt(
    state: SchedulerState,
    *,
    now: datetime,
    outcome: str,
    data_time: str | None,
    retry_after: float | None,
    config: ScheduleConfig,
) -> SchedulerState:
    if outcome in ADOPTED:
        latest = state.last_adopted
        if data_time is not None and (latest is None or data_time > latest):
            latest = data_time
        return SchedulerState(latest, 0, None)
    if outcome in QUIET:
        return SchedulerState(state.last_adopted, 0, now + timedelta(seconds=config.retry_seconds))
    failures = state.consecutive_failures + 1
    delay = float(config.retry_seconds)
    for threshold, seconds in zip(
        config.backoff_after_failures, config.backoff_seconds, strict=True
    ):
        if failures >= threshold:
            delay = float(seconds)
    if retry_after is not None:
        delay = max(delay, min(retry_after, float(config.retry_after_cap_seconds)))
    return SchedulerState(state.last_adopted, failures, now + timedelta(seconds=delay))
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_schedule.py -q`
Expected: `8 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/schedule.py tests/test_realtime_schedule.py
git commit -m "feat: schedule fetches around the publication delay" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: 條件式請求

依據規格 §4.4：沿用 `fetch.py` 的 certifi TLS；逾時 20 秒、回應上限 1 MB；urllib 把 304 當成 `HTTPError`，這裡要當正常結果；程式內不重試。

**Files:**
- Create: `src/ingest/realtime/client.py`
- Test: `tests/test_realtime_client.py`

**Interfaces:**
- Produces: `USER_AGENT`、`Opener`（`Callable[[urllib.request.Request, float], Any]`）、`FetchResult(status, body, etag, last_modified, retry_after, error_type, elapsed_ms)`、`default_opener(request, timeout)`、`parse_retry_after(value, *, now=None) -> float | None`、`fetch(url, *, etag, last_modified, timeout, max_bytes, opener=default_opener, monotonic=time.monotonic) -> FetchResult`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message

from ingest.realtime.client import USER_AGENT, fetch, parse_retry_after


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200):
        super().__init__(body)
        self.status = status
        self.headers = headers


def _headers(values: dict[str, str]) -> Message:
    message = Message()
    for key, value in values.items():
        message[key] = value
    return message


def _fetch(opener, **overrides):
    options = {"etag": None, "last_modified": None, "timeout": 20, "max_bytes": 100}
    options.update(overrides)
    ticks = iter([0.0, 0.25])
    return fetch(
        "https://example.invalid/x.json", opener=opener, monotonic=lambda: next(ticks), **options
    )


def test_success_returns_body_and_validators() -> None:
    seen: list[urllib.request.Request] = []

    def opener(request, timeout):
        seen.append(request)
        return _Response(
            b'{"ok":1}', {"ETag": '"abc:0"', "Last-Modified": "Thu, 24 Sep 2026 00:55:09 GMT"}
        )

    result = _fetch(opener, etag='"old:0"', last_modified="Thu, 24 Sep 2026 00:45:09 GMT")

    assert (result.status, result.body, result.etag) == (200, b'{"ok":1}', '"abc:0"')
    assert result.last_modified == "Thu, 24 Sep 2026 00:55:09 GMT"
    assert result.elapsed_ms == 250
    request = seen[0]
    assert request.get_header("If-none-match") == '"old:0"'
    assert request.get_header("If-modified-since") == "Thu, 24 Sep 2026 00:45:09 GMT"
    assert request.get_header("User-agent") == USER_AGENT


def test_not_modified_is_a_normal_result() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", _headers({}), None)

    result = _fetch(opener, etag='"abc:0"')

    assert (result.status, result.body, result.error_type, result.etag) == (
        304,
        None,
        None,
        '"abc:0"',
    )


def test_server_error_keeps_retry_after() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Busy", _headers({"Retry-After": "120"}), None
        )

    result = _fetch(opener)

    assert (result.status, result.error_type, result.retry_after) == (503, "HTTP503", 120.0)


def test_connection_errors_are_named() -> None:
    def reset(request, timeout):
        raise ConnectionResetError(10054, "reset")

    def timed_out(request, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    assert _fetch(reset).error_type == "ConnectionResetError"
    assert _fetch(timed_out).error_type == "TimeoutError"
    assert _fetch(reset).status is None


def test_oversized_response_is_refused() -> None:
    result = _fetch(lambda request, timeout: _Response(b"x" * 101, {}))

    assert (result.body, result.error_type) == (None, "ResponseTooLarge")


def test_retry_after_accepts_http_dates() -> None:
    now = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)

    assert parse_retry_after("Thu, 24 Sep 2026 00:10:00 GMT", now=now) == 600.0
    assert parse_retry_after("soon", now=now) is None
    assert parse_retry_after(None) is None
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_client.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.client'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/client.py`**

```python
"""Conditional GET for the d006001 endpoint (§4.4).

程式內部不自己重試：每一次重試都經過排程，所以每一次嘗試都會留下紀錄。
"""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import certifi

USER_AGENT = "PowerQuery-TW/0.3 (realtime collector)"

Opener = Callable[[urllib.request.Request, float], Any]


@dataclass(frozen=True)
class FetchResult:
    status: int | None  # 連線層失敗時為 None
    body: bytes | None
    etag: str | None
    last_modified: str | None
    retry_after: float | None
    error_type: str | None
    elapsed_ms: int


def default_opener(request: urllib.request.Request, timeout: float) -> Any:
    # Python 3.13 起系統憑證庫拒絕台電端點的憑證鏈；沿用 fetch.py 改用 certifi，不關閉驗證。
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(request, timeout=timeout, context=context)  # noqa: S310


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


def fetch(
    url: str,
    *,
    etag: str | None,
    last_modified: str | None,
    timeout: float,
    max_bytes: int,
    opener: Opener = default_opener,
    monotonic: Callable[[], float] = time.monotonic,
) -> FetchResult:
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    started = monotonic()

    def elapsed() -> int:
        return int((monotonic() - started) * 1000)

    try:
        with opener(request, timeout) as response:
            body = response.read(max_bytes + 1)
            status = int(getattr(response, "status", 200))
            response_headers = response.headers
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return FetchResult(304, None, etag, last_modified, None, None, elapsed())
        retry_after = parse_retry_after(error.headers.get("Retry-After") if error.headers else None)
        return FetchResult(
            error.code, None, None, None, retry_after, f"HTTP{error.code}", elapsed()
        )
    except urllib.error.URLError as error:
        reason = error.reason
        name = type(reason).__name__ if isinstance(reason, BaseException) else "URLError"
        return FetchResult(None, None, None, None, None, name, elapsed())
    except (OSError, http.client.HTTPException) as error:
        return FetchResult(None, None, None, None, None, type(error).__name__, elapsed())
    if len(body) > max_bytes:
        return FetchResult(status, None, None, None, None, "ResponseTooLarge", elapsed())
    if status != 200 or not body:
        error_type = "EmptyBody" if status == 200 else f"HTTP{status}"
        return FetchResult(status, None, None, None, None, error_type, elapsed())
    return FetchResult(
        200,
        body,
        response_headers.get("ETag"),
        response_headers.get("Last-Modified"),
        parse_retry_after(response_headers.get("Retry-After")),
        None,
        elapsed(),
    )
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_client.py -q`
Expected: `6 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/client.py tests/test_realtime_client.py
git commit -m "feat: fetch d006001 with conditional requests" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: 單一實例鎖

依據規格 §4.3：作業系統層級的非阻塞鎖、只試一次；行程死掉時自動釋放；只鎖第 0 個位元組，PID 寫在後面，讓其他行程在 Windows 上也讀得到。

**Files:**
- Create: `src/ingest/realtime/lock.py`
- Test: `tests/test_realtime_lock.py`

**Interfaces:**
- Produces: `AlreadyRunning(path, holder)`（屬性 `holder`）、`read_holder(path) -> dict | None`、`is_locked(path) -> bool`、`SingleInstanceLock(path)`（`acquire()`、`release()`、context manager）

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingest.realtime.lock import AlreadyRunning, SingleInstanceLock, is_locked, read_holder

SRC = Path(__file__).resolve().parents[1] / "src"


def test_second_instance_is_refused_with_the_holder_pid(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    with SingleInstanceLock(path):
        assert is_locked(path)
        assert read_holder(path)["pid"] == os.getpid()
        with pytest.raises(AlreadyRunning, match=str(os.getpid())):
            SingleInstanceLock(path).acquire()
    assert not is_locked(path)


def test_probe_without_a_lock_file(tmp_path: Path) -> None:
    assert not is_locked(tmp_path / "missing.lock")


def test_lock_is_released_when_the_process_dies(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from ingest.realtime.lock import SingleInstanceLock\n"
        "SingleInstanceLock(Path(sys.argv[1])).acquire()\n"
        "os._exit(0)\n"  # 不釋放就結束，模擬當機
    )
    environment = {**os.environ, "PYTHONPATH": str(SRC)}
    subprocess.run(
        [sys.executable, "-c", script, str(path)], check=True, env=environment, timeout=30
    )

    with SingleInstanceLock(path):
        assert is_locked(path)
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_lock.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.lock'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/lock.py`**

```python
"""Single-instance lock for the collector (§4.3).

原語與 serving/data_management.py 的 _FileLock 相同（Windows 用 msvcrt，其他平台用 fcntl），
但只試一次、不重試。只鎖第 0 個位元組；PID 與啟動時間寫在第 1 個位元組之後，
所以在 Windows 上其他行程仍讀得到持有者資訊。行程死掉時作業系統會自動釋放鎖。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class AlreadyRunning(RuntimeError):
    def __init__(self, path: Path, holder: dict[str, object] | None):
        pid = holder.get("pid", "未知") if holder else "未知"
        super().__init__(f"收集器已在執行（PID {pid}，鎖檔 {path}）。")
        self.holder = holder


def _open(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_holder(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as handle:
            handle.seek(1)
            text = handle.read().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        holder = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    return holder if isinstance(holder, dict) else None


def is_locked(path: Path) -> bool:
    """試鎖一次再立刻放掉；收集器在跑時回 True。"""
    if not path.exists():
        return False
    handle = _open(path)
    try:
        if _try_lock(handle):
            _unlock(handle)
            return False
        return True
    finally:
        handle.close()


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        handle = _open(self.path)
        if not _try_lock(handle):
            handle.close()
            raise AlreadyRunning(self.path, read_holder(self.path))
        holder = {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
        handle.truncate(1)
        handle.seek(1)
        handle.write(json.dumps(holder).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.release()
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_lock.py -q`
Expected: `3 passed`。CI（Ubuntu）測的是 `fcntl` 那條；`msvcrt` 那條只在 Windows 本機驗證，兩邊都要跑過一次。

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/lock.py tests/test_realtime_lock.py
git commit -m "feat: keep a single collector instance with an OS lock" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: 收集器主程式

依據規格 §4.2：啟動順序（鎖 → 開資料庫，必要時重建 → 補入庫 → 套用人工決定 → 補彙總與清除 → startup 紀錄）；主迴圈最多睡 30 秒、依牆上時鐘重算；相隔超過 2 分鐘寫 `resume`；每小時維護；`stop.request` 或 Ctrl+C 正常停止；先封存再入庫，入庫失敗不記 ETag。

**Files:**
- Create: `src/ingest/realtime/collector.py`
- Test: `tests/test_realtime_collector.py`

**Interfaces:**
- Consumes: Task 1–10 全部
- Produces: `EXIT_OK = 0`、`EXIT_FAILED = 1`、`EXIT_ALREADY_RUNNING = 3`、`MAINTENANCE_INTERVAL`、`Collector(config, *, clock=None, sleep=time.sleep, opener=client.default_opener, monotonic=time.monotonic)`，方法 `run() -> int`、`run_once() -> int`、`rebuild_only() -> int`、`startup()`、`shutdown()`、`fetch(target) -> str`、`maintain(now, *, reload_decisions=True)`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import io
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

from realtime_support import make_config, payload_bytes

from ingest.realtime import archive, store
from ingest.realtime.collector import EXIT_ALREADY_RUNNING, EXIT_OK, Collector
from ingest.realtime.lock import SingleInstanceLock, is_locked
from ingest.realtime.parse import read_datetime

START = datetime(2026, 9, 18, 13, 45, 20, tzinfo=UTC)  # 21:45:20 Taipei → target 21:40
HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, etag: str):
        super().__init__(body)
        self.status = 200
        self.headers = {"ETag": etag, "Last-Modified": "Fri, 18 Sep 2026 13:45:09 GMT"}


def ok(raw: bytes, etag: str = '"a:0"'):
    return lambda request, timeout: _Response(raw, etag)


def not_modified(request, timeout):
    raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", Message(), None)


def reset(request, timeout):
    raise ConnectionResetError(10054, "reset")


class ScriptedOpener:
    """Plays one scripted response per request; `after` runs once the script is used up."""

    def __init__(self, *steps, after=None):
        self.steps = list(steps)
        self.after = after
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        step = self.steps.pop(0)
        if not self.steps and self.after is not None:
            self.after()
        return step(request, timeout)


def _collector(config, clock: FakeClock, opener) -> Collector:
    return Collector(config, clock=clock, sleep=clock.sleep, opener=opener, monotonic=lambda: 0.0)


def _attempts(config) -> list[tuple[str, str | None]]:
    return [(r["kind"], r.get("outcome")) for r in archive.iter_attempts(config.attempts_dir)]


def _db(config) -> sqlite3.Connection:
    return sqlite3.connect(config.database)


def test_run_once_archives_ingests_and_logs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)

    code = _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()

    assert code == EXIT_OK
    assert _attempts(config) == [("startup", None), ("fetch", "new"), ("shutdown", None)]
    assert _db(config).execute("SELECT data_time FROM fact_rt_snapshot").fetchall() == [
        ("2026-09-18 21:40",)
    ]
    assert len(archive.iter_archive(config.archive_dir)) == 1
    assert not is_locked(config.lock_path)


def test_second_run_sees_not_modified(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()

    code = _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    assert code == EXIT_OK
    assert _attempts(config)[-2] == ("fetch", "not_modified")


def test_payload_archived_before_a_crash_is_ingested_on_startup(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = payload_bytes()
    archive.archive_payload(
        config.archive_dir, raw, source_time=read_datetime(raw), fetched_at=START
    )

    _collector(config, FakeClock(START), ScriptedOpener(not_modified)).run_once()

    rows = _db(config).execute("SELECT data_time, sha256 FROM fact_rt_snapshot").fetchall()
    assert rows == [("2026-09-18 21:40", archive.payload_sha256(raw))]


def test_loop_retries_after_a_failure_and_stops_on_request(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    stop = lambda: config.stop_path.touch()  # noqa: E731
    opener = ScriptedOpener(reset, ok(payload_bytes()), after=stop)

    assert _collector(config, clock, opener).run() == EXIT_OK

    records = list(archive.iter_attempts(config.attempts_dir))
    fetches = [r for r in records if r["kind"] == "fetch"]
    assert [(r["outcome"], r.get("error_type")) for r in fetches] == [
        ("error", "ConnectionResetError"),
        ("new", None),
    ]
    gap = datetime.fromisoformat(fetches[1]["attempted_at"]) - datetime.fromisoformat(
        fetches[0]["attempted_at"]
    )
    assert gap == timedelta(seconds=60)
    assert records[-1]["kind"] == "shutdown"
    assert not config.stop_path.exists()


def test_waking_from_sleep_is_recorded(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    naps = iter([3 * 3600.0])

    def sleep(seconds: float) -> None:
        clock.sleep(next(naps, seconds))  # 第一次 sleep 睡了三小時（電腦睡眠）
        if clock.now > START + timedelta(hours=3, minutes=1):
            config.stop_path.touch()

    collector = Collector(
        config,
        clock=clock,
        sleep=sleep,
        opener=ScriptedOpener(ok(payload_bytes()), not_modified, not_modified, not_modified),
        monotonic=lambda: 0.0,
    )
    collector.run()

    kinds = [kind for kind, _outcome in _attempts(config)]
    assert "resume" in kinds


def test_schema_mismatch_triggers_a_rebuild_from_the_archive(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    _collector(config, clock, ScriptedOpener(ok(payload_bytes()))).run_once()
    with _db(config) as connection:
        connection.execute("UPDATE meta_rt_manifest SET schema_version = '0'")

    _collector(config, clock, ScriptedOpener(not_modified)).run_once()

    connection = _db(config)
    assert connection.execute(
        "SELECT schema_version, build_kind FROM meta_rt_manifest"
    ).fetchone() == (
        store.SCHEMA_VERSION,
        "rebuild",
    )
    assert connection.execute("SELECT COUNT(*) FROM fact_rt_snapshot").fetchone()[0] == 1


def test_changed_decisions_are_applied_during_maintenance(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeClock(START)
    collector = _collector(config, clock, ScriptedOpener(ok(payload_bytes())))
    collector.startup()
    collector.fetch("2026-09-18 21:40")
    config.units_csv.write_text(HEADER + "燃氣,大潭CC#1,unit,plant,8,\n", encoding="utf-8")

    collector.maintain(clock())
    plant = collector.connection.execute(
        """SELECT "電廠" FROM v_rt_now WHERE "機組名稱" = '大潭CC#1'"""
    ).fetchone()[0]
    collector.shutdown()

    assert plant == "大潭發電廠"


def test_second_collector_exits_with_code_3(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SingleInstanceLock(config.lock_path):
        code = _collector(config, FakeClock(START), ScriptedOpener(not_modified)).run_once()

    assert code == EXIT_ALREADY_RUNNING
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_collector.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.collector'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/collector.py`**

```python
"""The collector process: startup sequence, fetch loop and hourly maintenance (§4).

每一輪都用 try 包起來，例外只記錄、不中止迴圈。先封存再入庫：入庫前當掉的回應，
下次啟動時由 `_reconcile_archive` 補進去。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ingest.realtime import archive, client, maintenance, parse, rebuild, store
from ingest.realtime.config import RealtimeConfig
from ingest.realtime.decisions import (
    DecisionFileError,
    Decisions,
    empty_decisions,
    file_sha256,
    load_decisions,
)
from ingest.realtime.lock import AlreadyRunning, SingleInstanceLock
from ingest.realtime.schedule import SchedulerState, after_attempt, plan_next, target_slot
from ingest.realtime.timeutil import format_slot, parse_slot, to_taipei, utc_iso

LOGGER = logging.getLogger("ingest.realtime")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ALREADY_RUNNING = 3
MAINTENANCE_INTERVAL = timedelta(hours=1)
RECONCILE_DAYS = 2
_INGEST_OUTCOMES = {"new": "new", "revised": "revised", "duplicate": "stale"}


class Collector:
    def __init__(
        self,
        config: RealtimeConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        opener: client.Opener = client.default_opener,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.opener = opener
        self.monotonic = monotonic
        self.connection: sqlite3.Connection | None = None
        self.decisions: Decisions | None = None
        self.state = SchedulerState()
        self.etag: str | None = None
        self.last_modified: str | None = None

    # ---- 指令 ----------------------------------------------------------------

    def run(self) -> int:
        try:
            with SingleInstanceLock(self.config.lock_path):
                self.startup()
                try:
                    self._loop()
                except KeyboardInterrupt:
                    LOGGER.info("收到 Ctrl+C，正在停止")
                finally:
                    self.shutdown()
        except AlreadyRunning as error:
            LOGGER.error("%s", error)
            return EXIT_ALREADY_RUNNING
        return EXIT_OK

    def run_once(self) -> int:
        outcome = "error"
        try:
            with SingleInstanceLock(self.config.lock_path):
                self.startup()
                try:
                    now = self.clock()
                    outcome = self.fetch(format_slot(target_slot(now, self.config.schedule)))
                    self.maintain(self.clock())
                finally:
                    self.shutdown()
        except AlreadyRunning as error:
            LOGGER.error("%s", error)
            return EXIT_ALREADY_RUNNING
        return EXIT_OK if outcome in {"new", "revised", "stale", "not_modified"} else EXIT_FAILED

    def rebuild_only(self) -> int:
        try:
            with SingleInstanceLock(self.config.lock_path):
                decisions = self._load_decisions() or empty_decisions(self.config.plants_csv)
                connection = store.connect(self.config.database)
                try:
                    report = rebuild.rebuild(connection, self.config, decisions, now=self.clock())
                finally:
                    connection.close()
        except AlreadyRunning as error:
            LOGGER.error("%s；請先停止收集器再重建。", error)
            return EXIT_ALREADY_RUNNING
        LOGGER.info(
            "重建完成：封存 %d 份、入庫 %d、拒收 %d、損毀 %d、無法判讀 %d",
            report.archive_files,
            report.ingested,
            report.rejected,
            report.corrupted,
            report.unparsed,
        )
        return EXIT_OK

    # ---- 啟動與停止 ------------------------------------------------------------

    def startup(self) -> None:
        now = self.clock()
        self.decisions = self._load_decisions()
        self.connection = self._open_database(now)
        if self.decisions is not None:
            with store.transaction(self.connection):
                stale = store.sync_decisions(self.connection, self.decisions)
            if stale:
                LOGGER.warning("人工決定檔有 %d 列從未出現在來源中", len(stale))
        self._reconcile_archive()
        self.maintain(now, reload_decisions=False)
        self.state = SchedulerState(last_adopted=store.latest_data_time(self.connection))
        self._record({"attempted_at": utc_iso(now), "kind": "startup"})

    def shutdown(self) -> None:
        if self.connection is not None:
            try:
                self._record({"attempted_at": utc_iso(self.clock()), "kind": "shutdown"})
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.connection.close()
                self.connection = None
        self.config.stop_path.unlink(missing_ok=True)

    def _load_decisions(self) -> Decisions | None:
        try:
            decisions = load_decisions(self.config.units_csv, self.config.plants_csv)
        except DecisionFileError as error:
            LOGGER.warning("%s；沿用資料庫裡上一次成功套用的決定。", error)
            return None
        for warning in decisions.warnings:
            LOGGER.warning("%s", warning)
        return decisions

    def _open_database(self, now: datetime) -> sqlite3.Connection:
        path = self.config.database
        existed = path.is_file()
        try:
            connection = store.connect(path)
            try:
                healthy = (
                    existed
                    and store.quick_check(connection) == "ok"
                    and store.schema_version(connection) == store.SCHEMA_VERSION
                )
            except sqlite3.DatabaseError:
                connection.close()
                raise
        except sqlite3.DatabaseError:
            self._move_aside(path, now)
            connection = store.connect(path)
            healthy = False
        if not healthy:
            LOGGER.warning("realtime.db 不存在、版本不符或檢查失敗，從封存重建")
            decisions = self.decisions or empty_decisions(self.config.plants_csv)
            report = rebuild.rebuild(connection, self.config, decisions, now=now)
            LOGGER.info("重建完成：入庫 %d、拒收 %d", report.ingested, report.rejected)
        return connection

    @staticmethod
    def _move_aside(path: Path, now: datetime) -> None:
        """打不開的檔案移到旁邊再重建；封存還在，所以不會遺失資料。"""
        corrupt = path.with_name(f"{path.name}.corrupt-{to_taipei(now):%Y%m%d%H%M%S}")
        path.replace(corrupt)
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        LOGGER.error("realtime.db 無法開啟，已移到 %s", corrupt.name)

    def _reconcile_archive(self) -> None:
        """補進「已封存、還沒入庫」的回應；同一時段已有較新版本的就略過。"""
        connection = self.connection
        assert connection is not None
        latest = store.latest_data_time(connection)
        since = None
        if latest is not None:
            since = (parse_slot(latest) - timedelta(days=RECONCILE_DAYS - 1)).date().isoformat()
        for archived in archive.iter_archive(self.config.archive_dir, since=since):
            raw = archive.read_payload(archived.path)
            sha = archive.payload_sha256(raw)
            if sha[:12] != archived.sha_prefix:
                continue
            fetched_at = rebuild.archived_fetch_time(archived, {})
            existing = store.snapshot_fetched_at(connection, str(archived.data_time))
            if existing is not None and (existing[0] == sha or existing[1] >= fetched_at):
                continue
            try:
                parsed = parse.parse_payload(
                    raw,
                    now=self.clock(),
                    validation=self.config.validation,
                    slot_minutes=self.config.schedule.slot_minutes,
                )
            except parse.PayloadRejected:
                continue
            with store.transaction(connection):
                store.ingest_snapshot(
                    connection, parsed, sha256=sha, fetched_at=fetched_at, decisions=self.decisions
                )
            LOGGER.info("補入庫：%s", parsed.data_time)

    # ---- 主迴圈 ----------------------------------------------------------------

    def _loop(self) -> None:
        schedule = self.config.schedule
        last_wake = self.clock()
        last_maintenance = last_wake
        while not self.config.stop_path.exists():
            now = self.clock()
            if (now - last_wake).total_seconds() > schedule.resume_gap_seconds:
                detail = json.dumps({"from": utc_iso(last_wake), "to": utc_iso(now)})
                self._record({"attempted_at": utc_iso(now), "kind": "resume", "detail": detail})
            last_wake = now
            wait = float(schedule.loop_max_sleep_seconds)
            try:
                if now - last_maintenance >= MAINTENANCE_INTERVAL:
                    self.maintain(now)
                    last_maintenance = now
                action = plan_next(now, self.state, schedule)
                if action.kind == "fetch":
                    self.fetch(action.target_slot)
                    continue
                wait = min(wait, max(0.0, (action.at - self.clock()).total_seconds()))
            except Exception:
                LOGGER.exception("這一輪發生例外，記錄後繼續")
            self.sleep(wait)

    def fetch(self, target: str) -> str:
        config = self.config
        result = client.fetch(
            config.source.url,
            etag=self.etag,
            last_modified=self.last_modified,
            timeout=config.source.timeout_seconds,
            max_bytes=config.source.max_bytes,
            opener=self.opener,
            monotonic=self.monotonic,
        )
        fetched_at = self.clock()
        record: dict[str, object] = {
            "attempted_at": utc_iso(fetched_at),
            "kind": "fetch",
            "target_slot": target,
            "http_status": result.status,
            "elapsed_ms": result.elapsed_ms,
        }
        data_time: str | None = None
        if result.status == 304:
            outcome = "not_modified"
        elif result.body is not None:
            outcome, data_time = self._handle_payload(result, fetched_at, record)
        else:
            outcome = "error"
            record["error_type"] = result.error_type or f"HTTP{result.status}"
        record["outcome"] = outcome
        self._record(record)
        self.state = after_attempt(
            self.state,
            now=fetched_at,
            outcome=outcome,
            data_time=data_time,
            retry_after=result.retry_after,
            config=config.schedule,
        )
        LOGGER.info("%s → %s %s", target, outcome, data_time or "")
        return outcome

    def _handle_payload(
        self, result: client.FetchResult, fetched_at: datetime, record: dict[str, object]
    ) -> tuple[str, str | None]:
        raw = result.body
        assert raw is not None and self.connection is not None
        sha = archive.payload_sha256(raw)
        archive.archive_payload(
            self.config.archive_dir,
            raw,
            source_time=parse.read_datetime(raw),
            fetched_at=fetched_at,
        )
        record.update({"sha256": sha, "bytes": len(raw), "etag": result.etag})
        try:
            parsed = parse.parse_payload(
                raw,
                now=fetched_at,
                validation=self.config.validation,
                slot_minutes=self.config.schedule.slot_minutes,
            )
        except parse.PayloadRejected as rejection:
            record.update(
                {
                    "reject_code": rejection.code,
                    "data_time": rejection.data_time,
                    "detail": rejection.detail,
                }
            )
            self._remember_validators(result)
            return "rejected", None
        record["data_time"] = parsed.data_time
        try:
            with store.transaction(self.connection):
                ingested = store.ingest_snapshot(
                    self.connection,
                    parsed,
                    sha256=sha,
                    fetched_at=utc_iso(fetched_at),
                    decisions=self.decisions,
                )
        except sqlite3.Error as error:
            # 不記 ETag：下一次會重新下載並再試入庫；封存已經寫好，重啟時也會補進去。
            record.update({"error_type": "IngestError", "detail": str(error)})
            return "error", None
        self._remember_validators(result)
        return _INGEST_OUTCOMES[ingested], parsed.data_time

    def _remember_validators(self, result: client.FetchResult) -> None:
        self.etag, self.last_modified = result.etag, result.last_modified

    def _record(self, record: dict[str, object]) -> None:
        """先寫 JSONL（真實來源），再寫資料庫鏡像；資料庫失敗時重建會補回。"""
        archive.append_attempt(self.config.attempts_dir, record)
        if self.connection is None:
            return
        try:
            with store.transaction(self.connection):
                store.record_attempt(self.connection, record)
        except sqlite3.Error:
            LOGGER.exception("抓取紀錄寫入資料庫失敗；JSONL 已保存，重建時會補回")

    # ---- 維護 ------------------------------------------------------------------

    def maintain(self, now: datetime, *, reload_decisions: bool = True) -> None:
        assert self.connection is not None
        if reload_decisions:
            self._reload_decisions_if_changed()
        report = maintenance.run_maintenance(self.connection, now, self.config)
        if report.rolled_up or report.purged:
            LOGGER.info("彙總 %s；清除 %s", list(report.rolled_up), list(report.purged))

    def _reload_decisions_if_changed(self) -> None:
        current = self.decisions
        if current is not None and (
            file_sha256(self.config.units_csv) == current.sha256
            and file_sha256(self.config.plants_csv) == current.plants_sha256
        ):
            return
        decisions = self._load_decisions()
        if decisions is None or self.connection is None:
            return
        with store.transaction(self.connection):
            store.sync_decisions(self.connection, decisions)
        self.decisions = decisions
        LOGGER.info("人工決定檔已更新並套用")
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_collector.py -q`
Expected: `8 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/collector.py tests/test_realtime_collector.py
git commit -m "feat: run the realtime collector loop" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: 健康狀態與指令列

依據規格 §4.5、§7.5、§8.3：`read_status()` 唯讀、不需要鎖，是 RT-3 `/api/health` 要共用的介面；`status` 結束碼 0／1／2；`stop` 建立 `stop.request` 並最多等 60 秒。

**Files:**
- Create: `src/ingest/realtime/status.py`、`src/ingest/realtime/__main__.py`
- Test: `tests/test_realtime_status.py`

**Interfaces:**
- Consumes: Task 1–11 全部
- Produces: `read_status(root=PROJECT_ROOT, *, now=None, config=None) -> dict`，鍵為 `collector_running`、`database`、`available`、`state`（`healthy`／`stale`／`stopped`／`unavailable`）、`schema_version`、`latest_data_time`、`lag_minutes`、`consecutive_failures`、`today`（`elapsed_slots`、`snapshots`）、`gaps_24h`（`collector_down`、`fetch_failed`、`rejected`）、`undecided_units`、`stale_decisions`、`decisions_error`、`latest_quality`、`latest_warnings`
- Produces: `ingest.realtime.__main__` — `STATUS_EXIT`、`STOP_WAIT_SECONDS`、`stop(config, *, wait_seconds=60, sleep=time.sleep) -> int`、`main(argv=None, *, root=None) -> int`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from realtime_support import make_config, payload_bytes, write_project

from ingest.realtime.__main__ import main, stop
from ingest.realtime.collector import Collector
from ingest.realtime.lock import SingleInstanceLock
from ingest.realtime.status import read_status

START = datetime(2026, 9, 18, 13, 45, 20, tzinfo=UTC)  # 21:45:20 Taipei


class _Response(io.BytesIO):
    status = 200
    headers = {"ETag": '"a:0"'}


def _collect_once(config) -> None:
    Collector(
        config,
        clock=lambda: START,
        sleep=lambda _seconds: None,
        opener=lambda request, timeout: _Response(payload_bytes()),
        monotonic=lambda: 0.0,
    ).run_once()


def test_missing_database_is_unavailable(tmp_path: Path) -> None:
    report = read_status(config=make_config(tmp_path), now=START)

    assert report["state"] == "unavailable"
    assert report["collector_running"] is False


def test_status_after_one_collection(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _collect_once(config)

    report = read_status(config=config, now=START + timedelta(minutes=2))

    assert report["state"] == "stopped"  # 收集器沒在跑
    assert report["latest_data_time"] == "2026-09-18 21:40"
    assert report["lag_minutes"] == 7.3
    assert report["today"] == {"elapsed_slots": 131, "snapshots": 1}
    assert report["gaps_24h"] == {"collector_down": 143, "fetch_failed": 0, "rejected": 0}
    assert report["undecided_units"] == 204
    assert report["latest_quality"] == "ok"


def test_running_collector_is_healthy_until_data_goes_stale(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _collect_once(config)

    with SingleInstanceLock(config.lock_path):
        fresh = read_status(config=config, now=START + timedelta(minutes=2))
        stale = read_status(config=config, now=START + timedelta(minutes=45))

    assert fresh["state"] == "healthy"
    assert stale["state"] == "stale"


def test_cli_status_exit_code_and_json(tmp_path: Path) -> None:
    root = write_project(tmp_path)
    output = io.StringIO()

    with redirect_stdout(output):
        code = main(["status", "--json"], root=root)

    assert code == 2
    assert json.loads(output.getvalue())["state"] == "unavailable"


def test_stop_when_not_running(tmp_path: Path) -> None:
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(["stop"], root=write_project(tmp_path))

    assert code == 0
    assert "沒有在執行" in output.getvalue()


def test_stop_waits_for_the_lock_to_be_released(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    lock = SingleInstanceLock(config.lock_path)
    lock.acquire()

    code = stop(config, wait_seconds=3, sleep=lambda _seconds: lock.release())

    assert code == 0
    assert config.stop_path.exists()  # 真正的收集器會在停止時刪掉它


def test_stop_times_out_while_the_collector_keeps_running(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SingleInstanceLock(config.lock_path):
        code = stop(config, wait_seconds=2, sleep=lambda _seconds: None)

    assert code == 1
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_status.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.__main__'`

- [ ] **Step 3: 寫實作**

`src/ingest/realtime/status.py`：

```python
"""Health status shared by `status` and, in RT-3, the service's /api/health (§7.5, §8.3).

唯讀開檔、不需要鎖；只為了判斷收集器有沒有在跑而試鎖一次、立刻放掉。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ingest.realtime.config import RealtimeConfig, load_config
from ingest.realtime.decisions import DecisionFileError, load_decisions
from ingest.realtime.lock import is_locked
from ingest.realtime.maintenance import classify_missing, day_slots
from ingest.realtime.schedule import target_slot
from ingest.realtime.store import schema_version
from ingest.realtime.timeutil import format_slot, parse_slot, to_taipei
from ingest.validate import PROJECT_ROOT


def read_status(
    root: Path = PROJECT_ROOT,
    *,
    now: datetime | None = None,
    config: RealtimeConfig | None = None,
) -> dict[str, object]:
    config = config or load_config(root)
    now = now or datetime.now(UTC)
    base: dict[str, object] = {
        "collector_running": is_locked(config.lock_path),
        "database": str(config.database),
    }
    if not config.database.is_file():
        return {**base, "available": False, "state": "unavailable"}
    try:
        connection = sqlite3.connect(f"{config.database.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return {**base, "available": False, "state": "unavailable", "error": str(error)}
    try:
        connection.execute("PRAGMA query_only = ON")
        return {**base, **_read(connection, config, now, bool(base["collector_running"]))}
    except sqlite3.Error as error:
        return {**base, "available": False, "state": "unavailable", "error": str(error)}
    finally:
        connection.close()


def _read(
    connection: sqlite3.Connection, config: RealtimeConfig, now: datetime, running: bool
) -> dict[str, object]:
    latest = connection.execute("SELECT MAX(data_time) FROM fact_rt_snapshot").fetchone()[0]
    lag = None if latest is None else round((now - parse_slot(latest)).total_seconds() / 60, 1)
    failures = 0
    for (outcome,) in connection.execute(
        "SELECT outcome FROM meta_rt_attempt WHERE kind = 'fetch' ORDER BY id DESC LIMIT 100"
    ):
        if outcome not in ("error", "rejected"):
            break
        failures += 1
    target = format_slot(target_slot(now, config.schedule))
    today = to_taipei(now).date().isoformat()
    elapsed = [slot for slot in day_slots(today, config.schedule.slot_minutes) if slot <= target]
    snapshots_today = connection.execute(
        "SELECT COUNT(*) FROM fact_rt_snapshot WHERE data_time >= ? AND data_time < ?",
        (f"{today} 00:00", f"{today} 24:00"),
    ).fetchone()[0]
    window_start = parse_slot(target) - timedelta(days=1)
    window = [
        format_slot(window_start + timedelta(minutes=config.schedule.slot_minutes * step))
        for step in range(1, config.slots_per_day + 1)
    ]
    gaps = Counter(classify_missing(connection, window).values())
    undecided = connection.execute(
        "SELECT COUNT(*) FROM dim_rt_unit WHERE grain = 'undecided' OR access_scope = 'undecided'"
    ).fetchone()[0]
    known = {
        (row[0], row[1])
        for row in connection.execute("SELECT unit_type, unit_name FROM dim_rt_unit")
    }
    decisions_error: str | None = None
    stale_decisions = 0
    try:
        decisions = load_decisions(config.units_csv, config.plants_csv)
        stale_decisions = len(set(decisions.units) - known)
    except (DecisionFileError, OSError) as error:
        decisions_error = str(error)
    quality_row = (
        connection.execute(
            "SELECT quality, warnings FROM fact_rt_snapshot WHERE data_time = ?", (latest,)
        ).fetchone()
        if latest is not None
        else None
    )
    if not running:
        state = "stopped"
    elif lag is None or lag > config.stale_after_minutes:
        state = "stale"
    else:
        state = "healthy"
    return {
        "available": True,
        "state": state,
        "schema_version": schema_version(connection),
        "latest_data_time": latest,
        "lag_minutes": lag,
        "consecutive_failures": failures,
        "today": {"elapsed_slots": len(elapsed), "snapshots": snapshots_today},
        "gaps_24h": {
            reason: gaps[reason] for reason in ("collector_down", "fetch_failed", "rejected")
        },
        "undecided_units": undecided,
        "stale_decisions": stale_decisions,
        "decisions_error": decisions_error,
        "latest_quality": None if quality_row is None else quality_row[0],
        "latest_warnings": [] if quality_row is None else json.loads(quality_row[1]),
    }
```

`src/ingest/realtime/__main__.py`：

```python
"""python -m ingest.realtime {run,once,rebuild,status,stop}（§4.5）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ingest.realtime.collector import EXIT_FAILED, EXIT_OK, Collector
from ingest.realtime.config import RealtimeConfig, RealtimeConfigError, load_config
from ingest.realtime.lock import is_locked, read_holder
from ingest.realtime.status import read_status
from ingest.realtime.timeutil import TAIPEI

STATUS_EXIT = {"healthy": 0, "stale": 1, "stopped": 2, "unavailable": 2}
STOP_WAIT_SECONDS = 60


def _configure_logging(config: RealtimeConfig, *, to_file: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if to_file:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(TAIPEI).strftime("%Y%m%d-%H%M%S")
        log_path = config.log_dir / f"realtime-collector-{stamp}.log"
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _format_status(report: dict[str, object]) -> str:
    lines = [
        f"狀態：{report['state']}",
        f"收集器：{'執行中' if report['collector_running'] else '未執行'}",
    ]
    if not report.get("available"):
        lines.append("realtime.db 不存在或無法開啟")
        return "\n".join(lines)
    today = report["today"]
    assert isinstance(today, dict)
    lines += [
        f"最新時段：{report['latest_data_time']}（落後 {report['lag_minutes']} 分鐘）",
        f"連續失敗：{report['consecutive_failures']} 次",
        f"今天：已過 {today['elapsed_slots']} 個時段，拿到 {today['snapshots']} 個",
        f"最近 24 小時缺口：{report['gaps_24h']}",
        f"未定機組：{report['undecided_units']}；過期決定：{report['stale_decisions']}",
        f"最新快照品質：{report['latest_quality']}",
    ]
    if report.get("decisions_error"):
        lines.append(f"人工決定檔錯誤：{report['decisions_error']}")
    return "\n".join(lines)


def stop(
    config: RealtimeConfig,
    *,
    wait_seconds: int = STOP_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if not is_locked(config.lock_path):
        print("收集器沒有在執行。")
        return EXIT_OK
    config.stop_path.parent.mkdir(parents=True, exist_ok=True)
    config.stop_path.touch()
    for _ in range(wait_seconds):
        if not is_locked(config.lock_path):
            print("收集器已停止。")
            return EXIT_OK
        sleep(1)
    holder = read_holder(config.lock_path) or {}
    print(
        f"收集器 {wait_seconds} 秒內沒有停止（PID {holder.get('pid', '未知')}），"
        "請到它的視窗按 Ctrl+C。",
        file=sys.stderr,
    )
    return EXIT_FAILED


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.realtime", description="即時機組發電量收集器（RT-1）"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="常駐收集")
    commands.add_parser("once", help="立刻抓一次、做一次維護就結束")
    commands.add_parser("rebuild", help="從封存重建 realtime.db（收集器必須先停）")
    status_parser = commands.add_parser("status", help="健康狀態")
    status_parser.add_argument("--json", action="store_true", help="輸出 JSON")
    commands.add_parser("stop", help="要求收集器停止，最多等 60 秒")
    args = parser.parse_args(argv)
    try:
        config = load_config(root) if root is not None else load_config()
    except (OSError, RealtimeConfigError) as error:
        print(f"設定錯誤：{error}", file=sys.stderr)
        return EXIT_FAILED
    if args.command == "status":
        report = read_status(config=config)
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else _format_status(report)
        )
        return STATUS_EXIT[str(report["state"])]
    if args.command == "stop":
        return stop(config)
    _configure_logging(config, to_file=args.command == "run")
    collector = Collector(config)
    if args.command == "run":
        return collector.run()
    if args.command == "once":
        return collector.run_once()
    return collector.rebuild_only()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_status.py -q`
Expected: `7 passed`

再確認指令列在真實專案根目錄可用（不連網）：

Run: `uv run python -m ingest.realtime status`
Expected: 結束碼 2，印出 `狀態：unavailable` 與 `realtime.db 不存在或無法開啟`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add src/ingest/realtime/status.py src/ingest/realtime/__main__.py tests/test_realtime_status.py
git commit -m "feat: add realtime status and the command line" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: 人工決定檔（含人工審核關卡）

依據規格 §5.3、§11.3 第 2 點：程式只產生候選；**每一列都要人確認後才提交**。執行期間程式不做任何字串比對推測。

**Files:**
- Create: `src/ingest/realtime/candidates.py`、`taipower_align/realtime_units.csv`
- Test: `tests/test_realtime_candidates.py`

**Interfaces:**
- Consumes: `DECISION_FIELDS`（Task 4）、`clean_type`（Task 2）
- Produces: `BUCKET_HINTS`、`SHARED_TYPES`、`plant_aliases(plants_csv) -> dict[int, tuple[str, ...]]`、`propose(snapshot, aliases) -> list[dict[str, str]]`、`main(argv=None) -> int`

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

from pathlib import Path

from realtime_support import make_config, snapshot_payload

from ingest.realtime.candidates import plant_aliases, propose


def _proposals(tmp_path: Path) -> dict[tuple[str, str], dict[str, str]]:
    aliases = plant_aliases(make_config(tmp_path).plants_csv)
    return {
        (row["unit_type"], row["unit_name"]): row for row in propose(snapshot_payload(), aliases)
    }


def test_every_detail_row_gets_exactly_one_proposal(tmp_path: Path) -> None:
    proposals = _proposals(tmp_path)

    assert len(proposals) == 204
    assert not any(name.startswith("小計") for _type, name in proposals)


def test_prefix_match_proposes_the_plant(tmp_path: Path) -> None:
    proposals = _proposals(tmp_path)

    assert proposals[("燃氣", "大潭CC#1")]["plant_id"] == "8"
    assert proposals[("儲能負載", "明潭#1")]["access_scope"] == "plant"
    assert proposals[("儲能負載", "明潭#1")]["plant_id"] == "12"


def test_renewables_and_buckets_are_shared(tmp_path: Path) -> None:
    proposals = _proposals(tmp_path)

    assert proposals[("太陽能", "其它購電太陽能")]["grain"] == "bucket"
    assert proposals[("太陽能", "其它購電太陽能")]["access_scope"] == "shared"
    assert proposals[("風力", "沃一風")]["access_scope"] == "shared"
    assert proposals[("汽電共生", "汽電共生")]["grain"] == "bucket"
    assert proposals[("儲能", "電池(註16)")]["grain"] == "bucket"


def test_no_unique_match_is_left_for_a_human(tmp_path: Path) -> None:
    row = _proposals(tmp_path)[("燃煤", "林口#1")]  # 測試名冊只有大潭與明潭

    assert row["access_scope"] == ""
    assert row["note"].startswith("待人工確認")
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_realtime_candidates.py -q`
Expected: `ModuleNotFoundError: No module named 'ingest.realtime.candidates'`

- [ ] **Step 3: 寫實作 `src/ingest/realtime/candidates.py`**

```python
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
```

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_realtime_candidates.py -q`
Expected: `4 passed`

- [ ] **Step 5: Lint 並提交程式**

```bash
uv run ruff format --check . && uv run ruff check .
git add src/ingest/realtime/candidates.py tests/test_realtime_candidates.py
git commit -m "feat: propose realtime unit decisions for human review" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: 用真實電廠名冊產生候選**

候選檔寫在 repo 外（例如系統暫存目錄），不要直接寫進 `taipower_align/`：

Run: `uv run python -m ingest.realtime.candidates > "%TEMP%\realtime_units.candidates.csv"`（PowerShell 用 `$env:TEMP`）
Expected: 204 列資料。以 2026-09-18 21:40 的快照實測：前綴命中 104 列、名稱中間命中 4 列、`shared` 64 列、待人工確認 32 列。

- [ ] **Step 7: ⚠️ 人工審核關卡——停下來，把候選檔交給使用者逐列確認**

執行者**不可自行決定**任何一列，只能整理下列資訊給使用者參考：

- 待人工確認的 32 列幾乎都是名稱裡沒有電廠名的水力分廠。`taipower_align/README.md` 的「關鍵解法」一節有對應：德基、青山、谷關、天輪、馬鞍 → 大甲溪發電廠（9）；明潭、鉅工、水里 → 明潭發電廠（12）；碧海、立霧、龍澗 → 東部發電廠（14）。其餘（例如翡翠、義興、名間、嘉南西口、卑南、捷祥關山、松林）需要查證，可能不屬於 34 座電廠，那就標 `shared`。
- 名稱中間命中的 4 列需要確認：`澎湖尖山(註4)` → 尖山（11）、`金門塔山(註4)` → 塔山（6）、`馬祖珠山(註4)` → 珠山（3）、`烏來&桂山&粗坑` → 桂山（16）；最後一列是三個電廠併在一起，可能應該是 `bucket`。
- 不屬於 34 座電廠的列（民間風場、購電彙總、汽電共生等）標 `shared`（規格 §5.3）。
- `note` 欄改寫成判斷依據（例如「大甲溪發電廠的德基分廠，依 taipower_align/README.md」），不要保留「候選：」字樣。

使用者確認後，把審核過的內容存成 `taipower_align/realtime_units.csv`（UTF-8、標頭一字不差）。

- [ ] **Step 8: 驗證審核後的檔案涵蓋全部明細、而且沒有任何警告**

Run:

```bash
uv run python -c "from datetime import datetime, timezone; from pathlib import Path; from ingest.realtime.config import load_config; from ingest.realtime.decisions import load_decisions; from ingest.realtime.parse import parse_payload; c = load_config(); d = load_decisions(c.units_csv, c.plants_csv); p = parse_payload(Path('tests/fixtures/realtime/d006001_2026-09-18T2140.json').read_bytes(), now=datetime(2026, 9, 18, 14, tzinfo=timezone.utc), validation=c.validation, slot_minutes=10); keys = {(r.unit_type, r.unit_name) for r in p.details}; print(len(d.units), 'decisions,', len(d.warnings), 'warnings, missing:', sorted(keys - set(d.units)))"
```

Expected: `204 decisions, 0 warnings, missing: []`

- [ ] **Step 9: Commit**

```bash
git add taipower_align/realtime_units.csv
git commit -m "feat: add the reviewed realtime unit decisions" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: 啟動批次檔

依據規格 §4.6：`即時收集啟動.bat` 異常結束時等一段時間後重啟，看到結束碼 3（已有一份在跑）就不重啟；`開機自動啟動-即時收集.bat` 以最小化視窗啟動前者；`停止即時收集.bat` 呼叫 `stop` 子指令並傳回結束碼。收集器自己把輸出寫進 `logs/`，批次檔不重導。等待改用 PowerShell `Start-Sleep`，因為 `timeout` 在輸入被重導時會直接失敗；`POWERQUERY_RESTART_DELAY` 讓測試不必等 60 秒。

**Files:**
- Create: `即時收集啟動.bat`、`開機自動啟動-即時收集.bat`、`停止即時收集.bat`、`.gitattributes`
- Test: `tests/test_realtime_launchers.py`

**Interfaces:**
- Consumes: `python -m ingest.realtime run`、`stop`（Task 12），結束碼 0／3
- 環境變數：`POWERQUERY_RESTART_DELAY`（預設 60）、`POWERQUERY_NO_PAUSE`（與 `啟動.bat` 相同，測試用）

- [ ] **Step 1: 寫會失敗的測試**

```python
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ingest.validate import PROJECT_ROOT

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")

COLLECT = "即時收集啟動.bat"
STARTUP = "開機自動啟動-即時收集.bat"
STOP = "停止即時收集.bat"

# 第一次呼叫回 1（模擬當掉），之後回 3（已有一份在跑），用來驗證「重啟一次、看到 3 就停」。
FAKE_UV = """@echo off
>>"%UV_LOG%" echo %*
if not exist "%UV_LOG%.count" (
  type nul > "%UV_LOG%.count"
  exit /b 1
)
exit /b 3
"""


def _run(tmp_path: Path, launcher: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    project = tmp_path / "OneDrive 測試" / "台電 查詢專案"
    fake_bin = tmp_path / "fake tools"
    project.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copyfile(PROJECT_ROOT / launcher, project / launcher)
    (fake_bin / "uv.cmd").write_text(FAKE_UV, encoding="ascii")
    log = tmp_path / "uv calls.log"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "UV_LOG": str(log),
        "POWERQUERY_NO_PAUSE": "1",
        "POWERQUERY_RESTART_DELAY": "0",
    }
    result = subprocess.run(
        f'cmd.exe /d /c call "{project / launcher}"',
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_collector_launcher_restarts_after_a_crash_but_not_when_already_running(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, COLLECT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == ["run --no-sync python -m ingest.realtime run"] * 2
    assert "Restarting" in result.stdout
    assert "already running" in result.stdout


def test_stop_launcher_calls_the_stop_command(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, STOP)

    assert calls == ["run --no-sync python -m ingest.realtime stop"]
    assert result.returncode == 1  # fake uv 第一次回 1，批次檔要把結束碼傳出去


def test_startup_launcher_opens_the_collector_minimised() -> None:
    content = (PROJECT_ROOT / STARTUP).read_text(encoding="utf-8")

    assert "chcp 65001" in content
    assert f'/min "%~dp0{COLLECT}"' in content


@pytest.mark.parametrize("launcher", [COLLECT, STARTUP, STOP])
def test_launchers_use_crlf(launcher: str) -> None:
    raw = (PROJECT_ROOT / launcher).read_bytes()

    assert raw.count(b"\n") == raw.count(b"\r\n")
```

- [ ] **Step 2: 執行，確認失敗（Windows）**

Run: `uv run pytest tests/test_realtime_launchers.py -q`
Expected: Windows 上 `FileNotFoundError`（找不到 `即時收集啟動.bat`）；非 Windows 全部 skipped

- [ ] **Step 3: 寫三個批次檔與 `.gitattributes`**

`即時收集啟動.bat`：

```bat
@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title PowerQuery TW - 即時收集

rem 從批次檔所在的專案目錄執行；pushd 也支援 UNC 路徑。
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto :project_directory_error

where uv >nul 2>&1
if errorlevel 1 goto :uv_missing

if not defined POWERQUERY_RESTART_DELAY set "POWERQUERY_RESTART_DELAY=60"

:collect
echo [realtime] Starting the collector. Press Ctrl+C in this window to stop it.
call uv run --no-sync python -m ingest.realtime run
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" goto :stopped
if "%RESULT%"=="3" goto :already_running
echo [realtime] The collector exited with code %RESULT%. Restarting in %POWERQUERY_RESTART_DELAY% seconds...
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "Start-Sleep -Seconds %POWERQUERY_RESTART_DELAY%"
goto :collect

:already_running
echo [realtime] Another collector is already running; this window will not start a second one.
goto :success

:stopped
echo [realtime] The collector has stopped.
goto :success

:uv_missing
echo [ERROR] uv was not found. Install uv, make sure uv.exe is on PATH, and try again.
goto :failure

:project_directory_error
echo [ERROR] Could not enter the project directory containing this launcher.
goto :failure_without_popd

:failure
popd

:failure_without_popd
if defined POWERQUERY_NO_PAUSE exit /b 1
pause
exit /b 1

:success
popd
if defined POWERQUERY_NO_PAUSE exit /b 0
pause
exit /b 0
```

`開機自動啟動-即時收集.bat`：

```bat
@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
rem 把這個檔案的捷徑放進「啟動」資料夾（Win+R 輸入 shell:startup），登入後就會開始收集。
start "PowerQuery TW - 即時收集" /min "%~dp0即時收集啟動.bat"
exit /b 0
```

`停止即時收集.bat`：

```bat
@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title PowerQuery TW - 停止即時收集
pushd "%~dp0" >nul 2>&1
call uv run --no-sync python -m ingest.realtime stop
set "RESULT=%ERRORLEVEL%"
popd
if defined POWERQUERY_NO_PAUSE exit /b %RESULT%
pause
exit /b %RESULT%
```

`.gitattributes`（新檔）：

```text
# Windows 批次檔必須是 CRLF：LF 行尾時 cmd.exe 的 goto 可能找不到標籤。
*.bat text eol=crlf
```

用編輯器存檔時不一定是 CRLF，最後統一轉一次：

Run: `uv run python -c "from pathlib import Path; [p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')) for p in Path('.').glob('*.bat') if '即時收集' in p.name]"`

- [ ] **Step 4: 執行，確認通過（Windows）**

Run: `uv run pytest tests/test_realtime_launchers.py -q`
Expected: Windows 上 `6 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: 無問題

- [ ] **Step 6: Commit**

```bash
git add 即時收集啟動.bat 開機自動啟動-即時收集.bat 停止即時收集.bat .gitattributes tests/test_realtime_launchers.py
git commit -m "feat: add the realtime collector launchers" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: 文件、實機驗證與儲存點

依據規格 §11.2、§11.3。對使用者的說法（README、ATTRIBUTION、SYSTEM_CARD、`coverage.yaml`）**不在這裡改**，留給 RT-3。

**Files:**
- Modify: `docs/SERVING.md`、`docs/lineage/01_資料來源.csv`、`docs/lineage/02_清洗規則.csv`、`docs/lineage/03_對齊產物.csv`、`docs/lineage/04_資料表與檢視.csv`、`docs/lineage/05_陷阱與人工裁決.csv`、`docs/lineage/README.md`、`log.md`

- [ ] **Step 1: `docs/SERVING.md` 加收集器操作說明**

在 `## 網頁工作台` 之前插入整節：

````markdown
## 即時資料收集器

> RT-1 階段：收集器只負責收資料，查詢頁與 API 要到 RT-3 才查得到這些資料。設計見[即時機組發電量設計規格](superpowers/specs/2026-09-24-realtime-ingest-design.md)。

收集器是獨立的常駐程式，每 10 分鐘向台電抓一次「各機組發電量即時資訊」（`d006001`）。原始回應先 gzip 封存到 `data/realtime/archive/`，再寫進 `data/processed/realtime.db`。它和網頁服務互不相干：重啟服務不影響收集，收集器也不必跟著服務一起啟動。

### 啟動與停止

- 雙擊 [`即時收集啟動.bat`](../即時收集啟動.bat)：開一個視窗持續收集，異常結束時 60 秒後自動重啟；同一時間只會有一份在跑。
- 登入後自動啟動：按 `Win + R` 輸入 `shell:startup`，把 [`開機自動啟動-即時收集.bat`](../開機自動啟動-即時收集.bat) 的捷徑放進去。電腦睡眠或關機期間收不到資料，那些時段會記成缺口。
- 雙擊 [`停止即時收集.bat`](../停止即時收集.bat)，或在收集器視窗按 `Ctrl+C`。

手動操作：

```powershell
uv run python -m ingest.realtime status    # 健康狀態；加 --json 給程式讀
uv run python -m ingest.realtime once      # 立刻抓一次、做一次維護就結束
uv run python -m ingest.realtime rebuild   # 從封存重建 realtime.db（收集器必須先停）
uv run python -m ingest.realtime stop      # 要求收集器停止，最多等 60 秒
```

`status` 的結束碼：0 正常、1 落後超過 30 分鐘、2 收集器沒在跑或資料庫不存在。

### 檔案

| 位置 | 內容 |
|---|---|
| `data/realtime/archive/` | 每份內容不同的原始回應，gzip 永久保存，是重建時的真實來源 |
| `data/realtime/attempts/` | 每次抓取嘗試一行 JSON：成功、304、失敗、拒收、睡眠後醒來 |
| `data/processed/realtime.db` | 10 分鐘明細（保留 14 天）、每日估算發電量（永久）、缺口紀錄 |
| `logs/realtime-collector-*.log` | 收集器的執行紀錄 |
| `taipower_align/realtime_units.csv` | 每條序列的人工決定：個別或彙總、屬於哪座電廠 |

`data/` 與 `logs/` 都不進版控。

### 升級

停止收集器 → 更新程式 → 重新啟動收集器（schema 版本不符時會自動從封存重建）→ 重啟網頁服務。修改 `realtime_units.csv` 不需要重建，收集器每小時會自動套用。

### 資料治理

即時快照不走資料管理的四眼審核：每 10 分鐘就有一筆，逐筆由人核准並不可行。四眼原則改套在規則上：解析程式、`realtime_units.csv` 與 `configs/realtime.yaml` 的變更一律走 PR 審查，每一筆快照則由自動驗證放行。結構有問題的快照不入庫（原始回應仍然封存），數值有問題的照收並標記品質。
````

- [ ] **Step 2: 更新資料血緣**

CSV 用 UTF-8（檔頭已有 BOM，附加時不要再寫 BOM），行尾沿用檔案現有的行尾。

`01_資料來源.csv` 附加一列：

```csv
9,data/realtime/archive/,台灣電力公司各機組發電量即時資訊(含外購電力),8931,https://data.gov.tw/dataset/8931,d006001,https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json,每 10 分鐘（收集器持續封存每一份）,每份 215,每份約 36.5 KB（gzip 後約 3.7 KB）,逐份不同（檔名帶前 12 碼）,data/realtime/archive/,不複製到對齊工作區,realtime_root,否（端點只提供當下快照，過去的只存在封存裡）
```

`02_清洗規則.csv`：第 29 列的「實測效果」由 `215 列中 10 列是小計；風力的小計列寫成「小計(註5)」而非「小計」` 改成 `215 列中 11 列是小計，其中風力那列寫成「小計(註5)」`，並附加：

```csv
即時封存（d006001）,讀取,以 utf-8-sig 解碼；DateTime 決定時段，必須在 10 分鐘整點且不晚於現在 15 分鐘,src/ingest/realtime/parse.py:parse_payload,不合格就整份拒收，但原始回應仍在封存，修好規則後重建即可補回
即時封存（d006001）,剔除,名稱以「小計」開頭的列不進明細，另存 fact_rt_type_subtotal 並逐類比對明細加總,src/ingest/realtime/parse.py:parse_payload,11 列小計（含「小計(註5)」）；11 類明細加總與小計全部相等
即時封存（d006001）,隔離,名稱不是「小計」開頭、值卻是「數值(百分比%)」格式的列,src/ingest/realtime/parse.py:parse_payload,改了名字的彙總列進 fact_rt_quarantine，不會混進明細
即時封存（d006001）,清洗,機組類型去掉 HTML 標籤與結尾的英文括號，原樣另存,src/ingest/realtime/parse.py:clean_type,「儲能負載(Energy Storage System Load)</b>」→「儲能負載」
即時封存（d006001）,解析,「-」「N/A」與空字串存 NULL；出力比去掉 % 後除以 100,src/ingest/realtime/parse.py:parse_number／parse_ratio,容量「-」45 列、發電量「N/A」2 列；NULL 不當成 0
即時封存（d006001）,標記,備註為「通訊異常」的值標 comm_error，不算進估算發電量,src/ingest/realtime/parse.py:parse_payload,龍三風的 0.0 不是真的零
```

`03_對齊產物.csv` 附加一列（`位元組` 填 `taipower_align/realtime_units.csv` 的實際大小，例如 `uv run python -c "import os; print(os.path.getsize('taipower_align/realtime_units.csv'))"`）：

```csv
realtime_units.csv,人工維護,204,<位元組>,python -m ingest.realtime.candidates 產生候選後人工逐列確認,即時機組的粒度（個別／彙總）與電廠歸屬；電廠帳號的權限依據
```

`04_資料表與檢視.csv` 附加：

```csv
realtime.db:meta_rt_manifest,資料表,1,收集器,否,id、schema_version、built_at、build_kind、archive_files、decisions_sha256、plants_sha256
realtime.db:dim_rt_plant,資料表,34,plants.csv,否,plant_id、plant_name
realtime.db:dim_rt_unit,資料表,約 204（隨來源增減）,即時封存 + realtime_units.csv,否,id、unit_type、unit_type_raw、unit_name、flow、grain、access_scope、plant_id、decision_note、first_seen、last_seen
realtime.db:fact_rt_snapshot,資料表,每天最多 144,即時封存,否,data_time、sha256、fetched_at、revision、detail_rows、quality、warnings
realtime.db:fact_rt_unit_10min,資料表,約 204 × 144 × 14（保留 14 天）,即時封存,否,data_time、unit_id、net_mw、capacity_mw、load_ratio、note、value_status
realtime.db:fact_rt_type_subtotal,資料表,約 11 × 144 × 14（保留 14 天）,即時封存,否,data_time、unit_type、subtotal_name、net_mw、net_share_pct、capacity_mw、capacity_share_pct、detail_net_mw
realtime.db:fact_rt_quarantine,資料表,通常為 0,即時封存,否,data_time、row_index、reason、raw_row
realtime.db:fact_rt_unit_daily,資料表,每天約 204（永久保留）,fact_rt_unit_10min,否,date、unit_id、energy_mwh_est、max_mw、avg_mw、min_mw、samples、expected_samples、notes_seen、source
realtime.db:fact_rt_day,資料表,每天 1（永久保留）,fact_rt_snapshot + meta_rt_attempt,否,date、snapshots、expected、missed_collector_down、missed_fetch_failed、missed_rejected、rows_at_rollup、rolled_up_at、purged_at
realtime.db:meta_rt_attempt,資料表,每天約 150–200,data/realtime/attempts/*.jsonl,否,id、attempted_at、kind、target_slot、outcome、http_status、error_type、reject_code、data_time、sha256、bytes、elapsed_ms、etag、detail
realtime.db:v_rt_10min,語意檢視,同 fact_rt_unit_10min,fact_rt_unit_10min + dim_rt_unit + dim_rt_plant,否（RT-3 開放）,資料時間、日期、時刻、機組鍵、機組類型、機組名稱、粒度、電廠、裝置容量_MW、淨發電量_MW、出力比、備註、數值狀態
realtime.db:v_rt_now,語意檢視,約 204,v_rt_10min 的最新時段,否（RT-3 開放）,同 v_rt_10min
realtime.db:v_rt_daily,語意檢視,同 fact_rt_unit_daily,fact_rt_unit_daily + dim_rt_unit + dim_rt_plant,否（RT-3 開放）,日期、機組鍵、機組類型、機組名稱、粒度、電廠、估算發電量_MWh、最高出力_MW、平均出力_MW、最低出力_MW、取樣點數、應有點數、資料完整度、當日備註、資料來源
```

`05_陷阱與人工裁決.csv`：第 3 列的「具體數字」由 `215 列中 10 列是小計，風力那列寫成「小計(註5)」` 改成 `215 列中 11 列是小計，其中風力那列寫成「小計(註5)」`；第 23 列的「目前怎麼處理」由 `能解釋機組出力長期偏低而歲修表查不到的情形，尚未實作` 改成 `RT-1 起逐時段收進 realtime.db（note、notes_seen），RT-3 開放查詢前仍查不到`；並附加：

```csv
資料陷阱,units_generation.json,機組名稱跨類型重複,12 個名稱各出現兩次：11 部儲能同時在「儲能」與「儲能負載」，「其它台電自有」同時在太陽能與風力,realtime.db 以（清洗後類型，名稱）為自然鍵，放電與充電分成兩條序列
資料陷阱,units_generation.json,明細裡也有彙總列,"「其它購電太陽能」一列就是 15,037.7 MW（太陽能容量的 97.7%）；「汽電共生」一列代表全部汽電共生",realtime_units.csv 逐列標 grain（個別／彙總），RT-3 回答單機排名時據此揭露
資料陷阱,units_generation.json,「通訊異常」的數值不可信,龍三風備註「通訊異常」、出力 0.0,value_status 標 comm_error，不算進估算發電量，反映在資料完整度
人工裁決,taipower_align/realtime_units.csv,即時機組的粒度與電廠歸屬,204 列（候選：前綴命中 104、中間命中 4、shared 64、待確認 32）,程式只產生候選，逐列人工確認後才提交；沒列在檔案裡的機組一律未定，電廠帳號看不到
```

`docs/lineage/README.md`：

- 第 5 行改成：`從八份台電官方開放資料，經過清洗與對齊，變成資料庫裡的 18 張表與 6 個語意檢視；另外，即時收集器把 d006001 的每一份快照封存後寫進 realtime.db 的 10 張表與 3 個檢視（RT-3 前 Text2SQL 還查不到）。`
- 五份檔案的列數依序改成 9、40、23、37、26。

驗證列數：

Run: `uv run python -c "import csv, glob; [print(len(list(csv.DictReader(open(f, encoding='utf-8-sig')))), f) for f in sorted(glob.glob('docs/lineage/0*.csv'))]"`
Expected: 依序印出 9、40、23、37、26

- [ ] **Step 3: 全套驗證**

Run: `uv run ruff format --check . && uv run ruff check . && uv run pytest -q`
Expected: ruff 無問題；pytest 全數通過（原有測試加上本計畫新增的 99 項；非 Windows 時 `test_realtime_launchers.py` 的 6 項 skipped）

Run: `git diff --stat origin/main -- src/text2sql src/serving src/eval benchmarks corpus configs/guard.yaml configs/coverage.yaml README.md ATTRIBUTION.md docs/SYSTEM_CARD.md`
Expected: 沒有任何輸出——RT-1 沒有改到任何查詢行為，四份題庫的評測結果不會變（規格 §11.3 第 6 點）

Run: `git diff --check origin/main`
Expected: 沒有任何輸出

- [ ] **Step 4: ⚠️ 實機驗證（會連網，先徵得使用者同意）**

這一步會對台電官方端點發出 1 次請求（約 36 KB）。**先問使用者**，同意後才執行：

Run: `uv run python -m ingest.realtime once` 然後 `uv run python -m ingest.realtime status`
Expected: `once` 結束碼 0；`status` 顯示 `最新時段` 為最近一個已發布的時段、`最新快照品質：ok`、`未定機組：0`（若台電新增了機組，未定數就是新機組的數量，回報給使用者，不要自行補決定）。`data/realtime/archive/` 出現一個 `.json.gz`，`data/realtime/attempts/` 出現當月的 `.jsonl`。

接著雙擊 `即時收集啟動.bat` 讓它跑超過一個時段（至少 11 分鐘），再雙擊 `停止即時收集.bat`；`status` 的抓取紀錄應該看得到 startup、fetch、shutdown。最後把電腦睡眠一次再喚醒，確認 `attempts` 出現 `resume`（規格 §12）。

- [ ] **Step 5: 寫 log.md 儲存點 CP-073**

在 `log.md` 最上方（標頭說明之後）加入，括號內的驗收數字用 Step 3、Step 4 的實際輸出：

````markdown
## CP-073 — 即時收集器 RT-1：先封存再入庫、可重建的 realtime.db

- 時間：（commit 當下的時間，格式如 2026-09-24 14:30 +08:00）
- 狀態：已完成（RT-1；RT-2、RT-3 尚未開始）
- 分支：`feat/realtime-collector`（從 `docs/realtime-ingest-design` 開，規格與計畫在 CP-072）
- 起點：依 `docs/superpowers/specs/2026-09-24-realtime-ingest-design.md` 與
  `docs/superpowers/plans/2026-09-24-realtime-ingest-rt1.md` 實作 RT-1。

### 做了什麼

- `src/ingest/realtime/`：常駐收集器（`python -m ingest.realtime run`），每個時段抓到就停；原始回應先
  gzip 封存到 `data/realtime/archive/`，再寫進 `data/processed/realtime.db`；抓取紀錄同時寫 JSONL 與資料庫。
- 結構有問題的快照整份拒收（封存仍在），數值有問題的照收並標記品質；小計、彙總列、跨類型同名、
  `N/A`、「通訊異常」都在入庫時處理。
- 每日估算發電量只算可信值；缺口分成收集器沒在跑、抓取失敗、拒收三種；清除條件寫在 SQL 裡。
- schema 版本不符或資料庫損壞時自動從封存重建；重建結果與逐筆入庫的內容 checksum 相同（測試釘住）。
- `taipower_align/realtime_units.csv`：204 列人工決定（粒度與電廠歸屬），逐列人工確認。
- 三個批次檔、`.gitattributes`（`*.bat` 固定 CRLF）、`docs/SERVING.md` 操作說明、`docs/lineage/` 更新。

### 刻意沒做的

- 查詢端完全沒動：`v_rt_*` 檢視存在但服務查不到（RT-3）。README、ATTRIBUTION、SYSTEM_CARD、
  `coverage.yaml` 的說法等 RT-3 再改。
- `d006010` 回補與對帳（RT-2）。

- 驗收：`uv run ruff format --check .`、`uv run ruff check .` 通過；`uv run pytest -q` →（貼上最後一行）；
  `git diff --stat origin/main -- src/text2sql src/serving src/eval benchmarks corpus` 無輸出；
  實機 `once` →（最新時段、品質、未定機組數）；連續收集 →（抓取紀錄摘要）；睡眠喚醒 →（是否出現 resume）。
- 回退方式：由新到舊 `git revert` 本分支的全部 commit；`data/realtime/` 與 `data/processed/realtime.db`
  不在版控，直接刪除即可，不影響 `power.db` 與網頁服務。
````

- [ ] **Step 6: Commit**

```bash
git add docs/SERVING.md docs/lineage log.md
git commit -m "docs: document the realtime collector and record CP-073" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: 合併前檢查**

使用 `pre-merge-check` skill 檢查 `feat/realtime-collector`。預期沒有 BLOCK；`configs/` 有變更而 `docs/SERVING.md` 也有更新，文件同步那條應為 PASS。
