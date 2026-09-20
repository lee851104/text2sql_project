# API Contract

服務使用 JSON 與結構化 envelope；本機 schema 位於 `/openapi.json`，互動式檢視器位於 `/docs`。檢視器只在自己的 nonce CSP 下以固定版本與 SRI 載入 Swagger UI CDN；主應用 CSP 不會因此放寬。除格式驗證、服務尚未就緒或模式設定錯誤外，Text2SQL 的業務層拒答仍使用 HTTP 200，並由 `success` 與 `error_code` 表達結果。

## Runtime 模式

`RuntimeManager` 支援三種模式：

- `offline`：只使用可重現的規則路由與唯讀 SQLite，不呼叫遠端模型。這不是安裝在本機的生成式模型；未涵蓋的長尾問句會明確拒答。
- `online`：規則路由未涵蓋時呼叫線上模型；必須有記憶體內 API key，或該 provider 對應的環境變數。
- `auto`：有可用 API key 時選用線上 runtime，否則退回離線規則模式。

### 線上 provider

打哪一家由 `configs/llm.yaml` 的 `provider` 決定，每家在 `providers` 底下各有一段設定。目前內建兩家：

| provider | 端點 | 環境變數 | 說明 |
|---|---|---|---|
| `openai` | `/v1/responses` | `OPENAI_API_KEY`／`OPENAI_MODEL` | OpenAI 自家的 Responses API |
| `gmi` | `/v1/chat/completions` | `GMI_API_KEY`／`GMI_MODEL` | GMI Cloud，base_url `https://api.gmi-serving.com/v1` |

`api` 欄位決定打哪個端點：`responses` 是 OpenAI 自家的，`chat_completions` 是業界相容的那個 —— 第三方講「OpenAI 相容」指的幾乎都是後者，所以只換 `base_url` 而不換端點會直接失敗。`structured_output: false` 給不支援 `strict` json_schema 的服務用，關掉後輸出格式改由 prompt 要求與寬容解析負責，安全仍由 `SqlGuard` 把關。`temperature: null` 表示不送該參數（OpenAI 的 reasoning 模型不接受它）。

`provider` 填了 `providers` 裡沒有的名字時，建立 runtime 會直接失敗，不會默默退回預設的那一家。

### `GET /api/runtime/llm`

回傳目前預設模式、實際啟用模式與不含憑證的設定：

```json
{
  "success": true,
  "data": {
    "default_mode": "auto",
    "active_mode": "offline",
    "online_configured": false,
    "provider": "openai",
    "model": "gpt-5.4-mini",
    "source": "offline",
    "offline_capability": true
  }
}
```

`source` 可能是 `offline`、`memory`、`environment` 或測試／嵌入情境使用的 `injected`。回應永遠不包含 API key。

### `PUT /api/runtime/llm`

更新程序內的預設 runtime。`mode` 必填；`api_key` 與 `model` 可省略，以保留既有值；`model: null` 也視為沿用目前模型。

```json
{
  "mode": "online",
  "api_key": "sk-…",
  "model": "gpt-5.4-mini"
}
```

成功時回傳與 `GET /api/runtime/llm` 相同的安全狀態。由此端點提供的 API key 只保留在伺服器程序記憶體中，不寫入磁碟、不記錄於語料，也不會由任何狀態端點回傳；程序重啟後需重新提供。以 `mode: "offline"` 或 `mode: "auto"` 搭配 `"api_key": null` 可清除記憶體內的 key；若仍有 `OPENAI_API_KEY`，狀態會繼續標示線上憑證可用，需從啟動環境移除。網頁提供「清除記憶體金鑰並切離線」控制。

模式或模型無效、缺少線上憑證、或線上額外依賴尚未安裝時回 400。設定採先建置後切換；失敗時保留先前可用的 runtime。

## 查詢

### `POST /api/query`

接受 1～500 字的 `question`；可用 `execution_mode` 只覆寫這一次查詢，不變更 RuntimeManager 的預設模式：

```json
{
  "question": "2026年7月20日出力前五名機組",
  "execution_mode": "offline"
}
```

`execution_mode` 可為 `offline`、`online`、`auto` 或省略。指定 `online` 但尚無線上憑證時回 409。

成功的 `data` 包含 `sql`、`params`、`columns`、`rows`、`record_count`、`disclosures`、`trace`、`chart_spec`、`explanation`、`statistics`，以及：

- `runtime`：本次實際使用的 `mode`、`provider`、`model`。
- `learning`：本次結果的語料觀察狀態；語料寫入失敗不會把已完成的查詢改成失敗。

`trace` 每一步固定有 `stage` 與 `elapsed_ms`，另依階段附 `attempt`、`code`、`record_count`。`retrieve` 階段附 `example_ids` 與 `dropped` —— 後者是被檢索門檻砍掉的範例數（設定見 `configs/retriever.yaml` 的 `min_score`／`relative_score`）。`dropped` 等於 `top_k` 表示這一題沒有任何夠接近的範例，prompt 只剩 schema 與領域規則；那比塞一堆 0 分範例好，但要看得見它發生了。

`chart_spec.kind` 只能是 `line`、`bar`、`scatter` 或 `null`；`data` 與 `layout` 是 Plotly 可接受的受限子集，x/y 數列直接由 SQL rows 投影，不允許 JavaScript formatter 或其他可執行內容。前端再次套用白名單後才交給 Plotly；載入不到 Plotly CDN 時會以相同 x/y 資料退回原生 SVG。

業務層拒答範例：

