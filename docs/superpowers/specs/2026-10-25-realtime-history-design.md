# 用 d006010 回補歷史發電量（RT-2）設計規格

> 狀態：已實作（feat/rt2-history-backfill，CP-082）
> 日期：2026-10-25
> 前置：RT-1 收集器（`2026-09-24-realtime-ingest-design.md`）、RT-3b 即時問句（`2026-10-25-realtime-query-design.md`）
> 範圍：RT-1 §13 留給 RT-2 的問題：`d006010` 的實際內容、機組對照、同一天兩邊都有值時的取捨、回補與重建。

## 1. 實測事實（2026-10-25 下載一次，經使用者同意）

| # | 事實 | 證據 | 對應的設計 |
|---|---|---|---|
| H1 | **晚約四個月** | 檔案 Last-Modified 2026-09-30，內容是 2026-03-01～05-31（92 天） | 回補的是「收集器啟動以前」，與即時收集**不重疊**；兩者之間的空檔補不回來（§6） |
| H2 | 格式 | `{"records":{"CATALOG":"UNIT_NET_P","START_DATE",…,"INTERVAL":"10MIN","UNIT_OF_MEASUREMENT":"MW","NET_P":[{FUEL_TYPE,UNIT_NAME,DATETIME,NET_P}…]}}`，開頭有 UTF-8 BOM，196,169,535 bytes | 串流解析，不整份載入（§3） |
| H3 | 規模 | 2,048,976 筆、153 條序列，每條 13,392 筆 | 每條序列每個時段一個 float，約 20 MB |
| H4 | **整天重複** | 2026-04-01 每條序列都出現兩次（共 22,032 筆），值完全相同 | 相同的重複計數後略過；值不同算「衝突」，以後到的為準並記錄 |
| H5 | 值 | 沒有 `N/A`；8 條序列有負值（廠用電，例如 `松林#1+2`、`彰工`），照存 | 與即時相同：負值照收 |
| H6 | **只有台電自有與部分機組** | 153 條序列 vs 即時 204 條；沒有民營電廠（IPP）、購電太陽能、購電風力等彙總列 | 類型或電廠合計會遠小於即時的同類合計：`RT_HISTORY_PARTIAL_SCOPE`（§5） |
| H7 | 命名與即時大多相同 | 依即時人工決定檔：完全相同 78；`燃油`／`輕油`→`燃料油`、`抽蓄發電`→`儲能`、`+`→`&`、即時多 `(註N)`，共 31 | 人工對照檔（§4） |
| H8 | 抽蓄只有放電 | `抽蓄發電` 各機組最小值 0.0，沒有負值 | 對到即時的「儲能」（放電），不是「儲能負載」 |
| H9 | 即時沒有的 44 條 | 已除役（`核三#1/#2`、`通霄CC#4/5`、`核一Gas1/2`）、台電自建太陽光電與風力場站 | 用自己的名字建成機組；歸屬寫進 `realtime_units.csv` |

## 2. 原則（沿用 RT-1）

1. **只有收集器的程式會寫入 realtime.db**：匯入要拿單一實例鎖，收集器在跑時拒絕（結束碼 3）。
2. **封存是唯一的真實來源**：原檔 gzip 封存在 `data/realtime/history/archive/d006010_<sha256 前 16 碼>.json.gz`；
   `rebuild` 重放所有封存，收集器啟動時補上封存了、還沒匯入的檔案。
3. **結構有問題整份不收**（不留封存）；數值有問題逐筆計數後略過。
4. **對照是人工決定**，執行期間不做字串比對推測；對照檔沒列到的序列照收，歸屬「未定」，電廠帳號看不到。

## 3. 解析

| 拒收代碼 | 條件 |
|---|---|
| `HIST_PAYLOAD_INVALID` | 開頭不是 `{"records":{…,"NET_P":[` |
| `HIST_UNEXPECTED_FORMAT` | `CATALOG` 不是 `UNIT_NET_P`、`INTERVAL` 不是 `10MIN`、單位不是 `MW`、期間不合理 |
| `HIST_RECORD_UNPARSEABLE` | 某一筆不是 `{FUEL_TYPE,UNIT_NAME,DATETIME,NET_P}` 四個字串（例如多了欄位），或結尾不是 `]}}` |
| `HIST_NO_RECORDS` | 一筆都沒有 |
| `HIST_TOO_LARGE` | 下載超過 `history.max_bytes`（500 MB） |

