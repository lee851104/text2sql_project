PRAGMA foreign_keys = ON;

CREATE TABLE dim_plant (
    id INTEGER PRIMARY KEY,
    plant_name TEXT NOT NULL UNIQUE,
    postal_code TEXT,
    county TEXT,
    address TEXT,
    phone TEXT,
    fax TEXT,
    primary_fuel TEXT NOT NULL
);

CREATE TABLE dim_unit (
    id INTEGER PRIMARY KEY,
    plant_id INTEGER NOT NULL REFERENCES dim_plant(id),
    unit_name TEXT NOT NULL,
    commercial_date DATE,
    commercial_date_raw TEXT,
    commercial_date_precision TEXT CHECK (commercial_date_precision IN ('day', 'month')),
    capacity_kw INTEGER NOT NULL CHECK (capacity_kw > 0),
    fuel TEXT NOT NULL,
    UNIQUE (plant_id, unit_name)
);

CREATE TABLE dim_date (
    date DATE PRIMARY KEY,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    day INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31)
);

CREATE TABLE dim_b_column (
    id INTEGER PRIMARY KEY,
    b_column TEXT NOT NULL UNIQUE,
    grain TEXT NOT NULL,
    category TEXT NOT NULL,
    has_unit_master INTEGER NOT NULL CHECK (has_unit_master IN (0, 1))
);

CREATE TABLE bridge_b_column (
    id INTEGER PRIMARY KEY,
    b_column_id INTEGER NOT NULL UNIQUE REFERENCES dim_b_column(id),
    a_plant TEXT NOT NULL,
    n_plants INTEGER NOT NULL CHECK (n_plants >= 0),
    n_units INTEGER NOT NULL CHECK (n_units >= 0),
    cap_a_wankw REAL NOT NULL CHECK (cap_a_wankw >= 0),
    obs_max_b REAL NOT NULL CHECK (obs_max_b >= 0),
    ratio REAL NOT NULL CHECK (ratio >= 0),
    confidence TEXT NOT NULL,
    is_residual INTEGER NOT NULL CHECK (is_residual IN (0, 1)),
    is_bucket INTEGER NOT NULL CHECK (is_bucket IN (0, 1)),
    note TEXT NOT NULL
);

CREATE TABLE bridge_b_column_unit (
    b_column_id INTEGER NOT NULL REFERENCES dim_b_column(id),
    unit_id INTEGER NOT NULL REFERENCES dim_unit(id),
    PRIMARY KEY (b_column_id, unit_id)
);

CREATE TABLE fact_daily_peak (
    date DATE NOT NULL REFERENCES dim_date(date),
    b_column_id INTEGER NOT NULL REFERENCES dim_b_column(id),
    peak_wankw REAL,
    PRIMARY KEY (date, b_column_id)
);

CREATE TABLE fact_daily_system (
    date DATE PRIMARY KEY REFERENCES dim_date(date),
    net_peak_supply_wankw REAL,
    peak_load_wankw REAL,
    operating_reserve_wankw REAL,
    operating_reserve_rate_pct REAL,
    industrial_usage_million_kwh REAL,
    residential_usage_million_kwh REAL
);

CREATE TABLE dim_outage (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER REFERENCES dim_unit(id),
    source_unit_name TEXT NOT NULL,
    fuel TEXT,
    start_date DATE,
    end_date DATE,
    reason TEXT,
    date_status TEXT NOT NULL CHECK (date_status IN ('valid', 'invalid_range')),
    alignment_status TEXT NOT NULL DEFAULT 'unmatched'
);

CREATE TABLE fact_generation_cost (
    id INTEGER PRIMARY KEY,
    source_group TEXT NOT NULL,
    generation_type TEXT NOT NULL,
    year INTEGER NOT NULL CHECK (year BETWEEN 1900 AND 2200),
    accounting_basis TEXT NOT NULL,
    cost_per_kwh REAL NOT NULL CHECK (cost_per_kwh >= 0),
    UNIQUE (source_group, generation_type, year)
);

CREATE TABLE meta_pitfall (
    id INTEGER PRIMARY KEY,
    pitfall_code TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('column', 'plant', 'unit', 'global')),
    target_name TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('refuse', 'disclose', 'clarify')),
    reason TEXT NOT NULL,
    suggestion TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE (pitfall_code, target_kind, target_name)
);

CREATE TABLE meta_manifest (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version TEXT NOT NULL,
    data_version TEXT NOT NULL,
    data_start DATE NOT NULL,
    data_end DATE NOT NULL,
    built_at TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    table_counts TEXT NOT NULL,
    data_checksum TEXT NOT NULL
);

