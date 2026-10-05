# 即時機組發電量：整體架構與 RT-1 收集儲存設計

> 狀態：設計已逐段確認（2026-09-24，log.md CP-072）；RT-1 已實作（CP-073），本文已依實作結果同步。
> 範圍：整體架構，以及三個子專案中的 RT-1（收集與儲存）。RT-2、RT-3 各自另寫規格。
> 資料本身的分析（功率與能量的差別、與 `v_peak` 的涵蓋範圍比較、來源端實測）見
> [docs/REALTIME_INGEST.md](../../REALTIME_INGEST.md)。

## 1. 目標與範圍

### 1.1 要回答的兩類問題

- **「現在／目前」**：某部機組現在出力多少、哪些機組在歲修或環保停機、風力目前合計多少。
  現有資料最細到日，而且只記系統尖峰那一刻，這類問題答不出來。
- **一段期間的發電量（度）**：把每 10 分鐘的功率積分成能量。這是 `PEAK_SUM_ACROSS_DAYS`
  擋下的那一類問題，第一次有合法的資料來源。

### 1.2 已確認的決策

| 項目 | 決定 |
|---|---|
| 目的與順序 | 兩類都要。先做即時收集（RT-1），再用 `d006010` 補歷史（RT-2） |
| 執行環境 | 與公開服務同一台 Windows，會關機或睡眠。每天有缺口是常態，不是異常 |
| 電廠帳號權限 | 只看得到自己電廠的機組，依據是人工審核的「機組→電廠」對照；還沒決定的機組，電廠帳號看不到 |
| 做法 | 獨立的收集器行程，先封存原始回應再入庫；`realtime.db` 可以從封存重建 |
| 文件 | 本規格放 `docs/superpowers/specs/`；`docs/REALTIME_INGEST.md` 改成資料分析 |

### 1.3 RT-1 不做的事

- 任何查詢端的修改。檢視會建在 `realtime.db` 裡，但服務還查不到（RT-3）
- 匯入 `d006010` 與對帳（RT-2）
- 與 `v_peak`／`dim_b_column` 的機組名稱對照：沿用草稿結論，目前不規劃
- 每小時彙總層：之後需要時，從封存重建即可加入
- 推播通知

## 2. 已實測的事實

以下事實推翻或補充了草稿的前提，本設計以此為準。「快照」指 repo 內的
`taipower_align/units_generation.json`（`DateTime` 為 2026-09-18T21:40:00）。

| # | 事實 | 證據 | 對應的設計 |
|---|---|---|---|
| F1 | **有歷史可以回補** | 資料集 37331（`d006010`，各機組過去發電量）：每 10 分鐘淨發電量（瞬間值），欄位 `FUEL_TYPE`、`UNIT_NAME`、`DATETIME`、`NET_P`，每月更新、滾動約 3 個月。2026-09-24 以 HEAD 實測：JSON 189,826,367 bytes，Last-Modified 2026-08-24；同路徑的 CSV 回 404 | RT-2；缺口原因要分得出之後補不補得回來（§7.4） |
| F2 | **機組名稱不唯一** | 12 個名稱跨類型重複：11 部儲能同時出現在「儲能」與「儲能負載」，`其它台電自有` 同時出現在太陽能與風力 | 自然鍵是（清洗後類型, 名稱）（§5.2） |
| F3 | **小計有 11 列** | 風力的小計名為 `小計(註5)`；小計值的格式是 `16521.4(50.877%)`；11 類的明細加總與小計全部相等（差 0.00 MW，N/A 不計）；儲能負載沒有小計 | 以「小計」開頭判定，並逐類驗證（§6） |
| F4 | **明細裡也有彙總列** | `其它購電太陽能` 15,037.7 MW，佔太陽能容量的 97.7%；`汽電共生` 一列代表全部汽電共生，出力是登記容量的 294%；另有 `電池(註16)`、四區小水力、`離島其它(註4)`、`其它購電風力`、`其它購電小水力`、`購電地熱`、`其它台電自有` | 粒度由人工決定（§5.3） |
| F5 | **值的格式** | 容量 `-` 45 列、發電量 `N/A` 2 列、空備註是一個空白字元、出力比帶 `%`；`龍三風` 備註「通訊異常」、出力 0.0；出力比超過 100% 的 9 列屬正常 | 清洗規則與 `value_status`（§6.4） |
| F6 | **草稿的類型計數含小計** | 草稿寫風力 32、太陽能 22、儲能 12、其它再生能源 4；實際明細是 31、21、11、3 | 入庫時辨識，不靠查詢的人記得排除 |
| F7 | **風力是場站，太陽能幾乎全是彙總** | 風力 31 列多是具名風場（例如 `沃一風` 605.2 MW），只有 2 列是彙總；太陽能 21 列中有容量的只有 5 列，其中 `其它購電太陽能` 一列就佔 97.7% | 粒度標籤用「個別／彙總」，不用「單機」（§5.4） |
| F8 | **發布延遲** | 草稿實測 5:08–5:09；2026-09-24 HEAD：08:50 的資料 Last-Modified 為當地 08:55:09 | 時段開始後 5:20 第一次請求（§4.1） |
| F9 | **部署機沒有時區資料庫** | Python 3.14.6：`ZoneInfo("Asia/Taipei")` 拋出 `ZoneInfoNotFoundError`；`uv.lock` 沒有 `tzdata` | 固定 UTC+8（§4.4） |
| F10 | **兩個服務行程** | 本機 8765 與公開 8766 可以同時執行，共用 `data/processed`；服務本身沒有任何背景執行緒 | 收集器是獨立行程（§4） |
| F11 | **受控版本會拒絕持續寫入的檔案** | 每次請求都重算 active 資料庫與來源檔的 checksum，檔案被改動會回 503「版本資料庫已被篡改」 | `realtime.db` 不進受控版本（§3.1） |
| F12 | **查詢路徑假設只有一個資料庫** | `ReadOnlySQLite`、`SemanticGuard`、`ScopeGuard`、`column_values` 都綁定同一個檔案；`SqlGuard` 禁止 `ATTACH`；`ScopeGuard` 遇到未分類的檢視會拒絕 | RT-3 加第二個執行器，依檢視分派（§8.5） |

另外發現一個與本設計無關、現在就會答錯的問題：「即時備轉容量率」回傳 2025-01-01 起的 200 天，
`disclosures` 是空的。已另案處理，不在本規格範圍。

## 3. 整體架構

```text
        d006001（每 10 分鐘）                         d006010（每月，RT-2）
                │                                            │
┌───────────────▼─────────────────────────┐                  │
│ 收集器：常駐、只跑一份、登入後自動啟動      │     月匯入 → 封存 → 對照 → 寫入
│ 排程 → 條件式請求 → 封存 → 解析驗證 → 寫入  │                  │
│ 啟動時與每小時：補彙總 → 清除               │                  │
└───────┬─────────────────────────┬───────┘                  │
        ▼                         ▼                          │
data/realtime/archive/     data/processed/realtime.db ◀──────┘
（原始回應，永久保存）      （WAL；可從封存重建）
                                  │ 唯讀
                  ┌───────────────┴───────────────┐
              服務 8765                       服務 8766
       power.db 執行器 ＋ realtime.db 執行器（依檢視分派，禁止跨庫 JOIN）
```

### 3.1 四條原則

1. **只有收集器會寫入。** 兩個服務都以唯讀開檔，不輪詢、不彙總、不清除。
2. **封存是唯一的真實來源。** `data/realtime/` 底下的封存與抓取紀錄，加上版控裡的設定檔，
   足以重建整個 `realtime.db`。
3. **收集不會因為缺少人工決定而停下來。** 沒見過的機組照常寫入並標成「未定」：
   電廠帳號看不到，健康狀態會報出數量。
4. **與 `power.db` 完全分開。** 不進受控資料版本，不走四眼審核。四眼原則改套在「規則」上：
   解析程式、人工決定檔與設定的變更走 PR 審查；每一筆快照由自動驗證放行。這一點要寫進
   SERVING（RT-1，給操作的人看）與 SYSTEM_CARD（RT-3，使用者查得到即時資料時），不讓人以為
   即時資料也經過兩個人核准。

