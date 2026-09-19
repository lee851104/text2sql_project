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

公開程序啟動前會移除 `OPENAI_API_KEY`，並建立一組共用的強管理員帳密。知道帳密的使用者可透過公開網址登入資料中心、審核與修改資料，或在執行環境介面輸入 OpenAI API key 啟用付費線上模型。API key 只保存在服務記憶體，服務重啟後需要重新輸入。

管理員帳密保存在 `.powerquery-public/admin-credentials.json`，這個資料夾已排除於版本控制。請只提供給獲准管理資料及使用付費模型的人。

### 資料發布需要第二個人

服務預設套用四眼原則：**提案人不能核准自己的資料變更**（API 回 403）。公開程序建立的是一組共用帳密，也就是一個帳號，因此上傳後無法自行發布。兩種做法：

- 建立 `configs/accounts.yaml`，至少放兩個帳號（格式見 `configs/accounts.example.yaml`），由不同人分別提案與審核。這是預期做法。
- 單人操作時設 `POWERQUERY_ALLOW_SELF_APPROVAL=true`。變更紀錄與稽核鏈仍會把該筆標上 `self_approved`，所以事後查得出哪些發布沒有經過第二個人。

駁回不受此限制 —— 撤回自己的提案不會讓任何東西上線。

## 啟動與停止

雙擊 `公開離線啟動.bat`。成功後視窗會顯示可分享的 HTTPS 網址。

雙擊 `停止公開服務.bat`，只會關閉 PowerQuery 的 HTTPS 8443 Funnel 與本機 8766 程序，不會停止 LH 專案。

雙擊 `顯示公開管理密碼.bat` 可再次查看共用帳號與密碼。

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
