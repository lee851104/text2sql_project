# PowerQuery TW 開發進度

> 這份檔案在每個可驗證、可回退的儲存點更新。回退前需保留使用者原有的未提交變更。

## CP-031 — 名冊盤點指令（唯讀）

- 時間：2026-09-19 18:06 +08:00
- 狀態：已完成
- 需求：要能查「目前有哪些帳號、各自什麼範圍」。稽核日誌記的是**做過事的帳號**，不是**存在的帳號** —— 從沒操作過的帳號不會出現在任何紀錄裡，所以撈紀錄看到的是活躍帳號而非名冊。要看名冊只能看名冊。
- 刻意不做的事：**沒有新增或修改帳號的介面。** 能在介面上建帳號的人就能建一個 `scope: all` 的帳號，再用它核准自己提的變更 —— CP-028 的四眼原則當場失效。權限提升藏在一個看起來無害的功能裡，是自建帳號管理最典型也最隱蔽的錯誤。密碼設定維持 `echo -n 'pw' | python -m serving.accounts hash`：密碼從 stdin 進、雜湊從 stdout 出，中間沒有任何一段會進入 log、錯誤訊息或瀏覽器。
- 處理：
  - 抽出 `describe_bindings()` 作為綁定規則的唯一定義處，逐筆回報問題而不是在第一個問題就拋例外。`resolve_plant_names()` 改成建立在它之上的從嚴版本（一有問題就失敗），兩者不再各寫一份判斷。既有錯誤訊息逐字不變，既有測試未修改即通過。
  - 新增 `render_roster()`：輸入名冊與電廠對照，輸出報表文字與離開碼。輸入可注入，所以測得動而不需要資料庫或檔案系統。
  - CLI 改成子指令：`hash`（預設，行為不變）與 `list`。
- 離開碼的語意：0 = 全部綁定對得上；1 = 有綁定對不上，**或讀不到資料庫因而無法驗證**。後者刻意不回 0 —— 未驗不等於通過，與專案合併門檻的判定原則一致。名冊不存在回 0，因為那是支援的部署方式（沿用單一管理員帳號），不是錯誤。
- 新增測試（8 筆）：一次回報所有壞掉的綁定而不是只報第一個、全部正常時的乾淨輸出、名冊不存在視為支援模式、資料庫讀不到回報「未驗證」且離開碼非零、報表不得出現任何密碼材料、未知子指令與多餘參數回 2、`--help` 回 0。
- 文件：README 帳號綁定段落補上指令與「為什麼沒有新增帳號介面」；`configs/accounts.example.yaml` 補上兩個指令。
- 驗收：`ruff format --check .`、`ruff check .` 通過；`pytest -q` **348 passed**（CP-030 後為 340，新增 8 筆，無回歸）。
- 回退方式：回退 `feat: add a read-only roster inventory command` 這個 commit。`resolve_plant_names` 的對外行為與訊息不變，服務端不受影響。

## CP-030 — 電廠帳號不得使用管理功能

- 時間：2026-09-19 17:52 +08:00
- 狀態：已完成
- 問題：CP-027 把帳號綁上電廠範圍，收窄了「看得到哪些列」，但**沒有收窄操作權限**。`AdminRead`／`AdminMutation` 只驗身分不看範圍，所以一個電廠帳號登入後可以使用全部管理端點：上傳與審核資料變更、切換執行模式並注入 OpenAI API key、審核語料，以及下載原始來源檔。
- 實證（修正前）：以林口帳號登入後 `GET /api/data/files/units_csv` 回 **200、21,364 bytes**，也就是完整的跨廠機組主檔。`scope_guard` 只管 SQL 改寫，管理端點根本不經過它 —— 等於整套授權可以從管理介面繞過去。
- 處理：新增 `_require_all_plants`，衍生 `ManageRead`／`ManageMutation`，套用到 18 個管理路由（training-status、runtime/llm ×2、corpus ×3、data ×11、raw/rebuild）。`plant_id` 不為 None 即回 403。`Admin*` 保留為純身分驗證，只供 `/api/admin/session` 使用 —— **登出必須對任何已登入帳號開放**，把它一起擋掉會讓電廠帳號無法結束自己的階段。
- 規則的一句話版本：**電廠帳號是資料使用者，不是系統管理者。** 語料補充的審核資格也由這條決定，不需要另外一套判斷。
- 新增測試（3 筆）：電廠帳號對 9 個讀取端點與 3 個異動端點全數 403；電廠帳號仍可登出；全權限帳號的管理權限不受影響。第一筆在修正前以 `/api/data/status` 回 200 失敗。
- 文件：README 的帳號綁定段落補上這條規則與理由。
- 驗收：`ruff format --check .`、`ruff check .` 通過；`pytest -q` **340 passed**（CP-029 後為 337，新增 3 筆，無回歸）。
- 回退方式：回退 `fix: keep plant accounts out of the management API` 這個 commit。回退後管理端點恢復為只驗身分。

