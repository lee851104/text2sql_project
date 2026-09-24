"""realtime.db: schema, writes and small reads (§5.2, §5.4).

只有收集器會寫入。連線用 autocommit（isolation_level=None），交易一律由 `transaction()` 明確開啟，
這樣 DDL 也能放進同一個交易，重建失敗時整份回滾。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ingest.realtime.decisions import Decisions, UnitDecision
from ingest.realtime.parse import ParsedSnapshot
from ingest.realtime.timeutil import utc_iso

SCHEMA_VERSION = "1"

VIEWS_IN_DROP_ORDER = ("v_rt_now", "v_rt_daily", "v_rt_10min")
TABLES_IN_DROP_ORDER = (
    "meta_rt_attempt",
    "fact_rt_day",
    "fact_rt_unit_daily",
    "fact_rt_quarantine",
    "fact_rt_type_subtotal",
    "fact_rt_unit_10min",
    "fact_rt_snapshot",
    "dim_rt_unit",
    "dim_rt_plant",
    "meta_rt_manifest",
)
ATTEMPT_COLUMNS = (
    "attempted_at",
    "kind",
    "target_slot",
    "outcome",
    "http_status",
    "error_type",
    "reject_code",
    "data_time",
    "sha256",
    "bytes",
    "elapsed_ms",
    "etag",
    "detail",
)

_GRAIN_LABEL = "CASE u.grain WHEN 'unit' THEN '個別' WHEN 'bucket' THEN '彙總' ELSE '未定' END"

SCHEMA = (
    """CREATE TABLE meta_rt_manifest (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version   TEXT    NOT NULL,
        built_at         TEXT    NOT NULL,
        build_kind       TEXT    NOT NULL CHECK (build_kind IN ('create', 'rebuild')),
        archive_files    INTEGER NOT NULL,
        decisions_sha256 TEXT    NOT NULL,
        plants_sha256    TEXT    NOT NULL
    )""",
    """CREATE TABLE dim_rt_plant (
        plant_id   INTEGER PRIMARY KEY,
        plant_name TEXT    NOT NULL UNIQUE
    )""",
    """CREATE TABLE dim_rt_unit (
        id            INTEGER PRIMARY KEY,
        unit_type     TEXT NOT NULL,
        unit_type_raw TEXT NOT NULL,
        unit_name     TEXT NOT NULL,
        flow          TEXT NOT NULL CHECK (flow IN ('generation', 'storage_load')),
        grain         TEXT NOT NULL CHECK (grain IN ('unit', 'bucket', 'undecided')),
        access_scope  TEXT NOT NULL CHECK (access_scope IN ('plant', 'shared', 'undecided')),
        plant_id      INTEGER REFERENCES dim_rt_plant(plant_id),
        decision_note TEXT NOT NULL DEFAULT '',
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL,
        UNIQUE (unit_type, unit_name),
        CHECK ((access_scope = 'plant') = (plant_id IS NOT NULL))
    )""",
    """CREATE TABLE fact_rt_snapshot (
        data_time   TEXT    PRIMARY KEY,
        sha256      TEXT    NOT NULL,
        fetched_at  TEXT    NOT NULL,
        revision    INTEGER NOT NULL DEFAULT 1,
        detail_rows INTEGER NOT NULL,
        quality     TEXT    NOT NULL CHECK (quality IN ('ok', 'warn')),
        warnings    TEXT    NOT NULL DEFAULT '[]'
    )""",
    """CREATE TABLE fact_rt_unit_10min (
        data_time    TEXT    NOT NULL REFERENCES fact_rt_snapshot(data_time),
        unit_id      INTEGER NOT NULL REFERENCES dim_rt_unit(id),
        net_mw       REAL,
        capacity_mw  REAL,
        load_ratio   REAL,
        note         TEXT    NOT NULL DEFAULT '',
        value_status TEXT    NOT NULL
                     CHECK (value_status IN ('ok', 'missing', 'invalid', 'comm_error')),
        PRIMARY KEY (data_time, unit_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_rt_unit_10min_unit ON fact_rt_unit_10min(unit_id, data_time)",
    """CREATE TABLE fact_rt_type_subtotal (
        data_time          TEXT NOT NULL REFERENCES fact_rt_snapshot(data_time),
        unit_type          TEXT NOT NULL,
        subtotal_name      TEXT NOT NULL,
        net_mw             REAL,
        net_share_pct      REAL,
        capacity_mw        REAL,
        capacity_share_pct REAL,
        detail_net_mw      REAL NOT NULL,
        PRIMARY KEY (data_time, unit_type)
    ) WITHOUT ROWID""",
    """CREATE TABLE fact_rt_quarantine (
        data_time TEXT    NOT NULL REFERENCES fact_rt_snapshot(data_time),
        row_index INTEGER NOT NULL,
        reason    TEXT    NOT NULL,
        raw_row   TEXT    NOT NULL,
        PRIMARY KEY (data_time, row_index)
    ) WITHOUT ROWID""",
    """CREATE TABLE fact_rt_unit_daily (
        date             TEXT    NOT NULL,
        unit_id          INTEGER NOT NULL REFERENCES dim_rt_unit(id),
        energy_mwh_est   REAL,
        max_mw           REAL,
        avg_mw           REAL,
        min_mw           REAL,
        samples          INTEGER NOT NULL,
        expected_samples INTEGER NOT NULL,
        notes_seen       TEXT    NOT NULL DEFAULT '',
        source           TEXT    NOT NULL DEFAULT 'live' CHECK (source IN ('live')),
        PRIMARY KEY (date, unit_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE fact_rt_day (
        date                  TEXT    PRIMARY KEY,
        snapshots             INTEGER NOT NULL,
        expected              INTEGER NOT NULL,
        missed_collector_down INTEGER NOT NULL,
        missed_fetch_failed   INTEGER NOT NULL,
        missed_rejected       INTEGER NOT NULL,
        rows_at_rollup        INTEGER NOT NULL,
        rolled_up_at          TEXT    NOT NULL,
        purged_at             TEXT,
        CHECK (snapshots + missed_collector_down + missed_fetch_failed + missed_rejected
               = expected)
    )""",
    """CREATE TABLE meta_rt_attempt (
        id           INTEGER PRIMARY KEY,
        attempted_at TEXT NOT NULL,
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
    )""",
    "CREATE INDEX idx_rt_attempt_slot ON meta_rt_attempt(target_slot)",
    f"""CREATE VIEW v_rt_10min AS
        SELECT f.data_time                        AS "資料時間",
               substr(f.data_time, 1, 10)         AS "日期",
               substr(f.data_time, 12, 5)         AS "時刻",
               u.unit_type || '|' || u.unit_name  AS "機組鍵",
               u.unit_type                        AS "機組類型",
               u.unit_name                        AS "機組名稱",
               {_GRAIN_LABEL}                     AS "粒度",
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
          LEFT JOIN dim_rt_plant AS p ON p.plant_id = u.plant_id""",
    """CREATE VIEW v_rt_now AS
        SELECT * FROM v_rt_10min
         WHERE "資料時間" = (SELECT MAX(data_time) FROM fact_rt_snapshot)""",
    f"""CREATE VIEW v_rt_daily AS
        SELECT d.date                                         AS "日期",
               u.unit_type || '|' || u.unit_name              AS "機組鍵",
               u.unit_type                                    AS "機組類型",
               u.unit_name                                    AS "機組名稱",
               {_GRAIN_LABEL}                                 AS "粒度",
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
          LEFT JOIN dim_rt_plant AS p ON p.plant_id = u.plant_id""",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def quick_check(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA quick_check").fetchone()[0])


def schema_version(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute(
            "SELECT schema_version FROM meta_rt_manifest WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else str(row[0])


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def drop_schema(connection: sqlite3.Connection) -> None:
    for view in VIEWS_IN_DROP_ORDER:
        connection.execute(f'DROP VIEW IF EXISTS "{view}"')
    for table in TABLES_IN_DROP_ORDER:
        connection.execute(f'DROP TABLE IF EXISTS "{table}"')


def write_manifest(
    connection: sqlite3.Connection,
    *,
    build_kind: str,
    archive_files: int,
    decisions: Decisions,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO meta_rt_manifest
               (id, schema_version, built_at, build_kind, archive_files,
                decisions_sha256, plants_sha256)
           VALUES (1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               schema_version = excluded.schema_version,
               built_at = excluded.built_at,
               build_kind = excluded.build_kind,
               archive_files = excluded.archive_files,
               decisions_sha256 = excluded.decisions_sha256,
               plants_sha256 = excluded.plants_sha256""",
        (
            SCHEMA_VERSION,
            utc_iso(now),
            build_kind,
            archive_files,
            decisions.sha256,
            decisions.plants_sha256,
        ),
    )


def sync_plants(connection: sqlite3.Connection, plants: Mapping[int, str]) -> None:
    connection.executemany(
        """INSERT INTO dim_rt_plant (plant_id, plant_name) VALUES (?, ?)
           ON CONFLICT(plant_id) DO UPDATE SET plant_name = excluded.plant_name""",
        sorted(plants.items()),
    )


def _decision_values(decision: UnitDecision | None) -> tuple[str, str, int | None, str]:
    if decision is None:
        return "undecided", "undecided", None, ""
    return decision.grain, decision.access_scope, decision.plant_id, decision.note


def sync_decisions(connection: sqlite3.Connection, decisions: Decisions) -> list[tuple[str, str]]:
    """把人工決定套到每一條序列；回傳檔案裡有、但來源從沒出現過的（過期）決定。"""
    sync_plants(connection, decisions.plants)
    known = {
        (unit_type, unit_name): unit_id
        for unit_id, unit_type, unit_name in connection.execute(
            "SELECT id, unit_type, unit_name FROM dim_rt_unit"
        )
    }
    for key, unit_id in known.items():
        grain, scope, plant_id, note = _decision_values(decisions.units.get(key))
        connection.execute(
            """UPDATE dim_rt_unit
                  SET grain = ?, access_scope = ?, plant_id = ?, decision_note = ?
                WHERE id = ?""",
            (grain, scope, plant_id, note, unit_id),
        )
    connection.execute(
        "UPDATE meta_rt_manifest SET decisions_sha256 = ?, plants_sha256 = ? WHERE id = 1",
        (decisions.sha256, decisions.plants_sha256),
    )
    return sorted(set(decisions.units) - set(known))


def _ensure_units(
    connection: sqlite3.Connection, parsed: ParsedSnapshot, decisions: Decisions | None
) -> dict[tuple[str, str], int]:
    ids: dict[tuple[str, str], int] = {}
    for detail in parsed.details:
        key = (detail.unit_type, detail.unit_name)
        row = connection.execute(
            "SELECT id FROM dim_rt_unit WHERE unit_type = ? AND unit_name = ?", key
        ).fetchone()
        if row is not None:
            ids[key] = row[0]
            connection.execute(
                """UPDATE dim_rt_unit
                      SET unit_type_raw = CASE WHEN ? >= last_seen THEN ? ELSE unit_type_raw END,
                          first_seen = min(first_seen, ?),
                          last_seen = max(last_seen, ?)
                    WHERE id = ?""",
                (
                    parsed.data_time,
                    detail.unit_type_raw,
                    parsed.data_time,
                    parsed.data_time,
                    row[0],
                ),
            )
            continue
        decision = decisions.units.get(key) if decisions is not None else None
        grain, scope, plant_id, note = _decision_values(decision)
        cursor = connection.execute(
            """INSERT INTO dim_rt_unit
                   (unit_type, unit_type_raw, unit_name, flow, grain, access_scope, plant_id,
                    decision_note, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                detail.unit_type,
                detail.unit_type_raw,
                detail.unit_name,
                detail.flow,
                grain,
                scope,
                plant_id,
                note,
                parsed.data_time,
                parsed.data_time,
            ),
        )
        ids[key] = int(cursor.lastrowid)
    return ids


def ingest_snapshot(
    connection: sqlite3.Connection,
    parsed: ParsedSnapshot,
    *,
    sha256: str,
    fetched_at: str,
    decisions: Decisions | None,
) -> str:
    """寫入一個時段，回傳 'new'、'revised' 或 'duplicate'。呼叫端負責交易，並先同步電廠名冊。"""
    existing = connection.execute(
        "SELECT sha256 FROM fact_rt_snapshot WHERE data_time = ?", (parsed.data_time,)
    ).fetchone()
    if existing is not None and existing[0] == sha256:
        return "duplicate"
    warnings = json.dumps(
        [{"code": w.code, "detail": w.detail} for w in parsed.warnings], ensure_ascii=False
    )
    if existing is not None:
        for table in ("fact_rt_unit_10min", "fact_rt_type_subtotal", "fact_rt_quarantine"):
            connection.execute(f"DELETE FROM {table} WHERE data_time = ?", (parsed.data_time,))
        connection.execute(
            """UPDATE fact_rt_snapshot
                  SET sha256 = ?, fetched_at = ?, revision = revision + 1,
                      detail_rows = ?, quality = ?, warnings = ?
                WHERE data_time = ?""",
            (sha256, fetched_at, len(parsed.details), parsed.quality, warnings, parsed.data_time),
        )
        outcome = "revised"
    else:
        connection.execute(
            """INSERT INTO fact_rt_snapshot
                   (data_time, sha256, fetched_at, revision, detail_rows, quality, warnings)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (parsed.data_time, sha256, fetched_at, len(parsed.details), parsed.quality, warnings),
        )
        outcome = "new"
    ids = _ensure_units(connection, parsed, decisions)
    connection.executemany(
        """INSERT INTO fact_rt_unit_10min
               (data_time, unit_id, net_mw, capacity_mw, load_ratio, note, value_status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                parsed.data_time,
                ids[(d.unit_type, d.unit_name)],
                d.net_mw,
                d.capacity_mw,
                d.load_ratio,
                d.note,
                d.value_status,
            )
            for d in parsed.details
        ],
    )
    connection.executemany(
        """INSERT INTO fact_rt_type_subtotal
               (data_time, unit_type, subtotal_name, net_mw, net_share_pct, capacity_mw,
                capacity_share_pct, detail_net_mw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                parsed.data_time,
                s.unit_type,
                s.subtotal_name,
                s.net_mw,
                s.net_share_pct,
                s.capacity_mw,
                s.capacity_share_pct,
                s.detail_net_mw,
            )
            for s in parsed.subtotals
        ],
    )
    connection.executemany(
        "INSERT INTO fact_rt_quarantine (data_time, row_index, reason, raw_row) "
        "VALUES (?, ?, ?, ?)",
        [(parsed.data_time, q.row_index, q.reason, q.raw_row) for q in parsed.quarantined],
    )
    return outcome


def record_attempt(connection: sqlite3.Connection, record: Mapping[str, object]) -> None:
    values = [record.get(column) for column in ATTEMPT_COLUMNS]
    values[-1] = values[-1] or ""
    placeholders = ", ".join("?" * len(ATTEMPT_COLUMNS))
    connection.execute(
        f"INSERT INTO meta_rt_attempt ({', '.join(ATTEMPT_COLUMNS)}) VALUES ({placeholders})",
        values,
    )


def latest_data_time(connection: sqlite3.Connection) -> str | None:
    return connection.execute("SELECT MAX(data_time) FROM fact_rt_snapshot").fetchone()[0]


def snapshot_fetched_at(connection: sqlite3.Connection, data_time: str) -> tuple[str, str] | None:
    """回傳（sha256, fetched_at）；該時段還沒有快照時回 None。"""
    row = connection.execute(
        "SELECT sha256, fetched_at FROM fact_rt_snapshot WHERE data_time = ?", (data_time,)
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))
