# 即時發電面板（RT-3a）設計規格

> 狀態：設計已確認，待實作
> 日期：2026-10-05
> 前置：RT-1 即時收集器（`docs/superpowers/specs/2026-09-24-realtime-ingest-design.md`，PR #18）
> 範圍：RT-3 的第一段——網頁上的即時發電面板。自然語言查詢即時資料是 RT-3b，另寫規格。

## 1. 目標

RT-1 已經每 10 分鐘把台電 `d006001` 的機組發電量收進 `data/processed/realtime.db`，但使用者在網頁上
看不到。RT-3a 在「總覽」頁加一個即時發電區塊，讓人一眼看到三件事：

1. 收集器有沒有在跑、最新資料是幾點的、落後多久。
2. 最新時段各發電類型的總出力。
3. 今天每 10 分鐘各類型出力的趨勢。

權限與查詢端一致：電廠帳號只看到自己電廠的機組加上 `shared` 的列；公開服務的訪客只看到狀態。

## 2. 範圍

**做：**

- 後端模組 `src/serving/realtime_panel.py`：唯讀讀取 `realtime.db`，用固定 SQL 算出面板資料。
- 權限純函式 `src/text2sql/realtime_scope.py`：身分範圍 → 看得到的機組鍵。RT-3b 直接重用。
- `GET /api/health` 加 `realtime` 欄位（只有狀態，不含發電數字）。
- 新增 `GET /api/realtime/overview`（套用權限）。
- 前端「總覽」頁的即時發電區塊：狀態列、各類型總出力表、今日趨勢圖。
- 對外文件：`docs/SERVING.md`、README、ATTRIBUTION、`docs/SYSTEM_CARD.md`。

**不做（RT-3b 或之後）：**

- 自然語言查詢即時資料：第二個執行器、守門、router、語料、評測（RT-1 規格 §8.5 的 15 項）。
- 機組明細表（逐機組列表、篩選、排序）。
- 從網頁啟動或停止收集器。收集器與網頁服務互不相干（RT-1 規格 §4.6），網頁只顯示狀態。
- `configs/coverage.yaml`：它描述的是「查詢」答得出什麼。RT-3a 之後查詢端仍然查不到即時資料，
  所以兩條限制（比日更細的時間粒度、火力與核能的發電量）維持不變，等 RT-3b 再改。

**不變的東西（驗收要確認）：** Text2SQL 的查詢路徑、`SqlGuard`、`SemanticGuard`、`ScopeGuard`、
`ALLOWED_COLUMNS`、`VIEW_TIME_SPANS`、語料學習與評測一行都不改，現有測試照舊通過。

## 3. 架構

```
瀏覽器（總覽頁）
  ├─ GET /api/health ───────────────▶ realtime_panel.status() ──▶ ingest.realtime.status.read_status()
  │     （任何人；只有狀態）
  └─ GET /api/realtime/overview ───▶ realtime_scope.scope_for(principal)      ← 與 /api/query 同一套身分判斷
        （套權限）                    realtime_panel.overview(scope, now)
                                        ├─ 唯讀開 realtime.db（每次請求開、用完即關）
                                        ├─ 讀 dim_rt_unit → realtime_scope.visible_unit_keys()
                                        ├─ 電廠帳號：比對 dim_rt_plant ↔ power.db dim_plant_scope
                                        └─ 固定 SQL：v_rt_now、v_rt_10min（今天）
```

- 面板**不經過 Text2SQL**。它跑的是寫死的 SQL，沒有任何使用者輸入進入 SQL。
- 查詢端「只有一個資料庫」的假設維持不變，留給 RT-3b。
- 服務建立時只記下路徑（`configs/config.yaml` 的 `paths.realtime_database` 與 RT-1 設定），
  **不在建置時量任何數字**。每次請求重新開檔，資料一直是新的，收集器更新後不必重啟服務。