### 3.2 三個子專案

| | 內容 | 主責（依 `docs/TEAM_4_ROLES.md`） | 依賴 |
|---|---|---|---|
| **RT-1** | 收集器、封存、`realtime.db`、解析與驗證、彙總保留重建、`status` | A；啟動批次檔與 `config.yaml` 路徑由 D | 無 |
| **RT-2** | `d006010` 月匯入、兩份資料的機組對照、回補、對帳 | A | RT-1 |
| **RT-3** | 第二個唯讀執行器、三道守門、離線路由與語料、runtime 與健康 API、`coverage.yaml` 與對外文件、評測固定資料 | B、C、D | RT-1 |

RT-2 和 RT-3 彼此不依賴，可以並行。

## 4. 收集器

### 4.1 排程：每個時段抓到就停

時段 s 是每 10 分鐘的整點（`HH:M0`）。台電在 s 之後約 5:09 發布，所以在 **s＋5:20** 第一次請求。

```text
target = 往下取整到 10 分鐘(now − 5:20)       # 最新一個「應該已經發布」的時段
若已採用的最新時段 ≥ target：睡到 target＋10:00＋5:20
否則：帶 If-None-Match／If-Modified-Since 請求，依下表處理
```

| 結果 | 動作 |
|---|---|
| 200，而且 `DateTime` 比已採用的最新時段新 | 先封存、再入庫，記下 ETag 與 Last-Modified。若它比 target 舊（台電晚發布），照樣採用，再繼續追 target |
| 304，或 200 但 `DateTime` 沒有比已採用的新 | 60 秒後再試 |
| 連線錯誤、逾時、5xx、回應超過 1 MB | 60 秒後再試。連續失敗 10 次後改成每 2 分鐘，20 次後改成每 5 分鐘（上限），成功一次就恢復 60 秒 |
| 時間已過下一個時段的＋5:20，這個時段還沒拿到 | target 往前推進，不再追這個時段；它若始終沒被採用，彙總時算成缺口（§7.4） |
| 回應帶 `Retry-After` | 照它指定的時間等，最多 30 分鐘 |
| 同一個時段的內容又變了（台電修正） | 兩份都封存，資料庫以後到的那份為準，`revision` 加 1 |

正常情況下一天約 150–200 次請求，每分鐘輪詢則是 1,440 次。失敗時的重試次數和每分鐘輪詢一樣多，
所以不論草稿那兩種失敗原因哪一個成立，這個排程都不會比較差。跑一週後，拿抓取紀錄裡的失敗率和
草稿量到的 22.5% 比較，就能回答那個未解的問題。

### 4.2 主迴圈、睡眠與當掉

- **迴圈最多睡 30 秒**，每次醒來都依牆上時鐘重新計算下一步，不累積長時間的 sleep。
  電腦從睡眠醒來後會馬上察覺，並抓取當下的時段。
- **前後兩次醒來相隔超過 2 分鐘**，就在抓取紀錄寫一筆 `resume`（從幾點到幾點），
  讓缺口原因分得出「收集器沒在跑」和「抓取失敗」。
- **啟動順序**：
  1. 取得單一實例鎖；拿到鎖時若還有 `stop.request`，那只可能是上一次留下的，立刻刪掉，
     不讓它擋住這次啟動；
  2. 載入 `realtime_units.csv` 與 `plants.csv`（重建要用，所以排在開資料庫之前）；
  3. 開啟資料庫。檔案不存在、schema 版本不符時，自動從封存重建（§7.3）。只有檔案真的損毀才把它
     移到旁邊再重建：`quick_check` 不是 `ok`（較舊的 SQLite 會直接拋出例外），或錯誤碼是
     `SQLITE_CORRUPT`／`SQLITE_NOTADB`。移開的檔名是 `realtime.db.corrupt-<日期時間>`，同一秒內
     再移一次就加序號，不會互相覆蓋。其他資料庫錯誤（鎖住、磁碟滿等）不動原檔、往上拋出，
     行程以結束碼 1 離開，由啟動批次檔稍後重試；
  4. 把人工決定寫進 `dim_rt_unit`；失敗只記錄，繼續啟動；
  5. 把已封存、但還沒入庫的回應補進去（處理「封存完、入庫前當掉」）。範圍是最新時段往前
     `RECONCILE_DAYS = 2` 天；抓取時間與重建相同，取自抓取紀錄（`rebuild.fetch_times`），
     所以補進去的結果與重建一致；讀不出來的封存檔記錄後略過；
  6. 補做所有已結束、還沒彙總的日子（§7.1），再清除過期明細（§7.2）；這一步失敗只記錄，
     抓取照常開始；
  7. 寫一筆 `startup` 紀錄，進入迴圈。
- **每一輪都用 try 包起來**，例外只記錄、不中止迴圈。每小時做一次維護：比對人工決定檔的
  SHA-256、補彙總、清除。維護有自己的 try，失敗也算做過這一次，下一個小時再試，同一輪的抓取照常進行。
- 抓取紀錄先寫 JSONL、再寫資料庫鏡像。JSONL 寫不進去（例如 Excel 開著當月的檔案）只記錄，
  資料庫鏡像照寫，排程照樣往下走。
- **行程異常結束**時，由啟動批次檔等 60 秒後重啟。
- **正常停止**：按 Ctrl+C，或建立 `data/realtime/stop.request`（迴圈每次醒來都會檢查）。
  收集器會寫完手上這筆、做 WAL checkpoint、釋放鎖、寫一筆 `shutdown` 紀錄，並刪掉停止檔。
  直接關閉主控台視窗可能是強制結束；因為先封存再入庫、啟動時又會補入庫，強制結束也不會損壞資料。

### 4.3 單一實例鎖

- 鎖檔 `data/realtime/collector.lock`，用作業系統層級的非阻塞鎖，行程在的期間一直持有。
  原語和 `src/serving/data_management.py` 的 `_FileLock` 相同（Windows 用 `msvcrt.locking`，
  其他平台用 `fcntl.flock`），但只試一次、不重試。
- 行程死掉時，作業系統會自動釋放鎖，不會留下失效的鎖檔。鎖檔內寫入 PID 與啟動時間，
  供 `status` 與停止批次檔使用。
- `once` 與 `rebuild` 也要取得這把鎖，收集器在跑的時候它們會拒絕執行。
- 服務不持有這把鎖，只在讀取健康狀態時試鎖一次、立刻放掉，用來判斷收集器有沒有在跑。

### 4.4 時區與 HTTP

- **時區**：一律用固定的 UTC+8（`timezone(timedelta(hours=8))`），台灣從 1979 年起沒有夏令時間。
  不使用 `zoneinfo`，也不新增 `tzdata` 相依套件。`data_time` 照來源存成當地時間文字
  `YYYY-MM-DD HH:MM`；抓取紀錄的時間存 UTC ISO 8601，並帶時區偏移。
- **HTTP**：沿用 `src/ingest/fetch.py` 的 certifi TLS 做法與 User-Agent 格式。逾時 20 秒，
  回應上限 1 MB。urllib 會把 304 當成 `HTTPError` 丟出，這裡要當作正常結果處理。程式內部不自己
  重試，每一次重試都經過排程，所以每一次嘗試都會留下紀錄。傳輸層以參數注入，測試時換成假的。

### 4.5 指令與結束碼

| 指令 | 用途 | 結束碼 |
|---|---|---|
| `python -m ingest.realtime run` | 常駐收集 | 0 正常停止；1 無法啟動（例如設定錯誤）；3 已有一份在跑 |
| `python -m ingest.realtime once` | 不看排程，立刻對目前的 target 請求一次，做一次維護後結束；除錯與手動補彙總用 | 0 成功（含 304）；1 失敗；3 已有一份在跑 |
| `python -m ingest.realtime rebuild` | 從封存重建 `realtime.db` | 0 成功；1 失敗；3 收集器在跑 |
| `python -m ingest.realtime status [--json]` | 健康狀態 | 0 正常；1 落後超過 30 分鐘；2 收集器沒在跑或資料庫不存在 |
| `python -m ingest.realtime stop` | 建立 `stop.request`，最多等 60 秒讓收集器停止 | 0 已停止或本來就沒在跑；1 逾時（印出 PID） |

