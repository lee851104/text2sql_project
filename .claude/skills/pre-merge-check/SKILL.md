---
name: pre-merge-check
description: 分支要併入 main 前的合併門檻檢查。當使用者說「這個分支可以合併嗎」「合併前先檢查」「merge 前檢查一下」「檢查分支符不符合規範」「能不能併進 main」「幫我看這個 branch 有沒有問題」「pre-merge check」「合併檢查」，或在整理多個待合併分支時使用。依 docs/TEAM_4_ROLES.md 四人所有權表與 .github/workflows/ci.yml 門檻，判定 BLOCK（不可合併）／WARN（可合併但需人工確認）／PASS（符合要求），並產出修正說明 md 檔。
---

## Purpose（使用時機）

把 `docs/TEAM_4_ROLES.md` 文末「儲存點與合併檢查表」那七條人工檢查，變成每次合併前都跑得出同一份證據的自動門檻。

**用在：**
- 有分支要併進 `main`，要先確認符不符合規範
- 手上有多個待合併分支，要排出「哪些能併、哪些要先修」
- 想知道某個分支跨了哪幾位 Owner 的檔案、要不要交接單

**不要用在：**
- 只是想看 diff 或 commit 紀錄 —— 直接用 `git diff` / `git log`
- 還在開發中、根本還沒要合併的分支
- 要求「幫我合併」—— 本 Skill 只出報告，不執行 merge、push 或任何改動分支的動作

## Steps（任務流程）

1. **確認要檢查哪個分支。**
   使用者沒指定就先列出候選，不要自己挑：

   ```bash
   git branch -vv
   ```

2. **執行檢查。** 分支就是目前簽出的那個時，可以拿到含 CI 的完整結果：

   ```bash
   python .claude/skills/pre-merge-check/scripts/check_merge.py
   ```

   檢查別的分支（不會切換分支，CI 項目會標記為 SKIP）：

   ```bash
   python .claude/skills/pre-merge-check/scripts/check_merge.py <branch>
   ```

   **判定為 PASS 需要工作目錄乾淨。** 有未提交變更時，ruff／pytest 驗的是工作目錄而不是那個
   commit，所以會多一項「工作目錄狀態」WARN，判定最高只到 WARN。要拿 PASS 就先 commit 或 stash。

   比較基準預設用 `origin/<base>` 而不是本機分支 —— 本機 main 常常落後，拿它比會漏掉衝突。
   要確保遠端快照也是最新的就加 `--fetch`。

   常用選項：
   - `--base <branch>` 目標分支，預設 `main`（實際比較 `origin/main`）
   - `--fetch` 先 `git fetch`，確保比較基準是 origin 最新狀態
   - `--owner A|B|C|D` 宣告這次是哪位成員的工作；省略則自動推斷涉及哪些 Owner
   - `--no-ci` 只跑 git 靜態檢查，不跑 ruff／pytest／node
   - `--out <path>` 指定報告路徑，預設 `reports/merge_check/<分支>_<時間>.md`
   - `--json` 額外輸出結構化結果

   離開碼：`0`=PASS、`1`=WARN、`2`=BLOCK、`3`=執行錯誤。

3. **CI 項目出現 SKIP 時，回報「未執行」，不要說通過。**
   要拿完整結果就請使用者先切分支再跑：

   ```bash
   git switch <branch>
   python .claude/skills/pre-merge-check/scripts/check_merge.py
   ```

   切分支前先跑 `git status` 確認沒有未提交變更會被影響；有的話先問使用者，不要自作主張 stash。

4. **讀報告，逐項給可執行的修正動作。**
   BLOCK 項目照報告的「修正」欄說明怎麼做；WARN 項目要明講「需要誰來確認什麼」。
   判定依據與各項來源見 `references/merge_rules.md`，不要自己發明規則。

5. **跨 Owner 的 WARN，直接產出交接單草稿。**
   用 `docs/TEAM_4_ROLES.md` 的「跨組契約／交接單」格式（交接來源／接手 Owner／目的／目前證據／
   需要的公開契約／不得改變／驗收案例／依賴儲存點），把報告裡的檔案清單填進去。

6. **回報判定與報告路徑（見 Output Format）。**
   **BLOCK 未清光不得說可以合併。** 合併指令由使用者自己執行，本 Skill 不動 git。

7. **分工或受控檔案有變動時，只改 `references/ownership.json`**，不要改 `scripts/check_merge.py`；
   改完用 `python -c "import json,pathlib;json.loads(...)"` 確認 JSON 仍合法（指令見 `references/merge_rules.md`）。

## Output Format（輸出規格）

回報必須包含：

- **判定** —— `BLOCK` / `WARN` / `PASS` 三者之一，逐字附上，不要換成自己的說法
- **一句話結論** —— 能不能合併；BLOCK 要說「先不要合併」
- **BLOCK 清單** —— 每項：哪個檢查、失敗原因、具體修正指令
- **WARN 清單** —— 每項：需要誰確認什麼；跨 Owner 要附交接單草稿
- **未執行項目** —— 哪些 CI 檢查是 SKIP、為什麼，以及怎麼補跑
- **報告路徑** —— `reports/merge_check/<分支>_<時間>.md` 的實際位置

判定為 BLOCK 時，回覆不得出現「可以合併」「沒問題」等結論；直接附上 BLOCK 項目與修正步驟。
檢查腳本沒跑成功（離開碼 3）時，附上錯誤輸出，不要憑 diff 自行推測結果。