逐筆計數、不入庫：時間不在 10 分鐘整點或不在檔案期間內（`out_of_range`）、值無法解析（`invalid_values`）。

## 4. 對照：`taipower_align/realtime_history_units.csv`

`history_type,history_name,unit_type,unit_name,relation,basis`，153 列。`relation`：`same_unit`（與即時是同一部機組，
序列接起來）或 `history_only`（即時沒有）。由一次性的機械規則產生候選（§1 H7），`basis` 寫明依據，經 PR 審查後提交。

- 兩條序列對到同一部機組時整份對照無效（每日彙總會互相覆蓋而且看不出來）。
- `風力,中港` 可能就是即時的 `台中港`，名稱不是機械規則能對上的，標 `history_only` 並註明待人工確認。
- `history_only` 機組的歸屬寫進 `realtime_units.csv`：已除役的火力與核能依名稱前綴歸電廠；台電自建太陽光電與風力場站
  比照 `v_re_generation` 不受管，標 `shared`。

## 5. 儲存與查詢

- **schema 2**（`store.SCHEMA_VERSION`）：新增 `fact_rt_unit_daily_history`、`fact_rt_history_day`、`meta_rt_history_import`。
  收集器啟動時發現 schema 1 會自動從封存重建（RT-1 §4.2）。
- **只存每日彙總**：10 分鐘明細保留期只有 14 天，歷史本來就超過。算法與即時相同：估算發電量＝Σ 值 ÷ 6。
- **與即時分開存**：即時的每日彙總每次重算會先刪整天；放同一張表會把歷史一起刪掉。
- **`v_rt_daily` 取一份**：同一天同一部機組兩邊都有時，取樣點數多的為準，一樣多取即時（有明細與備註）。
  欄位不變（RT-1 §8.2 的契約），「資料來源」多一個值「台電歷史資料」。
- **新的匯入覆蓋重疊的日子**：台電每月重發，後到的是修正版。
- **RT-3b 的變化**：
  - 「YYYY年M月」「YYYY年M月D日到M月D日」的發電量問句 → `rt_period_energy`（`v_rt_daily`）。共用的
    `extract_entities` 不認得日期區間（會靜默變成第一天），即時路徑自己解析。
  - 揭露：`RT_HISTORY_SOURCE`（數字來自 d006010）、`RT_HISTORY_PARTIAL_SCOPE`（類型合計不是全國）、
    `RT_PERIOD_GAPS`（期間內有幾天沒資料）；空檔整段沒資料 → `RT_NO_DATA_FOR_DAY`。
  - 「現在」只算最新時段裡真的出現的機組與類型：`核三#1` 進了機組表，但「現在核三#1」仍回 `RT_NOT_IN_REALTIME`。
  - 問過去的日子或期間時，不揭露「收集器停止／落後」（那只跟現在有關）。

## 6. 操作

```powershell
uv run python -m ingest.realtime stop                          # 收集器在跑時先停
uv run python -m ingest.realtime history import <d006010.json>  # 匯入一份已下載的檔案
uv run python -m ingest.realtime history download               # 或：下載最新一份（約 200 MB）再匯入
```

台電每月更新一次，每次涵蓋約三個月、晚約四個月：每月匯入一次就能把歷史往後延；但「四個月前～收集器啟動」
之間如果沒有匯入，那段就永遠空著（官方檔案滾動，舊的不再提供）。

## 7. 不做

- 用 d006010 補即時明細（10 分鐘）：超過保留期，存了也會被清掉。
- ~~自動排程每月下載~~：已做（CP-086）。收集器每日維護時 HEAD 一次，版本標記變了才下載匯入；`history.check_hours`（預設設定檔為 24，程式預設 0）。
- 與 `v_peak` 的機組名稱對照（RT-1 §1.3）。