確認設計時第 1 段曾列出 `rollup` 子指令，第 2 段又決定彙總只在持有鎖的行程裡做。兩者合併後，
手動補彙總由 `once` 負責，不另設 `rollup`。`stop` 是寫實作計畫時加的：把 §4.6 停止批次檔的
「等 60 秒、逾時顯示 PID」放進 Python，才能測試。

`run`、`once`、`rebuild` 遇到未預期的例外時，把完整的 traceback 寫進 log，在 stderr 印一行繁體中文
的原因，以結束碼 1 結束。設定檔讀不到或有誤時，每個指令（包括 `status` 與 `stop`）都印出
「設定錯誤」並以結束碼 1 結束。

### 4.6 啟動批次檔（D）

- `即時收集啟動.bat`：開一個主控台視窗執行 `run`。結束碼不是 0 也不是 3 時，等 60 秒後重啟。
  批次檔不重導輸出：`run` 自己（`__main__.py`）把 log 同時印在主控台並寫進
  `logs/realtime-collector-<日期時間>.log`，每次啟動一個新檔；`once` 與 `rebuild` 只印在主控台。
- `開機自動啟動-即時收集.bat`：給「啟動」資料夾的捷徑用，做法與公開服務相同。
- `停止即時收集.bat`：呼叫 `stop` 子指令（§4.5），建立 `stop.request`，最多等 60 秒讓鎖釋放；
  逾時就顯示鎖檔裡的 PID。

收集器與兩個服務互不相干，重啟服務不影響收集。

## 5. 儲存

### 5.1 檔案配置

```text
data/realtime/
  archive/2026/09/24/0850_3fa1c09e27bd.json.gz   每份內容不同的回應一個檔：原樣 gzip、原子寫入
  archive/_unparsed/<抓取時間>_<sha12>.json.gz   DateTime 解析不出來的回應
  attempts/2026-09.jsonl                         每次嘗試一行，只增不改（格式見附錄 B）
  collector.lock
  stop.request                                   只在要求停止時存在
data/processed/realtime.db（＋ -wal／-shm）
```

- 封存檔名是「資料時段 `HHMM`＋原始位元組 SHA-256 的前 12 碼」，資料夾是資料時段的日期。
  解壓後與台電回傳的內容逐位元組相同（包括 BOM），所以可以用檔名驗證內容。
- 抓取時間、ETag、Last-Modified 不寫進封存檔，只記在抓取紀錄裡，兩邊以 SHA-256 對應。
- 同一時段內容相同的回應只存一次。
- 整個 `data/` 已在 `.gitignore`，封存不會進版控。

### 5.2 Schema

實體表用英文命名，只有 `v_` 檢視用中文，與 `power.db` 相同。每條連線都開
`PRAGMA foreign_keys = ON`；資料庫建立時設 `PRAGMA journal_mode = WAL`。

```sql
CREATE TABLE meta_rt_manifest (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version   TEXT    NOT NULL,
    built_at         TEXT    NOT NULL,          -- UTC ISO
    build_kind       TEXT    NOT NULL CHECK (build_kind IN ('create', 'rebuild')),
    archive_files    INTEGER NOT NULL,          -- 重建時重放的封存檔數
    decisions_sha256 TEXT    NOT NULL,          -- realtime_units.csv
    plants_sha256    TEXT    NOT NULL           -- plants.csv
);

-- 與 plants.csv、power.db 的 dim_plant_scope 使用相同的授權編號
CREATE TABLE dim_rt_plant (
    plant_id   INTEGER PRIMARY KEY,
    plant_name TEXT    NOT NULL UNIQUE
);

-- 一列 = 來源裡的一條時間序列
CREATE TABLE dim_rt_unit (
    id            INTEGER PRIMARY KEY,
    unit_type     TEXT NOT NULL,                -- 清洗後：儲能負載
    unit_type_raw TEXT NOT NULL,                -- 最近一次看到的原樣
    unit_name     TEXT NOT NULL,                -- 原樣，保留 (註N)
    flow          TEXT NOT NULL CHECK (flow IN ('generation', 'storage_load')),
    grain         TEXT NOT NULL CHECK (grain IN ('unit', 'bucket', 'undecided')),
    access_scope  TEXT NOT NULL CHECK (access_scope IN ('plant', 'shared', 'undecided')),
    plant_id      INTEGER REFERENCES dim_rt_plant(plant_id),
    decision_note TEXT NOT NULL DEFAULT '',
    first_seen    TEXT NOT NULL,                -- data_time
    last_seen     TEXT NOT NULL,
    UNIQUE (unit_type, unit_name),
    CHECK ((access_scope = 'plant') = (plant_id IS NOT NULL))
);

-- 每個被採用的時段一列；永久保留，記錄每個時段用的是哪一份封存
CREATE TABLE fact_rt_snapshot (
    data_time   TEXT    PRIMARY KEY,            -- 'YYYY-MM-DD HH:MM'，UTC+8
    sha256      TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL,               -- UTC ISO
    revision    INTEGER NOT NULL DEFAULT 1,
    detail_rows INTEGER NOT NULL,
    quality     TEXT    NOT NULL CHECK (quality IN ('ok', 'warn')),
    warnings    TEXT    NOT NULL DEFAULT '[]'   -- JSON：[{"code": ..., "detail": ...}]
);

-- 10 分鐘明細；保留期見 §7.2
CREATE TABLE fact_rt_unit_10min (
    data_time    TEXT    NOT NULL REFERENCES fact_rt_snapshot(data_time),
    unit_id      INTEGER NOT NULL REFERENCES dim_rt_unit(id),
    net_mw       REAL,                          -- N/A、空值、無法解析 → NULL
    capacity_mw  REAL,                          -- '-' → NULL；每筆各記，容量會變
    load_ratio   REAL,                          -- 小數：0.795，不是 79.5
    note         TEXT    NOT NULL DEFAULT '',
    value_status TEXT    NOT NULL
                 CHECK (value_status IN ('ok', 'missing', 'invalid', 'comm_error')),
    PRIMARY KEY (data_time, unit_id)
) WITHOUT ROWID;

CREATE INDEX idx_rt_unit_10min_unit ON fact_rt_unit_10min(unit_id, data_time);

-- 小計列不進明細，只留作驗證與占比；保留期同明細
CREATE TABLE fact_rt_type_subtotal (
    data_time          TEXT NOT NULL REFERENCES fact_rt_snapshot(data_time),
    unit_type          TEXT NOT NULL,
    subtotal_name      TEXT NOT NULL,           -- 小計、小計(註5)
    net_mw             REAL,
    net_share_pct      REAL,
    capacity_mw        REAL,
    capacity_share_pct REAL,
    detail_net_mw      REAL NOT NULL,           -- 同類明細的數值加總（NULL 不計）
    PRIMARY KEY (data_time, unit_type)
) WITHOUT ROWID;

-- 被隔離、不進明細的列（例如改了名字的彙總列）；永久保留
CREATE TABLE fact_rt_quarantine (
    data_time TEXT    NOT NULL REFERENCES fact_rt_snapshot(data_time),
    row_index INTEGER NOT NULL,
    reason    TEXT    NOT NULL,
    raw_row   TEXT    NOT NULL,                 -- 原樣 JSON
    PRIMARY KEY (data_time, row_index)
) WITHOUT ROWID;

-- 每日彙總；永久保留
CREATE TABLE fact_rt_unit_daily (
    date             TEXT    NOT NULL,          -- 'YYYY-MM-DD'
    unit_id          INTEGER NOT NULL REFERENCES dim_rt_unit(id),
    energy_mwh_est   REAL,                      -- Σ(ok 值) ÷ 6；沒有 ok 值時 NULL
    max_mw           REAL,
    avg_mw           REAL,
    min_mw           REAL,
    samples          INTEGER NOT NULL,          -- value_status = 'ok' 的點數
    expected_samples INTEGER NOT NULL,          -- 144
    notes_seen       TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL DEFAULT 'live' CHECK (source IN ('live')),  -- RT-2 擴充
    PRIMARY KEY (date, unit_id)
) WITHOUT ROWID;

-- 從第一次啟動那天起每天一列，整天都沒拿到資料也有一列
CREATE TABLE fact_rt_day (
    date                  TEXT    PRIMARY KEY,
    snapshots             INTEGER NOT NULL,
    expected              INTEGER NOT NULL,     -- 144
    missed_collector_down INTEGER NOT NULL,
    missed_fetch_failed   INTEGER NOT NULL,
    missed_rejected       INTEGER NOT NULL,
    rows_at_rollup        INTEGER NOT NULL,     -- 彙總當下的明細列數
    rolled_up_at          TEXT    NOT NULL,     -- UTC ISO
    purged_at             TEXT,                 -- 明細被清除的時間；NULL = 尚未清除
    CHECK (snapshots + missed_collector_down + missed_fetch_failed + missed_rejected = expected)
);

-- attempts/*.jsonl 的鏡像
CREATE TABLE meta_rt_attempt (
    id           INTEGER PRIMARY KEY,
    attempted_at TEXT NOT NULL,                 -- UTC ISO
    kind         TEXT NOT NULL CHECK (kind IN ('fetch', 'startup', 'shutdown', 'resume')),
    target_slot  TEXT,
    outcome      TEXT CHECK (outcome IN
                 ('new', 'revised', 'not_modified', 'stale', 'error', 'rejected')),
    http_status  INTEGER,
    error_type   TEXT,
    reject_code  TEXT,
    data_time    TEXT,
    sha256       TEXT,
    bytes        INTEGER,
    elapsed_ms   INTEGER,
    etag         TEXT,
    detail       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_rt_attempt_slot ON meta_rt_attempt(target_slot);
```

