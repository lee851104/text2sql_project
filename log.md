# PowerQuery TW 開發進度

> 這份檔案在每個可驗證、可回退的儲存點更新。回退前需保留使用者原有的未提交變更。

## CP-018 — 修復 CI 門檻靜默失敗

- 時間：2026-09-18 17:40 +08:00
- 狀態：已完成
- 現象：CP-017 的 merge-gate workflow 在 PR #4 上回報 Success，但沒有貼出任何報告，且 gate job 只跑了 9 秒（光 pytest 在 CI 就要約 50 秒）。實際上門檻根本沒執行到 ruff 與 pytest。
- 根因一（門檻中止）：`actions/checkout` 只建立 PR 分支的遠端追蹤 ref。`--fetch` 原本執行 `git fetch origin main`，這只保證更新 `FETCH_HEAD`，不會建立 `refs/remotes/origin/main`，因此 `resolve_base()` 找不到基準而 `die()`（離開碼 3）。本機刪除 `refs/remotes/origin/main` 後複現：`git fetch origin main` 之後該 ref 仍不存在，改用明寫 refspec `+refs/heads/main:refs/remotes/origin/main` 則正確重建。
- 根因二（失敗被當成通過，較嚴重）：workflow 只讓離開碼 2 失敗，離開碼 3 因此被視為成功；報告未產生又使貼留言步驟被 `hashFiles` 條件跳過，於是門檻壞掉時全程無聲。
- 修正：`resolve_base()` 的 fetch 改為明寫 refspec；workflow 另加一個獨立的 base 分支 fetch 步驟並以 `git rev-parse --verify` 確認；離開碼改為 `0`／`1` 通過、`2` BLOCK 失敗、其餘一律視為門檻故障並失敗；報告不存在時改貼「門檻執行失敗」留言並附最後 40 行輸出，不再靜默跳過。
- 測試：`tests/test_merge_gate.py` 新增離開碼契約測試（不存在的分支 → 離開碼 3 且不產生報告），鎖住 workflow 依賴的這個區分。全檔 13 項通過。
- CI 實證：PR #5 上 `github-actions` 已成功貼出完整報告（判定 WARN、15 PASS），證實修正有效，比較基準該項顯示「以 origin/main 為基準」，確認基準解析已正常。
- 追加修正（門檻報告自己造成的 WARN）：workflow 原本把 `gate-output.txt` 與 `gate-report.md` 寫在 repo 根目錄，門檻的「工作目錄狀態」把它們算成未提交變更，於是每個 PR 都會多出一個自己造成的 WARN 並使判定無法為 PASS。所有產物改寫入 `$RUNNER_TEMP`；同時把 `--fetch` 加回門檻呼叫，消除報告中「未加 --fetch」這句在 CI 情境下會誤導的註記（獨立的 base fetch 步驟保留，作用是讓基準取不到時提早失敗）。
- 回退方式：回退本 CP 對應的 commit；CP-017 的 workflow 會回到會靜默失敗的版本。

## CP-017 — 合併門檻報告自動貼上 PR

- 時間：2026-09-18 17:05 +08:00
- 狀態：已完成
- 目的：讓門檻報告從「只存在執行者本機」變成「PR 上所有人都看得到」，不必靠口頭轉述判定結果。
- 新增 `.github/workflows/merge-gate.yml`：`on: pull_request` 執行門檻，把報告以 `gh pr comment --edit-last` 貼成留言（就地更新，不洗版），並在判定 BLOCK 時讓該檢查失敗。使用 Actions 內建的 `GITHUB_TOKEN`，不需要額外憑證，也不需要任何人經手 token。
- checkout 取 `head.ref` 而非 PR 的合併預覽 commit，因為門檻評的是分支本身；搭配 `fetch-depth: 0`，否則算不出 merge-base 也讀不到逐個 commit。
- 只有 BLOCK（離開碼 2）會讓檢查失敗，WARN 不會 —— WARN 的定義是「可合併但需人看一眼」，讓它擋住合併會養成無視紅燈的習慣。判定取腳本離開碼，不解析報告文字。
- 貼留言步驟設 `continue-on-error`：fork 發出的 PR 拿不到 `pull-requests: write`，貼不上去不該讓門檻整個失敗。
- 腳本修正：判斷「要檢查的分支是不是目前簽出的」改比 SHA 而非分支名稱。CI 簽出後常是 detached HEAD（`--abbrev-ref HEAD` 會回 `HEAD`），只比名稱會誤判成「不是目前分支」而白白跳過 ruff／pytest，使 CI 上的門檻永遠拿不到 PASS。
- 驗收：`ruff check`、`ruff format --check` 通過；workflow YAML 以 `yaml.safe_load` 驗證可解析；門檻對本分支自檢通過。
- 回退方式：刪除 `.github/workflows/merge-gate.yml` 即停止自動貼留言；SHA 比對修正可獨立保留，它在本機執行時同樣正確。

## CP-016 — 合併門檻縮回格式對齊範圍

