# 用中文查即時資料（RT-3b）設計規格

> 狀態：已實作（feat/rt3b-realtime-query，CP-081）
> 日期：2026-10-25
> 前置：RT-1 收集器（`2026-09-24-realtime-ingest-design.md`）、RT-3a 面板（`2026-10-05-realtime-panel-design.md`）
> 範圍：RT-3 的第二段——查詢中心用中文問 `realtime.db`。RT-1 規格 §8.5 的 15 項逐項對應見 §9。

## 1. 目標

RT-1 每 10 分鐘把台電 `d006001` 收進 `realtime.db`，RT-3a 把它畫在總覽頁，但查詢中心還是答不出
「現在風力發多少」「台中#1 今天發了幾度電」。RT-3b 讓這類問題走得通，並維持三件事：

1. **數字旁一定有資料時間與收集狀態**，舊資料不能說成現在。
2. **權限與面板一致**：電廠帳號只看到自己電廠的機組加上 `shared` 的列，`undecided` 一律看不到。
3. **兩個資料庫不混用**：同一句 SQL 不能同時查 `v_rt_*` 與 `power.db` 的檢視。

## 2. 已確認的決策

| 項目 | 決定 |
|---|---|
| 生成方式 | 離線規則先接；規則接不住、且該 runtime 有 LLM 時，交給 LLM 針對 `v_rt_*` 產生 SQL |
| 本機 LLM | 新增 `ollama` provider（OpenAI 相容的 chat completions，`http://127.0.0.1:11434/v1`），不需要 API key |
| 分流點 | 在既有管線**之前**：問句判定為即時，就整句交給即時路徑，不經 `SemanticGuard`／`v_peak` 的 router |
| 判定依據 | 問句用詞（現在、目前、今天、昨天⋯），不是機組名稱：兩套命名有約 21 個同名（§8.5 第 14 點） |
| 執行器 | 每次查詢唯讀開 `realtime.db`，與面板相同；不常駐持有，收集器重建時不會被鎖住 |
| 語料學習 | 即時 SQL 一律不進語料：結果每 10 分鐘就變，重放比對 checksum 必定失敗 |

## 3. 架構

```text
POST /api/query
   │
   ▼
Text2SQLPipeline.query(question, plant)
   │  realtime.handles(question)？
   ├── 是 ─▶ RealtimeQueryEngine.query()
   │          1. 讀 read_status()（資料時間、狀態、品質）
   │          2. 解析「現在／今天／昨天／日期」→ 時段（評測時固定「現在」）
   │          3. 規則路由 → SQL；接不住且有 LLM → LLM 產生 SQL（最多重試 max_attempts）
   │          4. RealtimeSqlGuard：只准 v_rt_*；混用 power.db 檢視 → RT_CROSS_DATABASE
   │          5. 權限改寫：每個 v_rt_* 包成「機組鍵 IN（可見鍵）」的子查詢
   │          6. 唯讀執行 realtime.db
   │          7. 揭露（§6）
   └── 否 ─▶ 既有管線（完全不變）
```

新檔案：

| 檔案 | 內容 |
|---|---|
| `src/text2sql/realtime_query.py` | 判定、時間解析、規則路由、守門、權限改寫、揭露、`RealtimeQueryEngine` |
| `src/text2sql/realtime_prompt.py` | 即時路徑的 LLM prompt（檢視、欄位、封閉集合的值、規則） |
| `benchmarks/realtime_questions.json` | 即時評測題（固定封存＋固定現在） |

## 4. 判定與時間解析

### 4.1 什麼算即時問句

符合任一條就走即時路徑：

- 有即時時間詞：`現在`、`目前`、`即時`、`此刻`、`剛剛`、`最新`、`今天`、`今日`、`昨天`、`昨日`、`前天`、
  `過去N小時`、`最近N小時`、`這一小時`。
- 有「發電量／度／MWh」，而且日期落在即時資料的期間內（`fact_rt_day` 最早一天到今天）。

`目前` 有歧義：「目前資料涵蓋哪些期間」問的是資料範圍。問句同時出現 `資料`、`涵蓋`、`期間`、`範圍`
時不算即時。

### 4.2 時間

- 「現在」＝`read_status()` 的最新時段，不是牆上時鐘；答案附上它。
- 「今天」＝`now` 所在的台灣日期（UTC+8，沿用 `timeutil`），範圍 00:00～最新時段。
- 「昨天」「前天」「YYYY年M月D日」＝整天，讀 `v_rt_daily`；當天還沒彙總（還沒過 00:15）就退回
  10 分鐘明細計算。
- `now` 由建構子注入（`clock`）；評測與測試固定它。

## 5. 規則路由

| 意圖 | 例句 | 檢視 |
|---|---|---|
| `rt_now_unit` | 現在台中#1出力多少 | `v_rt_10min`（最新時段） |
| `rt_now_type` | 目前風力發多少電 | 同上，依類型加總 |
| `rt_now_by_type` | 現在各類型發電量 | 同上，依類型分組 |
| `rt_now_top` | 現在出力最高的前 5 部機組 | 同上，`粒度 = 個別` 排序 |
| `rt_now_abnormal` | 現在哪些機組通訊異常／在歲修／環保停機 | 同上，數值狀態或備註 |
| `rt_now_plant` | 現在大潭發電廠出力多少 | 同上，依電廠加總 |
| `rt_today_energy` | 今天風力發了多少度電 | `v_rt_10min`（今天 00:00 起） |
| `rt_today_peak` | 今天台中#1最高出力 | 同上 |
| `rt_day_energy` | 昨天太陽能發電量 | `v_rt_daily`；未彙總時退回明細 |