### 5.3 人工決定檔：`taipower_align/realtime_units.csv`

每列是一個人工決定，格式見附錄 C。

- 沒列在檔案裡的機組：`grain` 與 `access_scope` 都是 `undecided`。
- `grain`：`unit` 是一部機組或一座場站（例如一座風場）；`bucket` 是多個不相干的設施併成一列
  （例如 `其它購電太陽能`）。
- `access_scope`：
  - `plant`：屬於 `plants.csv` 34 座電廠中的一座，必須填 `plant_id`；
  - `shared`：所有電廠帳號都看得到。不屬於 34 座電廠任何一座的列（民間風場、購電彙總、汽電共生等）
    標 `shared`，比照 `v_re_generation` 不受管、`v_peak` 共用欄位對所有電廠帳號可見的前例。
- 載入規則：
  - 欄位標頭不符，或同一（類型, 名稱）重複：整份視為無效，沿用上一次成功載入的內容並發出警告；
    從來沒成功載入過，就全部當成未定；
  - 任一個檔案讀不了，也比照整份無效處理：例如用 Excel 另存成非 UTF-8 編碼的 `realtime_units.csv`、
    缺檔或格式壞掉的 `plants.csv`。可能的例外集中在 `decisions.LOAD_ERRORS` 一個 tuple，
    收集器與 `status` 共用；收集器只發出警告，沿用資料庫裡上一次成功套用的決定，`status` 把原因放在
    `decisions_error`，不會因此當掉；
  - 需要重建 `realtime.db`、而 `plants.csv` 又讀不了時，重建改用空的決定（沒有機組決定、沒有電廠名冊），
    不讓啟動失敗；檔案修好後，下一次每小時重新載入（檔案 SHA 與空字串不同）會把真正的決定讀回來；
  - 少欄的列（欄位比標頭少）當成那一列不合法：視為未定，並發出警告；
  - `plant_id` 不在 `plants.csv`、欄位值不合法：只有那一列視為未定，並發出警告；
  - 檔案裡有、但來源從沒出現過的列：列為過期的決定並回報，比照 `b_column_capacity` 的
    unknown_columns。
- `flow` 不寫在檔案裡：由類型機械推導，`儲能負載` 就是 `storage_load`。
- 初版做法：程式依名稱前綴產生候選，**人工逐列確認後才提交**。執行期間程式不做任何字串比對推測，
  沿用 `column_capacity.py`「逐欄明寫」的原則。

### 5.4 檢視

檢視一律以 `v_rt_` 開頭：一方面和既有的 `v_unit`（`大潭複一機` 那套命名）明確區分，另一方面讓
RT-3 的執行器只用一條規則就能分派。粒度標籤用「個別」而不是「單機」，因為風力的一列是一座風場（F7）。

```sql
CREATE VIEW v_rt_10min AS
SELECT f.data_time                        AS "資料時間",
       substr(f.data_time, 1, 10)         AS "日期",
       substr(f.data_time, 12, 5)         AS "時刻",
       u.unit_type || '|' || u.unit_name  AS "機組鍵",
       u.unit_type                        AS "機組類型",
       u.unit_name                        AS "機組名稱",
       CASE u.grain WHEN 'unit' THEN '個別' WHEN 'bucket' THEN '彙總' ELSE '未定' END
                                          AS "粒度",
       p.plant_name                       AS "電廠",
       f.capacity_mw                      AS "裝置容量_MW",
       f.net_mw                           AS "淨發電量_MW",
       f.load_ratio                       AS "出力比",
       f.note                             AS "備註",
       CASE f.value_status WHEN 'ok' THEN '正常' WHEN 'missing' THEN '無值'
                           WHEN 'invalid' THEN '無法解析' ELSE '通訊異常' END
                                          AS "數值狀態"
FROM fact_rt_unit_10min AS f
JOIN dim_rt_unit AS u ON u.id = f.unit_id
LEFT JOIN dim_rt_plant AS p ON p.plant_id = u.plant_id;

CREATE VIEW v_rt_now AS
SELECT * FROM v_rt_10min
WHERE "資料時間" = (SELECT MAX(data_time) FROM fact_rt_snapshot);

CREATE VIEW v_rt_daily AS
SELECT d.date                                         AS "日期",
       u.unit_type || '|' || u.unit_name              AS "機組鍵",
       u.unit_type                                    AS "機組類型",
       u.unit_name                                    AS "機組名稱",
       CASE u.grain WHEN 'unit' THEN '個別' WHEN 'bucket' THEN '彙總' ELSE '未定' END
                                                      AS "粒度",
       p.plant_name                                   AS "電廠",
       d.energy_mwh_est                               AS "估算發電量_MWh",
       d.max_mw                                       AS "最高出力_MW",
       d.avg_mw                                       AS "平均出力_MW",
       d.min_mw                                       AS "最低出力_MW",
       d.samples                                      AS "取樣點數",
       d.expected_samples                             AS "應有點數",
       round(1.0 * d.samples / d.expected_samples, 4) AS "資料完整度",
       d.notes_seen                                   AS "當日備註",
       CASE d.source WHEN 'live' THEN '即時收集' ELSE d.source END AS "資料來源"
FROM fact_rt_unit_daily AS d
JOIN dim_rt_unit AS u ON u.id = d.unit_id
LEFT JOIN dim_rt_plant AS p ON p.plant_id = u.plant_id;
```

- `機組鍵`（`類型|名稱`）是給 `ScopeGuard` 用的授權欄位，對應 `SCOPE_KEYS` 單一欄位的設計。
- `電廠` 來自人工決定檔；`shared` 與 `undecided` 的列是 NULL。
- `資料完整度` 與 `出力比` 一樣存 0–1 的小數。
- 小計不在任何檢視裡，所以不可能被重複加總。

### 5.5 大小