- 時間：2026-09-18 09:20 +08:00
- 狀態：已完成
- 決定：門檻只保留「這個分支的格式與規範對齊了嗎」，不再細分每位成員該負責什麼。CP-014 建立的 A／B／C／D 所有權判定全數移除。
- 理由：分工會隨階段調整，把它寫死在工具裡，每次分工一變規則就過期，警告會退化成噪音；而「誰該審查什麼」本來就是人的判斷。`docs/TEAM_4_ROLES.md` 作為人看的分工文件保持不動，需要時直接查閱。
- 移除項目：`owner_of()`、`check_ownership()`、`check_rules_integrity()`、`check_handoff()` 四個函式與 `--owner` 參數；報告與主控台不再輸出涉及 Owner。
- 規則檔改名並精簡：`references/ownership.json` → `references/gate_rules.json`，刪去 `owners`、`shared`、`rules`（79 條所有權）、`handoff` 四個區段；保留 `base_branch`、`ignore`、`forbidden`、`secret_patterns`、`secret_allowlist`、`conventional_commit_types`、`doc_sync`、`max_file_bytes`。
- 檢查項目由 19 項減為 14 項：比較基準、合併衝突、與 main 同步、機密外洩、禁入檔案、大型檔案、空白字元、commit 格式、log.md 儲存點、測試同步、文件同步、工作目錄狀態，以及 ruff format／ruff check／pytest／node --check 四項 CI 等價驗收。三級判定與離開碼不變。
- 文件同步：`SKILL.md` 的 description、Purpose、選項與步驟已移除所有權相關內容；`references/merge_rules.md` 改寫，新增「範圍：只檢查對齊，不檢查分工」一節，並補上 ruff 掃描範圍對齊 CI 的作法與 `--force-exclude` 的必要性。
- 驗收：`uv run ruff format --check`、`uv run ruff check` 對 `check_merge.py` 皆通過；`create-skill` 的 `validate.py` 回報 ERROR 0；以 `--base 81fe958` 實跑確認 14 項檢查全部可執行且無所有權殘留。
- 修復 ruff 掃描範圍：CI 是乾淨簽出只看得到受版控檔案，本機 `ruff .` 會連未 gitignore 的暫存目錄一起掃，曾因 `extensions/` 產生 28 個無關錯誤造成假 BLOCK。改為明列 `git ls-files '*.py' '*.pyi'` 的結果並加 `--force-exclude`（明確傳路徑時 ruff 會忽略 pyproject 的 `exclude`，不加會多掃 `taipower_align`），報告附上「N 個受版控檔案，與 CI 範圍一致」。以未進版控且未被忽略的爛格式檔案實測：`ruff check .` 報 5 個錯，新範圍 All checks passed，而「工作目錄狀態」仍正確提醒該檔案存在。
- 修復文件同步誤報：原規則只看「這個檔案有沒有被改」，`ruff format` 重排 `src/serving/app.py` 也會被要求更新 API 契約（實測誤報過）。新增 `format_only_files()`，把同一檔案的新舊版本都以 `ruff format -` 正規化後比對，相同即視為純排版變更並排除在 `doc_sync` 之外；取不到 ruff 時回傳空集合，寧可保留警告也不靜默放行。
- 新增自動化測試：`tests/test_merge_gate.py` 12 項，以 importlib 載入門檻腳本，並用暫存 git repo 搭配 monkeypatch 改寫 `REPO`。涵蓋先前未被觸發過的路徑：大型檔案 BLOCK、log.md 只寫一半、中文檔名八進位還原、機密允許清單與 inline marker、髒工作目錄不得判 PASS、SKIP 不得判 PASS、純排版與真實變更的區分，以及一項端到端（離開碼 2 並產出報告）。門檻的判定聚合抽成 `overall()` 以便直接測試。
- 全套驗收：`uv run pytest -q` 227 passed（main 基線 215，本分支淨增 12）；`ruff format --check`、`ruff check`、`node --check` 全數通過；門檻自檢 15 PASS／1 WARN，唯一 WARN 為使用者未提交的簡報檔。
- 回退方式：回退本 CP 對應的 commit 即可恢復 CP-014 的所有權版本；`docs/TEAM_4_ROLES.md` 未在本次變更，不受影響。

## CP-015 — 電廠分權分支的格式修復與 main 同步

- 時間：2026-09-18 00:07 +08:00
- 狀態：已完成
- 問題：`feat/plant-scope-authorisation` 建立時未執行 `ruff format`，且落後 main 兩個 commit（PR #2 的格式基線修正）。`git merge-tree` 顯示無文字衝突，但實際做完合併後 `ruff format --check` 仍有 3 個檔案不合規（`src/ingest/build_db.py:546`、`tests/test_scope_guard.py:135`、`tests/test_semantic_guard.py:169`），依 CP-014 的 `merge_rules.md` 屬 BLOCK。
- 修正：在分支上執行 `ruff format`（10 個檔案重新格式化，其中 7 個是 main 已修好、分支尚未同步的部分），再以 `--no-ff` 把 main 合併進分支。合併無衝突。
- 合併進來的 main 內容：CP-014 的 `pre-merge-check` Skill，以及 `docs/sync-stale-docs` 的文件同步修正。
- 驗收：`ruff format --check .` 98 files already formatted；`ruff check .` All checks passed；`git diff --check HEAD` 無輸出；`pytest -q` 214 passed、1 skipped。main 當前基線為 196 passed，本分支淨增 18 題，主要來自 `tests/test_scope_guard.py`。
- 唯一 skip：`tests/test_windows_launcher.py:168`，原因是本機 port 8765 已被既有服務占用，屬環境因素，CI 的 ubuntu runner 不受影響。
- 待人工處理：本分支橫跨 A／B／C／D 四位 Owner 的檔案，依 `docs/TEAM_4_ROLES.md` 需附跨組交接單；`tests/test_scope_guard.py` 尚未列入 `ownership.json`，需先決定歸屬。
- 回退方式：回退 `style: apply ruff format to tracked sources` 與其後的 merge commit；`d23a3ab`、`7bea052` 兩個功能 commit 保持不動。