機組名稱、類型、電廠名每次查詢從 `dim_rt_unit`／`dim_rt_plant` 讀，取問句裡最長的那個；同一個名稱出現在
兩個類型（例如儲能與儲能負載）時，兩列都列出。即時問句卻找不到目標時，回 `clarify` 並給兩個例句。

## 6. 揭露

| 代碼 | 條件 |
|---|---|
| `RT_DATA_TIME` | 一律附：「資料時間 YYYY-MM-DD HH:MM（台灣時間）」 |
| `RT_STALE`／`RT_COLLECTOR_STOPPED` | 落後超過 30 分鐘／收集器沒在跑 |
| `RT_QUALITY_WARN` | 用到的快照有 `quality = 'warn'`；電廠帳號只給代碼 |
| `RT_BUCKET_ROWS` | 結果含 `粒度 = 彙總／未定`，且問句在排名或列表 |
| `RT_ENERGY_ESTIMATE` | 發電量是 10 分鐘瞬間值 ÷ 6 的估算，不是電表讀數 |
| `RT_INCOMPLETE` | 資料完整度 < 0.9：附取樣點數與應有點數 |
| `RT_STORAGE_NETTING` | 加總同時含儲能與儲能負載 |
| `RT_BEFORE_FIRST_DAY` | 問到即時資料第一天之前；附資料從哪天開始 |
| `RT_TODAY_PARTIAL` | 今天還沒過完：只算到最新時段 |
| `RT_SCOPE_PLANT` | 電廠帳號：只含本廠與 `shared`；`undecided` 不列入 |

## 7. 守門與權限

- `RealtimeSqlGuard`：沿用 `SqlGuard` 全部規則（只准 SELECT、單一敘述、參數化、LIMIT、函式白名單），
  允許的檢視換成 `v_rt_now`、`v_rt_10min`、`v_rt_daily` 及其欄位。
- 混用：SQL 同時出現 `v_rt_*` 與任何其他表 → `RT_CROSS_DATABASE`（refuse）。既有管線的 `SqlGuard`
  本來就不認得 `v_rt_*`，那一側自然拒絕。
- 權限改寫：每個 `v_rt_*` 參照換成 `(SELECT * FROM v_rt_x WHERE "機組鍵" IN (?,…)) AS 別名`；看得到的
  鍵由 `realtime_scope.visible_unit_keys()` 算出，與面板同一份。可見鍵為空時是 `1 = 0`。
- 電廠帳號：比對 `dim_rt_plant` 與 `power.db` 的 `dim_plant_scope`，不一致就 `RT_SCOPE_MISMATCH`（refuse）。
- `realtime.db` 不存在或讀不出來：`RT_UNAVAILABLE`（error），訊息指向 `即時收集啟動.bat`。

## 8. 本機 LLM（Ollama）

- `configs/llm.yaml` 新增 `ollama`：`api: chat_completions`、`base_url: http://127.0.0.1:11434/v1`、
  `requires_api_key: false`、`default_model: qwen3:8b`、`structured_output: false`、`temperature: 0`。
- 選擇 provider：`configs/llm.yaml` 的 `provider`，或環境變數 `POWERQUERY_LLM_PROVIDER`（優先）。
  預設仍是原本的 `gmi`：本機沒裝 Ollama 時，`auto` 模式的行為與現在一模一樣。
- 不需要 key 的 provider 在 `auto` 模式下視為已設定，所以 runtime 會是 `online`；規則接得住的題目照樣不打
  LLM。Ollama 沒在跑時，LLM 那一步失敗成 `LLM_UNAVAILABLE`，離線規則與澄清照常運作。
- 公開服務強制離線（`PUBLIC_OFFLINE_SERVING.md`），不受影響。

## 9. RT-1 §8.5 逐項

| # | 要求 | 做法 |
|---|---|---|
| 1 | 第二個執行器，禁止跨庫 | §3、§7 |
| 2 | 附資料時間；落後與停止要揭露 | `RT_DATA_TIME`、`RT_STALE`、`RT_COLLECTOR_STOPPED` |
| 3 | 彙總／未定列 | `RT_BUCKET_ROWS` |
| 4 | 估算與完整度 | `RT_ENERGY_ESTIMATE`、`RT_INCOMPLETE` |
| 5 | 儲能相抵 | `RT_STORAGE_NETTING` |
| 6 | 品質警告 | `RT_QUALITY_WARN` |
| 7 | 單位 | 跨庫比較直接拒絕；即時欄位名稱都帶 `_MW`／`_MWh` |
| 8 | 第一天之前 | `RT_BEFORE_FIRST_DAY` |
| 9 | 既有守門會擋即時問句 | 分流在 `SemanticGuard` 之前 |
| 10 | 期間每次現量 | 每次查詢讀 `read_status()` 與 `fact_rt_day` |
| 11 | 今天／昨天／現在；評測固定現在 | §4.2 |
| 12 | 不進語料學習 | `CorpusLearningService.observe` 對 `v_rt_*` 回 `realtime_query` |
| 13 | coverage 與對外文件 | 兩條限制改寫，`absent` 涵蓋 `v_rt_*`；README、ATTRIBUTION、SYSTEM_CARD |
| 14 | 同名機組 | §4.1，依用詞分流 |
| 15 | 不變式擴充 | 測試：兩個資料庫的檢視各自＝守門 allowlist＝授權分類＝coverage |

## 10. 不做

- 即時資料與 `v_peak`／`v_unit` 的機組對照與跨庫比較（沒有人工對照前做出來的 JOIN 只會看起來對）。
- 即時問句的語料檢索範例：即時 prompt 只有檢視、欄位、值與規則。
- 每小時彙總層。