## CP-029 — 代理後面的來源判斷，與語料晉升的職責分離

- 時間：2026-09-19 17:26 +08:00
- 狀態：已完成
- 範圍：清掉 CP-028 列的兩項未處理。

### 一、信任代理與真實來源

- 問題：`request.client.host` 在反向代理後面是代理自己。公開流量走 Tailscale Funnel → `127.0.0.1:8766`，所以**所有外部訪客在服務眼中都是 `127.0.0.1`**。後果有二：登入限速變成全域共用一桶（任何人打錯 5 次就鎖住所有管理員）；`_is_loopback_client` 會把外部訪客判成本機，使「預設帳密只准本機使用」形同虛設。後者目前沒爆，只是因為公開啟動程序會先設強帳密。
- 處理：新增 `AdminAuthManager.client_address()`。只有當直連對端本身在 `POWERQUERY_TRUSTED_PROXIES` 裡時才讀 `X-Forwarded-For`，由右往左跳過信任代理，第一個非代理位址即為來源。**未設定信任代理時完全忽略該 header** —— 否則任何人都能自己填來源位址，一次繞過限速與 loopback 兩道。任何一段無法解析就回報來源未知（限速進 unknown 桶、loopback 判定為否），從嚴不猜。登入限速與預設帳密檢查都改用這個結果。
- 反向驗證：把 `admin_auth.py` 還原成修正前版本（補上回傳對端位址的 `client_address` shim），7 筆新測試中 **6 筆紅**，包含「代理後的外部訪客可用預設帳密」與「共用代理的兩個訪客共用限速桶」。唯一在修正前後都綠的是「沒設信任代理時不採信 XFF」，那是迴歸護欄不是抓蟲工具。

### 二、語料晉升的職責分離

- 先確認結構再決定做法：語料候選由系統從成功查詢自動抓下（`source` 是 `router`／`llm`），**沒有人類提案人欄位**。直接把 CP-028 的規則搬過來是照搬，因為沒有可比對的對象。
- 真正的缺口是實質的：任何人問一個問題讓管線成功，那個問答就成為待審語料；若這個人同時是管理員，他就能核准自己引發的語料進正式語料庫。系統原本連「是誰讓它進來的」都沒記，所以連要套規則都沒有依據。CP-027 已讓 `/api/query` 有身分，因此現在記得起來。
- 處理：**先記錄，再管制**。候選新增 `proposed_by`（由 `/api/query` 的登入身分帶入，經同一套 `deidentify`）；`review()` 在核准且 `approved_by == proposed_by` 時擋下，丟 `CorpusSelfApprovalError`（`ValueError` 子類，API 對應 403，except 順序排在泛用 ValueError 之前），並寫入 `candidate_self_approval_refused` 事件。共用 `POWERQUERY_ALLOW_SELF_APPROVAL` 覆寫。
- 已知邊界（刻意，不是遺漏）：匿名查詢記為 `None` 且不套此規則。匿名不是一個身分，兩個不同訪客都會長一樣，拿來比對只會擋到不相干的人，也擋不住真的想繞的人（登出、問、再登入）。這條寫進 README 與測試名稱，不留在程式碼裡讓人自己發現。
- 相容性：`proposed_by` 缺席的舊候選取值為 `None`，規則不觸發，無需 bump `CANDIDATE_SCHEMA`。

- 新增測試（13 筆）：`test_admin_auth.py` 7 筆（未設代理時忽略 XFF、僅信任對端才採信、取最右側非代理、無法解析回報未知、代理後的外部訪客不得用預設帳密、共用代理的兩訪客分屬不同限速桶、代理設定格式錯誤即拒絕）；`test_corpus_learning.py` 6 筆（記錄 proposed_by、提案帳號不得晉升、換帳號可晉升、可駁回自己的候選、匿名候選在規則外、覆寫涵蓋語料晉升）。
- 文件：README 新增「反向代理後面的來源判斷」與語料規則差異說明；`PUBLIC_OFFLINE_SERVING.md` 新增「反向代理後面要設信任來源」；`.env.example` 補上 `POWERQUERY_TRUSTED_PROXIES`。
- 未動公開啟動程序：是否真的能取到來源，取決於代理有沒有送 `X-Forwarded-For`。這點未實測，文件寫成「設定後仍需代理確實送出該 header 才生效」，沒有替 Tailscale 背書。
- 驗收：`ruff format --check .`、`ruff check .` 通過；`pytest -q` **337 passed**（CP-028 後為 324，新增 13 筆，無回歸）。
- 回退方式：回退 `feat: resolve the real client behind a proxy and extend four eyes to the corpus` 這個 commit。未設 `POWERQUERY_TRUSTED_PROXIES` 時來源判斷與回退前相同；舊語料候選沒有 `proposed_by`，規則不觸發。

