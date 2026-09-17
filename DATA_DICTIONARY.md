# PowerQuery TW 資料字典

## 設計原則

SQLite 物理層使用英文 `snake_case`，Text2SQL 只允許查詢中文 `v_*` 語意檢視。日期在入庫時從 `YYYYMMDD` 轉為 ISO `YYYY-MM-DD`；設備主檔容量保留「瓩」，每日尖峰資料保留「萬瓩」。

`python -m ingest.fetch` 會把最新檔放在 `data/raw/`，同時以 SHA-256 為檔名放入 `data/archive/<dataset>/`。相同內容不會產生重複快照，並在 `data/raw/manifest.json` 記錄來源與授權。

## 物理表

| 表 | 主鍵 | 粒度 | 來源／限制 |
|---|---|---|---|
| `dim_plant` | `id` | 一列一電廠 | `units.csv`；電廠主要燃料由所屬機組汇總 |
| `dim_unit` | `id` | 一列一機組 | `capacity_kw` 單位為瓩；不含核能、IPP 與風光彙總主檔。商轉日期原檔同時有日與月精度，月精度以當月 1 日作排序值，並保留原值與精度欄位 |
| `dim_date` | `date` | 一列一日 | ISO 日期，由 `daily.csv` 取得 |
| `dim_b_column` | `id` | 一列一個原始機組／彙總欄 | 64 個欄位全數保留，包含 21 個沒有機組主檔的欄位 |
| `bridge_b_column` | `id` | 一列一個可對應 B 欄位 | 43 列；容量比值是歷史實測最大值除以主檔容量 |
| `bridge_b_column_unit` | 複合鍵 | B 欄位與機組的多對多關係 | 解決多機彙總與離島／殘差桶的多機組對應 |
| `fact_daily_peak` | `date, b_column_id` | 一日一欄位 | `peak_wankw` 是系統尖峰時刻的瞬時出力，不是發電量 |
| `fact_daily_system` | `date` | 一日一列 | 系統尖峰與用電指標；功率單位萬瓩，用電單位百萬度 |
| `fact_generation_cost` | `id` | 一年一發電方式 | `generation_cost.csv`；72 列、涵蓋 2023～2025 年。`cost_per_kwh` 單位為元／度，`accounting_basis` 區分審定決算與自編決算 |
| `dim_outage` | `id` | 一列一歲修事件 | `d006008` 對齊；`unit_id` 允許為 NULL 以保留未對齊事件。官方快照有 1 列結束日早於開始日，原值保留並以 `date_status=invalid_range` 標記 |
| `meta_pitfall` | `id` | 一列一對象的陷阱 | 由 crosswalk 的殘差、容量比與電廠重疊關係產生，不另外手寫對象清單 |
| `meta_manifest` | 固定 `id=1` | 一個資料庫版本 | 資料期間、來源 SHA-256、列數、schema 版本與內容 checksum |

## Text2SQL 語意檢視

| View | 主要欄位 | 查詢規則 |
|---|---|---|
| `v_unit` | `機組名`、`電廠`、`縣市`、`裝置容量_瓩`、`裝置容量_萬瓩`、`燃料`、`商轉日期`、`商轉日期精度` | 查設備屬性；跨單位運算前必須轉換。`商轉日期精度` 為 `month` 時，日期是當月 1 日的排序值 |
| `v_peak` | `日期`、`機組欄位`、`尖峰出力_萬瓩`、`電廠`、`粒度`、`類別`、殘差／彙總旗標 | 尖峰出力是瞬時功率；禁止跨日 `SUM` 解釋為發電量 |
| `v_system` | `日期`、`尖峰負載_萬瓩`、`備轉容量_萬瓩`、`備轉容量率_pct`與用電指標 | 可做期間極值、日趨勢與同日比較 |
| `v_outage` | `機組名`、`電廠`、起訖日期、`原因`、`對齊狀態` | 未對齊列必須保留原始機組名，不得猜測主檔對應 |
| `v_generation_cost` | `年度`、`電力來源`、`發電方式`、`成本_元每度`、`決算類型` | 年度、分發電方式粒度；不可拆到電廠或機組，不同決算類型不可混用 |

## Schema 決策

原始規格同時要求 `bridge_b_column` 保留 43 個已對應欄位，且 `fact_daily_peak` 保留 64 欄 × 577 天的 36,928 列。因此增加 `dim_b_column` 容納全部 64 欄，`bridge_b_column` 僅存 43 個有主檔對應的關係；這能同時滿足列數驗收與 B-only 資料不丟失。