## CP-014 — 合併門檻 Skill 與 main 的 ruff 基線修復

- 時間：2026-09-17 09:02 +08:00
- 狀態：已完成
- 目的：把 `docs/TEAM_4_ROLES.md` 文末「儲存點與合併檢查表」那七條人工檢查，變成每次合併前都跑得出同一份證據的自動門檻，避免四人分支併入 main 時靠口頭確認。
- 新增 Skill：`.claude/skills/pre-merge-check/`，四件式結構。`scripts/check_merge.py` 執行 16 項檢查；`references/ownership.json` 以資料形式保存 74 條所有權、24 條禁入路徑、5 組機密樣式與允許清單，分工異動只改這個檔案不動程式；`references/merge_rules.md` 記錄每條檢查的規範出處；`templates/merge_report.md.template` 固定報告骨架。
- 三級判定：BLOCK（衝突、機密外洩、禁入檔案、大型檔案、空白字元、ruff format、ruff check、pytest、node --check）／WARN（比較基準、工作目錄狀態、Owner 所有權、落後 main、commit 格式、log.md 儲存點、測試同步、文件同步）／PASS。任一 BLOCK 即 BLOCK；有 WARN 或 SKIP 即 WARN；有檢查未執行時不得判 PASS。離開碼 0／1／2 可接自動化。
- 所有權採警告不阻擋：跨 Owner 在 TEAM_4_ROLES 中本來就合法，只需附交接單，故判斷權保留給人。比對以最長 pattern 優先，`log.md` 列為共享例外，比不到規則的檔案標為未指派並警告。
- 本檢查唯讀：不執行 merge、push 或任何改動分支的操作，只輸出報告到 `reports/merge_check/`（已列入 .gitignore）。
- 防假綠燈：比較基準一律取 `origin/<base>`，本機 main 落後時自動改用遠端並在報告載明（實測本機 `9a8ecad` 落後 `origin/main` `81fe958` 兩個 commit）；工作目錄有未提交變更時，ruff／pytest 驗的是工作目錄而非該 commit，列為 WARN 使其無法判定 PASS。
- main 基線修復（事後確認為重工）：本分支先以 `ruff format` 重排 8 個受版控檔案並修正 `src/ingest/build_db.py` 的 import 順序；修復前模擬 CI 乾淨簽出時 `ruff check` 5 個錯誤、`ruff format --check` 7 個檔案不符。合併 `origin/main` 時發現 PR #2（`chore: fix CI formatting checks`）已獨立完成同一件事，且 8 個檔案逐字元相同，故本分支的 `style:` commit 不產生任何實際差異。此事由門檻的「與 main 的同步狀態」警告先行揭露 —— 若沿用落後的本機 main 當基準就不會發現。
- 版控衛生：`extensions/`（暫存實驗）、`.codex-*/`（Codex 建置暫存，內含 node_modules 上百個 .py 會被 ruff 掃到）、`reports/merge_check/`（本檢查產出）列入 .gitignore。
- 自動驗收：`uv run ruff format --check .`、`uv run ruff check .`、`uv run pytest -q`（197 passed）、`node --check src/serving/static/app.js`、`git diff --check` 全數通過；`create-skill` 的 `validate.py` 對本 Skill 回報 ERROR 0 / WARN 0。
- 功能驗收：以合成 git repo 驗證埋入 `.env`＋`sk-` 金鑰、`data/` 產物、行尾空白與真實衝突時全部正確判 BLOCK（exit 2）；commit 格式、缺 log.md、缺測試、跨 Owner 正確判 WARN；本機 main 落後遠端時自動改用 `origin/main` 比較。修正兩個實測缺陷：git 對中文檔名的八進位跳脫使 `*.bat` 規則失效，以及 loopback 展示密碼 `PowerQuery@123` 被誤判為外洩（已列入允許清單並在報告留痕）。
- 所有權實證審查：TEAM_4_ROLES 只在各角色最低驗收點名了部分測試檔，其餘原為檔名推論。改以「測試 import 哪個 production 模組」為依據重審，修正 6 條：`test_naming.py` B→A、`test_pitfalls.py` C→A、`test_readonly_db.py` C→B、`test_raw_data.py` A→D、`ATTRIBUTION.md` D→A，`reports/` 由整包歸 A 拆為資料品質產物歸 A、`eval_*` 與 `figures/` 歸 C。審查依據已記入 `references/merge_rules.md`。
- 三個所有權爭議的處置：`src/text2sql/db.py`（`ReadOnlySQLite`）與 `tests/test_readonly_db.py` 改歸 C，唯讀保證是 C 的第一責任，比照 guards 從 `src/text2sql/` 劃出；`src/align/pitfalls.py` 留在 A 但列為跨組介面，改到即要求附交接單給 C；`tests/test_serving_auth.py` 原先同時列在 C 與 D 的最低驗收清單，先由新增的「所有權規則一致性」檢查 BLOCK 指名衝突，裁決後歸 C（登入安全的測試不應由整合角色自行變更即通過；D 在跨契約變更時仍跑全套 pytest），並移除 D 清單裡的該行。`docs/TEAM_4_ROLES.md` 的分工表、角色 Prompt 與檔案所有權表已同步，並加註尚未裁決項。
- 新增兩項檢查：「所有權規則一致性」（同一路徑指派給多位 Owner 即 BLOCK，因為此時任何所有權判定都不可信）與「跨組介面交接」（改到 `handoff` 清單中的檔案即 WARN 並要求交接單）。後者擋的是評測結果失真，與 benchmark／corpus 隔離的洩漏控制無關。
- 已知限制：ruff／pytest 只能反映目前簽出的工作目錄，檢查非當前分支時標記 SKIP 並將判定壓在 WARN，不自動切換分支以免影響使用者未提交的變更。尚未建立針對本 Skill 自身的自動化測試；PASS 判定至今未在實際執行中產生過。
- 回退方式：本儲存點對 `origin/main` 的實際差異只有 `.claude/skills/pre-merge-check/`、`.gitignore`、`docs/TEAM_4_ROLES.md` 與本檔，回退 `feat/pre-merge-gate` 的合併即可；`style: apply ruff format to tracked sources` 與 PR #2 內容相同，回退它不會改變任何檔案內容。CP-001～013、已發布 Release 與使用者原有未提交變更（`reports/data_quality.json`）保持不動。