## CP-028 — 四眼原則：提案人不可核准自己的變更

- 時間：2026-09-19 17:02 +08:00
- 狀態：已完成
- 問題：`stage_*` 記了 `created_by`、`review()` 記了 `reviewed_by`，但兩者從未比對。核准在 docstring 裡寫明是「發布邊界」，實際上一個帳號就能跨過去 —— 那兩個欄位記的是同一件事，精心設計的稽核鏈（checksum、atomic write、conflict 偵測）證明不了任何分工。
- 軸的區分：資料範圍（CP-027）管「看得到什麼」，這條管「誰能讓東西上線」，是兩條不同的軸，不該混成一個等級階梯。
- 處理：
  - 新增 `SeparationOfDutiesError`，API 對應 **403**（不是混進既有的 409，語意不同）。
  - `review()` 在 `approve` 且 `created_by == reviewer` 時拒絕。**駁回不受限制** —— 撤回自己的提案不會讓任何東西上線。
  - 被擋下的自審寫入稽核鏈（`self_approval_refused`），不是靜默失敗。
  - 單人部署覆寫 `POWERQUERY_ALLOW_SELF_APPROVAL`（預設關閉）。開啟後每一筆自審在**變更紀錄與稽核事件兩處**標上 `self_approved: true`，所以「沒有第二個人看過」不會因為設了環境變數就消失。逃生口是明寫且留痕的，不是把規則關掉。
- 為什麼選「預設強制＋可稽核覆寫」而不是「有第二個帳號時才強制」：後者的逃生口是結構性的 —— 刪掉第二個帳號就自動恢復自審，不需要任何人明確決定，也不會留下痕跡。
- 新增測試（5 筆，`tests/test_data_management.py`）：自審被拒且變更維持 `pending_review`、作用中版本不變；拒絕事件進稽核鏈；換一個帳號可核准且 `self_approved` 為 false；自己駁回自己允許；覆寫後仍標記 `self_approved`。
- 改寫 `tests/test_data_management_api.py`：原本一個管理員從頭做到尾，改成 `data-uploader` 提案、`data-reviewer` 審核的兩帳號流程（用 CP-027 的名冊），並在中間斷言提案人自審回 403 且資料未上線。稽核鏈上提案與發布是兩個不同的名字，被擋下的那次也查得到。這個端到端測試現在本身就是四眼原則的證明。
- 同步修正 `tests/test_runtime_consistency.py` 三處：原本 `actor` 與 `reviewer` 同名，改為不同名。那些測試測的是熱抽換一致性，不是授權。
- 文件：README 新增「職責分離」小節；`PUBLIC_OFFLINE_SERVING.md` 新增「資料發布需要第二個人」（公開程序建立的是一組共用帳密，也就是一個帳號，預設無法自行發布，必須建第二個帳號或設覆寫）；`.env.example` 補上新環境變數。
- 驗收：`ruff format --check .`、`ruff check .` 通過；`pytest -q` **324 passed**（CP-027 後為 319，新增 5 筆，無回歸）。
- 未處理：`corpus_learning.review()` 的語料晉升也是一個發布邊界，目前未套用同一條規則；登入限速仍以 `request.client.host` 分桶，走反向代理時所有外部訪客共用一桶。
- 回退方式：回退 `feat: require a second account to publish a data change` 這個 commit。回退後 `review()` 恢復為不比對提案人與審核人，既有變更紀錄與稽核鏈不受影響（`self_approved` 欄位只是多出來的鍵）。

## CP-027 — 帳號名冊與電廠範圍接上查詢路徑

