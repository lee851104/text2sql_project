# Offline Evaluation

`uv run python -m eval.run_eval` 在固定 SQLite 快照上執行四份版控題庫，不需要 API key；已安裝 `make` 的環境也可用 `make eval` 捷徑。評測會產生：

- `reports/eval_latest.json`：當次完整指標、驗收條件、失敗清單與對照實驗。
- `reports/eval_history.jsonl`：只追加的歷史摘要，用於追蹤語料或規則更新是否退步。
- `reports/figures/eval_summary.svg`：意圖、執行、語意與 SQL 攻擊防護的簡表。

## 指標定義

意圖準確率以黃金題庫比對。執行準確率會各自執行候選 SQL 與標準 SQL，將欄位順序與列順序正規化後比對結果集，不比對 SQL 字串。執行指標分別報告 `in_corpus=true/false`，不用合併數字掩蓋泛化差異。

語意安全同時報告 45 題陷阱命中率與 20 個合法邊界反例的誤攔率。SQL 安全守門必須攔截全部 15 種攻擊。

### 陷阱題報兩個數字

`semantic_traps.accuracy` 只驗 `check_question()` 的判斷是否正確；`semantic_traps.end_to_end` 再走一次離線路徑，驗**使用者實際看不看得到那個結論**。

兩者不等價，而且分歧集中在一種嚴重度：

- `refuse`／`clarify` 一判就短路回傳，守門的結論**就是**回應本身，所以兩個數字必然一致。
- `disclose` 不是。它只是掛在成功答案上的附註 —— 答案產不出來，揭露就跟著消失，使用者看到的是 `GENERATION_FAILED`。

目前守門判斷 45/45，端到端 36/45；差額 9 題全是 `disclose`，全部卡在 `NO_OFFLINE_CANDIDATE`（離線 router 沒有規則接「電廠總出力」「容量缺口」這類問法）。清單在 `end_to_end.unreachable`，逐題列出問句、期望代碼與實際結果。

只報前者的話，那 15 題 `disclose` 端到端全滅，指標仍然是 100%，而且驗不出退步。驗收條件 `semantic_traps_end_to_end_no_regression` 以 `TRAP_END_TO_END_BASELINE` 擋住往下掉 —— 那是目前的量測底線，不是「80% 夠好」的宣稱；補上離線涵蓋後要一併調高。

## 離線結果的界線

目前執行準確率是 `offline_deterministic_rules` 基準：衡量規則 handler 可重現的覆蓋與結果正確性，不是線上 GPT 模型的準確率。RAG ablation 只報檢索器 top-1 意圖；關閉規則路由的線上 LLM 對照在沒有 API key 時標記為 `not_run_without_online_llm`，不會使用標準答案假裝模型輸出。

`CorpusRegressionGate` 以獨立 eval 題庫測試候選 corpus 的 top-1 意圖檢索準確率，超過可容忍退步才拒絕整批晉升；評測題本身仍由 no-leakage 關卡禁止進入 corpus。