## CP-013 — 資料治理、專案交接與 Windows 一鍵展示

- 時間：2026-09-13 22:25 +08:00
- 狀態：已完成
- 管理介面：原「語料中心」已改為「資料管理」，登入後可在資料檔、待審異動、語料審查、資料庫版本與稽核紀錄五個頁籤中調閱及操作；左側仍保留查詢中心、資料總覽、API 與模型及 API 文件。
- 登入與權限：本機展示預設為 `admin`／`PowerQuery@123`，只允許 loopback；可由環境變數覆寫。管理 session 使用短期 HttpOnly／SameSite cookie，異動 API 另驗證同源與 CSRF，審核者一律取自伺服器 session。
- 資料熱插拔：五個固定 CSV 資料槽可新增或替換，`outage_csv` 可移除；上傳、移除與回退只建立 `pending_review` 候選，驗證 schema、跨檔資料品質並建置不可變 SQLite 後，仍須人工核准才切換。舊版與來源 checksum 保留，可下載歷史來源、建立需再次審核的回退異動。
- 語料治理：離線 router 與線上 LLM 產生的新語料均先停在待人工審核；核准時重新執行去識別、SQL／語意、結果與 benchmark 回歸檢查，通過才發布及重建索引。候選保存查詢 view、資料版本與實際來源檔 SHA-256，未保存結果 rows。
- 發布一致性：build journal 先綁定候選 DB 與確切來源 checksum；mutation journal 使 staged、failed、rejected 的 change 與 audit 在中斷後成對、冪等恢復；不可變來源、DB、版本、核准異動及稽核事件持久化後，`active.json` 才最後切換。publish journal 可恢復中斷發布，audit head 與永久建立標記能分辨首次舊版遷移和後續錨點遺失，active revision 會與發布事件交叉核對；來源、DB、manifest、指標或稽核遭竄改時，查詢及所有管理讀寫都 fail closed。每次查詢的 runtime 與 provenance 使用同一個已驗證 snapshot，多 worker 依共享指標同步。
- 熱插拔驗收：在隔離的暫存工作區，以同一歲修問句驗證 `138 筆 → 移除候選仍 138 筆 → 核准後 0 筆 → 重傳候選仍 0 筆 → 核准後恢復 138 筆`，證明人工核准是唯一生效點。
- 瀏覽器驗收：實際登入新版資料管理，完成查詢「2026年四月有哪些機組在歲修？」、來源追溯、語料人工核准與稽核調閱；候選顯示 `v_outage`、作用中資料版本及 `outage.csv`／`units.csv` checksum，頁面無先前的大型失效圖片。
- 重啟驗收：既有 `.powerquery-data` 工作區已平滑建立 audit head 及永久建立標記；連續兩次啟動後登入、作用中版本、五個來源、DB checksum 與稽核鏈均有效，且沒有殘留 mutation／publish journal。驗收後已停止服務，避免 Windows／OneDrive 占用 SQLite。
- 自動驗收：`uv lock --check`、`ruff format --check .`、`ruff check .`、`node --check src/serving/static/app.js`、`git diff --check HEAD` 與 `pytest -q` 全數通過（184 passed，含 3 個 Windows launcher 測試）；只保留 Starlette 第三方淘汰警告與使用公開 demo 帳密的預期警告。
- 本機安全備份：實機遷移前保留 `data/processed/.powerquery-data-pre-cp013` 與 `data/processed/.powerquery-learning-pre-cp013`，均位於 gitignored 執行期資料目錄。
- 進度與分工：README 已加入 0.3.0／Phase 1～8、184 passed、熱插拔及實機驗收快照；新增 `docs/TEAM_4_ROLES.md`，以 A～D 明定 Phase 9 唯一檔案所有權、量化驗收、跨組交接單、共用 AI 規則與四段可直接啟動的角色 Prompt；`log.md` 明訂為各角色可追加本次 checkpoint 的共享例外。
- 一鍵展示：新增根目錄 `啟動.bat` 與兩個 `scripts/launcher_*.ps1` 輔助程式，可從中文、OneDrive 或含空白路徑雙擊；會檢查 `uv`、只在依賴輸入改變時同步環境、初始化目錄、缺少時建庫、重用既有服務，並在 `127.0.0.1:8765` 健康後開啟瀏覽器。實機驗證依賴 stamp 為兩段 64 位 SHA-256、已啟動服務可直接沿用，且 `/api/health` 與首頁均回 200；驗收後已停止服務。
- 目錄整理：`DATA_DICTIONARY.md`、`EVALUATION.md`、`SEMANTIC_GUARD.md`、`SERVING.md`、`SYSTEM_CARD.md`、`TEXT2SQL_PIPELINE.md` 已集中到 `docs/`，README 與相對連結同步更新且本機 Markdown link check 無斷鏈；根目錄只保留專案入口、建置設定、進度紀錄與一鍵啟動檔。
- 回退方式：回退 `feat: add governed data management and project launcher` 這個 commit；先停止服務，再視需要以 CP-013 前本機備份恢復執行期工作區。CP-001～012、已發布 Release 與使用者原有未提交變更保持不動。