- 每次請求開檔、讀完立即關閉，不常駐持有 `realtime.db`。這也讓 RT-1 規格 §12.1 的已知限制
  （Windows 上服務開著檔案時，收集器無法把損壞的資料庫移到旁邊）只剩請求進行中的極短時間。

## 4. 後端

### 4.1 `src/serving/realtime_panel.py`

```python
class RealtimePanel:
    def __init__(self, config: RealtimeConfig, power_database: Path) -> None: ...
    def status(self, *, now: datetime | None = None) -> dict[str, object]: ...
    def overview(
        self, scope: RealtimeScope, *, now: datetime | None = None
    ) -> dict[str, object]: ...
```

- `now` 可注入，測試固定「現在」；預設是 `datetime.now(UTC)`。
- 開檔方式與 `text2sql.db.ReadOnlySQLite` 相同：URI `mode=ro`，加 `PRAGMA query_only = ON`。
- 「今天」依台灣時間（RT-1 的 `ingest.realtime.timeutil.TAIPEI`，固定 UTC+8）：取 `now` 在台灣的日期，
  序列從當天 00:00 到最新一筆資料時間為止。
- 加總只算 `數值狀態 = '正常'` 的值；其他狀態（例如「通訊異常」）的機組另計 `unreliable_units`。
- 各類型總出力包含「個別」與「彙總」兩種粒度。電廠帳號不含「未定」（見 §5）。
- 裝置容量只加有標容量的機組，沒有容量的不當成 0。

### 4.2 `GET /api/health` 的 `realtime` 欄位

現有回應加一個欄位，**任何人都看得到，不含發電數字**：

```json
"realtime": {"available": true, "state": "healthy", "collector_running": true,
             "latest_data_time": "2026-10-05 15:20", "lag_minutes": 7.4}
```

- `state`：`healthy`、`stale`（落後超過 `configs/realtime.yaml` 的 `status.stale_after_minutes`，預設 30）、
  `stopped`（收集器沒在跑）、`unavailable`（還沒有 `realtime.db`，或讀不到）。
- 取值直接來自 RT-1 的 `read_status()`（RT-1 規格 §8.3）。
- `read_status()` 出任何錯都只回 `{"available": false, "state": "unavailable"}`，並寫進服務 log。
  **`/api/health` 本身絕不因為即時資料而出錯**，其他欄位與狀態碼照舊。

### 4.3 `GET /api/realtime/overview`

```json
{"success": true,
 "data": {
   "available": true,
   "data_time": "2026-10-05 15:20", "state": "healthy", "lag_minutes": 7.4, "quality": "ok",
   "scope": "all",
   "by_type": [{"type": "燃氣", "net_mw": 13703.4, "capacity_mw": 15210.0,
                "units": 30, "unreliable_units": 0}, ...],
   "today": {"date": "2026-10-05",
             "slots": ["00:00", "00:10", ...],
             "series": [{"type": "燃氣", "net_mw": [12001.2, null, ...]}, ...]},
   "disclosures": [{"code": "RT_STALE", "reason": "資料落後 42 分鐘"}]}}
```

- `by_type` 依 `net_mw` 由大到小排序。
- **儲能與儲能負載各自一列，不合併**（放電與充電會相抵，RT-1 規格 §8.5 第 5 點）。
- `today.series` 的每個值對應 `slots` 的同一位置。**缺值是 `null`，不是 0**：那個時段沒抓到資料，
  或該類型那個時段沒有任何正常值。
- `scope`：`all`，或電廠帳號的 `plant:<電廠名稱>`。
- 電廠帳號的 `by_type` 改成拆開的兩個數字（§5.3），`today.series` 只含看得到的機組。
- 還沒有 `realtime.db`：回 200 與 `{"available": false, "state": "unavailable"}`，前端顯示「尚無即時資料」。

### 4.4 揭露代碼

格式沿用查詢結果的 `{code, reason}`，前端用同一種提示框顯示。