- 時間：2026-09-19 16:38 +08:00
- 狀態：已完成
- 問題：`scope_guard` 的兩級授權（`plant`／`all`）自 CP-022 起就有完整實作與測試，但 `/api/query` 從來沒有傳入 `plant`，`Pipeline.query` 的預設是 `None`＝全廠。也就是說整套授權改寫沒有任何 HTTP 入口會觸發，能力只存在於測試裡，產品裡看不到。
- 處理：
  - **名冊**（`src/serving/accounts.py`）：`configs/accounts.yaml`，每個帳號有 username、PBKDF2 雜湊、`scope: all | <plant_id>`。檔案已加入 `.gitignore`；版控只留 `configs/accounts.example.yaml`，與 `.env.example` 同一套慣例。附 `python -m serving.accounts` 由 stdin 讀密碼產生雜湊，密碼不會進命令列歷史。
  - **綁定用 plant_id 不用名稱**：名稱會改，編號是建庫時釘住並逐筆比對過漂移的識別碼。名冊可選填 `plant_name`，啟動時與 `dim_plant_scope` 比對，編號與名稱對不起來就拒絕服務 —— 建庫層漂移偵測在授權層的延伸。
  - **多帳號驗證**：`AdminAuthManager` 改為持有帳號表，新增 `from_roster`。單一帳號路徑（環境變數）完全不變，兩者互斥 —— 兩套帳號來源同時有效會讓「誰能登入」取決於載入順序。登入仍是一次 PBKDF2 加一個假憑證比對，未知帳號與錯密碼維持同一條失敗路徑。
  - **接上查詢**：`/api/query` 取得可選身分後帶入 `plant`。**沒有 cookie 才算匿名；帶了但已失效回 401**，不能悄悄退回匿名範圍，否則電廠帳號 session 一過期權限是往上跳。
  - **堵住 raw 繞道**：電廠帳號的 `query_scope` 只能是 `trusted`。`/api/raw/*` 與 `auto` fallback 不經 `ScopeGuard`，開放給電廠帳號等於留一條繞過授權的路。
  - **越權留痕與回應語意**：`SCOPE_DENIED` 寫入稽核日誌；電廠帳號拿到空結果時附 `scope_notice`，明講「可能是超出授權範圍，不代表資料不存在」。底層是公開資料，藏起邊界沒有保護作用，只會讓人一直重問，所以選擇講明 —— 這是刻意做的取捨，不是預設。
  - **匿名政策**：`POWERQUERY_ANONYMOUS_QUERY_SCOPE`＝`all`（預設，維持公開展示現況）或 `denied`。預設不變更現有行為，但把這個選擇從隱含變成明寫。
- 新增測試（20 筆，`tests/test_account_scope.py`）：名冊格式與重複帳號、scope 型別、雜湊格式的負向測試；**編號查不到**與**編號還在但已是另一座廠**兩條綁定失敗；名冊帳號各自帶著自己的範圍；名冊與單一帳號互斥；以及四筆端到端 —— 同一問句兩種帳號結果不同、電廠帳號讀得到自己廠、失效 session 回 401 不放寬、電廠帳號的 raw／auto 回 403。
- 可展示證據：`uv run python scripts/demo_plant_scope.py`。問「列出台中發電廠所有設備」，全權限帳號 **14 筆**、林口帳號 **0 筆**；林口帳號問自己的廠 **3 筆**。輸出印出實際送進 SQLite 的 SQL，可看到 `FROM (SELECT * FROM v_unit WHERE "電廠" IN (?))`，證明限制在後端執行。
- 同步修正：三處測試替身的 `query()` 補上 `plant` 參數（替身簽名與真實 `Pipeline.query` 不一致會掩蓋呼叫端改動）；`test_foundation` 的 configs 清單加入 `accounts.example.yaml`。
- 驗收：`ruff format --check .`、`ruff check .` 通過；`pytest -q` **319 passed**（CP-026 後為 299，新增 20 筆，無回歸）。
- 未處理：資料上傳與審核仍是同一個帳號可以自己審自己（四眼原則尚未加上）；登入限速仍以 `request.client.host` 分桶，走反向代理時所有外部訪客共用一桶。
- 回退方式：回退 `feat: bind accounts to a plant scope and apply it to queries` 這個 commit。名冊不存在時服務行為與本 commit 前相同（單一管理員、全廠範圍），既有環境變數設定不受影響。

## CP-026 — 授權範圍未涵蓋的檢視改為拒絕

- 時間：2026-09-19 16:10 +08:00
- 狀態：已完成
- 問題：`ScopeGuard.apply` 原本寫成「不在 `SCOPE_KEYS` 的資料表就原封不動回傳」，這是 fail-open。`SHARED_VIEWS` 雖然列了三張不受管的檢視，但全專案只有定義那一行、沒有任何地方讀它，等於註解而不是機制。目前 6 張檢視剛好被兩個集合蓋滿是巧合；只要新增一張帶電廠欄位的檢視並加進 `sql_guard.ALLOWED_COLUMNS`，電廠帳號就會讀到它的全部列，沒有錯誤訊息、測試也不會紅。
- 對照：建庫層（`build_db.py`）早就是 fail-closed —— 新的每日欄位沒指定 `access_scope` 就中止建庫。查詢層少了同一道。這次把兩層對齊。
- 處理：
  - `SHARED_VIEWS` 從宣告變成機制：改寫時先放行不受管檢視，兩個集合都查不到就丟 `UnclassifiedViewError`（繼承 `ScopeRewriteError`，沿用既有的 `SCOPE_DENIED` 拒絕路徑）。
  - `_allowed_values` 移除 fallthrough。原本任何非 `v_unit`／`v_peak` 的檢視都會拿到 `outage_ids`，等於用錯的欄位值當授權範圍；改為明確判斷 `v_outage`，其餘丟錯。
  - 只影響電廠帳號路徑；`plant=None`（全權限）維持原樣不改寫。