## CP-012 — Windows 資料庫占用提示與建庫復原

- 時間：2026-09-13 20:27 +08:00
- 狀態：已完成
- 現象與根因：Windows 執行 `ingest.build_db` 時，仍在運作的 PowerQuery／SQLite connection 占用 `data/processed/power.db`，使最後的原子 `os.replace` 回傳 WinError 5；前段資料建置本身沒有失敗。
- 修正：原子發布遇到 `PermissionError` 時改拋相容於既有 `PermissionError`／`OSError` 捕捉邏輯的 `DatabasePublishError`，明確指示先以 `Ctrl+C` 停止服務並確認目錄可寫；CLI 只顯示這段操作訊息與 exit code 1，不再輸出 traceback。
- 保護：替換失敗時保留既有 `power.db`，並由 `finally` 嘗試移除本次 `.power-*.db` 暫存檔；測試鎖定情境確認舊檔內容未變且一般 target-only lock 不留暫存檔。
- 文件：`docs/SERVING.md` 已補上 Windows 重建資料庫前必須停止服務的順序與原因。
- 實機復原：關閉先前預覽服務後重新建庫成功，SQLite `PRAGMA quick_check=ok`；再由 `uv run powerquery --serve` 於 `127.0.0.1:8000` 啟動，`GET /api/health` 回 200。占用狀態重跑建庫則正確保留原資料庫並輸出新提示。
- 自動驗收：`ruff format --check .`、`ruff check .`、`git diff --check`、`pytest -q` 全數通過（113 passed）；僅保留既有 Starlette 第三方 AnyIO alias 淘汰警告。
- 回退方式：回退 `fix: explain locked database rebuilds on Windows` 這個 commit；CP-001～011、已發布 Release 與使用者原有變更保持不動。

## CP-011 — 台電資料 GitHub Release

- 時間：2026-09-13 16:43 +08:00；公開完成：2026-09-13 16:46 +08:00
- 狀態：已完成
- Release：https://github.com/chenliyu0410/text2sql_project/releases/tag/taipower-data-2026-09-13
- Git tag：`taipower-data-2026-09-13`，指向 `da749ad6f93eae7d949b47e1d291c690a0e4cb29`。
- 目標：`taipower-data-2026-09-13`／「台電開放資料與 PowerQuery 資料包（2026-09-13）」。
- 資產：完整原始開放資料、PowerQuery 可直接查詢資料、專案文件三個 ZIP，另附 `SHA256SUMS.txt`；詳細成員、大小與 checksum 見 `docs/releases/taipower-data-2026-09-13.md`。
- 安全邊界：排除含真實電號或機構名稱的參考筆記、`.powerquery-learning` 本機學習工作區、查詢紀錄、語料事件、憑證、虛擬環境與快取。每包都附台灣電力公司顯名、OGL 1.0、非即時資料與精確基礎設施位置提醒。
- 驗證：三包 CRC 通過；中文檔名保留；原始包 204 個資源與 manifest 逐筆一致；三個 ZIP 的 SHA-256 已重算一致。GitHub 回報四個 asset 均為 `uploaded`，三個 ZIP 的遠端 byte 大小與 SHA-256 digest 和本機完全相同。
- 回退方式：先從 GitHub Release 管理介面刪除 `taipower-data-2026-09-13` Release 與 tag，再回退 `chore: prepare Taipower data release` 及本筆完成紀錄 commit；大型資產未加入 Git，CP-001～010 與使用者原有變更保持不動。

## CP-000 — 實作前基線

- 時間：2026-09-13 13:02 +08:00
- 狀態：已完成
- Git 基線：`4565ca08adbb2a3d413c4d94283a1a3a9e67a2c8`
- 已驗證：`taipower_align/align.py` 與 `taipower_align/final.py` 都可在 Python 3.13 成功執行。
- 現有資料基準：175 台機組、577 天、43 個已對應欄位、36,928 筆長表資料。
- 使用者原有未提交變更：`.gitignore`、`README.md`、系統規格、`docs/AI_AGENT_COLLABORATION.md`、`scripts/`、`參考資料/`。
- 回退方式：只回退 CP-001 之後新增的實作檔；不對上述使用者變更執行 reset 或 checkout。

## CP-010 — 多頁操作中心、執行模式與可稽核語料學習