| 項目 | 估計 | 依據 |
|---|---|---|
| 封存 | 每份約 3.7 KB，每天約 0.53 MB，每年約 193 MB | 快照原檔 36,646 bytes，gzip 等級 9 後 3,680 bytes |
| 10 分鐘明細（14 天，含索引） | 約 32 MB | 以 204 列 × 144 × 14 = 411,264 列實測 |
| 每日彙總 | 每年約 7.4 萬列、約 6 MB | 204 條序列 × 365 天 |
| 抓取紀錄 | 每年約 7 萬列 | 每天約 200 次嘗試 |

資料庫穩定在 40 MB 上下，每年增加約 15 MB。

## 6. 解析與驗證

原則：**結構有問題就整份不收，數值有問題就逐列標記後照收。**「不收」只是不入庫；原始回應已經
封存，修好解析程式後重建就能補回，所以拒收不等於遺失。

### 6.1 解析步驟

解析是純函式：輸入原始位元組與現在時間，輸出解析結果或拒收代碼，不碰檔案與資料庫。

1. 以 `utf-8-sig` 解碼並解析 JSON；頂層必須是物件，且有 `DateTime`（字串）與 `aaData`（陣列）。
2. `DateTime` 轉成 `YYYY-MM-DD HH:MM`，檢查是否在 10 分鐘整點、是否在未來。
3. 逐列檢查六個欄位，清洗類型，判定是小計、明細或隔離列。
4. 解析數值，決定 `value_status`。
5. 逐類驗證明細加總與小計。
6. 輸出時段、明細、小計、隔離列，以及品質與警告清單。

### 6.2 整份不收

| 代碼 | 條件 | 為什麼不能照收 |
|---|---|---|
| `PAYLOAD_INVALID` | 不是 JSON，或者缺 `DateTime`／`aaData` | 不知道是哪個時段 |
| `DATETIME_OFF_SLOT` | `DateTime` 不在 10 分鐘整點 | 台電如果改成 5 分鐘一筆，照收會讓估算發電量翻倍 |
| `DATETIME_IN_FUTURE` | 比現在晚 15 分鐘以上 | 時鐘或資料有錯 |
| `FIELD_MISSING` | 六個欄位少了任何一個 | 欄位改名時照收，整欄都會變成 NULL，看起來像全部停機 |
| `DUPLICATE_KEY` | 同一份裡（清洗後類型, 名稱）重複 | 不知道哪一列才對 |
| `NO_DETAIL_ROWS` | 明細是 0 列 | 空的快照會被當成全部零出力 |

### 6.3 照收，但加上標記

快照標成 `quality = 'warn'`，警告寫進 `fact_rt_snapshot.warnings`。

| 代碼 | 條件 | 處理方式 |
|---|---|---|
| `SUBTOTAL_MISMATCH` | 某一類的明細數值加總與小計差超過 0.1 MW。差值先取到小數 6 位再比（`round(abs(diff), 6) > 0.1`），所以剛好差 0.1 MW 不警告；明細加總本身存到小數 3 位 | 記下是哪一類。最可能是有新的彙總列混進明細 |
| `UNRECOGNIZED_AGGREGATE` | 名稱不是「小計」開頭，值卻是「數值(百分比%)」格式 | 這一列進 `fact_rt_quarantine`，不進明細 |
| `UNKNOWN_TYPE` | 沒見過的機組類型 | 照收，例如核能又出現 |
| `ROW_COUNT_UNUSUAL` | 明細列數不在 100–400 之間 | 照收（實測 204） |
| `EXTRA_FIELD` | 多出新的欄位 | 照收，該欄忽略 |
| `VALUE_UNPARSEABLE` | 數值欄出現無法解析的字串 | 該值存 NULL；若是淨發電量，`value_status = 'invalid'` |

取小數 6 位是 2026-09-29 第一次實際抓取時發現的：風力明細 596.6、小計 596.7，浮點相減得
0.10000000000002，原本直接比較會誤報 `warn`。

已知的類型：燃氣、民營電廠-燃氣、燃煤、民營電廠-燃煤、汽電共生、燃料油、太陽能、風力、水力、
儲能、其它再生能源、儲能負載，另外預先列入核能。清單放在 `configs/realtime.yaml`。

### 6.4 清洗規則

| 欄位 | 規則 | 例子 |
|---|---|---|
| 機組類型 | 先去掉 HTML 標籤，再去掉結尾的純英文括號；原樣另存 | `儲能負載(Energy Storage System Load)</b>` → `儲能負載` |
| 機組名稱 | 只去掉前後空白，保留 `(註N)` | `興達#1(註15)` 維持原樣 |
| 是否為小計 | 名稱以「小計」開頭 | `小計`、`小計(註5)` |
| 發電量、容量 | 去掉空白與千分位逗號；`-`、`N/A`、空字串一律存 NULL | `N/A` 存 NULL，不是 0 |
| 小計的值 | 拆成數值與占比 | `16521.4(50.877%)` → 16521.4 與 50.877 |
| 出力比 | 去掉 `%` 再除以 100；`-`、`N/A` 存 NULL | `79.521%` → 0.79521 |
| 備註 | 去掉前後空白 | 只有一個空白 → 空字串 |
| 負值 | 照存 | `電池(註16)` 的儲能負載 −19.4 |

`value_status` 描述的是淨發電量：

| 情況 | `value_status` |
|---|---|
| 有數值，備註不在「數值不可信」清單 | `ok` |
| `N/A`、`-` 或空字串 | `missing` |
| 無法解析的字串 | `invalid` |
| 備註在「數值不可信」清單（目前只有「通訊異常」），值照存 | `comm_error` |

**只有 `ok` 算進估算發電量與最高／平均／最低出力。** 其他三種都算缺漏，反映在資料完整度，
不會被當成 0：「沒有資料」與「發電量為零」必須分開。

## 7. 彙總、保留、重建與缺口

### 7.1 每日彙總

- **時機**：收集器啟動時，以及每小時維護時。凡是隔天 00:15 已過、還沒彙總的日子都補做；
  彙總過、尚未清除、但明細列數與 `rows_at_rollup` 不符的日子要重算。
- **範圍**：從第一次啟動那天起，每一天都有一列 `fact_rt_day`，整天都沒拿到資料也一樣。
- **可重複**：同一個交易內先刪掉那一天的彙總再重算，重跑結果永遠相同。
- **只有一份實作**：平常的維護與重建（§7.3）共用同一段彙總程式碼。

每天、每條序列：

| 欄位 | 算法 | 沒有任何 `ok` 值時 |
|---|---|---|
| `energy_mwh_est` | `ok` 值加總 ÷ 6 | NULL，不是 0 |
| `max_mw`、`avg_mw`、`min_mw` | 只看 `ok` 值 | NULL |
| `samples` | `ok` 的點數 | 0 |
| `expected_samples` | 1440 ÷ 時段分鐘數，即 144 | 144 |
| `notes_seen` | 當天出現過的非空備註，依第一次出現的時間排序，以 `\|` 串接 | 空字串 |

- 當天所有快照裡都沒出現的序列，那天沒有彙總列。
- 00:00～23:50 這 144 點屬於當天，每一點代表其後的 10 分鐘。
- 儲能負載積出來的是負的充電電量，照存，不與放電相抵。
- 當天還沒結束的資料不進日彙總。「今天到目前」由 RT-3 用明細計算，完整度以已經過去的時段數為分母。
- 每一天的缺口依 §7.4 分類，計入 `fact_rt_day`。

### 7.2 清除：條件寫在 SQL 裡

保留期預設 14 天（`retention.raw_days`），因為「跟上週同一時段比較」需要超過 7 天的明細。
清除在同一個交易內完成：

