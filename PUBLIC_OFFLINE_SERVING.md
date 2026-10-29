# PowerQuery TW 公開離線服務

## 架構

公開流量採用以下路徑：

```text
訪客瀏覽器
  -> Tailscale Funnel HTTPS 8443
  -> http://127.0.0.1:8766
  -> PowerQuery TW（offline）
```

原本的本機服務仍使用 `http://127.0.0.1:8765/`。LH 專案使用的 HTTPS 443 與本機 8000 不會被變更。

公開程序啟動前會移除 `OPENAI_API_KEY`，並建立**兩組**強管理員帳密（`powerquery-admin-1`、`powerquery-admin-2`），兩者都有審核權。知道帳密的使用者可透過公開網址登入資料中心、審核與修改資料，或在執行環境介面輸入 OpenAI API key 啟用付費線上模型。API key 只保存在服務記憶體，服務重啟後需要重新輸入。

**公開網址的查詢需要登入。** 執行模式與 API key 是行程全域的，匿名可查等於任何訪客都在燒管理員輸入的那把 key，所以啟動程序會設 `POWERQUERY_ANONYMOUS_QUERY_SCOPE=denied`。

兩組帳密而不是一組，是因為四眼原則禁止提案人核准自己的變更：只有一個帳號時公開環境無法發布任何資料。兩個互為審核者，示範時也剛好能演完整流程（A 上傳 → A 自審被擋 → B 核准 → 資料上線）。

帳號名冊由啟動程序寫在 `.powerquery-public/accounts.yaml`，**不會寫進 `configs/accounts.yaml`** —— 那個檔案會同時改變本機服務的帳號來源，而且公開服務停掉之後還留著。名冊被刪掉時可用 `-Mode roster` 重建，不必啟動服務。

管理員帳密保存在 `.powerquery-public/admin-credentials.json`，這個資料夾已排除於版本控制。請只提供給獲准管理資料及使用付費模型的人。

### 反向代理後面要設信任來源

公開流量經 Tailscale Funnel 轉到 `127.0.0.1:8766`，因此**所有外部訪客在服務眼中都是 `127.0.0.1`**。不處理的話有兩個後果：登入限速變成全域共用一桶（任何人打錯 5 次就鎖住所有管理員），以及「預設帳密只准本機」的判斷會把外部訪客當成本機。

設定 `POWERQUERY_TRUSTED_PROXIES=127.0.0.1` 後，服務才會從 `X-Forwarded-For` 取真正的來源（由右往左跳過信任代理）。未設定時一律忽略該 header —— 否則任何人都能自己填一個來源位址。設定後仍需代理確實送出該 header 才會生效，可從服務日誌確認。

（目前公開啟動程序會設定強管理員帳密，所以預設帳密那條不會被觸發；限速分桶則會受影響。）

### 資料發布需要第二個人

服務套用四眼原則：**提案人不能核准自己的資料變更**（API 回 403）。啟動程序已經建立兩個互為審核者的帳號，所以照流程走即可：一個帳號上傳，另一個帳號核准。

駁回不受此限制 —— 撤回自己的提案不會讓任何東西上線。

真的要單人操作時可設 `POWERQUERY_ALLOW_SELF_APPROVAL=true`，變更紀錄與稽核鏈會把該筆標上 `self_approved`，事後查得出哪些發布沒有經過第二個人。

## 啟動與停止

雙擊 `公開離線啟動.bat`。成功後視窗會顯示可分享的 HTTPS 網址。

雙擊 `停止公開服務.bat`，只會關閉 PowerQuery 的 HTTPS 8443 Funnel 與本機 8766 程序，不會停止 LH 專案。

雙擊 `顯示公開管理密碼.bat` 可再次查看兩組帳號與密碼。

名冊被刪掉或要重新產生時：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\public_offline_service.ps1 -Mode roster
```

查詢狀態：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\public_offline_service.ps1 -Mode status
```

## 日誌

日誌位於專案的 `logs` 資料夾：

- `public-offline-launcher.log`：啟動、停止及 Funnel 錯誤。
- `public-offline-日期時間.stdout.log`：Uvicorn 存取紀錄。
- `public-offline-日期時間.stderr.log`：Python 與服務錯誤。

Text2SQL 查詢失敗的結構化紀錄仍位於 `data/processed/.powerquery-data/query-errors.jsonl`。本機登入資料中心後，可用「下載查詢錯誤 LOG」直接下載。

## Windows 登入後自動啟動

1. 按 `Win + R`，輸入 `shell:startup`。
2. 對 `開機自動啟動-公開離線.bat` 建立捷徑。
3. 把捷徑放入開啟的「啟動」資料夾；不要複製批次檔本身。

這種方式會在使用者登入 Windows 後啟動；電腦關機、睡眠、Tailscale 登出或網路中斷時，公開網址將無法使用。

## 公開前注意事項

- 公開 Funnel 的任何人只要知道網址就能存取查詢頁面；取得管理員帳密的人還能修改資料並設定付費模型。
- 請勿把 `.env`、API key、管理員密碼或完整服務日誌提供給公開訪客。
- OpenAI API key 啟用後，公開訪客選擇線上模式所產生的費用會計入該 API key 所屬帳戶。
- 如需回報查詢問題，優先提供資料中心下載的 `powerquery-query-errors.jsonl`；若服務無法啟動，再提供 `logs/public-offline-launcher.log` 與最新的 stderr log。
# 全量原始資料查詢

公開查詢頁的「資料範圍」可選擇：

- `可信資料`：預設模式，查詢已清理並具有語意規則的 `power.db`。
- `全量原始資料`：搜尋 204 筆原始資源目錄；輸入結果中的 `resource_id` 可讀取該資源內容。

原始資料目錄位於 `data/processed/raw_open_data.db`，只保存來源中繼資料、欄位與少量預覽，實際內容仍由 `data/raw/files` 惰性讀取，因此不會再複製約 928 MiB 的檔案。

新增 CSV 時，將檔案放進 `data/raw/inbox`，登入資料中心後呼叫 `POST /api/raw/rebuild` 重建目錄。新檔會成為獨立原始資源，不會自動和 `power.db` 的表合併；要升級為可信資料，仍須新增資料槽、欄位映射、驗證規則與專用 View。