- 時間：2026-09-13 16:35 +08:00
- 狀態：已完成
- 前端工作台：完成查詢中心、資料總覽、語料中心、API 與模型、API 文件五個頁面；左側導覽加入常用分析與只留問句的本機最近查詢。逐次查詢的「沿用預設」不再誤送 `auto`，手機抽屜具備 inert／焦點管理，Plotly 與原生 SVG fallback 均無破圖。
- 離線／線上模式：新增 `offline`、`online`、`auto` runtime 管理與逐次覆寫；OpenAI API key 只存在伺服器程序記憶體，不寫磁碟、不回傳、不進瀏覽器儲存，並可由介面明確清除。憑證移除或環境 key 輪替會清掉舊 client cache，狀態與錯誤回應不洩漏 key 或 adapter 細節。
- OpenAI adapter：使用 Responses API Structured Outputs、`store=False`、30 秒 timeout 與單層重試控制；線上 extra 已安裝，並以無效測試 key 驗證 client 初始化後立即清除，沒有向 OpenAI 發出模型請求。
- 自動語料治理：Web／API 查詢可建立候選；router 候選須通過 benchmark 洩漏、去重、SQL／語意、結果重播與檢索回歸關卡才自動發布，LLM 候選必須人工審核。語料中心可搜尋、篩選、查看完整欄位、事件與審核內容。
- 資料安全與一致性：候選只保存問句、參數、SQL、驗證與結果 checksum，不保存結果 rows；遞迴去識別化涵蓋電號、身分證、電話、卡號與標記姓名。工作區使用程序鎖、跨程序檔案鎖、原子寫入、版本／checksum manifest；canonical 或資料庫基線變更時自動備份、重建索引並要求既有候選重新驗證。
- API 與文件：新增 runtime、語料清單、事件、審核及訓練狀態端點；422 驗證錯誤不反射敏感輸入。主工作台提供離線 API 說明，`/openapi.json` 可本機使用；`/docs` 固定 Swagger UI 5.32.15、nonce、精確 SRI 與頁面專用 CSP，且已明示首次載入需要 jsDelivr。
- 實機驗收：在新版工作台執行「2026年7月20日出力前五名機組」，取得 5 筆、平均 405.92 萬瓩並正常顯示圖表；五個頁面、語料詳細視窗、runtime 狀態與 Swagger UI 均通過，瀏覽器 console 無 CSP／載入錯誤，頁面影像檢查無破圖。
- 自動驗收：`ruff format --check .`、`ruff check .`、`node --check src/serving/static/app.js`、`pytest -q` 全數通過（111 passed）。Starlette TestClient 仍有一則第三方 AnyIO alias 淘汰警告，不影響測試或執行。
- 回退方式：回退 `feat: add runtime and corpus control center` 這個 commit；執行期 `.powerquery-learning` 可獨立移除或由其版本備份回復，不影響 canonical corpus；CP-001～009 與使用者原有變更保持不動。

## CP-009 — Plotly 圖示與畫布樣式修復

- 時間：2026-09-13 15:30 +08:00
- 狀態：已完成
- 根因：`.chart svg` 後代 selector 誤中 Plotly modebar 的內部 SVG，把相機、分享與縮放圖示強制放大為至少 620×260px；嚴格 CSP 同時阻擋 Plotly 以 CSSOM 注入的版面規則，使三張 overlay SVG 在文件流中垂直堆疊。
- 修正：fallback 圖改用 `.chart > svg`；Plotly 容器固定 320px，並在本機 stylesheet 以 `.plotly-chart` scope 補齊必要的 overlay 與 modebar 規則，沒有放寬 `style-src` CSP。
- 瀏覽器驗收：實際執行「2026年7月20日出力前五名機組」，正常顯示 5 根長條與資料表；圖表／主 SVG 均為 820×320px 左右，三張主 SVG 全為 absolute overlay，8 個 modebar icons 均為 16×16px。
- 回歸保護：靜態端點測試明確禁止 `.chart svg {`，並要求 direct-child fallback selector 與 Plotly absolute overlay 規則存在。
- 自動驗收：`ruff format --check .`、`ruff check .`、`node --check src/serving/static/app.js`、`pytest -q` 全數通過（65 passed）。
- 回退方式：回退 `fix: scope plotly svg styles` 這個 commit；CP-001～008 與使用者原有變更保持不動。

## CP-008 — Phase 7 API、CLI 與對話式呈現層

- 時間：2026-09-13 14:23 +08:00
- 狀態：已完成
- API：完成 FastAPI 應用、延遲載入 runtime、健康／資料統計／語料狀態／範例／查詢端點與 OpenAPI 文件；資料庫未就緒時回 503，輸入格式錯誤回 422，語意拒答維持結構化業務 envelope。
- CLI：`powerquery` 可直接查詢、輸出完整 `--json`，或以 `--serve` 啟動 Uvicorn；`make serve` 與 `docs/SERVING.md` 收錄可重現操作方式。
- 呈現：後端只建立 `line`、`bar`、`scatter` 白名單圖表規格，日期／數值、類別／數值與雙數值形狀各自選圖；scalar、空結果、全 NULL 或純文字回傳 `chart_spec: null`。圖表 x/y 直接投影自 SQL rows。
- 前端：完成繁中對話介面、資料涵蓋側欄、範例問句、階段式進度、可取消查詢、結構化錯誤／限制揭露、KPI、表格、SQL 細節與響應式版面。固定版本 Plotly.js basic bundle提供互動圖表，無法載入 CDN 時退回相同資料的原生 SVG。
- 安全與無障礙：所有動態內容使用 `textContent` 或 SVG attribute，不拼接不可信 HTML；加入 CSP、`nosniff`、frame deny 與 no-referrer headers；支援雙 live region、`aria-current`、鍵盤焦點、中文輸入法組字、防誤送 Enter、reduced motion 與手機 safe area。
- 相依版本：FastAPI 0.141.1、Uvicorn 0.52.4、HTTPX2 2.12.0；Plotly.js basic bundle 固定為 4.0.0 並驗證 SHA-384 SRI。
- 實機驗收：本機 Uvicorn 啟動後，`GET /api/health` 與 `POST /api/query` 均回 200；「2026年6月每日備轉容量率」取得 30 筆，`chart_spec.data[0].x[0]` 與 SQL 第一列日期同為 `2026-06-01`。
- CLI 驗收：「2026年7月備轉容量率最低是哪一天？」成功回傳 `2026-07-05`、`10.23%`；無 API key 時明確標示離線規則模式，長尾問題不以假模型輸出替代。
- 自動驗收：`ruff format --check .`、`ruff check .`、`node --check src/serving/static/app.js`、`pytest -q` 全數通過（65 passed）。Starlette 1.6.0 仍從第三方 `testclient.py` 發出一則 AnyIO 型別別名淘汰警告，不影響測試或執行。
- 回退方式：回退 `feat: deliver api cli and accessible web interface` 這個 commit；CP-001～007 與使用者原有變更保持不動。

