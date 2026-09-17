# 合併門檻規範依據

這份文件說明每一條檢查「憑什麼這樣判」，讓判定結果可以被稽核、被質疑、被修改。
規範來源有三個：

1. `docs/TEAM_4_ROLES.md` —— 四人唯一檔案所有權表、各角色最低驗收、文末「儲存點與合併檢查表」
2. `.github/workflows/ci.yml` —— offline-ci 的實際門檻（ruff format / ruff check / pytest）
3. `.gitignore` 與專案規格 §9 —— 哪些產物不得進版控

## 三級判定

| 級別 | 意義 | 原則 |
|---|---|---|
| **BLOCK** | 不可合併 | 合進去會讓 main 壞掉、CI 紅燈，或造成不可逆的外洩 |
| **WARN** | 可合併，但需人工確認 | 規範要求人的判斷，機器不該替人決定 |
| **PASS** | 符合要求 | 全數通過，沒有任何 SKIP |

聚合規則：**任一 BLOCK → BLOCK；否則任一 WARN 或 SKIP → WARN；全 PASS → PASS。**
有檢查未執行（SKIP）時不得判為 PASS —— 沒跑過的東西不能算通過。

## 逐項依據

| 檢查 | 級別 | 依據 |
|---|---|---|
| 比較基準 | WARN | 一律以 `origin/<base>` 為準。本機 main 常落後遠端（實測就落後 2 個 commit），拿它當基準會漏掉真正會發生的衝突。只有連遠端分支都找不到時才退回本機並警告 |
| 工作目錄狀態 | WARN | 工作目錄有未提交變更時，ruff／pytest 驗的是工作目錄而不是那個 commit。**這時不得判 PASS** —— 假的綠燈比沒有門檻更糟 |
| 與 main 的合併衝突 | BLOCK | 有衝突就不可能自動合併；先在分支解衝突是 Integration Owner 的合併順序要求 |
| 與 main 的同步狀態 | WARN | 落後 main 時，分支上的驗收證據不等於合併後的結果，需人工判斷要不要重跑 |
| Owner 檔案所有權 | WARN | TEAM_4_ROLES：「每個 production 檔案只有一位主要 Owner」。但跨 Owner 是**合法**的，只是必須附「跨組契約／交接單」並由對應 Owner 確認 —— 所以警告而不阻擋 |
| 機密字串外洩 | BLOCK | 共用 Prompt 第 5 條：不得把 API key、密碼、session、真實識別資料加入 Git |
| 禁止進版控的檔案 | BLOCK | `.gitignore` 明列 + 規格 §9：raw／interim／processed 都不進版控；`架構參考/` 含未去識別化真實電號 |
| 大型檔案 | BLOCK | 原始快照與 release 壓縮包由 GitHub Releases 保存，不重複提交 |
| 空白字元檢查 | BLOCK | 成員 D 最低驗收明列 `git diff --check HEAD` |
| Commit message 格式 | WARN | main 上全部是 Conventional Commits（`feat:`／`fix:`／`docs:`／`chore:`），但這是慣例不是 CI 門檻 |
| log.md 儲存點紀錄 | WARN | 共用 Prompt 第 9 條：每到要 commit 的可驗證儲存點，必須先更新 log.md 記錄狀態、證據與精確回退方式 |
| 測試同步 | WARN | 共用 Prompt 第 7 條：高風險改動必須加負向、競態或故障注入測試。純重構可能合理，故不阻擋 |
| 文件同步 | WARN | 合併檢查表：README／SERVING／API 契約已在行為變更時同步 |
| `ruff format --check` | BLOCK | ci.yml 的 Format check step |
| `ruff check` | BLOCK | ci.yml 的 Lint step |
| `pytest` | BLOCK | ci.yml 的 Offline tests step；目前基線 184 passed |
| `node --check app.js` | BLOCK | 成員 D 最低驗收明列；找不到 node 時降為 WARN（未驗不等於通過） |

## Owner 所有權的比對方式

規則在 `references/ownership.json`，程式不內建任何分工知識。

- `pattern` 以 `/` 結尾 → 比對整個目錄前綴；否則用 glob（`fnmatch`）
- **最長 pattern 優先**。所以 `src/serving/` 歸 D，但 `src/serving/admin_auth.py` 歸 C、
  `src/serving/data_management.py` 歸 A、`src/serving/corpus_learning.py` 歸 B
- `shared` 清單（目前只有 `log.md`）不計入跨 Owner —— 這是 TEAM_4_ROLES 明訂的 append-only 共享例外
- 比不到任何 pattern 的檔案標成 `?`，一律 WARN：所有權表沒寫到的檔案，要先決定歸誰

**四位成員都可以擔任 A／B／C／D 任一角色**，所以檢查的是「這批變更橫跨幾個 Owner 範圍」，
不是「這個人是誰」。要針對特定角色驗收時才加 `--owner A`。

### 測試檔的歸屬依據（2026-09-17 實證審查）

`docs/TEAM_4_ROLES.md` 只在各角色「最低驗收」點名了部分測試檔，其餘是審查出來的。
**判斷依據是測試 import 哪個 production 模組，不是檔名**，因為檔名會誤導：