```sql
-- 1. 可以清除的日子：早於保留期、還沒清除過、彙總後沒有新的明細寫入
CREATE TEMP TABLE purgeable AS
SELECT d.date
FROM fact_rt_day AS d
WHERE d.date < :cutoff
  AND d.purged_at IS NULL
  AND d.rows_at_rollup = (SELECT COUNT(*) FROM fact_rt_unit_10min AS f
                          WHERE substr(f.data_time, 1, 10) = d.date);

-- 2. 只刪這些日子
DELETE FROM fact_rt_unit_10min    WHERE substr(data_time, 1, 10) IN (SELECT date FROM purgeable);
DELETE FROM fact_rt_type_subtotal WHERE substr(data_time, 1, 10) IN (SELECT date FROM purgeable);
UPDATE fact_rt_day SET purged_at = :now WHERE date IN (SELECT date FROM purgeable);
```

- 還沒彙總的日子不在 `fact_rt_day` 裡，自然不可能被清除；就算程式的呼叫順序寫錯，也刪不到。
- `fact_rt_snapshot`、`fact_rt_quarantine` 與每日彙總永久保留。
- 封存與抓取紀錄，收集器一律不刪。清除的只是資料庫裡的明細，所以清除不是破壞性的操作。
- 穩態下檔案大小會收斂，free page 會被重用，不需要 `VACUUM`。

### 7.3 重建

觸發：手動執行 `rebuild`（收集器必須先停），或收集器啟動時偵測到資料庫不存在、schema 版本不符、
檔案損毀（判定方式見 §4.2 第 3 步）。

在原檔內以單一交易完成，不換檔：

1. `BEGIN IMMEDIATE`；服務端透過 WAL 繼續讀到舊資料。
2. 刪除並重建所有資料表與檢視。
3. 載入 `realtime_units.csv` 與 `plants.csv`。
4. 依序重放 `attempts/*.jsonl` 到 `meta_rt_attempt`。當機時寫到一半的殘行（包括切在多位元組字元
   中間的）解不出 JSON，直接略過。
5. 逐日重放封存：依（資料時段, 抓取時間）排序，用與平常相同的解析與驗證程式碼；同一時段取抓取時間
   最晚的那份。抓取時間取自抓取紀錄，紀錄缺漏時退回封存檔的修改時間。讀不出來（gzip 截斷、
   位元損壞）或內容與檔名的 SHA-256 不符的封存檔計為損毀、略過，不讓一個壞檔擋住整個重建。
6. 每重放完一天，就用同一段程式碼彙總那一天；超出保留期的日子隨即清除。
7. 寫入 `meta_rt_manifest`，`COMMIT`。

封存還是空的時候（第一次啟動），同一個流程就等於建立空的資料庫，`build_kind` 記為 `create`。

不換檔的原因：`os.replace` 在 Windows 上遇到有人開著檔案就會失敗；舊檔留下的 `-wal` 如果被套用到
新檔上，資料庫會損壞。唯一的例外是原檔真的損毀（§4.2 第 3 步）：這時才把它移到旁邊、刪掉留下的
`-wal`／`-shm`，換新檔重建；封存還在，所以不會遺失資料。

修改人工決定檔不需要重建：收集器在啟動時與每小時比對 CSV 的 SHA-256，有變就直接更新
`dim_rt_unit` 的屬性。

一年約 5.3 萬份封存，重建預估在數分鐘內；實作計畫以合成封存實測。

### 7.4 缺口原因

| 原因 | 判斷方式 | 之後補得回來嗎 |
|---|---|---|
| `rejected` | 有任何一次目標是這個時段的嘗試被拒收 | 修好解析程式後重建即可 |
| `fetch_failed` | 有嘗試，但全是錯誤、304 或舊資料 | RT-2 可用 `d006010` 補回有執照的機組 |
| `collector_down` | 沒有任何一次嘗試的目標是這個時段（睡眠、關機或收集器沒在跑） | 同上 |

依表格由上往下判斷，先符合的為準。

### 7.5 健康狀態

`status` 與 RT-3 的健康 API 共用 `read_status()`（§8.3），內容包括：

- 資料庫在不在、schema 版本；
- 最新時段，以及落後分鐘數（現在減去最新時段；正常情況在 5–16 分鐘之間）；
- 收集器有沒有在跑（試鎖）；
- 連續失敗次數；
- 今天已經過的時段數與拿到的時段數；
- 最近 24 小時依原因分類的缺口數；
- 未定機組數、過期決定數；
- 最新快照的品質與警告。

`state` 的取值：`healthy`、`stale`（落後超過 30 分鐘）、`stopped`（收集器沒在跑）、
`unavailable`（資料庫不存在或打不開）。

## 8. 對外介面

### 8.1 路徑與設定

`configs/config.yaml` 的 `paths` 新增三個 key（D 維護，A 提供內容）：

```yaml
  realtime_database: data/processed/realtime.db
  realtime_root: data/realtime
  realtime_units_csv: taipower_align/realtime_units.csv
```

資料中心只取資料槽那幾個 key（`src/serving/app.py` 的 `build_managed_service`），新增的 key
不影響受控版本。行為參數放在 `configs/realtime.yaml`（A），見附錄 A。

### 8.2 檢視

`v_rt_now`、`v_rt_10min`、`v_rt_daily` 的欄位名稱與意義是契約。要變動就升 `schema_version`，
並在同一個 PR 同步更新 `SqlGuard.ALLOWED_COLUMNS`、`configs/coverage.yaml` 與 router（C、B）。

### 8.3 健康狀態函式

`ingest.realtime.status.read_status(root: Path) -> dict`：唯讀開檔、不需要鎖，回傳 §7.5 的內容。
`status` 指令與 RT-3 的 `/api/health` 共用這個函式。

### 8.4 權限

- 依據：`dim_rt_unit.access_scope`／`plant_id`，以及檢視裡的 `機組鍵`、`電廠`。
- RT-3 在建 runtime 時，替每座電廠算出看得到的機組鍵：自己電廠的機組，加上 `shared` 的列；
  `undecided` 一律看不到。
- RT-3 要比對 `dim_rt_plant` 與 `power.db` 的 `dim_plant_scope`，編號或名稱對不上就拒絕，
  沿用建庫時 `ScopeAlignmentError` 的原則。

### 8.5 RT-3 必須處理的事

RT-1 保證下列判斷所需的欄位都已經存好；規則本身屬於 RT-3。

1. 服務端加第二個唯讀執行器，`v_rt_*` 查 `realtime.db`，其他查 `power.db`；同一句 SQL 混用兩邊的
   檢視直接拒絕。兩套命名之間沒有對照前，跨庫 JOIN 只會做出「看起來對上了」的錯誤結果。
2. 「現在」的答案一律附資料時間；落後超過 30 分鐘或收集器沒在跑時要揭露，不能把舊資料說成現在。
3. 結果含 `粒度 = 彙總／未定` 的列、而問句要逐機組排名或列表時，要揭露。
4. 估算發電量一律標明是估算；資料完整度低於門檻（預設 0.9）時，揭露取樣點數與缺口原因。
5. 加總同時包含儲能與儲能負載時，要揭露放電與充電會相抵。
6. 用到 `quality = 'warn'` 的快照時要揭露。
7. 與 `v_peak`（萬瓩）或 `v_unit`（瓩）比較時的單位差異，延伸 `UNIT_MISMATCH`。
8. 問到第一個收集日之前的期間，要說明資料從哪天開始（RT-2 之後改為 `d006010` 的範圍）。
9. 現有的 `NO_UNIT_DETAIL`、`PEAK_SUM_ACROSS_DAYS`、`PLANT_DAILY_ONLY_IN_BUCKET`、
   `DATA_RANGE_OUT_OF_BOUNDS` 是為 `v_peak` 寫的，會在路由前擋掉即時問句，要依資料來源區分。
10. 即時檢視的期間不能沿用建 runtime 時量一次的 `VIEW_TIME_SPANS`，要每次查詢時現量。
11. 「今天、昨天、現在」要能解析成日期；評測時固定「現在」。
12. 查即時檢視的 SQL 不進語料自動學習：學習流程會重放 SQL 比對結果 checksum，即時結果一定對不上。
13. `configs/coverage.yaml` 的「比日更細的時間粒度」與「火力與核能的發電量」兩條限制要改寫，
    `absent` 檢查要涵蓋 `v_rt_*`；README、ATTRIBUTION 的「不是即時資料服務」與 SYSTEM_CARD
    的相關句子同步修正。