-- 帳號授權對照。dim_plant 只收錄有機組主檔的台電水火力電廠，但每日尖峰欄位還包含
-- 民營與核能電廠，因此授權名冊獨立成表，不影響星狀模型的既有列數與外鍵。
-- plant_id 1–22 及 b_column_id 必須與重建後的 dim_plant／dim_b_column 完全一致，
-- 由 build_db 在建庫時逐筆比對；編號一旦漂移就中止建庫，不容許靜默錯置。
CREATE TABLE dim_plant_scope (
    plant_id INTEGER PRIMARY KEY,
    plant_name TEXT NOT NULL UNIQUE,
    plant_type TEXT NOT NULL CHECK (plant_type IN ('水力', '火力', '核能')),
    fuel_types TEXT NOT NULL,
    ownership TEXT NOT NULL CHECK (ownership IN ('台電', '民營')),
    aliases TEXT NOT NULL DEFAULT ''
);

CREATE TABLE b_column_scope (
    b_column_id INTEGER PRIMARY KEY REFERENCES dim_b_column(id),
    access_scope TEXT NOT NULL CHECK (access_scope IN ('plant', 'shared')),
    membership_status TEXT NOT NULL CHECK (membership_status IN ('complete', 'partial', 'unknown')),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE bridge_b_column_plant (
    b_column_id INTEGER NOT NULL REFERENCES dim_b_column(id),
    plant_id INTEGER NOT NULL REFERENCES dim_plant_scope(plant_id),
    PRIMARY KEY (b_column_id, plant_id)
);

CREATE TABLE outage_scope (
    outage_id INTEGER PRIMARY KEY REFERENCES dim_outage(id),
    plant_id INTEGER NOT NULL REFERENCES dim_plant_scope(plant_id),
    mapping_level TEXT NOT NULL CHECK (mapping_level IN ('unit', 'plant_only'))
);

CREATE INDEX idx_daily_peak_column_date ON fact_daily_peak (b_column_id, date);
CREATE INDEX idx_b_column_plant_plant ON bridge_b_column_plant (plant_id);
CREATE INDEX idx_outage_scope_plant ON outage_scope (plant_id);
CREATE INDEX idx_unit_name ON dim_unit (unit_name);
CREATE INDEX idx_pitfall_target ON meta_pitfall (target_kind, target_name);
CREATE INDEX idx_generation_cost_year_type ON fact_generation_cost (year, generation_type);

CREATE VIEW v_unit AS
SELECT
    u.unit_name AS "機組名",
    p.plant_name AS "電廠",
    p.county AS "縣市",
    u.capacity_kw AS "裝置容量_瓩",
    ROUND(u.capacity_kw / 10000.0, 4) AS "裝置容量_萬瓩",
    u.fuel AS "燃料",
    u.commercial_date AS "商轉日期",
    u.commercial_date_precision AS "商轉日期精度"
FROM dim_unit AS u
JOIN dim_plant AS p ON p.id = u.plant_id;

CREATE VIEW v_peak AS
SELECT
    f.date AS "日期",
    c.b_column AS "機組欄位",
    f.peak_wankw AS "尖峰出力_萬瓩",
    COALESCE(b.a_plant, '') AS "電廠",
    c.grain AS "粒度",
    c.category AS "類別",
    COALESCE(b.n_units, 0) AS "涵蓋機組數",
    b.cap_a_wankw AS "對應裝置容量_萬瓩",
    COALESCE(b.is_residual, 0) AS "是殘差欄",
    COALESCE(b.is_bucket, 0) AS "是彙總欄",
    c.has_unit_master AS "有機組主檔"
FROM fact_daily_peak AS f
JOIN dim_b_column AS c ON c.id = f.b_column_id
LEFT JOIN bridge_b_column AS b ON b.b_column_id = c.id;

CREATE VIEW v_system AS
SELECT
    date AS "日期",
    net_peak_supply_wankw AS "淨尖峰供電能力_萬瓩",
    peak_load_wankw AS "尖峰負載_萬瓩",
    operating_reserve_wankw AS "備轉容量_萬瓩",
    operating_reserve_rate_pct AS "備轉容量率_pct",
    industrial_usage_million_kwh AS "工業用電_百萬度",
    residential_usage_million_kwh AS "民生用電_百萬度"
FROM fact_daily_system;

CREATE VIEW v_outage AS
SELECT
    o.id AS "歲修編號",
    COALESCE(u.unit_name, o.source_unit_name) AS "機組名",
    p.plant_name AS "電廠",
    o.fuel AS "燃料",
    o.start_date AS "開始日期",
    o.end_date AS "結束日期",
    o.reason AS "原因",
    o.date_status AS "日期狀態",
    o.alignment_status AS "對齊狀態"
FROM dim_outage AS o
LEFT JOIN dim_unit AS u ON u.id = o.unit_id
LEFT JOIN dim_plant AS p ON p.id = u.plant_id;

CREATE VIEW v_generation_cost AS
SELECT
    year AS "年度",
    source_group AS "電力來源",
    generation_type AS "發電方式",
    cost_per_kwh AS "成本_元每度",
    accounting_basis AS "決算類型"
FROM fact_generation_cost;
