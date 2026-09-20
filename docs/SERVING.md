# PowerQuery TW 使用方式

## 啟動

### Windows 一鍵啟動

安裝好 [uv](https://docs.astral.sh/uv/) 後，直接雙擊專案根目錄的 [`啟動.bat`](../啟動.bat)。啟動器會自動：

1. 從批次檔所在的專案目錄執行，因此 OneDrive、中文或含空白路徑均可使用。
2. 首次使用或 `pyproject.toml`／`uv.lock` 改變時同步離線、線上與開發依賴。
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

開啟 <http://127.0.0.1:8000> 即可使用。只需要離線規則時不必安裝線上額外依賴；要啟用 OpenAI 長尾查詢時改用：

```powershell
uv sync --extra dev --extra online
```

Windows 手動重建 `data/processed/power.db` 前，請先在服務視窗按 `Ctrl+C`。仍在執行的服務、SQLite 檢視器或同步程式可能鎖住檔案。網頁內的「資料管理」使用不可變資料庫版本與原子切換，不覆寫目前正在使用的資料庫，因此展示熱插拔時不需要停止服務。

命令列查詢不需要管理員登入：

```powershell
uv run powerquery "2026年7月備轉容量率最低是哪一天？"
uv run powerquery --json "天然氣機組共有幾台？"
```

`.env.example` 是環境變數參考，不會被程式自動載入；請透過 PowerShell、服務管理器或容器環境注入需要的值。

## 網頁工作台

左側導覽包含五個工作區：

- **查詢中心**：自然語言查詢、單次執行模式、常用問題，以及只保存非敏感問句的瀏覽器本機歷史。
- **資料總覽**：資料期間、尖峰紀錄、機組與歲修筆數，以及主題式分析入口。
- **資料管理**：登入後管理資料檔、待審資料異動、語料審查、資料庫版本與雜湊鏈稽核紀錄。
- **API 與模型**：登入後設定 `offline`、`online`、`auto`、模型及程序記憶體內 API key。
- **API 文件**：本機端點與請求範例；完整 schema 位於 `/openapi.json`。

手機版導覽具備焦點鎖定與畫面外隱藏。Plotly CDN 無法載入時會使用相同資料的本機 SVG，不會顯示撐大的失效圖片。

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

上傳上限為 64 MiB。系統會檢查副檔名、UTF-8、NUL、重複／缺少欄位、空資料及跨檔資料品質，再建立候選 SQLite、來源 manifest 與 checksum。上傳、移除和回退都只建立 `pending_review` 變更，不會立刻影響查詢；只有登入者按下人工核准後，才會驗證候選 checksum、原子切換 runtime 與 active pointer。拒絕、建置失敗、舊基線衝突及發布失敗都保留紀錄。

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
- `GET /api/data/events`：驗證過完整 hash chain 後回傳稽核事件。

執行期工作區位於資料庫旁的 `.powerquery-data/`，包含不可變來源、候選／已發布資料庫、變更 manifest、`active.json`、短暫的 build／mutation／publish journals、append-only `audit.jsonl`、獨立 `audit-head.json` 與錨點永久建立標記。build journal 綁定候選 DB 與確切來源 checksum；mutation journal 確保 staged、failed、rejected 狀態與稽核事件成對恢復；版本檔、核准異動與稽核事件會先落盤，`active.json` 才作為發布的最後 commit point。若程序中途終止，下次 transaction 會從 journal 完成或安全拒絕。稽核事件依操作記錄登入帳號、動作、版本或 checksum 等細節，並以 `previous_hash`／`event_hash` 串接；錨點可偵測整檔遺失、截尾，active revision 也會與發布事件交叉核對。若來源、DB、manifest、指標或稽核歷史遭修改，查詢與所有管理讀取／異動都會 fail closed，而不是默默接受。含 `password`、`secret`、`token`、`credential` 或 `api_key` 等欄位會在稽核邊界遮罩。

## 離線、線上與自動模式

`RuntimeManager` 支援三種模式：

- `offline`：只使用已審核的規則路由與本機唯讀 SQLite，不呼叫遠端模型；未涵蓋的長尾問題會清楚拒答。
- `online`：保留相同 SQL 與語意護欄，規則未涵蓋時使用 OpenAI Responses API。
- `auto`：有可用 API key 時使用線上 runtime，否則退回離線模式。

API key 可在啟動前由 `OPENAI_API_KEY` 提供，模型可由 `OPENAI_MODEL` 指定；也可登入後在「API 與模型」或 `PUT /api/runtime/llm` 設定。經 API 設定的 key 只存在伺服器程序記憶體，重啟即失效，狀態 API 只回 `online_configured`。線上 adapter 使用 [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)，並設定 `store=false`。

`GET /api/runtime/llm` 需要管理 session；`PUT` 另外需要最新 CSRF 與同源 headers。單次公開查詢仍可用 `execution_mode` 覆寫模式，且不變更預設設定：

```json
{
  "question": "2026年7月20日出力前五名機組",
  "execution_mode": "offline"
}
```

## 語料治理與來源調閱

每次透過 Web/API 成功完成的查詢都會交給隔離的語料學習服務評估。這裡的「學習」是新增 Text2SQL 檢索範例，不是背景微調模型。

- 規則路由與線上 LLM 產生的新候選一律先通過去識別、重複、benchmark 洩漏、SQL／語意、結果重播及檢索回歸檢查。
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
- 服務預設綁定 `127.0.0.1`。若要對外提供，必須覆寫預設帳密、限制 allowed hosts，並由可信任反向代理提供 TLS。記憶體內 OpenAI API key 不等於 API 身分驗證。