## CP-007 — Phase 6 離線評測與回歸關卡

- 時間：2026-09-13 14:09 +08:00
- 狀態：已完成
- 結果比對：候選 SQL 與標準 SQL 都在同一個唯讀 SQLite 快照上執行；指標以欄名集合與列集合等價性計算，不比對 SQL 字串。
- 離線基準：黃金意圖 80/80；eval 意圖 60/60；執行結果 60/60，`in_corpus=true` 與 `false` 各 30/30；攻擊 15/15；語意陷阱 45/45；合法邊界題 0/20 誤攔。所有驗收條件通過。
- 詮釋界線：上述執行成績標示為 `offline_deterministic_rules`，是可重現規則 handler 基準，不是線上 GPT 準確率；沒有 API key 時不會把 benchmark 答案假裝成 LLM 輸出。
- Ablation：RAG top-1 意圖為 31/60（51.67%），無檢索且預設 other 為 6/60（10%）；語意守門開／關陷阱處理為 100% / 0%；規則查詢首次已成功，重試 1/2/3 次無差異；關閉路由的對照需線上 LLM，誠實標記 `not_run_without_online_llm`。
- 語料回歸：`CorpusRegressionGate` 比較候選 corpus 與基線的獨立題庫 top-1 意圖檢索率，超過可容忍退步就拒絕整批晉升。
- 產物：`reports/eval_latest.json`、只追加的 `reports/eval_history.jsonl`、`reports/figures/eval_summary.svg`；`make eval` 可重建。
- 驗收：`python -m eval.run_eval`、`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（57 passed）。
- 回退方式：回退 `feat: add reproducible offline evaluation` 這個 commit；CP-001～006 與使用者原有變更保持不動。

## CP-006 — Phase 5 資料語意守門

- 時間：2026-09-13 13:52 +08:00
- 狀態：已完成
- 雙層判斷：在生成前檢查問句可答性，在執行前再以 `sqlglot` AST 檢查真實 SQL 形狀；所有結果都是結構化 `code`、`severity`、說明、建議與 evidence。
- 規則：完成 `PEAK_SUM_ACROSS_DAYS`、`UNIT_MISMATCH`、`NO_UNIT_DETAIL`、`RESIDUAL_TREND`、`PLANT_TOTAL_INCOMPLETE`、`KNOWN_CAPACITY_GAP`、`ZERO_PERIOD_AMBIGUOUS`、`AMBIGUOUS_UNIT_NAME`、`DATA_RANGE_OUT_OF_BOUNDS` 九條守門。
- 分級：`refuse` 與 `clarify` 不執行 SQL；`disclose` 可繼續查詢但必須隨結果回傳限制。同一規則在問句層與 SQL 層同時命中時只揭露一次。
- 動態資料：資料期間從 `meta_manifest` 讀取；殘差欄、電廠總量不完整與容量缺口對象從 `meta_pitfall` 讀取，查詢層不重複寫死清單。
- 邊界檢查：單日跨機組加總、同單位容量比較、殘差欄單日值、具明確期間的零出力等 20 個反例均放行。
- 驗收：陷阱題 45/45 命中（100%，要求 ≥ 95%）；20 個合法邊界反例 0 誤攔（0%，要求 ≤ 5%）；`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（52 passed）。
- 回退方式：回退 `feat: add data-aware semantic guardrails` 這個 commit；CP-001～005 與使用者原有變更保持不動。

## CP-005 — Phase 4 Text2SQL 管線