14. 兩套命名有約 21 個同名（例如 `台中#1`），要靠問句用詞（現在、目前…）而不是名稱決定查哪個
    資料庫；即時的機組名稱要獨立一組，不能併進 `v_peak` 的欄位清單。
15. 測試裡「資料庫的檢視＝`SCOPE_KEYS` ∪ `SHARED_VIEWS`＝`ALLOWED_COLUMNS`＝`coverage.yaml`」
    這組不變式要擴充成兩個資料庫。

### 8.6 測試用的固定資料

`tests/fixtures/realtime/` 放少量封存檔：真實快照一份，加上由它衍生的合成序列。RT-3 的測試與評測用
「固定封存＋固定現在」重建出固定的 `realtime.db`，結果可以重現。

## 9. 錯誤處理

| 情境 | 行為 |
|---|---|
| 網路錯誤、逾時、5xx | 照 §4.1 的排程重試，並記錄 |
| 回應超過 1 MB | 當成失敗，並記錄 |
| 解析或驗證拒收 | 封存，記錄 `rejected`，不入庫 |
| 封存寫入失敗（例如磁碟滿） | 不入庫（先封存才入庫）；抓取紀錄記成 `error`、`error_type = ArchiveError`；不記下 ETag，下一次會重新下載；照 §4.1 的失敗退避重試，健康狀態的連續失敗次數會增加 |
| 入庫失敗（資料庫鎖住、損壞） | 記成 `IngestError`，不記下 ETag；封存已經完成，下次啟動時的補入庫步驟會補上；資料庫損壞時下次啟動自動重建（§4.2 第 3 步） |
| 啟動時開資料庫遇到損毀 | 只有 `quick_check` 不是 `ok`（或在較舊的 SQLite 上直接拋出例外）、或錯誤碼是 `SQLITE_CORRUPT`／`SQLITE_NOTADB` 才算：原檔移到不重複的 `realtime.db.corrupt-<日期時間>`，從封存重建 |
| 啟動時開資料庫遇到其他錯誤（鎖住、磁碟滿等） | 不動原檔，往上拋出，行程以結束碼 1 離開，啟動批次檔 60 秒後重試 |
| 彙總或清除失敗 | 不影響收集；清除條件保證不會刪到還沒彙總的日子；下一個小時重試。啟動時的維護、人工決定寫入資料庫失敗也一樣只記錄，照常進入迴圈 |
| 人工決定檔格式錯誤或讀不了（含 Excel 另存的非 UTF-8 檔、壞掉的 `plants.csv`） | 只發出警告（`decisions.LOAD_ERRORS`），沿用上一次成功套用的決定；從來沒載入成功過，就全部當成未定；`status` 照常回報，原因放在 `decisions_error` |
| 抓取紀錄 JSONL 寫入失敗（例如被 Excel 開著） | 記錄到 log；資料庫鏡像照寫，排程照樣往下走 |
| 封存檔讀不出來 | 重建時計為損毀、補入庫時記錄，兩者都略過那一份繼續 |
| 抓取紀錄有寫到一半的殘行（含切在多位元組字元中間） | 讀取時略過那一行 |
| 上一次留下的 `stop.request` | 拿到鎖後立刻刪掉，照常啟動 |
| `run`／`once`／`rebuild` 發生未預期的例外 | traceback 寫進 log，stderr 印繁體中文原因，結束碼 1 |
| 系統時鐘倒退 | 不會當掉，以回應裡的 `DateTime` 為準 |
| 同時啟動第二個收集器 | 拿不到鎖，印出正在跑的那一份的 PID，以結束碼 3 離開 |

## 10. 測試

CI 跑在 Ubuntu、不連網，以下全部要在 CI 上通過（Windows 專屬項目除外）。

- **不打真的網路**：傳輸層可替換，沿用 `download_datasets(downloader=...)` 的做法；假的傳輸層
  照劇本回 200、304、`ConnectionResetError`、逾時或 `Retry-After`。
- **不真的等**：排程是純函式，用表格列出「現在時間、狀態、結果 → 下一步」逐條測試；主迴圈用假的
  時鐘與假的 sleep 來跑。
- **真實快照當基準**：204 列明細、11 列小計、12 個跨類型同名分得開、11 類小計驗證全部通過、
  `N/A` 存 NULL、`</b>` 被清掉、`小計(註5)` 被認出。
- **變造測試**：§6.2 與 §6.3 的每個代碼，各準備一份故意改壞的回應。
- **彙總**：已知序列算出精確的發電量；沒有 `ok` 值時回 NULL；通訊異常被排除；儲能負載為負值；
  `fact_rt_day` 的缺口數加上快照數等於 144。
- **故障注入**：彙總拋出例外時，清除不刪任何東西；彙總之後又寫入新列時，那天要等重算完才能清除。
- **重建結果一致**：同一串回應逐筆入庫，和從封存重建，兩個資料庫的內容 checksum 必須相同，
  比照 `build_db` 的 `_database_content_checksum`。
- **當掉後的恢復**：封存已寫入但入庫失敗時，下次啟動要補進去。
- **並行讀寫**：用與 `ReadOnlySQLite` 相同的方式唯讀開檔，寫入端提交時讀取端照常讀得到；收集器停著、
  `-wal`／`-shm` 不存在時也要讀得到。這是 WAL 加唯讀最容易出事的邊角。
- **只跑一份**：第二次取鎖要失敗；行程結束後鎖要自動釋放（用子行程測）。
- **睡眠後醒來**：假時鐘跳一段時間，要寫出 `resume` 紀錄，缺口原因要是 `collector_down`。
- **人工決定檔**：標頭錯、重複列、`plant_id` 不存在、過期決定，各自的處理符合 §5.3。
- **Windows 專屬**：CI 只測得到 `fcntl` 那條鎖；`msvcrt` 那條與批次檔在本機 Windows 上測，
  比照現有的 `test_windows_launcher.py`，非 Windows 時跳過。

## 11. 檔案、分工與流程

### 11.1 RT-1 會新增或修改的檔案

| 檔案 | 內容 | Owner |
|---|---|---|
| `src/ingest/realtime/config.py` | 讀 `configs/realtime.yaml` 與路徑 | A |
| `src/ingest/realtime/timeutil.py` | 固定 UTC+8、時段取整與格式（parse、schedule、maintenance、status 共用） | A |
| `src/ingest/realtime/candidates.py` | 人工決定檔的候選產生器（§5.3），執行期不用 | A |
| `src/ingest/realtime/client.py` | 條件式請求，傳輸層可注入 | A |
| `src/ingest/realtime/schedule.py` | 純函式：下一步做什麼、什麼時候做 | A |
| `src/ingest/realtime/parse.py` | 純函式：原始位元組 → 解析結果或拒收代碼 | A |
| `src/ingest/realtime/archive.py` | 封存與抓取紀錄的寫入、讀取、重放 | A |
| `src/ingest/realtime/decisions.py` | 載入並驗證 `realtime_units.csv` 與 `plants.csv` | A |
| `src/ingest/realtime/store.py` | schema、入庫、維度同步、抓取紀錄鏡像 | A |
| `src/ingest/realtime/maintenance.py` | 日彙總、清除、缺口分類 | A |
| `src/ingest/realtime/rebuild.py` | 從封存重建 | A |
| `src/ingest/realtime/lock.py` | 單一實例鎖 | A |
| `src/ingest/realtime/status.py` | `read_status()` | A |
| `src/ingest/realtime/collector.py` | 主迴圈，串起以上模組 | A |
| `src/ingest/realtime/__main__.py` | 指令列 | A |
| `configs/realtime.yaml` | 行為參數 | A |
| `taipower_align/realtime_units.csv` | 人工決定 | A |
| `tests/test_realtime_*.py`、`tests/fixtures/realtime/` | 測試與固定資料 | A |
| `configs/config.yaml` | 新增三個路徑 | D |
| `即時收集啟動.bat`、`開機自動啟動-即時收集.bat`、`停止即時收集.bat` | 啟動、自動啟動、停止 | D |
| `.gitattributes` | `*.bat` 固定 CRLF（LF 行尾時 cmd.exe 的 goto 可能找不到標籤） | D |
| `docs/SERVING.md` | 收集器操作說明 | D（內容由 A 提供） |
| `docs/lineage/` 相關 CSV | 新來源、清洗規則、資料表、人工裁決 | A |
| `log.md` | 儲存點 | 各自 |

