# GridLens TW 使用方式

## 啟動

### Windows 一鍵啟動

安裝好 [uv](https://docs.astral.sh/uv/) 後，直接雙擊專案根目錄的 [`啟動.bat`](../啟動.bat)。啟動器會自動：

1. 從批次檔所在的專案目錄執行，因此 OneDrive、中文或含空白路徑均可使用。
2. 首次使用或 `pyproject.toml`／`uv.lock` 改變時同步開發與本機 LLM（`local`）依賴。
3. 建立必要目錄，並只在 `data/processed/power.db` 不存在時建庫。
4. 啟動 <http://127.0.0.1:8765/>，健康檢查成功後才開啟預設瀏覽器；若服務已在執行則直接沿用。

服務視窗需保持開啟；要關閉服務時，在該視窗按 `Ctrl+C`。若 Windows 接著詢問 `Terminate batch job (Y/N)?`，輸入 `Y`。啟動失敗時視窗會保留錯誤提示，不會立即消失。

### 手動啟動

第一次使用需安裝依賴並建立可重現的 SQLite 快照：

```powershell
uv sync --extra dev
uv run python -m project_tasks init-dirs
uv run python -m ingest.build_db
uv run powerquery --serve
```

開啟 <http://127.0.0.1:8000> 即可使用。只需要離線規則時不必安裝額外依賴；要讓本機 Ollama 處理長尾查詢時改用：

```powershell
uv sync --extra dev --extra local
```

Windows 手動重建 `data/processed/power.db` 前，請先在服務視窗按 `Ctrl+C`。仍在執行的服務、SQLite 檢視器或同步程式可能鎖住檔案。網頁內的「資料管理」使用不可變資料庫版本與原子切換，不覆寫目前正在使用的資料庫，因此展示熱插拔時不需要停止服務。

命令列查詢不需要管理員登入：

```powershell
uv run powerquery "2026年7月備轉容量率最低是哪一天？"
uv run powerquery --json "天然氣機組共有幾台？"
```

`.env.example` 是環境變數參考，不會被程式自動載入；請透過 PowerShell、服務管理器或容器環境注入需要的值。

## 即時資料收集器

> 收集器只負責收資料；總覽頁的面板（RT-3a）與查詢中心的即時問句（RT-3b，見下方「[用中文查即時資料](#用中文查即時資料)」）唯讀使用它。設計見[即時機組發電量設計規格](superpowers/specs/2026-09-24-realtime-ingest-design.md)。

收集器是獨立的常駐程式，每 10 分鐘向台電抓一次「各機組發電量即時資訊」（`d006001`）。原始回應先 gzip 封存到 `data/realtime/archive/`，再寫進 `data/processed/realtime.db`。它和網頁服務互不相干：重啟服務不影響收集，收集器也不必跟著服務一起啟動。每小時的維護（重新套用人工決定、對帳、彙總與清除）若出錯只會記錄，不會中斷抓取。

### 啟動與停止

- 雙擊 [`即時收集啟動.bat`](../即時收集啟動.bat)：開一個視窗持續收集，異常結束時 60 秒後自動重啟；同一時間只會有一份在跑。
- 登入後自動啟動：按 `Win + R` 輸入 `shell:startup`，把 [`開機自動啟動-即時收集.bat`](../開機自動啟動-即時收集.bat) 的捷徑放進去。電腦睡眠或關機期間收不到資料，那些時段會記成缺口。
- 雙擊 [`停止即時收集.bat`](../停止即時收集.bat)，或在收集器視窗按 `Ctrl+C`。

手動操作：

```powershell
uv run python -m ingest.realtime status    # 健康狀態；加 --json 給程式讀
uv run python -m ingest.realtime once      # 立刻抓一次、做一次維護就結束
uv run python -m ingest.realtime rebuild   # 從封存重建 realtime.db（收集器必須先停）
uv run python -m ingest.realtime stop      # 要求收集器停止，最多等 60 秒
uv run python -m ingest.realtime history import <d006010.json>  # 匯入歷史（收集器必須先停）
uv run python -m ingest.realtime history download               # 下載最新 d006010（約 200 MB）再匯入
```

`status` 的結束碼：0 正常、1 落後超過 30 分鐘、2 收集器沒在跑或資料庫不存在。`run`／`once`／`rebuild` 遇到未預期的錯誤時，會以繁體中文印出原因並以結束碼 1 結束；`once` 這一次抓取失敗時也以結束碼 1 結束。結束碼 3 表示已有另一份收集器拿著鎖，啟動批次檔把它當成正常停止，不會重啟。設定檔有誤時，每個指令都會印出「設定錯誤」並以結束碼 1 結束。

### 檔案

| 位置 | 內容 |
|---|---|
| `data/realtime/archive/` | 每份內容不同的原始回應，gzip 永久保存，是重建時的真實來源；重建與對帳時讀不了的封存檔會被略過 |
| `data/realtime/attempts/` | 每次抓取嘗試一行 JSON：成功、304、失敗（含封存寫入失敗，記為 `ArchiveError`）、拒收、睡眠後醒來 |
| `data/processed/realtime.db` | 10 分鐘明細（保留 14 天）、每日估算發電量（永久）、缺口紀錄 |
| `logs/realtime-collector-*.log` | 收集器的執行紀錄，由收集器自己寫入（批次檔不轉存輸出） |
| `taipower_align/realtime_units.csv` | 每條序列的人工決定：個別或彙總、屬於哪座電廠 |
| `data/realtime/history/archive/` | 匯入過的 `d006010` 原檔（gzip），重建時一併重放 |
| `taipower_align/realtime_history_units.csv` | `d006010` 序列 → 即時機組的人工對照 |

`data/` 與 `logs/` 都不進版控。

### 升級

停止收集器 → 更新程式 → 重新啟動收集器（schema 版本不符時會自動從封存重建）→ 重啟網頁服務。修改 `realtime_units.csv` 不需要重建，收集器每小時會自動套用；如果決定檔或電廠檔壞了（例如用 Excel 另存成非 UTF-8），只會記一則警告，繼續使用上一份好的決定。

### 歷史回補（d006010）

台電的「各機組過去發電量」（`d006010`）每月更新一次，涵蓋約三個月，實測**晚約四個月**（2026-09-30 更新的檔案是 2026-03-01～05-31）。所以它補的是收集器啟動以前的日子，不會和即時收集重疊；兩者之間的空檔補不回來。每月匯入一次就能把歷史往後延。

- **收集器每天自動檢查一次**（`configs/realtime.yaml` 的 `history.check_hours: 24`，0 表示關閉）：只發 HEAD 看台電的版本標記，有新版才下載（約 200 MB）並匯入，原始下載檔匯入後刪除。失敗只記錄，隔一天再試。手動的 `history import`／`download` 照樣可用。
- 匯入只存每日彙總，並封存原檔；`rebuild` 會連歷史一起重放。重複匯入同一份檔案不會有任何變化。
- 同一天同一部機組兩邊都有時，取樣點數多的為準，一樣多取即時。
- `d006010` 沒有民營電廠與購電彙總列：問「四月風力發了多少度」只會是台電自有的那部分，答案會附上這個限制。
- 設計見 [RT-2 規格](superpowers/specs/2026-10-25-realtime-history-design.md)。

### 資料治理

即時快照不走資料管理的四眼審核：每 10 分鐘就有一筆，逐筆由人核准並不可行。四眼原則改套在規則上：解析程式、`realtime_units.csv` 與 `configs/realtime.yaml` 的變更一律走 PR 審查，每一筆快照則由自動驗證放行。結構有問題的快照不入庫（原始回應仍然封存），數值有問題的照收並標記品質。

## 網頁工作台

左側導覽包含五個工作區：

- **查詢中心**：自然語言查詢、單次執行模式、常用問題，以及只保存非敏感問句的瀏覽器本機歷史。
- **資料總覽**：即時發電（收集器狀態、各類型總出力、今日趨勢）、資料期間、尖峰紀錄、機組與歲修筆數，以及主題式分析入口。
- **資料管理**：登入後管理資料檔、待審資料異動、語料審查、資料庫版本與雜湊鏈稽核紀錄。
- **本機 LLM**：登入後設定 `offline`、`local`、`auto` 與 Ollama 模型，並看到 Ollama 目前是否連得上。
- **API 文件**：本機端點與請求範例；完整 schema 位於 `/openapi.json`。

手機版導覽具備焦點鎖定與畫面外隱藏。Plotly CDN 無法載入時會使用相同資料的本機 SVG，不會顯示撐大的失效圖片。

首頁與 `/static/` 的檔案都帶 `Cache-Control: no-cache`：瀏覽器每次載入都會向服務確認，檔案沒變只回 304，
升級後一定拿到新版前端，不會出現新頁面配上舊 `app.js` 的情況。`index.html` 引用的檔案另帶 `?v=` 版本參數，
用來讓加上這個標頭之前就快取了舊檔的瀏覽器改抓新網址。

## 即時發電面板

「資料總覽」頁最上方的「即時發電」區塊，顯示即時收集器（見「即時資料收集器」一節）收進 `realtime.db` 的資料：

- **狀態列**：燈號綠色＝收集器在跑、資料是新的；黃色＝資料落後超過 30 分鐘（`configs/realtime.yaml` 的 `status.stale_after_minutes`）；灰色＝收集器沒在跑，或還沒有資料。文字列出最新時段（台灣時間）、落後幾分鐘、今天收到幾個時段。
- **各類型總出力**：最新時段各發電類型的淨發電量、裝置容量與通訊異常的機組數。只加總「正常」的值；儲能（放電）與儲能負載（充電）分列，不相抵。
- **今日趨勢**：今天每 10 分鐘各類型的出力；沒抓到的時段會斷線，不畫成 0。圖下可以展開數字表。

頁面開著、且分頁在前景時，每 60 秒更新一次；切到其他頁或分頁在背景就暫停。按「重新整理」會立刻更新。資料本身每 10 分鐘才變一次。收集器更新資料後，不必重啟網頁服務。

權限與查詢一致：

| 身分 | 看得到 |
|---|---|
| 管理員、全電廠帳號 | 全部機組，包括歸屬未定的 |
| 電廠帳號 | 自己電廠的機組與跨廠共用（shared）的列，分「本廠」「共用」兩欄；歸屬未定的機組看不到 |
| 訪客（`POWERQUERY_ANONYMOUS_QUERY_SCOPE=all`） | 全部 |
| 訪客（`denied`） | 只有狀態列，沒有數字 |

- 面板不經過 Text2SQL，用寫死的 SQL；查詢中心的即時問句見下一節。
- 收集器沒在跑時，面板照常顯示最後一筆，並標出「收集器目前沒有在執行」。網頁服務不會啟動或停止收集器。
- API：`GET /api/health` 的 `realtime` 欄位只有狀態，任何人都看得到；`GET /api/realtime/overview` 回傳面板數字，套用上表的權限。電廠帳號遇到兩邊電廠名冊不一致時回 409 `RT_SCOPE_MISMATCH`；`realtime.db` 讀不出來時回 503。

## 用中文查即時資料

問句帶「現在、即時、此刻、剛剛、今天、今日、昨天、昨日、前天」，或「目前、最新」加上出力／發電，就改查 `realtime.db`，
不經過歷史快照的守門與路由。例：

- 現在風力發多少電？／現在各類型發電量／現在台中#6出力多少？／現在出力最高的前 5 部機組
- 現在哪些機組在歲修？／現在哪些機組通訊異常？／現在台中發電廠出力多少？
- 今天太陽能估計發了多少度電？／今天大潭CC#8最高出力／昨天燃煤發電量
- 匯入 `d006010` 之後：2026年4月台中#5發電量／2026年4月1日到4月10日燃氣發電量

規則：

- **分流看用詞，不看機組名**：兩邊有約 21 個同名機組（例如 `台中#1`）。「哪一些機組目前維修中」「資料最新一天的尖峰負載」
  沒有出力用詞，照舊查歷史快照。
- **只查 `v_rt_*`**：同一句 SQL 混用 `power.db` 的檢視回 `RT_CROSS_DATABASE`（兩邊的機組名稱沒有對照）。
- **權限與面板相同**：電廠帳號只看到自己電廠與 shared 的機組，歸屬未定的看不到；兩邊電廠名冊不一致回 `RT_SCOPE_MISMATCH`。
- **答案一定附資料時間**，另依情況揭露：落後或收集器停止、品質警告、彙總／未定列、發電量是估算（10 分鐘瞬間值 ÷ 6）、
  資料完整度低於 90%、儲能與儲能負載相抵、今天還沒過完、問到收集開始以前（`RT_BEFORE_FIRST_DAY`）。
- **指名的機組或類型不在即時資料裡**（例如已除役的 `台中#1`、`核能`）回 `RT_NOT_IN_REALTIME` 並建議相近名稱，不會改答全系統合計。
- 規則接不住的問法（最低、平均、比較、趨勢⋯）交給 LLM；離線模式回 `RT_CLARIFY` 並給例句。
- 即時 SQL **不進語料學習**（結果每 10 分鐘就變）。`/api/coverage` 的 `realtime` 欄位列出即時資料的起始日與最新時段。

### 本機 LLM（Ollama）

這個專題唯一的 LLM。不連外網、不需要 API key：

1. 安裝 [Ollama](https://ollama.com)，`ollama pull qwen3:8b`。
2. `uv sync --extra local`（Ollama 的 OpenAI 相容端點用的 client；`啟動.bat` 會自動做）。
3. 啟動服務。預設 `auto` 偵測得到 Ollama 就用；要換推理模式設 `POWERQUERY_LLM_PROVIDER`，換模型用 `OLLAMA_MODEL`。

| provider | 推理 | 規則接不住的 20 題 | 每題時間 | 逾時 |
|---|---|---|---|---|
| `ollama`（建議） | 關 | 答對 6 | 中位數 11 秒 | 120 秒 |
| `ollama-reasoning` | 開 | 答對 10 | 中位數約 6 分鐘，7 題寫不完 | 600 秒 |

數字來自 `python -m eval.llm_eval`（見 README「本機 LLM 評測」）。兩種模式都會檢查 LLM 寫的 SQL：值不在欄位的值清單、
`LIKE` 沒有萬用字元、數字被當成字串、0 筆或被 LIMIT 截斷時，把原因交回模型重寫。

`auto` 模式每 30 秒最多確認一次 Ollama 在不在（只問 `/v1/models`，1 秒逾時）；在就用本機 LLM，不在就退回離線規則。
規則接得住的題目照樣不打 LLM。`local` 模式不偵測：Ollama 沒開時 LLM 那一步明確失敗（`LLM_UNAVAILABLE`），規則與澄清照常運作。
公開服務以 `POWERQUERY_RUNTIME_MODE=offline` 固定只用規則。

## 資料管理登入

本機展示預設帳號為 `admin`，預設密碼為 `PowerQuery@123`。這組帳密是公開的 demo 預設，只允許 loopback 用戶端登入，不能視為正式部署的安全邊界。正式使用必須在啟動前同時覆寫：

```powershell
$env:POWERQUERY_ADMIN_USERNAME="你的管理帳號"
$env:POWERQUERY_ADMIN_PASSWORD="至少八字元的長密碼"
uv run powerquery --serve
```

兩個帳密變數必須同時存在；只設定其中一個會拒絕啟動。另可設定：

- `POWERQUERY_ADMIN_SESSION_TTL_SECONDS`：絕對有效期，允許 300～3600 秒，預設 900 秒。
- `POWERQUERY_ADMIN_ALLOWED_HOSTS`：逗號分隔的可信任 hostname/IP，不含 port；一般瀏覽器使用預設只允許 `127.0.0.1`、`localhost` 與 `::1`。

登入密碼只用於伺服器端 PBKDF2-HMAC-SHA256 驗證，前端不會寫入 `localStorage` 或 `sessionStorage`。成功後瀏覽器取得 host-only、`HttpOnly`、`SameSite=Strict`、`Path=/api` 的短期 cookie；後端只保存 opaque token 與 CSRF 值的 SHA-256 digest，服務重啟會撤銷所有 session。HTTPS 時 cookie 另帶 `Secure`。

所有管理寫入除了 cookie，還必須帶目前 session 的 `X-PowerQuery-CSRF`，並通過精確同源 `Origin` 檢查。登入回應與每次 `GET /api/admin/session` 會提供新的 CSRF 值；後者會立刻使舊值失效，因此 API 用戶端應永遠採用最近一次回傳值。管理回應帶 `Cache-Control: no-store`。

PowerShell 可用記憶體內的 `WebRequestSession` 呼叫管理 API，不必把 cookie 寫入檔案：

```powershell
$origin = "http://127.0.0.1:8000"
$webSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
$originHeaders = @{ Origin = $origin; "Sec-Fetch-Site" = "same-origin" }
$loginBody = @{
  username = "admin"
  password = "PowerQuery@123"
} | ConvertTo-Json
$loginParams = @{
  Uri = "$origin/api/admin/session"
  Method = "Post"
  WebSession = $webSession
  Headers = $originHeaders
  ContentType = "application/json"
  Body = $loginBody
}
$login = Invoke-RestMethod @loginParams
$csrf = $login.data.csrf_token
$writeHeaders = @{
  Origin = $origin
  "Sec-Fetch-Site" = "same-origin"
  "X-PowerQuery-CSRF" = $csrf
}
```

未登入或 session 失效回 401；Origin 或 CSRF 錯誤回 403；登入失敗過多回 429 並附 `Retry-After`。錯誤不會回顯密碼、cookie、CSRF 或 API key。

## 資料熱插拔、資料庫版本與稽核

資料管理不是任意 SQL 編輯器，也不接受使用者上傳 SQLite。它只管理五個有 schema 約束的 UTF-8 CSV 資料槽，並透過既有 deterministic builder 重建候選資料庫：

| 資料槽 | 檔名 | 可新增／替換 | 可移除 |
|---|---|:---:|:---:|
| `units_csv` | `units.csv` | 是 | 否 |
| `daily_csv` | `daily.csv` | 是 | 否 |
| `crosswalk_csv` | `crosswalk.csv` | 是 | 否 |
| `daily_long_csv` | `daily_long.csv` | 是 | 否 |
| `outage_csv` | `outage.csv` | 是 | 是 |

上傳上限為 64 MiB。系統會檢查副檔名、UTF-8、NUL、重複／缺少欄位、空資料及跨檔資料品質，再建立候選 SQLite、來源 manifest 與 checksum。上傳、移除、回退和重建都只建立 `pending_review` 變更，不會立刻影響查詢；只有登入者按下人工核准後，才會驗證候選 checksum、原子切換 runtime 與 active pointer。拒絕、建置失敗、舊基線衝突及發布失敗都保留紀錄。

適合展示的熱插拔流程：

1. 登入「資料管理」，對 `outage_csv` 建立移除異動。
2. 在「待審異動」查看基線版本、候選版本、筆數與 checksum，按核准。
3. 回查詢中心問「2026年三月有哪些機組在歲修？」；新的 `v_outage` 應無資料。
4. 上傳原本或更新後的 UTF-8 `outage.csv`，再進行人工核准。
5. 重問相同問題；新請求會立即使用新資料庫。「版本」與「稽核紀錄」可調閱完整過程。

管理 API：

- `GET /api/data/status`：作用中版本、資料槽、變更統計及稽核鏈狀態。
- `GET /api/data/files`：作用中來源 manifest；`GET /api/data/files/{dataset}?version=...` 可下載指定版本的來源檔。
- `GET /api/data/changes`、`GET /api/data/changes/{change_id}`：調閱資料異動。
- `POST /api/data/changes/upload`：以 `content_base64` 建立新增／替換候選。
- `POST /api/data/changes/remove`：建立移除候選；目前只有 `outage_csv` 可移除。
- `POST /api/data/changes/{change_id}/review`：人工核准或拒絕。
- `GET /api/data/versions`：不可變的已發布版本。
- `POST /api/data/versions/{version}/rollback`：建立回退候選；仍需再呼叫 review 才會套用。
- `POST /api/data/changes/rebuild`：用作用中的來源檔、現行建庫程式建立重建候選；仍需再呼叫 review 才會套用。作用中版本已是現行 schema 時回 409。
- `GET /api/data/events`：驗證過完整 hash chain 後回傳稽核事件。

資料版本 id 由「來源內容＋建庫程式的 schema 版本（`build_db.SCHEMA_VERSION`）」一起雜湊決定，所以同一組來源用新 schema 建出來是另一個版本，不會被當成篡改。升級程式後，服務照常以原本的作用中版本啟動；`/api/data/status` 的 `rebuild_required` 會變成 `true`（`active_build_schema` 與 `build_schema` 不同；舊版工作區的版本沒有記錄 schema，顯示 `null`），「資料管理」頁上方也會出現提示。按「建立重建異動」後由另一個帳號核准，新的資料表與檢視才會上線。改了 `schema.sql` 一定要把 `SCHEMA_VERSION` 加一，`tests/test_database.py` 會檢查。

執行期工作區位於資料庫旁的 `.powerquery-data/`，包含不可變來源、候選／已發布資料庫、變更 manifest、`active.json`、短暫的 build／mutation／publish journals、append-only `audit.jsonl`、獨立 `audit-head.json` 與錨點永久建立標記。build journal 綁定候選 DB 與確切來源 checksum；mutation journal 確保 staged、failed、rejected 狀態與稽核事件成對恢復；版本檔、核准異動與稽核事件會先落盤，`active.json` 才作為發布的最後 commit point。若程序中途終止，下次 transaction 會從 journal 完成或安全拒絕。稽核事件依操作記錄登入帳號、動作、版本或 checksum 等細節，並以 `previous_hash`／`event_hash` 串接；錨點可偵測整檔遺失、截尾，active revision 也會與發布事件交叉核對。若來源、DB、manifest、指標或稽核歷史遭修改，查詢與所有管理讀取／異動都會 fail closed，而不是默默接受。含 `password`、`secret`、`token`、`credential` 或 `api_key` 等欄位會在稽核邊界遮罩。

## 離線規則、本機 LLM 與自動模式

`RuntimeManager` 支援三種模式：

- `offline`：只使用已審核的規則路由與本機唯讀 SQLite，不呼叫任何模型；未涵蓋的長尾問題會清楚拒答或反問。
- `local`：保留相同 SQL 與語意護欄，規則未涵蓋時交給本機 Ollama（[configs/llm.yaml](../configs/llm.yaml) 的 `base_url`）。
- `auto`（預設）：偵測得到 Ollama 時同 `local`，否則同 `offline`。

啟動時的預設模式可用 `POWERQUERY_RUNTIME_MODE`（`offline`／`local`／`auto`）指定，打錯字會讓服務起不來而不是默默退回 `auto`。
登入後可在「本機 LLM」或 `PUT /api/runtime/llm` 切換模式與模型；狀態 API 回報 `local_available` 與端點位址。沒有任何金鑰或雲端端點可設定。

`GET /api/runtime/llm` 需要管理 session；`PUT` 另外需要最新 CSRF 與同源 headers。單次公開查詢仍可用 `execution_mode` 覆寫模式，且不變更預設設定：

```json
{
  "question": "2026年7月20日出力前五名機組",
  "execution_mode": "offline"
}
```

## 語料治理與來源調閱

每次透過 Web/API 成功完成的查詢都會交給隔離的語料學習服務評估。這裡的「學習」是新增 Text2SQL 檢索範例，不是背景微調模型。

- 規則路由與本機 LLM 產生的新候選一律先通過去識別、重複、benchmark 洩漏、SQL／語意、結果重播及檢索回歸檢查。
- 通過自動檢查的候選一律停在 `pending_review`；router 和 LLM 都沒有自動發布例外。只有登入者按人工核准，並以當下資料與語料基線重新驗證成功後，才會發布及熱重載檢索索引。
- 失敗或拒答不加入候選；無效或重複候選可直接成為 `rejected`／`ignored`，但絕不會因此進入 active corpus。
- 候選保存去識別問句、SQL、參數、來源、資料表、驗證結果、結果 checksum 及 `data_provenance`，不保存查詢結果 rows。來源資訊可追到資料版本、資料槽、顯示檔名、SHA-256、大小與語意 view。
- Email、token、台灣身分證、電話、長帳號及明確標示姓名會遮罩；需遮罩的 SQL／參數不可發布。
- 工作區位於 SQLite 旁的 `.powerquery-learning/`，不直接覆寫版控中的 canonical corpus，並以程序鎖及跨程序檔案鎖保護。

資料版本切換後會重建語料服務。資料庫基線變更會備份舊 active corpus、重建索引，並把先前已發布候選改回 `pending_review`，避免沿用舊資料答案。

相關管理 API：

- `GET /api/training-status`
- `GET /api/corpus/entries?state=all&limit=100`
- `GET /api/corpus/events?limit=100`
- `POST /api/corpus/entries/{candidate_id}/review`

以上端點都需要管理 session；review 另外需要同源與 CSRF。審核 body 只接受 `decision` 與可選 `note`，審核者名稱一律取自已驗證 session，不能由 request body 冒名。

## 介面與安全邊界

- 首頁／靜態資源、`/docs`、`/openapi.json`、`GET /api/health`、`GET /api/stats`、`GET /api/examples` 與 `POST /api/query` 保持公開；`GET`／`POST /api/admin/session` 用於查詢狀態與登入，其餘 runtime、語料及資料管理端點需要登入。
- 原始開放資料檔比語意檢視更嚴：`GET /api/raw/status`、`/api/raw/resources`、`/api/raw/resources/{id}/rows` 與 `POST /api/query` 的 `query_scope=raw` 都需要登入，且必須是全廠帳號（電廠帳號一律 403）。原因是原始檔不經 `SqlGuard` 與 `ScopeGuard`，給的是整份來源檔 —— 與 `/api/data/files/{dataset}` 同一類。`query_scope=auto` 在沒有這個權限時不會退回原始檔，而是回傳語意檢視自己的失敗結果。
- `/docs` 以 nonce CSP、固定版號與 SRI 載入 Swagger UI；第一次使用互動檢視器需要連至 jsDelivr。主工作台內建文件不需要該 CDN。
- 後端只允許受限的 `line`、`bar`、`scatter` 圖表；前端載入不到 Plotly 時退回原生 SVG。
- 瀏覽器取消按鈕只停止等待與顯示，不中止已在後端執行的查詢。
- 服務預設綁定 `127.0.0.1`。若要對外提供，必須覆寫預設帳密、限制 allowed hosts，並由可信任反向代理提供 TLS。
- 送給本機模型的 prompt 會帶上「封閉集合」欄位的值（值少於 25 個、非純數字、串起來不長的欄位，例如 `v_unit."燃料"`）。這些值從作用中資料庫現讀，隨資料更新而變。目的是讓模型用資料實際的值當參數，而不是從問句抄詞 —— 後者會產生結構正確但查不到東西的 SQL，回 0 筆且不報錯。機組名、日期、各種量測值不在此列，它們是資料而不是列舉。
- prompt 另外列出**同名而值不同**的欄位。目前有三組：`燃料` 在 `v_outage` 是「燃煤／燃氣」而在 `v_unit` 是「煤／天然氣」；`縣市` 在 `v_unit` 含鄉鎮而在 `v_re_generation` 只到縣市；`電廠` 在 `v_peak` 有殘差桶的複合值。光列出值不足以讓模型發現這些差異，所以比對由服務做掉並明講。粒度差異另外在語料的 `documentation` 有一條規則說明該怎麼處理。
