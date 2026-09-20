# Text2SQL Pipeline Contract

## 入口與輸出

`Text2SQLPipeline.query(question)` 接受一個中文問句，回傳 `PipelineResponse`。成功回應的 `data` 固定含 `question`、`intent`、`sql`、`params`、`columns`、`rows`、`record_count`、`disclosures` 與 `trace`；失敗回應使用結構化 `error_code`、`severity`、`suggestions` 與 `evidence`。

## 管線順序

1. 實體與日期抽取。
2. 問句層語意守門。
3. 規則意圖路由；無可直接執行查詢才進入 RAG。
4. 字元 n-gram TF-IDF 檢索（餘弦相似度），組合 schema、領域規則、近似範例與動態資料期間。切分單位是**字元**不是詞，不依賴任何分詞器；比對前先以 `normalize_question()` 去掉標點與空白並轉小寫。n-gram 範圍由 `configs/retriever.yaml` 的 `character_ngram` 決定（目前 2～4），**索引與檢索讀同一份設定**：`build_index()` 也走 `ngram_range()`，否則設定一改，語料學習服務就會拿不同切法的索引去比對，永遠判定「索引不同步」。切法本身記在 `corpus/index.json` 的 `character_ngram` 欄位，事後才看得出那份索引是用幾個字切的。

檢索結果再經兩道下限（`min_score` 絕對、`relative_score` 相對於第一名）才進 prompt，全被砍掉時 prompt 只剩 schema 與領域規則，`trace.retrieve.dropped` 會說砍了幾個。**這兩個數字不是用來找出相關範例**：實測意圖相符與不符的分數分佈重疊（中位數 0.118 對 0.053），沒有任何單一門檻切得開，門檻只把雜訊比例從 71% 降到 49%。純度 51% 是字元 n-gram 只懂字面的極限 —— 「哪一廠最耗電」與「用電量最高的廠區」餘弦是 0.000，而「林口#1的出力」與「林口#2的出力」是 0.586。要本質改善得換語意向量；收益上限也量過：`eval_questions` 0/60 會走到檢索，`golden_questions` 27/80。

兩道下限的預設值都是 0（不篩），由 serving 層從設定檔給 —— 離線評測的 top-1 意圖準確率與範圍問句建議都靠 top-1 分數自己判斷，不能被門檻改掉。
5. LLM 回傳 `{sql, params}`；最多按 `max_attempts` 重生。
6. 所有 SQL，包含手寫 router SQL，一律經過 AST 安全守門。
7. SQL 層語意守門。
8. 透過注入的 `run_sql` 唯讀執行，失敗才進入下一次重生。

## 答不出來的時候

規則沒接、模型也生不出能過守門的 SQL 時，依序再試三件事，全部落空才回報失敗：

1. **缺參數就反問**（`MISSING_PARAMETER`，`severity=clarify`）。「某天機組尖峰功率排行榜」沒有說是哪一天，任何模型都推不出那個日期，能做的只有挑一天然後回一張看起來完全正常的表。`evidence.missing` 標明缺的是 `date`／`unit`／`units`／`plant`／`metric`，`suggestions` 給一句可以直接照用的問法。
2. **近似問法建議**（`DATA_SCOPE_NEAR_MATCH`）。
3. **線上生成不可用**（`LLM_UNAVAILABLE`）。連不上、逾時、額度用盡或未設定 key 時，只呼叫一次就停 —— 重問同一句不會有不同結果。這與 `GENERATION_FAILED`（模型有回答、只是答不好，會重試到上限）是兩回事，必須分得開：前者請使用者稍後再試，後者請他換個問法。

這一層放在最後而不是放在意圖分類，是因為走到這裡就表示沒有任何 handler 認領這句話，**不可能**從既有規則或線上模型手上搶題目。安全靠順序，不靠把判斷寫得多精準。

## 後設問句

問「這裡有什麼資料」與問「服務現在是什麼狀態」都沒有對應的 SQL，在問句守門就澄清：前者 `DATA_SCOPE_QUESTION` 指向 `/api/coverage`，後者 `SYSTEM_STATUS_QUESTION` 指向 `/api/health`。比對前先經 `compact_question()` 去空白並統一異體字（「甚麼」→「什麼」），否則同一句話會因為一個字形而走上完全不同的路。

## SQL 安全契約

- 單一 `SELECT`，不允許註解、多敘述、CTE、DDL、DML、`ATTACH`、`PRAGMA` 或危險函式。
- 資料來源只能是 `v_unit`、`v_peak`、`v_system`、`v_outage`，欄位也必須在各 view allowlist。
- 字串條件使用 `?` placeholder，`params` 數量必須一致。
- 必須有整數 `LIMIT`，上限由 `configs/guard.yaml` 決定。

## LLM adapter

`FakeLLM` 用於離線 CI 與重試測試。`OpenAILLM` 使用 OpenAI Responses API 的 JSON Schema Structured Outputs；需安裝 `online` extra，而且必須有 `OPENAI_API_KEY`。缺 key 時由 `DisabledLLM` 拋 `LLMUnavailableError`，管線回 `LLM_UNAVAILABLE`，不會自動改用 `FakeLLM` 假裝在用真實模型。

## Trace

每步記錄 `stage`、`elapsed_ms` 及必要的 `attempt`、`code`、`record_count` 或 `dropped`。Trace 不記錄完整 prompt、API key 或 stack trace。