| 代碼 | 條件 |
|---|---|
| `RT_STALE` | `state = stale`：落後超過門檻，附落後分鐘數 |
| `RT_COLLECTOR_STOPPED` | `state = stopped`：數字是收集器停止前的最後一筆 |
| `RT_QUALITY_WARN` | 最新快照 `quality = warn`，附警告內容（RT-1 規格 §6） |
| `RT_SCOPE_PLANT` | 電廠帳號：只含本廠機組與 `shared` 的列 |
| `RT_NO_DATA_TODAY` | 最新資料不是今天的：今日趨勢是空的 |

### 4.5 狀態碼

| 情況 | `/api/realtime/overview` |
|---|---|
| 正常 | 200 |
| 還沒有 `realtime.db` | 200，`available: false` |
| 訪客且 `POWERQUERY_ANONYMOUS_QUERY_SCOPE=denied` | 401「即時發電數字需要先登入。」（與 `/api/query` 相同） |
| 帶了 cookie 但已過期或撤銷 | 401（與 `/api/query` 相同，不降級成訪客） |
| 電廠帳號，電廠編號比對不符 | 409，代碼 `RT_SCOPE_MISMATCH` |
| 讀取 `realtime.db` 發生 SQLite 錯誤 | 503「即時資料暫時讀不到。」，寫進服務 log |

## 5. 權限

### 5.1 `src/text2sql/realtime_scope.py`

```python
@dataclass(frozen=True)
class RealtimeScope:
    kind: Literal["all", "plant"]
    plant_id: int | None = None
    plant_name: str | None = None


@dataclass(frozen=True)
class UnitRow:
    key: int  # dim_rt_unit.id = 檢視的「機組鍵」
    access_scope: str  # plant / shared / undecided
    plant_id: int | None


def visible_unit_keys(scope: RealtimeScope, units: Iterable[UnitRow]) -> frozenset[int] | None: ...
def own_unit_keys(scope: RealtimeScope, units: Iterable[UnitRow]) -> frozenset[int]: ...
```

- 純函式，不讀資料庫、不看 HTTP。`None` 代表全部看得到。
- 放在查詢端的套件，RT-3b 的守門直接重用，兩條路徑的權限不會分岔。

### 5.2 身分 → 範圍

| 身分 | 範圍 | 看得到的機組 |
|---|---|---|
| 管理員、全電廠帳號 | `all` | 全部，包括未定的機組 |
| 電廠帳號 | `plant` | `access_scope = plant` 且 `plant_id` 相同的機組，加上全部 `shared` 的列；**`undecided` 一律看不到**（RT-1 規格 §8.4） |
| 訪客，`ANONYMOUS_QUERY_SCOPE=all` | `all` | 全部 |
| 訪客，`denied` | — | `overview` 回 401；`/api/health` 的狀態照常 |

「登入身分 → 範圍」從 `/api/query` 現有的邏輯（`_query_principal`、`resolve_account_plant`、
`anonymous_scope`）抽成一個共用的小函式，`/api/query` 與 `/api/realtime/overview` 都呼叫它。
抽出後 `/api/query` 的行為不變，由現有測試保證。

### 5.3 電廠帳號的加總拆開

只給一個總數，大潭的帳號會看到「太陽能 7,731 MW」——那幾乎全是 `shared` 的購電太陽能，
容易誤會成自己電廠的出力。所以電廠帳號的每一類改成：

```json
{"type": "太陽能", "own_net_mw": 0.0, "shared_net_mw": 7731.1, "capacity_mw": ..., "units": ..., "unreliable_units": ...}
```

前端分「本廠」「共用」兩欄，並附 `RT_SCOPE_PLANT`。`all` 範圍只有一個 `net_mw`。

### 5.4 電廠編號比對（RT-1 規格 §8.4）

- 兩邊的電廠編號都來自 `taipower_align/plants.csv`，正常情況一定一致。
- 電廠帳號每次請求時，比對 `realtime.db` 的 `dim_rt_plant` 與 `power.db` 的 `dim_plant_scope`
  （34 列，成本很低）。編號或名稱任何一項對不上就拒絕：409、`RT_SCOPE_MISMATCH`，寫進服務 log。