- 新增測試（5 筆）：
  - `test_every_queryable_view_has_an_authorisation_classification`：`ALLOWED_COLUMNS` 的鍵必須與 `SCOPE_KEYS ∪ SHARED_VIEWS` 完全相等。這是涵蓋性不變式，涵蓋**未來新增**的檢視，不需資料庫，每一層測試都會跑。
  - `test_a_view_cannot_be_both_managed_and_shared`：兩個集合互斥。
  - `test_database_views_match_the_authorisation_classification`：`power.db` 實際的 6 張 `v_*` 與分類表相符。
  - `test_unclassified_view_is_refused_instead_of_passed_through`：未分類檢視必須拒絕。
  - `test_managed_view_without_a_value_rule_is_refused`：列進 `SCOPE_KEYS` 卻沒有可見列規則時必須拒絕。
- 反向驗證：把 `scope_guard.py` 還原成修正前版本（僅補上例外類別以便匯入）後重跑，後兩筆行為測試以 **DID NOT RAISE** 失敗，確認修正前確實是靜默放行、不是本來就會擋。前三筆涵蓋性測試在修正前後都綠 —— 它們是給未來的迴歸護欄，不是今天的抓蟲工具，這點不混為一談。
- 驗收：`ruff format --check .`、`ruff check .` 通過；`pytest -q` **299 passed**（修改前基準 294，新增 5 筆，無回歸）。
- 未處理（留待後續儲存點）：帳號名冊與 `plant` 綁定尚未建立，因此 `/api/query` 仍以全權限範圍執行，本次修正保護的是接上帳號之後的路徑；`/api/raw/*` 與 `query_scope="raw"` 仍完全不經 `ScopeGuard`，電廠帳號的處置另案處理。
- 回退方式：回退 `fix: refuse views that carry no authorisation classification` 這個 commit。授權對照表、建庫流程與全權限查詢路徑都不受影響。

## CP-025 — 發電成本資料源納入可重現下載

- 時間：2026-09-19 11:02 +08:00
- 狀態：已完成
- 問題：`taipower_align/generation_cost.csv` 是八份原始資料中唯一不在 `ingest.fetch` 的 DATASETS 裡的一份。它沒有資源代號、沒有 `data/raw/` 副本、也沒有 checksum 紀錄，等於人工放進版控後就無從驗證是否與官方一致，也無法重抓。
- 查到的來源：資料集 10856「台灣電力公司各種發電方式之發電成本」，識別碼 313310000K-000014，資源代號 `d018001`，官方標示每年更新。API 回報 26 筆、四個欄位，與本地檔完全相同。
- 處理：加入 `src/ingest/fetch.py` 的 DATASETS。實際下載後與既有檔逐行比對（忽略換行符差異）**內容完全相同**，1,019 bytes，SHA-256 `73a574486ef6…`。證實人工放的那份就是官方原檔，沒有被改過。
- 結果：八份原始資料現在全部可由 `uv run python -m ingest.fetch` 重現，並全部具備內容定址封存與 manifest 記錄。
- 文件：README 資料來源表的發電成本列補上 data.gov.tw 連結。
- 驗收：`uv run python -m ingest.fetch` 八份全數下載；`python -m ingest.build_db` 成功；`ruff format --check`、`ruff check` 通過；`pytest -q` **290 passed**，無回歸。
- 回退方式：回退 `feat: fetch the generation cost dataset instead of tracking it by hand` 這個 commit。既有的 `taipower_align/generation_cost.csv` 內容不變，建庫路徑不受影響。

## CP-024 — 資料血緣盤點與一處數字更正

- 時間：2026-09-19 03:46 +08:00
- 狀態：已完成（僅文件）
- 盤點：以 8 份原始資料各一組「回溯 + 獨立查證」agent 逐一追出來源、清洗規則與產出檔案，並自行清點 `taipower_align/`（22 檔 + source_raw/）、`power.db`（18 表 6 檢視）與 `configs/`（8 個 yaml）。
- 更正：CP-020 記載「去掉結尾『發電站』後提升至 61 個」有誤，實測為 **62 個**（場址主檔 64 個不重複站名、發電量檔 65 個、交集 62）。後續的 64 對齊與 65 覆蓋不受影響，因為那兩個數字是另外量出來的。`README.md` 與 `docs/DATA_ROADMAP.md` 已更正；CP-020 依 append-only 慣例保留原文，以本則更正。
- 同時修正 `docs/DATA_ROADMAP.md` 的分類錯誤：原本把 `彰工風力Changgong Wind Power` 列為「需人工裁決」的四組之一，實際上 `chinese_part` 剝除結尾拉丁字母串後它自動對上，屬程式處理。真正的人工裁決只有高訓中心與龜山加壓站兩組，中屯風力則是補充檔。
- 一致性查核：語料 `corpus/training_corpus.json` 的 6 條 DDL 與 `power.db` 實際檢視欄位逐欄比對，6/6 完全一致（v_peak 12 欄、v_re_generation 10 欄）。
- 觀察但未處理：`re_sites.csv` 有兩筆容量是 149,999 與 99,999 瓩（台南鹽田、彰化彰濱），數字貼著整數門檻。這是案場為法規門檻刻意設計的常見做法，非資料缺陷，故不標記，但記錄備查。
- 回退方式：回退 `docs: correct the station alignment figure and lineage notes` 這個 commit。