- 時間：2026-09-13 13:44 +08:00
- 狀態：已完成
- 管線：建立實體抽取→問句語意接點→零成本路由→字元 n-gram TF-IDF 檢索→LLM結構化產生→SQL AST 守門→SQL語意接點→唯讀 SQLite 執行的完整編排，並在 trace 保留每步驟耗時。
- 實體：支援西元日期、民國年、中文／全形數字、去年／今年／上個月／上下半年、燃料與 Top-N；相對日期可注入 reference date 以便重現。
- 線上／離線：`OpenAILLM` 使用 Responses API 與 JSON Schema Structured Outputs；`FakeLLM` 用於無 key 的離線 CI，線上模式缺 key 時明確報錯，不會假裝成真實模型。
- 安全：所有路由與 LLM SQL 都經過同一守門；只允許單一 `SELECT`、四個審核 view 與欄位 allowlist，強制參數化字串、`LIMIT <= 200`，禁止註解、多敘述、寫入、系統表與危險函式。SQLite adapter 以 `mode=ro`、`query_only` 與 progress handler 做唯讀及逾時防護。
- 失敗處理：LLM 輸出、SQL 守門或執行錯誤會帶結構化原因重生，上限 3 次；仍失敗時回傳 `GENERATION_FAILED`，不偷換預設查詢。
- 驗收：黃金意圖題庫 80/80（100%，要求 ≥ 90%）；攻擊題庫 15/15 全數攔截；`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（42 passed）。
- 回退方式：回退 `feat: implement guarded text2sql pipeline` 這個 commit；CP-001～004 與使用者原有變更保持不動。

## CP-004 — Phase 3 語料與獨立題庫

- 時間：2026-09-13 13:31 +08:00
- 狀態：已完成
- 正式語料：`corpus/training_corpus.json` 含 DDL、領域文件與 40 組 question–SQL examples；已建立可重現的字元 n-gram `corpus/index.json`。
- 題庫：`golden_questions.json` 80 題（10 意圖各 8 題）、`eval_questions.json` 60 題（`in_corpus=true/false` 各 30）、`trap_questions.json` 45 題（9 條規則各 5）、`attack_questions.json` 15 題。
- 訓練／測試邊界：corpus 與所有 benchmark 問句正規化後無逐字重疊；benchmark 不會被索引。
- 自動語料學習：加入去識別、批次去重、benchmark 洩漏阻擋、可注入 SQL／語意／結果關卡、回歸關卡、版本 checksum、舊版備份與 rollback。
- 原子性：一個 batch 中任一候選失敗時，正式 corpus 與 index 都不會被部分更新。
- 驗證：`python -m text2sql.corpus`、`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（23 passed）。
- 回退方式：回退 `feat: add governed corpus and isolated benchmarks` 這個 commit；已發布的 corpus 也可用 `rollback_corpus` 切回 `corpus/versions/` 備份。

## CP-003 — Phase 2 對齊層與歲修資料

- 時間：2026-09-13 13:24 +08:00
- 狀態：已完成
- 範圍：命名轉換、粒度／燃料分類、crosswalk 容量比驗證、歲修對齊、從對齊結果產生 `meta_pitfall`，所有核心郏輯都是無資料庫、無網路 I/O 的純函式。
- Crosswalk：43 列唯一對應、175 台機組全數涵蓋，ratio 重算與異常備註檢查無 issue。
- 歲修快照：官方 `d006008` 138 列已以內容 SHA-256 `f0b30c1d32a3…` 封存。自動對齊 125 列（90.58%），達成 ≥ 90% 驗收門檻。
- 未對齊：`大潭#8`、`大潭#9`、`興達新#1` 不存在目前機組主檔；`立霧` 可指向兩台機組。全數留在 `reports/outage_unmatched.txt` 供人工核對，沒有猜測。
- 下載問題與修正：Python 3.13 系統 CA 第一次拒絕官方端點的舊憑證鏈。改用 `certifi` 信任 CA bundle 後通過，未關閉 TLS 驗證。
- 官方資料警告：歲修第 102 列的開始日 `2027-12-20` 晚於結束日 `2027-02-23`。系統保留原值、設 `date_status=invalid_range`，不自行猜測正確年份。
- 資料陷阱：`meta_pitfall` 共 10 列：`RESIDUAL_TREND` 2、`PLANT_TOTAL_INCOMPLETE` 6、`KNOWN_CAPACITY_GAP` 2。
- 驗證：`python -m align`、重建 `power.db`、`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（15 passed）。
- 回退方式：回退 `feat: extract alignment layer and import outages` 這個 commit；CP-001／002 保持不動。

## CP-002 — Phase 1 SQLite 資料層

- 時間：2026-09-13 13:18 +08:00
- 狀態：已完成
- 範圍：資料下載與內容尋址封存、CSV schema／日期／數值／重複鍵驗證、SQLite 星狀模型、四個 `v_*` 語意檢視、`meta_manifest`、資料字典與品質報告。
- 驗收數字：22 座電廠、175 台機組、64 個原始出力欄位、43 個主檔對應、36,928 筆尖峰出力、577 筆系統日資料。
- 資料期間：2025-01-01 ～ 2026-07-31；來源檔、建庫內容與 schema 都有 checksum，重建內容冪等。
- Schema 決策：增加 `dim_b_column` 保留全部 64 欄，`bridge_b_column` 專注 43 個已對應關係，避免 B-only 的 21 欄在 fact 裡丟失。
- 資料異常與修正：52 台機組的商轉日期只有月精度（`YYYYMM`）。系統保留原值與 `month` 精度，並以當月 1 日作可排序值，沒有偽造不存在的精確日期。
- 資料陷阱：建庫時由 crosswalk 導出 2 個殘差趨勢、6 個電廠總量不完整、2 個容量缺口，共 10 列，沒有寫死對象清單。
- 驗證：`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（7 passed）；SQLite `quick_check=ok`，四個 views 齊全。
- 產物：`data/processed/power.db` 為可重建、gitignored 產物；`reports/data_quality.json` 保留本次驗證結果。
- 回退方式：回退 `feat: build validated SQLite semantic layer` 這個 commit；Phase 0 與使用者原有變更保持不動。

## CP-001 — Phase 0 專案地基

- 時間：2026-09-13 13:12 +08:00
- 狀態：已完成
- 範圍：`pyproject.toml`、`uv.lock`、`Makefile`、`configs/`、`src/` 套件骨架、離線 CI、`SYSTEM_CARD.md`、`.env.example`。
- OpenAI 設定：依官方 Responses API 與模型文件預留 `OPENAI_MODEL`，線上套件放在選用 `online` extra，CI 不需 API key。
- 驗證：`uv sync --extra dev`、`ruff format --check .`、`ruff check .`、`pytest -q` 全數通過（2 passed）。
- 回退方式：回退 `feat: scaffold phase 0 project foundation` 這個 commit；不影響 CP-000 列出的使用者變更。