- 用意：收集器換了電廠名冊、網頁還用舊名冊時，不會把別的電廠的機組算給你。
- `all` 範圍不需要比對，不受影響。

## 6. 前端

### 6.1 位置與內容

「總覽」頁最上方、四張統計卡片之上，新增「即時發電」區塊：

1. **狀態列**：燈號（綠＝正常、黃＝落後、灰＝停止或無資料），與一行文字，例如
   「最新時段 2026-10-05 15:20（台灣時間）· 7 分鐘前 · 今天 92／92 個時段」。
   訪客權限 `denied` 時只有這一列，下方加「登入後可查看即時發電數字」。
2. **各類型總出力表**：類型、淨發電量（MW）、裝置容量（MW）、通訊異常機組數；依出力排序。
   儲能、儲能負載旁標註「放電／充電，不相抵」。電廠帳號多「本廠」「共用」兩欄。
3. **今日趨勢圖**：每個類型一條線，缺值處斷線（不畫成 0）。用 `index.html` 已載入的 Plotly
   （`plotly-basic`），不新增相依套件。圖下附可展開的數字表，給讀螢幕的使用者與要看精確數字的人。

揭露沿用查詢結果現有的提示框樣式。

### 6.2 更新

- 總覽頁開著、且瀏覽器分頁在前景時，每 60 秒更新一次；切到其他頁或分頁在背景就暫停
  （`visibilitychange`）。資料只每 10 分鐘變一次，60 秒已足夠。
- 現有的「重新整理」按鈕同時立刻更新即時區塊。

### 6.3 程式位置

寫在現有的 `src/serving/static/app.js`（約 150–200 行，獨立一組函式），樣式加在 `app.css`，
元素加在 `index.html`。合併門檻只對 `app.js` 跑 `node --check`；另開檔案就要改門檻腳本，
那是整個專案共用的工具，不為此修改。

## 7. 錯誤處理

| 情況 | 行為 |
|---|---|
| 還沒有 `realtime.db` | health：`available: false`；overview：200 + `available: false`；前端「尚無即時資料」 |
| 收集器沒在跑 | 照常顯示最後一筆，附 `RT_COLLECTOR_STOPPED`，燈號灰 |
| 資料落後超過門檻 | 照常顯示，附 `RT_STALE`，燈號黃 |
| 最新資料不是今天 | 總出力照常（附資料時間），今日趨勢空，附 `RT_NO_DATA_TODAY` |
| 讀取時 SQLite 出錯（例如收集器正在重建） | health：`available: false`；overview：503；前端「即時資料暫時讀不到」，下次更新再試 |
| 電廠編號比對不符 | overview 409 `RT_SCOPE_MISMATCH`；前端「電廠對照不一致，請聯絡管理員」 |
| 讀取 RT-1 設定失敗（`configs/realtime.yaml` 錯） | 視同無資料：health `available: false`、overview 200 `available: false`，寫進服務 log；服務照常啟動 |

## 8. 測試

不連網；資料庫建在 `tmp_path`；時間一律注入。

- **`tests/test_realtime_scope.py`**：權限矩陣逐格測——`all` 看全部（含未定）；電廠帳號只有本廠＋
  `shared`；`undecided` 絕不出現在電廠帳號；`own_unit_keys` 只含本廠。
- **`tests/test_realtime_panel.py`**：用 RT-1 的測試固定資料（`tests/realtime_support.py`、
  `tests/fixtures/realtime/`），以固定封存＋固定「現在」建出固定的 `realtime.db`，驗證：
  - 加總只算「正常」的值，通訊異常另計；
  - 缺值是 `null`；
  - 儲能與儲能負載分開；
  - 本廠與共用拆分正確；
  - 每種狀態產生對應的揭露代碼（§4.4）；
  - 沒有 `realtime.db` 時 `available: false`；
  - 「今天」依台灣時間（例如 UTC 16:30 已是台灣隔天）。
