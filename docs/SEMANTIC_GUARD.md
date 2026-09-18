# Semantic Guard Contract

`SemanticGuard` 在 SQL 執行前做兩次決策：先用問句與實體判斷資料是否足以回答，再用 `sqlglot` 的 AST 檢查候選 SQL 是否違反指標、單位、粒度或完整性規則。它不使用 LLM 判定風險。

## 決策類型

- `refuse`：結果本身沒有意義或資料沒有要求的粒度，不執行 SQL。
- `clarify`：名稱或日期條件無法唯一解讀，不執行 SQL。
- `disclose`：結果仍可計算，但必須把資料缺口或預設期間連同結果回傳。
- `pass`：沒有命中規則。

## 動態依據

`SemanticGuard.from_database()` 以唯讀方式從 `meta_manifest` 讀取目前資料期間，並從 `meta_pitfall` 讀取殘差欄、電廠總量不完整與容量缺口對象。新資料重建後，守門對象會跟著對齊產物更新，不另外在查詢層維護電廠清單。

## 十條結構化規則

`PEAK_SUM_ACROSS_DAYS`、`UNIT_MISMATCH`、`NO_UNIT_DETAIL`、`RESIDUAL_TREND`、`PLANT_TOTAL_INCOMPLETE`、`KNOWN_CAPACITY_GAP`、`ZERO_PERIOD_AMBIGUOUS`、`AMBIGUOUS_UNIT_NAME`、`DATA_RANGE_OUT_OF_BOUNDS` 與 `RENEWABLE_SELF_BUILT_ONLY` 都回傳穩定的 `code`、`severity`、說明、建議與 evidence，前端不需要比對中文錯誤字串。

陷阱 45 題用來測命中率；測試同時保留 20 個規則反例，包含「單日跨機組加總」、「同單位容量比較」、「殘差欄單日值」與「有明確期間的零出力」，避免以「全部攔下」來虛增安全指標。