## CP-023 — 無機組主檔欄位的裝置容量補齊

- 時間：2026-09-18 22:15 +08:00
- 狀態：已完成
- 缺口：64 個每日出力欄位中有 21 個在 `units.csv` 找不到對應機組（核能 6、IPP 12、汽電共生 1、風光彙總 2），沒有分母就算不出容量利用率。
- 新資料源：`ingest.fetch` 加入 `d056001`（核能發電廠位置及機組設備，資料集 10858）。`d006001` 的 JSON 另存 `units_generation.json` 供容量對照使用。
- 對照方式：`configs/b_column_capacity.yaml` 逐欄明寫來源，不用字串比對。理由是前綴比對會把「台中#1」配到「台中#10」、「大林(#5-#6)」配到「大林#1」，先前實測已出現這類誤配。
- 結果：21 欄補到 18 欄（核能主檔 6、8931 明細 9、8931 小計 3），`v_peak` 的容量覆蓋從 43／64 提升到 61／64。`dim_b_column` 新增 `capacity_kw` 與 `capacity_source`，`v_peak` 新增 `容量來源` 欄位。
- 快照而非主檔：8931 每 10 分鐘覆寫，機組除役會直接從清單消失——本次快照的民營電廠-燃煤只剩和平 #1／#2，麥寮已不在其中。因此 `capacity_source` 對 8931 來源另附當次快照的 SHA-256 前綴，可回溯容量是哪一刻的狀態。
- 補不到的 3 欄：麥寮 #1／#2／#3。8931 即時清單已無該機組，台電簡明月報表 4-1／4-3 只按能源別彙總、沒有逐廠容量，因此維持 NULL 並在設定檔記錄原因，不從舊快照或第三方網站臆測。
- 容量比檢查：填入的容量套用與 crosswalk 相同的 `expected_max=1.06` 門檻，超標者寫入 `KNOWN_CAPACITY_GAP`。實測命中兩欄——星彰#1（107%，瞬時略高於銘牌屬正常）與汽電共生（332%，8931 登記的是售予台電的契約容量，與每日欄位量的整體出力口徑不同）。若不檢查，「汽電共生容量利用率」會算出 332% 這種荒謬數字。
- 其他修正：`dim_b_column` 的 INSERT 原本未指名欄位，加欄位即失敗，改為明確列出。SCHEMA_VERSION 升至 5。
- 驗收：`ruff format --check`、`ruff check` 通過；`pytest -q` **290 passed**（新增 14 項）；`python -m ingest.build_db` 回報 21 configured／18 filled／0 unknown_columns；`python -m eval.run_eval` 意圖 100%、執行 100%、語意陷阱 100%，無回歸。
- 未完成：8931 `備註` 欄的 15 種運轉狀態尚未建維度；火力與核能仍無發電量資料（需 `d006010`）。
- 回退方式：回退 `feat: fill installed capacity for B columns without a unit master` 這個 commit 並重建資料庫；CP-020～022 不受影響。

## CP-022 — 再生能源資料表、語意檢視與範圍守門