### 11.2 流程

- `docs/SERVING.md` 的操作說明涵蓋：啟動、停止、`status`、重建，以及升級步驟（停收集器 → 更新程式
  → 啟動收集器，schema 不符會自動重建 → 重啟服務）。
- 對使用者的說法等 RT-3 讓使用者查得到時才改（§8.5 第 13 點）。RT-1 完成時，使用者看到的行為
  沒有任何改變。
- 每個可驗證的儲存點照慣例寫 log.md、用 Conventional Commits，從 `origin/main` 開分支。

### 11.3 RT-1 完成的定義

1. `run` 能連續收集；電腦醒著的時段都拿得到資料，`status` 顯示 `healthy`。
2. `realtime_units.csv` 涵蓋當時快照的全部明細列，`status` 的未定機組數為 0。
3. 從封存重建出的資料庫，與逐筆入庫的資料庫內容 checksum 相同。
4. §10 的測試在 CI 上不連網全部通過；`ruff format --check`、`ruff check`、`pytest` 通過；
   合併門檻沒有 BLOCK。
5. `docs/SERVING.md` 有收集器操作說明；`docs/lineage/` 已更新。
6. 既有的四份題庫評測結果不變：RT-1 不改任何查詢行為。

## 12. 風險與待驗證

| 風險 | 處理 |
|---|---|
| WAL 資料庫在收集器停止、`-wal`／`-shm` 不存在時，唯讀連線能否開啟 | §10 專門測試；若失敗，改由收集器維持 WAL 檔存在，或讀取端重試 |
| Windows 睡眠後 `time.sleep` 的行為 | 迴圈以牆上時鐘重算，設計上不依賴 sleep 準時；2026-10-05 已用啟動批次檔實機驗證，寫出 `resume` 紀錄並正常 `shutdown`（log.md CP-073） |
| 機組改名讓序列斷開（例如 `(註10)` 試俥機組商轉後改名） | 已知限制：新名稱是新序列，在人工決定檔的 `note` 註記；RT-2 的對照可能合併 |
| 台電修正同一時段的頻率不明 | 已由 `revision` 處理；抓取紀錄會量出頻率 |
| 來源格式或頻率改變 | 拒收代碼讓變化立刻看得到，封存讓修正後可以補回 |
| 多年封存的重建時間 | 實作計畫以合成封存實測 |
| 失敗原因不明（草稿 22.5%） | 排程在兩種假設下都不會比較差；一週後以抓取紀錄分析 |
| 封存成長 | 每年約 193 MB；是否定期打包到 GitHub Releases 另議，不在 RT-1 |

### 12.1 已知限制（RT-1 決定延後處理）

- **啟動時只補最近兩天**：`RECONCILE_DAYS = 2`，補入庫只看最新時段往前兩天的封存。更早的
  「已封存、未入庫」只有手動 `rebuild` 才會補回。
- **啟動重建期間按 Ctrl+C 會被重啟**：重建在交易內，中斷會整個回復，資料不受影響；但這時的結束碼
  不是 0 或 3，啟動批次檔會在 60 秒後重新啟動，重建從頭再做。
- **重建用重建當下的時間解析**：`DATETIME_IN_FUTURE` 等檢查以重建時的 `now` 判斷，不是當初抓取的時間。
- **Windows 上移開損毀檔案可能失敗**：另一個行程開著 `realtime.db` 時（例如資料庫瀏覽工具），
  移到旁邊會失敗，啟動以結束碼 1 離開、由批次檔重試，直到那個行程關檔。RT-1 的服務不開這個檔；
  RT-3 讓服務常駐唯讀開檔後就會碰到，屆時要處理。

## 13. 留給 RT-2、RT-3 的問題

- **RT-2**：`d006010` 的實際涵蓋期間與機組命名（初步看與 `d006001` 同屬調度命名，例如 `南部CC#1`）、
  189.8 MB JSON 的串流解析、同一時段兩邊都有值時以哪邊為準、回補後重算彙總、`source` 的取值。
  驗證要下載一次 `d006010`（189.8 MB），須先取得同意。
- **RT-3**：§8.5 各項的實作方式，包括執行器分派與跨庫拒絕的錯誤代碼、`SemanticGuard` 的即時期間
  來源、router 意圖與實體、語料、評測題與固定資料、健康 API 與介面顯示、對外文件。
- **不規劃**：與 `v_peak` 的機組名稱對照、每小時彙總層。

## 附錄 A：`configs/realtime.yaml`

```yaml
# 即時機組發電量收集器（RT-1）。路徑在 configs/config.yaml 的 paths。
source:
  url: https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json
  timeout_seconds: 20
  max_bytes: 1000000
schedule:
  slot_minutes: 10
  first_probe_seconds: 320          # 時段開始後 5:20；實測發布延遲 5:08–5:09
  retry_seconds: 60
  backoff_after_failures: [10, 20]  # 連續失敗達到這些次數時，改用下一行對應的間隔
  backoff_seconds: [120, 300]
  retry_after_cap_seconds: 1800
  loop_max_sleep_seconds: 30
  resume_gap_seconds: 120
validation:
  future_tolerance_seconds: 900
  subtotal_tolerance_mw: 0.1
  detail_rows_expected: [100, 400]
  known_types: [燃氣, 民營電廠-燃氣, 燃煤, 民營電廠-燃煤, 汽電共生, 燃料油,
                太陽能, 風力, 水力, 儲能, 其它再生能源, 儲能負載, 核能]
  unreliable_notes: [通訊異常]
retention:
  raw_days: 14
rollup:
  day_closed_after: "00:15"
status:
  stale_after_minutes: 30
```

## 附錄 B：抓取紀錄（`data/realtime/attempts/YYYY-MM.jsonl`）

每行一個 JSON 物件，欄位與 `meta_rt_attempt` 相同（`id` 除外），沒有值的欄位省略。以下為示意，
`sha256` 實際是完整的 64 碼：

```json
{"attempted_at": "2026-09-24T00:55:21+00:00", "kind": "fetch", "target_slot": "2026-09-24 08:50", "outcome": "new", "http_status": 200, "data_time": "2026-09-24 08:50", "sha256": "3fa1c09e27bd…", "bytes": 36356, "elapsed_ms": 131, "etag": "\"80d4b358bf4bdd1:0\""}
{"attempted_at": "2026-09-24T01:05:21+00:00", "kind": "fetch", "target_slot": "2026-09-24 09:00", "outcome": "error", "error_type": "ConnectionResetError", "elapsed_ms": 88}
{"attempted_at": "2026-09-24T14:02:10+00:00", "kind": "resume", "detail": "{\"from\": \"2026-09-24T01:40:03+00:00\", \"to\": \"2026-09-24T14:02:10+00:00\"}"}
```

## 附錄 C：`taipower_align/realtime_units.csv`

```csv
unit_type,unit_name,grain,access_scope,plant_id,note
燃氣,大潭CC#1,unit,plant,8,
儲能負載,明潭#1,unit,plant,12,抽蓄機組的充電負載，值為負
太陽能,其它購電太陽能,bucket,shared,,購電太陽能全部併在這一列（2026-09-18 為 15,037.7 MW）
風力,沃一風,unit,shared,,民間離岸風場，不屬於 34 座電廠
```

`plant_id` 取自 `taipower_align/plants.csv`（大潭發電廠 8、明潭發電廠 12）。