```json
{
  "success": false,
  "error_code": "PEAK_SUM_ACROSS_DAYS",
  "error": "…",
  "severity": "refuse",
  "suggestions": ["…"],
  "evidence": {}
}
```

## 語料學習與調閱

這裡的「學習」是受治理的檢索語料擴充，不是背景微調或重新訓練生成式模型。系統只觀察透過本服務 Web/API 完成的成功查詢：規則路由結果通過去識別、重複檢查、benchmark 洩漏檢查、SQL／語意護欄、結果重播與檢索回歸門檻後可自動升版；線上模型產生的候選內容會停在 `pending_review`，不會未經人工審查自動發布。

學習資料寫入資料庫旁的隔離工作區 `.powerquery-learning/`，不直接修改版控中的 `corpus/training_corpus.json`。候選紀錄包含問句、參數、SQL、來源、驗證結果與結果 checksum，但不保存查詢結果 rows。所有 read-modify-write 週期同時使用程序內共用鎖與跨程序檔案鎖。

最終序列化邊界會遞迴遮罩 Email、token、台灣身分證、手機／市話、長帳號與明確標示的姓名；若 SQL 或參數需要遮罩，候選會直接拒絕而不發布。相同問題的非發布版本可在 SQL、參數、來源或意圖修正後建立 `revision_of` revision；已發布問題維持冪等。

工作區 manifest 記錄 canonical corpus、資料庫與 active corpus 的版本/checksum。canonical 或資料庫基線改變時會先備份舊 active corpus、重建 canonical 索引，保留候選 ledger，並將舊 `promoted` 候選改回 `pending_review` 要求重新驗證。

### `GET /api/training-status`

回傳實際工作區與索引狀態：

```json
{
  "success": true,
  "is_training_complete": true,
  "training_status": "語料學習工作區已就緒",
  "data": {
    "workspace_ready": true,
    "corpus_version": "…",
    "corpus_checksum": "…",
    "published_examples": 40,
    "index_synchronized": true,
    "candidate_counts": {
      "total": 0,
      "validating": 0,
      "pending_review": 0,
      "promoted": 0,
      "rejected": 0,
      "ignored": 0
    },
    "policy": {
      "auto_promote_source": "router",
      "llm_requires_review": true,
      "maximum_retrieval_drop": 0.0
    },
    "workspace_identity": {
      "schema_version": "corpus-workspace-v1",
      "canonical_corpus_checksum": "…",
      "database_manifest_version": "…",
      "active_corpus_checksum": "…"
    }
  }
}
```

`is_training_complete` 只有在工作區存在且索引與語料同步時為 `true`。

### `GET /api/corpus/entries`

調閱已去識別的候選內容。查詢參數：

- `state`：`all`（預設）、`validating`、`pending_review`、`promoted`、`rejected` 或 `ignored`。
- `limit`：1～500，預設 100。

回應的 `data` 包含 `state`、`entries` 與 `returned`。entries 依新到舊排列，並帶有來源、狀態、時間、驗證與升版資訊；不包含查詢結果 rows。

### `GET /api/corpus/events`

調閱語料工作區的去識別事件紀錄。`limit` 為 1～500，預設 100；回應的 `data` 包含 `events` 與 `returned`。

### `POST /api/corpus/entries/{candidate_id}/review`

只接受 `pending_review` 候選。核准時會以目前資料庫與語料基線重新跑全部檢查，通過才發布；拒絕則保留可稽核紀錄：

```json
{
  "decision": "approve",
  "reviewer": "本機審核人員"
}
```

`decision` 可為 `approve` 或 `reject`，`reviewer` 為 1～80 字。找不到候選回 404，候選已變更或不可審核回 409。這是會改變語料狀態的本機管理端點；服務預設只綁定 loopback。若對外提供，部署端必須另加 TLS、身分驗證與授權。

## 其他端點與錯誤

- `GET /api/health`：服務、runtime 模式與資料期間。
- `GET /api/stats`：尖峰事實列數、機組數、歲修列數與資料期間。
- `GET /api/examples`：已審核的範例問句。

空問題、超過長度、額外 JSON 欄位、不合法 enum、非 JSON，或 `limit` 超出範圍時使用 FastAPI 422；422 的 detail 不反射原始 `input`，避免 malformed API key 或問句出現在錯誤回應。資料庫或語料學習工作區未就緒時使用 503。所有回應附加 CSP、`nosniff`、`DENY` frame 與 no-referrer headers。

### 執行環境錯誤訊息的揭露範圍

`PUT /api/runtime/llm` 失敗時，只有**講設定檔而非內部狀態**的訊息會原樣回傳：缺 API key（訊息帶上該 provider 的環境變數名，例如 `GMI_API_KEY`）、provider 名稱不在 `providers` 裡、以及 online 套件未安裝。其餘一律換成一句通用訊息，避免連線字串、憑證片段或內部例外外流。

放行這兩類是刻意的：它們正是操作者最需要當場看懂的兩件事，遮掉會讓「你沒設 key」和「provider 名字打錯」變成同一句沒有指向性的錯誤。

### 查詢結果的資料出處

`POST /api/query` 成功時，`data.data_provenance` 附上該次查詢用到的來源檔。作用中快照缺少某個 slot 時（版本 id 不含 schema 版本，見 `docs/SPEC.md` 的「已知待修」），該筆出處會**略過而不是捏造**，`data_sources` 可能因此是空陣列。查詢結果本身不受影響 —— 出處是補充說明，不該讓一個已經成功的查詢失敗。