- 時間：2026-09-18 21:51 +08:00
- 狀態：已完成
- 補充檔：新增 `taipower_align/re_sites_supplement.csv`。`中屯風力發電站` 在 17141 場址主檔缺漏，依台電 11307 簡明月報第 8 頁補上 4,800 瓩、8 部機組、澎湖縣與「112.10.19起安全性停機」，並記錄來源網址。官方原檔維持不可變，補充資料獨立成檔，`source` 欄位讓兩者在查詢結果中可分辨。
- 對齊率與覆蓋率分開：`summarize_alignment` 新增 `supplemented` 狀態與 `coverage_rate`。對齊率 98.5%（兩個官方檔真正對上的 64 站）與覆蓋率 100%（含補充的 65 站）分別報告，避免把補上的講成對上的。
- Schema v4：新增 `dim_re_site`（一列一發電站，17141 同站多場址在入庫時彙總）與 `fact_re_monthly`（一站一月，`generation_kwh` 為淨發電量、缺值存 NULL，`value_status` 保留解析結果），以及語意檢視 `v_re_generation`。兩表同時納入表計數與資料庫內容 checksum。
- 資料源：`ingest.fetch` 的三份新資料接入 `configs/config.yaml` 與建庫流程；`_insert_renewable` 對找不到主檔的發電量列採取回報並略過，不建立臆測對應。實測 65 站、1,976 月、0 筆無主檔。
- 範圍守門：`RENEWABLE_SELF_BUILT_ONLY` 以 `meta_pitfall` 的 global 列驅動，問句層攔截「全國／全台／各縣市 + 再生能源 + 發電量」的問法，SQL 層則對任何觸及 `v_re_generation` 的查詢附上揭露。涵蓋範圍約為全國風光地熱的 3～4%，不附範圍的答案會小一個數量級。
- 查詢面：`v_re_generation` 加入 `SqlGuard` 欄位白名單、`ScopeGuard` 的 `SHARED_VIEWS`（再生能源場站不在 34 筆電廠主檔的組織範圍內）與 `app.py` 的來源追溯對照；語料新增 1 條 DDL、2 條領域文件與 2 個範例。
- 離線可用：`router.py` 新增 `renewable_generation` 與 `renewable_site` 兩個零成本 intent，公開離線展示不需 API key 即可回答。只說「風力」時涵蓋陸域與離岸，不替使用者挑一種。
- 驗收：`ruff format --check`、`ruff check`、`git diff --check HEAD` 通過；`pytest -q` **276 passed**（新增 7 項對齊測試與 6 項守門測試）；`python -m align` status=pass；`python -m eval.run_eval` 意圖 100%、執行 100%、語意陷阱 100%，無回歸。實機以 CLI 驗證三個問句，均回傳資料且帶 `RENEWABLE_SELF_BUILT_ONLY` 揭露。
- 未完成：8931 尚未用於補 `dim_b_column` 的 21 個無主檔欄位；火力與核能仍然沒有任何發電量資料。
- 回退方式：回退 `feat: serve台電自建再生能源發電量 with scope disclosure` 這個 commit 並重建資料庫；CP-020／CP-021 的清洗與對齊層不受影響。

## CP-021 — 再生能源踩坑紀錄與缺值查證

- 時間：2026-09-18 21:21 +08:00
- 狀態：已完成（僅文件，未動程式或資料）
- README：新增「資料整理踩坑：兩個檔案講同一批電廠，卻對不起來」，以既有踩坑段落的體例寫四個坑——小計混在明細、站名兩檔不一致、單格壞值的查證過程、涵蓋率僅 3～4%。供後續成果簡報取用。
- 交叉驗證擴大樣本：以 11307 簡明月報表 2-2 比對 2024-07 共 8 個可對應站別，其中 7 站的 17140 數值與月報「淨發電量」欄完全相同，第 8 站即已修復的澎湖湖西風力。確認該欄是淨發電量而非毛發電量，非單一樣本巧合。
- 缺值查證：16 筆缺值中 15 筆屬 `中屯風力`（有值的 6 個月全為 0 度，11307 月報顯示 #1~#8 當月亦全為 0），1 筆屬 `澎湖龍門風力` 2024-02（11302 月報表 2-2 顯示三部機組毛發電量、廠用電量、淨發電量皆為 0，較上年同期 -100%）。
- 結論：不需要再取得其他月份月報。兩者都是機組確實未發電，補齊不改變任何加總；缺值維持 NULL 不改寫為 0，以區分「沒有資料」與「發電量為零」。
- 附記：月報逐機組列出全部電廠（含火力與核能）的毛／淨發電量，是 `d006010` 之外另一條補足發電量的路，但屬於每月一份、七十餘頁 PDF 的解析工程，已記入計畫文件但不列入本階段。
- 驗收：`ruff format --check`、`ruff check`、`git diff --check HEAD` 均通過；未改動程式，測試結果與 CP-020 相同。
- 回退方式：回退 `docs: record the renewable data pitfalls and missing-value findings` 這個 commit；CP-020 的清洗與對齊層不受影響。

## CP-020 — 再生能源兩份開放資料的清洗與對齊層