- **`tests/test_realtime_api.py`**（FastAPI TestClient）：
  - `/api/health` 有 `realtime` 欄位，且**不含任何發電數字**；即時資料壞掉時 health 仍回 200；
  - overview 的 200、401（訪客 denied、cookie 逾期）、409（比對不符）、503（SQLite 錯誤）；
  - 電廠帳號拿不到別廠或未定的機組；
  - `/api/query` 抽出共用身分函式後，行為不變（現有測試）。
- **前端**：`node --check` 照舊；加一個測試確認 `index.html` 有即時區塊需要的元素 id。
- **不變式**：現有測試全部照舊通過，證明查詢路徑沒有被動到。

## 9. 對外文件

使用者在網頁上看得到即時數字了，所以這一段就要改說法：

- **`docs/SERVING.md`**：新增「即時發電面板」一節——看得到什麼、權限、燈號意義、資料多久更新、
  收集器沒在跑時會怎樣。
- **README 第 244 行、ATTRIBUTION 第 27 行**：「快照，不是即時資料服務」改成兩句：分析用的
  `power.db` 仍是固定時間的快照；總覽頁的即時發電區塊每 10 分鐘取自台電 `d006001`，可能落後或中斷，
  頁面會標出資料時間。兩者都不能作為供電或調度決策依據。
- **`docs/SYSTEM_CARD.md`**：新增即時資料一節——即時資料**不經過四眼審核**，每一筆快照由自動驗證
  放行；規則（解析程式、人工決定檔、設定）的變更才走 PR 審查（RT-1 規格 §3.1 第 4 點）。
  也寫明即時資料目前只在面板上顯示，查詢還查不到（RT-3b）。
- **`log.md`**：儲存點 CP-074。

## 10. 檔案

| 檔案 | 內容 |
|---|---|
| `src/text2sql/realtime_scope.py` | 新增：權限純函式（§5.1） |
| `src/serving/realtime_panel.py` | 新增：面板資料（§4.1） |
| `src/serving/app.py` | health 的 `realtime` 欄位、`/api/realtime/overview`、抽出共用的身分 → 範圍函式 |
| `src/serving/static/index.html`、`app.js`、`app.css` | 即時發電區塊（§6） |
| `tests/test_realtime_scope.py`、`test_realtime_panel.py`、`test_realtime_api.py` | 測試（§8） |
| `docs/SERVING.md`、`README.md`、`ATTRIBUTION.md`、`docs/SYSTEM_CARD.md`、`log.md` | 文件（§9） |

不新增相依套件（只用標準函式庫、FastAPI、前端已載入的 Plotly）。時區一律固定 UTC+8，不用 `zoneinfo`
（RT-1 規格：部署機的 Windows Python 沒有 IANA 時區資料庫）。

## 11. 驗收

1. `ruff format --check`、`ruff check`、`pytest` 全部通過；現有測試數不減。
2. 合併門檻（pre-merge-check）PASS。
3. 本機實際開服務驗證，三種身分各看一次：管理員看到全部；電廠帳號只看到本廠＋共用、且拆兩欄；
   `ANONYMOUS_QUERY_SCOPE=denied` 的訪客只看到狀態列。
4. 收集器在跑時燈號綠；停掉後燈號變灰並出現 `RT_COLLECTOR_STOPPED`；不重啟服務，資料隨收集器更新。
5. `git diff` 確認 Text2SQL 查詢路徑、守門、`ALLOWED_COLUMNS`、`coverage.yaml`、語料、評測都沒有改。

## 12. 留給 RT-3b

- 自然語言查詢即時資料：RT-1 規格 §8.5 的 15 項（第二個執行器與分派、跨庫拒絕、守門、
  今天／昨天／現在的解析、router 與語料、評測固定「現在」、語料學習排除即時 SQL、`coverage.yaml`）。
- 重用 `realtime_scope.visible_unit_keys()` 做查詢端的即時權限。
- 機組明細表（如果 RT-3b 之後仍有需要）。
