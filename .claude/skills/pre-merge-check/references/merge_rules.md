# 合併門檻規範依據

這份文件說明每一條檢查「憑什麼這樣判」，讓判定結果可以被稽核、被質疑、被修改。
規範來源有三個：

1. `.github/workflows/ci.yml` —— offline-ci 的實際門檻（ruff format / ruff check / pytest）
2. `.gitignore` 與專案規格 §9 —— 哪些產物不得進版控
3. `docs/TEAM_4_ROLES.md` 文末「儲存點與合併檢查表」中**機器可判定**的那幾條

## 範圍：只檢查對齊，不檢查分工

本門檻只回答「**這個分支的格式與規範對齊了嗎**」：CI 會不會掛、有沒有把不該進版控的東西帶進來、
commit 訊息格式、`log.md` 有沒有記錄、跟 `main` 同不同步。

**誰擁有哪個檔案、誰該審查什麼、要不要交接單，不在本門檻範圍內。** 那是人的判斷，
規則寫死在工具裡只會在分工調整時變成過期的噪音。需要時請直接看 `docs/TEAM_4_ROLES.md`。

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
| 比較基準 | WARN | 一律以 `origin/<base>` 為準。本機 main 常落後遠端（實測曾落後 2 個 commit），拿它當基準會漏掉真正會發生的衝突。只有連遠端分支都找不到時才退回本機並警告 |
| 工作目錄狀態 | WARN | 工作目錄有未提交變更時，ruff／pytest 驗的是工作目錄而不是那個 commit。**這時不得判 PASS** —— 假的綠燈比沒有門檻更糟 |
| 與 main 的合併衝突 | BLOCK | 有衝突就不可能自動合併，必須先在分支上解掉 |
| 與 main 的同步狀態 | WARN | 落後 main 時，分支上的驗收證據不等於合併後的結果，需人工判斷要不要重跑。實測靠這條發現過整個分支是重工 |
| 機密字串外洩 | BLOCK | 共用 Prompt 第 5 條：不得把 API key、密碼、session、真實識別資料加入 Git |
| 禁止進版控的檔案 | BLOCK | `.gitignore` 明列 + 規格 §9：raw／interim／processed 都不進版控；`架構參考/` 含未去識別化真實電號 |
| 大型檔案 | BLOCK | 原始快照與 release 壓縮包由 GitHub Releases 保存，不重複提交 |
| 空白字元檢查 | BLOCK | 合併檢查表明列 `git diff --check HEAD` |
| Commit message 格式 | WARN | main 上全部是 Conventional Commits（`feat:`／`fix:`／`docs:`／`chore:`），但這是慣例不是 CI 門檻 |
| log.md 儲存點紀錄 | WARN | 共用 Prompt 第 9 條：每到要 commit 的可驗證儲存點，必須先更新 log.md 記錄狀態、證據與精確回退方式 |
| 測試同步 | WARN | 共用 Prompt 第 7 條：高風險改動必須加負向、競態或故障注入測試。純重構可能合理，故不阻擋 |
| 文件同步 | WARN | 合併檢查表：README／SERVING／API 契約已在行為變更時同步 |
| `ruff format --check` | BLOCK | ci.yml 的 Format check step |
| `ruff check` | BLOCK | ci.yml 的 Lint step |
| `pytest` | BLOCK | ci.yml 的 Offline tests step |
| `node --check app.js` | BLOCK | 合併檢查表明列；找不到 node 時降為 WARN（未驗不等於通過） |

## 機密偵測的誤判處理

只掃**新增的行**，跳過 `*.lock` 與 `tests/`。報告只記 `檔案:行號` 與樣式名稱，**不會把命中的內容寫進報告**。

專案裡有刻意保留的憑證值 —— 公開展示密碼 `PowerQuery@123`（loopback-only，`log.md` CP-013 與
`docs/SERVING.md` 明載）。這種值若每次都 BLOCK，門檻很快就會被當成雜訊忽略，所以放進
`gate_rules.json` 的 `secret_allowlist`，並在報告裡以「另有 N 處命中允許清單，已排除」留痕，不是靜默放行。

要排除單一行，也可以在該行加註解 `pre-merge-check: allow-secret`。

**加進允許清單前先確認那個值真的可以公開。** 判斷不了就別加，維持 BLOCK。