- 時間：2026-09-18 21:12 +08:00
- 狀態：已完成（清洗與對齊層；尚未建立資料表與語意檢視）
- 資料源：`ingest.fetch` 新增 `d693002`（再生能源各場址）、`d693001`（自建再生能源發電量）與 `d006001`（各機組發電量即時資訊）。`Dataset` 增加 `suffix` 欄位，因為 `d006001` 只提供 JSON；封存副檔名改為跟隨 `suffix`，既有三個 CSV 資料源行為不變。
- 清洗規則：新增 `src/align/renewable.py`，全部是無 I/O 純函式——雙語字串取中文側、剔除小計列、地址解析縣市、發電量分類為 ok／missing／suspect／invalid、站名正規化與對齊。
- 實測結果：場址主檔 97 列中 4 列為小計，明細 93 列容量合計 757,960 瓩（97 列直接加總會得 1,514,419 瓩，重複近一倍）；縣市解析 93／93 成功；發電量 1,976 列中 1,959 ok、16 missing、1 repaired。
- 名稱對齊：去掉「發電站」後綴後，65 站中 64 站對齊，對齊率 98.5%（未處理前僅 10 站）。`configs/renewable_overrides.yaml` 記錄兩組人工確認的別名（高訓中心、龜山加壓站），依據是以裝置容量推算的容量因數 15.0% 與 12.6%，均落在合理範圍。
- 未對齊：`中屯風力` 在發電量檔有 21 個月但場址主檔沒有它。台電 11307 簡明月報表 2-2 證實該站存在（#1~#8，當月全為 0 度），判定為主檔漏收，保留原名標記 `generation_only`，不建立對應。
- 壞值處理：`澎湖湖西風力` 2024-07 原始值為 `357,156.00 2`。解析器一律標為 suspect 且不列入加總；經月報交叉驗證（六機毛發電量 382,722 度 − 站級廠用電 25,566 度 = 357,156 度）後，以 overrides 記錄修復並附完整依據。同一驗算也確認本欄是淨發電量而非毛發電量。
- 產物：`taipower_align/re_station_crosswalk.csv`（65 列，含狀態、能源別、縣市、場址數、容量、月數、可加總度數與缺值月數）；`reports/alignment.json` 新增 `renewable` 區段，對齊率低於 90% 時整份報告判定 fail。
- 驗收：新增 `tests/test_renewable_align.py` 34 項，案例全部取自官方真實列；`uv run python -m align` 回報 status=pass；`ruff format --check`、`ruff check`、`pytest -q` 全過（262 passed）。`tests/test_foundation.py` 的設定檔清單同步加入新的 overrides 檔。
- 未執行：`dim_re_site`、`fact_re_monthly`、`v_re_generation` 與限定「台電自建」範圍的 disclose 規則都還沒做；8931 只完成下載與封存，尚未用於補 `dim_b_column`。
- 回退方式：回退 `feat: clean and align the renewable open-data files` 這個 commit；既有資料庫、語意檢視與查詢行為完全未動。

## CP-019 — 三份台電開放資料的實測盤點與擴充計畫

- 時間：2026-09-18 19:55 +08:00
- 狀態：已完成（僅文件，未動程式或資料庫）
- 範圍：新增 `docs/DATA_ROADMAP.md`，並在 README 的「資料來源」加入規劃中資料與陷阱警告。
- 實測方式：2026-09-18 直接下載三份官方檔案後分析，數字不取自詮釋資料。`d693002` 18,647 bytes、`d693001` 163,945 bytes、`d006001` 36,437 bytes，均為 UTF-8 with BOM。
- 17141 場址主檔：97 列，其中 4 列是小計。93 筆明細裝置容量合計 757,960 瓩，4 筆小計合計 756,459 瓩，97 列直接加總得 1,514,419 瓩，重複計算近一倍。單位為瓩，與 `units.csv` 相同。申設狀態只有 63 筆「取得執照」。
- 17140 發電量：1,976 列，2024-01～2026-07 共 31 個月，65 個不重複發電站，單位為度，全期合計約 3,485 GWh。這是目前唯一能合法回答「發電量」的來源。
- 名稱對齊：兩檔以 `發電站名稱` 直接 join 只有 10 個相符；去掉結尾「發電站」後提升至 61 個。剩餘 4 組需人工裁決，其中 `彰工風力Changgong Wind Power` 是官方資料漏掉雙語分隔符的 bug。
- 8931 更正：官方 JSON 頂層帶 `DateTime`（實測 `2026-09-18T19:40:00`），先前依資料集頁面匯出的 CSV 判斷「無時間戳記」並不適用於 JSON 端點。`aaData` 215 列，其中 10 列為小計，`備註` 有 15 種運轉狀態。
- 涵蓋範圍限制：17140／17141 僅含台電自建案場，合計約 758 MW，約為全國風光地熱的 3～4%。接入時必須以 `disclose` 強制附範圍說明。
- 未執行：三份資料都尚未進 `data/raw`、`schema.sql` 或任何 `v_*` 檢視，本次只產出計畫。
- 回退方式：回退 `docs: add data expansion roadmap for three Taipower datasets` 這個 commit；不影響任何程式、資料庫或既有文件內容。

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