| 測試檔 | import 的模組 | 歸屬 | 備註 |
|---|---|---|---|
| `test_naming.py` | `align.naming` | A | 檔名像命名規則，實際是 `src/align/` 的名稱正規化 |
| `test_pitfalls.py` | `align.crosswalk`、`align.pitfalls` | A | 檔名像 C 的語意陷阱，實際模組在 `src/align/` |
| `test_readonly_db.py` | `text2sql.db` | B | 唯讀 SQL 聽起來像 C，但 C 只擁有兩個 guard 檔 |
| `test_raw_data.py` | `serving.raw_data` | D | 名字像資料，實際是 `src/serving/` 的 HTTP 讀取層 |
| `test_llm.py` | `text2sql.llm` | B | |
| `test_promotion_gate.py` | `eval.promotion_gate` | C | |
| `test_foundation.py` | `project_tasks` | D | |
| `test_crosswalk.py`／`test_fetch.py`／`test_outage_align.py` | `align.*`、`ingest.*` | A | |

`reports/` 是混合的：`data_quality.json`（`ingest/build_db.py` 產生）、`alignment.json`、
`outage_unmatched.txt`（`align/__main__.py` 產生）歸 A；`eval_latest.json`、
`eval_history.jsonl`、`figures/`（`eval/run_eval.py` 產生）歸 C。

`ATTRIBUTION.md` 是資料來源顯名與快照版本，屬於 A 的「資料文件」，不是 D 的共用文件。

### 三個尚未裁決的爭議

這三項超出我能單方面決定的範圍，**需要四人確認後再改 `ownership.json`**：

1. **`tests/test_serving_auth.py` 同時出現在 C 與 D 的最低驗收清單裡。** 目前歸 C（安全優先）。
   這是 `docs/TEAM_4_ROLES.md` 本身的矛盾，建議在該文件裡一併修正。
2. **`src/text2sql/db.py`（`ReadOnlySQLite`）** 依檔案規則屬 B，但「維持唯讀 SQL」寫在 C 的第一責任裡。
   要嘛把它列入 C 的擁有範圍，要嘛在 C 的責任描述中註明它靠 B 的模組實作。
3. **`src/align/pitfalls.py`** 在 A 的目錄裡，但產出的是 C 用來評測的語意陷阱。
   目前歸 A（依檔案位置），跨組介面建議以交接單約定。

## 機密偵測的誤判處理

只掃**新增的行**，跳過 `*.lock` 與 `tests/`。報告只記 `檔案:行號` 與樣式名稱，**不會把命中的內容寫進報告**。

專案裡有刻意保留的憑證值 —— 公開展示密碼 `PowerQuery@123`（loopback-only，`log.md` CP-013 與
`docs/SERVING.md` 明載）。這種值若每次都 BLOCK，門檻很快就會被當成雜訊忽略，所以放進
`ownership.json` 的 `secret_allowlist`，並在報告裡以「另有 N 處命中允許清單，已排除」留痕，不是靜默放行。

要排除單一行，也可以在該行加註解 `pre-merge-check: allow-secret`。

**加進允許清單前先確認那個值真的可以公開。** 判斷不了就別加，維持 BLOCK。

## CI 檢查為什麼可能被跳過

`ruff`／`pytest`／`node` 跑的是**目前的 working tree**，不是任意分支的 commit 內容。
所以只有「要檢查的分支就是目前簽出的分支」時才會實跑；否則標記 SKIP，整體判定最高只到 WARN。
要拿到完整結果就先 `git switch <分支>` 再跑。

這是刻意的取捨：與其自動切分支（會動到使用者未提交的變更），不如誠實回報沒跑。

### ruff 版本落差

`pyproject.toml` 只寫 `ruff>=0.12,<1`，本機環境很容易漂到比 CI 新的版本，而 ruff 的
格式化與 lint 規則會隨版本變。版本不一致時，本檢查會報出 CI 根本不會有的 BLOCK。

所以報告會附上實際執行的 ruff 版本。看到 ruff 失敗但覺得不合理時，先對齊環境再判斷：

```bash
uv sync --extra dev
```

要根治就把 `pyproject.toml` 的 ruff 改成精確 pin（例如 `ruff==0.12.*`），讓本機與 CI 一致 —— 這是成員 D 的範圍。

## 修改規則

分工改變、新增受控檔案、或要調整禁入清單時，**只改 `references/ownership.json`**，
不要改 `scripts/check_merge.py`。可改的欄位：

| 欄位 | 用途 |
|---|---|
| `rules` | Owner 檔案所有權，`{"owner": "A", "pattern": "src/align/"}` |
| `shared` | 不計入跨 Owner 的共享檔案 |
| `ignore` | 完全不列入所有權統計的路徑（例如本檢查自己產生的報告） |
| `forbidden` | 禁止進版控的路徑，`reason` 會直接出現在報告裡 |
| `secret_patterns` | 機密字串 regex，`name` 會出現在報告裡（報告只記位置不記內容） |
| `secret_allowlist` | 已知且刻意進版控的值，每條都要寫 `reason` |
| `conventional_commit_types` | 允許的 commit type |
| `doc_sync` | 「改了 A 就該同步 B」的對應表 |
| `max_file_bytes` | 大型檔案上限，預設 5 MB |

改完務必確認 JSON 仍然合法：

```bash
python -c "import json,pathlib;json.loads(pathlib.Path('.claude/skills/pre-merge-check/references/ownership.json').read_text(encoding='utf-8'));print('OK')"
```
