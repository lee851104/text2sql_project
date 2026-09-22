# 台電資料 Release 清單（2026-09-13）

- 發布狀態：已公開（2026-09-13 16:46 +08:00）；2026-09-22 11:25 UTC 鏡像到現在的 repo
- Release：https://github.com/lee851104/text2sql_project/releases/tag/taipower-data-2026-09-13
- 原始發行：https://github.com/chenliyu0410/text2sql_project/releases/tag/taipower-data-2026-09-13（舊帳號，資產位元組相同）
- Tag commit：`da749ad6f93eae7d949b47e1d291c690a0e4cb29`
- Release tag：`taipower-data-2026-09-13`
- 名稱：台電開放資料與 PowerQuery 資料包（2026-09-13）
- 資料提供機關：台灣電力公司
- 抓取時間：`2026-09-13T12:42:33.5886416+08:00`
- 快照規模：186 個資料集、204 個資源；161 CSV、25 ZIP、12 XML、6 JSON
- 原始資源：973,960,428 bytes；204 成功、0 失敗、0 略過

## 發行資產

| 檔案 | ZIP 大小 | 成員數 | SHA-256 |
| --- | ---: | ---: | --- |
| `powerquery-documentation-20260913.zip` | 48,127 bytes | 11 | `fbf365353f520ef78f6668432aad21a25be2a62eb9f3467d96cca43c6d51baca` |
| `powerquery-ready-dataset-20260913.zip` | 1,216,918 bytes | 18 | `6bffc221a682c76908031cabb054f072174b69957b50408c5cbff8c64312a32a` |
| `taipower-open-data-snapshot-20260913.zip` | 182,029,567 bytes | 207 | `0a6c292b8b5e143ba488690eaea43a4572eef584fe1e5242af30aaf093035080` |
| `SHA256SUMS.txt` | － | 3 筆 | 發行層 checksum 清單 |

原始快照包含 204 個原始資源、`manifest.csv`、`manifest.json` 與 `ATTRIBUTION.md`。可直接查詢包含版控中的 `taipower_align/**`、`data/raw/**`、`data/archive/**`、`data/processed/power.db` 與顯名文件。文件包只從 README、資料字典、評估／守門／服務／系統／管線文件、API 契約、設計規格、對齊 README 白名單取樣，並附相同顯名文件。

## 驗證與排除

- 三個 ZIP 均以 Python `zipfile` 建立，啟用 ZIP64 與 UTF-8 檔名；CRC 全檔驗證通過。
- 原始包內 204 個 `files/` 成員與 `manifest.json` 的 `RelativePath` 逐筆完全一致，且 manifest 記錄 204 成功、0 失敗。
- 重新計算的 SHA-256 與上表及 `SHA256SUMS.txt` 一致。
- GitHub 上傳完成後回報的四個 asset 狀態均為 `uploaded`；三個 ZIP 的遠端 byte 大小與 SHA-256 digest 均和本機完全一致。
- 明確排除 `參考資料/00-05*.md`、`參考資料/power-analysis-ui.skill`、`data/processed/.powerquery-learning/**`、本機查詢／語料事件、`.env`、`*.key`、API 金鑰、虛擬環境、快取與 Git 內部資料。
- 文件白名單掃描未發現 API secret、台灣身分證格式或 10～16 位連續數字。
- 2026-09-22 鏡像：三個 ZIP 自舊 Release 取回，SHA-256 與版控中的 `releases/taipower-data-2026-09-13/SHA256SUMS.txt`
  逐檔相符；舊 Release 的 `SHA256SUMS.txt` 與版控那份內容也完全相同。上傳後再從新 Release 下載一次重算，同樣三檔相符，
  四個 asset 狀態均為 `uploaded`。新 repo 的 tag `taipower-data-2026-09-13` 指向同一個 commit `da749ad`。
  搬的是同一批位元組，不是重新打包的版本。

## 顯名、時效與權利

台電資料依[政府資料開放授權條款－第 1 版](https://data.gov.tw/license)顯名台灣電力公司；各資源名稱、更新時間、直接來源網址及授權保留於 manifest。這是固定時間快照，不是即時資料；部分官方資料包含基礎設施地址、座標、桿號或設備資訊，使用與再次發布時應審慎評估。

本 repo 目前沒有 `LICENSE`。台電資料的 OGL 1.0 不會自動延伸至專案程式或自寫文件，發布也不代表台灣電力公司為本專案背書。

如需完整撤回此公開發行，兩個帳號的 Release 與 tag 都要刪（先 `lee851104`，再 `chenliyu0410`），再回退 `chore: prepare Taipower data release` 與後續進度紀錄 commit；不需、也不應改動使用者原有的未提交檔案。