## CI 檢查為什麼可能被跳過

`ruff`／`pytest`／`node` 跑的是**目前的 working tree**，不是任意分支的 commit 內容。
所以只有「要檢查的分支就是目前簽出的分支」時才會實跑；否則標記 SKIP，整體判定最高只到 WARN。
要拿到完整結果就先 `git switch <分支>` 再跑。

這是刻意的取捨：與其自動切分支（會動到使用者未提交的變更），不如誠實回報沒跑。

### ruff 的掃描範圍對齊 CI

CI 是乾淨簽出，只看得到受版控的檔案。本機直接跑 `ruff check .` 會連未被 gitignore 的暫存目錄
一起掃 —— 實測曾因 `extensions/` 產生 28 個與分支無關的錯誤，報出 CI 根本不會有的 BLOCK。

因此 ruff 改成明列 `git ls-files '*.py' '*.pyi'` 的結果，範圍與 CI 一致，報告也會附上
「N 個受版控檔案，與 CI 範圍一致」讓人看得到掃了什麼。兩個實作細節：

- **必須加 `--force-exclude`**：明確傳入路徑時 ruff 預設會忽略 `pyproject.toml` 的 `exclude`
  （本專案排除 `taipower_align`），不加就會多掃出一堆與 CI 不符的錯誤。
- 受版控檔案過多、命令列長度超過上限時，會退回掃描整個工作目錄，並在報告裡註明。

未進版控的檔案仍由「工作目錄狀態」那條負責提醒，只是不再讓它們左右 ruff 的判定。

`pytest` 不需要這個處理：`pyproject.toml` 的 `testpaths = ["tests"]` 已經把收集範圍限制住了。

### ruff 版本

`pyproject.toml` 只寫 `ruff>=0.12,<1`，但 `uv.lock` 鎖定確切版本，CI 以 `uv sync --extra dev` 安裝同一版，
所以本機與 CI 原則上一致。報告仍會附上實際執行的 ruff 版本，環境沒同步時才查得出來。

## 在 PR 上自動貼報告

`.github/workflows/merge-gate.yml` 會在每個 PR 上跑一次門檻，把報告貼成 PR 留言，
判定 BLOCK 時讓該檢查失敗。用 Actions 內建的 `GITHUB_TOKEN`，不需要任何額外憑證。

幾個刻意的設計：

- **checkout 取 `head.ref` 而不是 PR 的合併預覽 commit**，因為門檻要評的是這個分支本身；
  搭配 `fetch-depth: 0`，否則算不出 merge-base 也讀不到逐個 commit。
- **只有 BLOCK 會讓檢查失敗**，WARN 不會 —— WARN 的定義就是「可合併，但需要人看一眼」，
  讓它擋住合併會逼大家養成無視紅燈的習慣。
- **貼留言用 `--edit-last` 就地更新**，每次推送不會洗版。
- **貼留言失敗不影響門檻結果**（`continue-on-error`）：fork 發出的 PR 拿不到
  `pull-requests: write`，不該因此把整個門檻判成失敗。

判定用的是腳本的離開碼（`2` = BLOCK），不是去解析報告文字。

## 修改規則

要調整門檻時，**只改 `references/gate_rules.json`**，不要改 `scripts/check_merge.py`。可改的欄位：

| 欄位 | 用途 |
|---|---|
| `base_branch` | 預設目標分支 |
| `ignore` | 完全不列入統計的路徑（例如本檢查自己產生的報告） |
| `forbidden` | 禁止進版控的路徑，`reason` 會直接出現在報告裡 |
| `secret_patterns` | 機密字串 regex，`name` 會出現在報告裡（報告只記位置不記內容） |
| `secret_allowlist` | 已知且刻意進版控的值，每條都要寫 `reason` |
| `conventional_commit_types` | 允許的 commit type |
| `doc_sync` | 「改了 A 就該同步 B」的對應表 |
| `max_file_bytes` | 大型檔案上限，預設 5 MB |

`pattern` 以 `/` 結尾代表整個目錄前綴；否則用 glob（`fnmatch`）。

改完務必確認 JSON 仍然合法：

```bash
python -c "import json,pathlib;json.loads(pathlib.Path('.claude/skills/pre-merge-check/references/gate_rules.json').read_text(encoding='utf-8'));print('OK')"
```
