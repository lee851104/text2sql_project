"""Import d006010 (past unit generation) into realtime.db (RT-2 spec).

d006010 是一份約 196 MB 的 JSON，每 10 分鐘一筆、每部機組一條序列，台電每月更新、涵蓋約三個月，
實測比現在晚約四個月（2026-09-30 更新的檔案涵蓋 2026-03-01～05-31）。它補的是「收集器開始收集
以前」的歷史，所以只存成每日彙總（10 分鐘明細的保留期只有 14 天，存了也會立刻被清掉）。

原則與 RT-1 相同：

- **封存是唯一的真實來源**：原檔 gzip 封存、檔名帶 SHA-256；`rebuild` 會重放所有封存。
- **結構有問題整份不收**，數值有問題逐筆計數後略過。
- **機組對照是人工決定**（`taipower_align/realtime_history_units.csv`），執行期間不做字串比對推測；
  對照檔沒列到的序列照收，用它自己的名字建成「未定」的機組，電廠帳號看不到。
"""

from __future__ import annotations

import codecs
import csv
import gzip
import hashlib
import json
import math
import re
import shutil
import sqlite3
import tempfile
from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from ingest.realtime.config import RealtimeConfig
from ingest.realtime.decisions import Decisions
from ingest.realtime.store import transaction
from ingest.realtime.timeutil import utc_iso

CATALOG = "UNIT_NET_P"
SLOT_MINUTES = 10
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
CHUNK_BYTES = 8 << 20
_MAX_PENDING_BYTES = 1 << 20
_HEADER_BYTES = 4096
_HEADER = re.compile(rb'^\s*\{\s*"records"\s*:\s*\{(?P<meta>.*?)"NET_P"\s*:\s*\[', re.DOTALL)
_RECORD = re.compile(
    rb'\{"FUEL_TYPE":"(?P<type>[^"\\]*)","UNIT_NAME":"(?P<name>[^"\\]*)",'
    rb'"DATETIME":"(?P<time>[^"\\]*)","NET_P":"(?P<value>[^"\\]*)"\}'
)
_SEPARATOR = re.compile(rb"\s*,\s*")
_TRAILER = re.compile(rb"^\s*\]\s*\}\s*\}\s*$")
# 對照檔裡出現的類型別名（同一套規則寫在對照檔的 basis 欄，這裡只給沒列到的序列一個合理的類型）。
TYPE_ALIASES = {"燃油": "燃料油", "輕油": "燃料油", "抽蓄發電": "儲能"}
MAPPING_HEADER = ("history_type", "history_name", "unit_type", "unit_name", "relation", "basis")


class HistoryRejected(ValueError):
    """The whole file is structurally wrong; nothing from it is imported."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass
class _Series:
    values: array  # 每個時段一格，NaN = 沒有值


@dataclass(frozen=True)
class DailyAggregate:
    day: str
    energy_mwh_est: float | None
    max_mw: float | None
    avg_mw: float | None
    min_mw: float | None
    samples: int


@dataclass
class ParsedHistory:
    start_date: str  # 'YYYY-MM-DD'
    end_date: str
    records: int = 0
    duplicates: int = 0
    conflicts: int = 0
    invalid_values: int = 0
    out_of_range: int = 0
    series: dict[tuple[str, str], _Series] = field(default_factory=dict)

    @property
    def days(self) -> list[str]:
        start, end = date.fromisoformat(self.start_date), date.fromisoformat(self.end_date)
        return [
            (start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)
        ]

    def daily(self, key: tuple[str, str]) -> Iterator[DailyAggregate]:
        values = self.series[key].values
        for index, day in enumerate(self.days):
            window = [
                value
                for value in values[index * SLOTS_PER_DAY : (index + 1) * SLOTS_PER_DAY]
                if not math.isnan(value)
            ]
            if not window:
                continue
            total = sum(window)
            yield DailyAggregate(
                day=day,
                energy_mwh_est=total * SLOT_MINUTES / 60.0,
                max_mw=max(window),
                avg_mw=total / len(window),
                min_mw=min(window),
                samples=len(window),
            )


def _without_bom(header: bytes) -> bytes:
    # 台電的 JSON 開頭帶 UTF-8 BOM（d006001 也是，parse.py 用 utf-8-sig 解）。
    return header.removeprefix(codecs.BOM_UTF8)


def _meta(header: bytes) -> dict[str, object]:
    found = _HEADER.match(_without_bom(header))
    if found is None:
        raise HistoryRejected("HIST_PAYLOAD_INVALID", '開頭不是 {"records": {…, "NET_P": [')
    try:
        return json.loads(b"{" + found.group("meta").rstrip().rstrip(b",") + b"}")
    except json.JSONDecodeError as error:
        raise HistoryRejected("HIST_PAYLOAD_INVALID", f"表頭解析失敗：{error}") from error


def _slot_date(text: str) -> date:
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as error:
        raise HistoryRejected(
            "HIST_PAYLOAD_INVALID", f"START_DATE／END_DATE 不是日期：{text!r}"
        ) from error


def parse_history(handle: BinaryIO) -> ParsedHistory:
    """Stream the file once; memory is one float per slot per series (~20 MB for 3 months)."""

    header = _without_bom(handle.read(_HEADER_BYTES))
    meta = _meta(header)
    if meta.get("CATALOG") != CATALOG:
        raise HistoryRejected(
            "HIST_UNEXPECTED_FORMAT", f"CATALOG 不是 {CATALOG}：{meta.get('CATALOG')!r}"
        )
    if meta.get("INTERVAL") != "10MIN" or meta.get("UNIT_OF_MEASUREMENT") != "MW":
        raise HistoryRejected(
            "HIST_UNEXPECTED_FORMAT",
            f"INTERVAL／單位不是 10MIN／MW：{meta.get('INTERVAL')!r}／"
            f"{meta.get('UNIT_OF_MEASUREMENT')!r}",
        )
    start = _slot_date(str(meta.get("START_DATE")))
    end = _slot_date(str(meta.get("END_DATE")))
    if end < start or (end - start).days > 400:
        raise HistoryRejected("HIST_UNEXPECTED_FORMAT", f"期間不合理：{start}～{end}")
    parsed = ParsedHistory(start.isoformat(), end.isoformat())
    slots = ((end - start).days + 1) * SLOTS_PER_DAY
    origin = datetime(start.year, start.month, start.day)

    body = header[_HEADER.match(header).end() :]
    expect_separator = False
    while True:
        chunk = handle.read(CHUNK_BYTES)
        body += chunk
        position = 0
        while True:
            if expect_separator:
                separator = _SEPARATOR.match(body, position)
                if separator is None or separator.end() == len(body):
                    break
                position = separator.end()
                expect_separator = False
            record = _RECORD.match(body, position)
            if record is None:
                break
            position = record.end()
            expect_separator = True
            _accept(parsed, record, origin, slots)
        body = body[position:]
        if not chunk:
            break
        if len(body) > _MAX_PENDING_BYTES:
            # 一筆紀錄不到 100 bytes；累積這麼多還解不出來，就是格式變了。
            # 不要等整份檔案都讀進記憶體才發現。
            raise HistoryRejected(
                "HIST_RECORD_UNPARSEABLE",
                f"第 {parsed.records + 1} 筆附近有不認得的內容：{body[:80]!r}",
            )
    if not _TRAILER.match(body):
        raise HistoryRejected(
            "HIST_RECORD_UNPARSEABLE",
            f"第 {parsed.records + 1} 筆附近有不認得的內容：{body[:80]!r}",
        )
    if not parsed.series:
        raise HistoryRejected("HIST_NO_RECORDS", "檔案裡沒有任何一筆資料。")
    return parsed


def _accept(parsed: ParsedHistory, record: re.Match[bytes], origin: datetime, slots: int) -> None:
    parsed.records += 1
    key = (record["type"].decode("utf-8"), record["name"].decode("utf-8"))
    try:
        moment = datetime.fromisoformat(record["time"].decode("ascii"))
    except ValueError:
        parsed.out_of_range += 1
        return
    offset = moment - origin
    minutes = offset.days * 24 * 60 + offset.seconds // 60
    index, remainder = divmod(minutes, SLOT_MINUTES)
    if remainder or offset.seconds % 60 or not 0 <= index < slots:
        parsed.out_of_range += 1
        return
    try:
        value = float(record["value"])
    except ValueError:
        parsed.invalid_values += 1
        return
    if not math.isfinite(value):
        parsed.invalid_values += 1
        return
    series = parsed.series.get(key)
    if series is None:
        series = parsed.series[key] = _Series(array("d", [math.nan]) * slots)
    current = series.values[index]
    if not math.isnan(current):
        parsed.duplicates += 1
        if current != value:
            parsed.conflicts += 1  # 同一時段兩個不同的值：以後到的為準，次數留在匯入紀錄裡
    series.values[index] = value


# --- 對照 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryMapping:
    units: dict[tuple[str, str], tuple[str, str]]  # (d006010 類型, 名稱) → (機組類型, 機組名稱)
    sha256: str

    def target(self, key: tuple[str, str]) -> tuple[str, str] | None:
        return self.units.get(key)


def load_mapping(path: Path | None) -> HistoryMapping:
    if path is None or not path.is_file():
        return HistoryMapping({}, "")
    raw = path.read_bytes()
    reader = csv.reader(raw.decode("utf-8-sig").splitlines())
    header = tuple(next(reader, ()))
    if header != MAPPING_HEADER:
        raise ValueError(f"{path.name} 的欄位必須是 {','.join(MAPPING_HEADER)}，收到 {header}")
    units: dict[tuple[str, str], tuple[str, str]] = {}
    targets: dict[tuple[str, str], tuple[str, str]] = {}
    for line, row in enumerate(reader, start=2):
        if len(row) != len(MAPPING_HEADER):
            raise ValueError(f"{path.name} 第 {line} 列欄位數不對。")
        key, target = (row[0], row[1]), (row[2], row[3])
        if key in units:
            raise ValueError(f"{path.name} 第 {line} 列重複：{key}")
        # 兩條序列對到同一部機組，每日彙總會互相覆蓋，而且看不出來；整份對照當成無效。
        if target in targets:
            raise ValueError(
                f"{path.name} 第 {line} 列與 {targets[target]} 對到同一部機組 {target}"
            )
        units[key] = target
        targets[target] = key
    return HistoryMapping(units, hashlib.sha256(raw).hexdigest())


# --- 封存 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchivedHistory:
    path: Path
    sha256: str


def _sha256_of(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def archive_history(source: Path, archive_dir: Path) -> ArchivedHistory:
    """gzip 封存原檔；內容相同只存一次。檔名不含期間：期間要讀了才知道，而封存要先於解析。"""

    with source.open("rb") as handle:
        sha = _sha256_of(handle)
    target = archive_dir / f"d006010_{sha[:16]}.json.gz"
    if target.is_file():
        return ArchivedHistory(target, sha)
    archive_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=archive_dir)
    temporary = Path(temporary_name)
    try:
        with (
            source.open("rb") as raw,
            open(descriptor, "wb") as out,
            gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as compressed,
        ):
            shutil.copyfileobj(raw, compressed, CHUNK_BYTES)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return ArchivedHistory(target, sha)


def iter_history_archive(archive_dir: Path) -> Iterator[Path]:
    if archive_dir.is_dir():
        yield from sorted(archive_dir.glob("d006010_*.json.gz"))


def _archived_sha(path: Path) -> str:
    with gzip.open(path, "rb") as handle:
        return _sha256_of(handle)


# --- 寫入 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportReport:
    sha256: str
    start_date: str
    end_date: str
    records: int
    series: int
    mapped_series: int
    days: int
    duplicates: int
    conflicts: int
    invalid_values: int
    out_of_range: int
    unmapped: tuple[tuple[str, str], ...]
    skipped: bool = False


def _unit_id(
    connection: sqlite3.Connection,
    key: tuple[str, str],
    decisions: Decisions | None,
    first: str,
    last: str,
) -> int:
    row = connection.execute(
        "SELECT id FROM dim_rt_unit WHERE unit_type = ? AND unit_name = ?", key
    ).fetchone()
    if row is not None:
        connection.execute(
            "UPDATE dim_rt_unit SET first_seen = min(first_seen, ?), last_seen = max(last_seen, ?) "
            "WHERE id = ?",
            (first, last, row[0]),
        )
        return int(row[0])
    decision = decisions.units.get(key) if decisions is not None else None
    cursor = connection.execute(
        """INSERT INTO dim_rt_unit
               (unit_type, unit_type_raw, unit_name, flow, grain, access_scope, plant_id,
                decision_note, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            key[0],
            key[0],
            key[1],
            "storage_load" if key[0] == "儲能負載" else "generation",
            decision.grain if decision else "undecided",
            decision.access_scope if decision else "undecided",
            decision.plant_id if decision else None,
            decision.note if decision else "",
            first,
            last,
        ),
    )
    return int(cursor.lastrowid)


def write_history(
    connection: sqlite3.Connection,
    parsed: ParsedHistory,
    *,
    sha256: str,
    archive: str,
    mapping: HistoryMapping,
    decisions: Decisions | None,
    now: datetime,
) -> ImportReport:
    """把一份已解析的 d006010 寫進 realtime.db。呼叫端負責交易。

    同一天之前匯入過的歷史整天換掉：台電每月重發，後到的那份是修正版。
    """

    days = parsed.days
    placeholders = ", ".join("?" for _ in days)
    connection.execute(
        f"DELETE FROM fact_rt_unit_daily_history WHERE date IN ({placeholders})", days
    )
    connection.execute(f"DELETE FROM fact_rt_history_day WHERE date IN ({placeholders})", days)
    first, last = f"{parsed.start_date} 00:00", f"{parsed.end_date} 23:50"
    unmapped: list[tuple[str, str]] = []
    totals: dict[str, list[int]] = {day: [0, 0] for day in days}  # 日期 → [序列數, 取樣點數]
    for history_key in sorted(parsed.series):
        target = mapping.target(history_key)
        if target is None:
            unmapped.append(history_key)
            target = (TYPE_ALIASES.get(history_key[0], history_key[0]), history_key[1])
        unit_id = _unit_id(connection, target, decisions, first, last)
        rows = []
        for aggregate in parsed.daily(history_key):
            rows.append(
                (
                    aggregate.day,
                    unit_id,
                    aggregate.energy_mwh_est,
                    aggregate.max_mw,
                    aggregate.avg_mw,
                    aggregate.min_mw,
                    aggregate.samples,
                    SLOTS_PER_DAY,
                    sha256,
                )
            )
            totals[aggregate.day][0] += 1
            totals[aggregate.day][1] += aggregate.samples
        # 對照檔已保證一對一；沒列到的序列用自己的名字，理論上也不會撞名，OR REPLACE 只是保險。
        connection.executemany(
            """INSERT OR REPLACE INTO fact_rt_unit_daily_history
                   (date, unit_id, energy_mwh_est, max_mw, avg_mw, min_mw, samples,
                    expected_samples, import_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    connection.executemany(
        """INSERT INTO fact_rt_history_day (date, series, samples, expected, import_sha256)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (day, count, samples, count * SLOTS_PER_DAY, sha256)
            for day, (count, samples) in totals.items()
            if count
        ],
    )
    report = ImportReport(
        sha256=sha256,
        start_date=parsed.start_date,
        end_date=parsed.end_date,
        records=parsed.records,
        series=len(parsed.series),
        mapped_series=len(parsed.series) - len(unmapped),
        days=sum(1 for count, _samples in totals.values() if count),
        duplicates=parsed.duplicates,
        conflicts=parsed.conflicts,
        invalid_values=parsed.invalid_values,
        out_of_range=parsed.out_of_range,
        unmapped=tuple(unmapped),
    )
    connection.execute(
        """INSERT INTO meta_rt_history_import
               (sha256, archive, start_date, end_date, imported_at, records, series,
                mapped_series, duplicates, conflicts, invalid_values, out_of_range, unmapped)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(sha256) DO UPDATE SET
               archive = excluded.archive, imported_at = excluded.imported_at""",
        (
            sha256,
            archive,
            report.start_date,
            report.end_date,
            utc_iso(now),
            report.records,
            report.series,
            report.mapped_series,
            report.duplicates,
            report.conflicts,
            report.invalid_values,
            report.out_of_range,
            json.dumps([list(key) for key in report.unmapped], ensure_ascii=False),
        ),
    )
    return report


def imported_hashes(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT sha256 FROM meta_rt_history_import")}


def _import_archived(
    connection: sqlite3.Connection,
    path: Path,
    *,
    mapping: HistoryMapping,
    decisions: Decisions | None,
    now: datetime,
) -> ImportReport:
    sha = _archived_sha(path)
    with gzip.open(path, "rb") as handle:
        parsed = parse_history(handle)
    return write_history(
        connection,
        parsed,
        sha256=sha,
        archive=path.name,
        mapping=mapping,
        decisions=decisions,
        now=now,
    )


def replay_history(
    connection: sqlite3.Connection,
    config: RealtimeConfig,
    decisions: Decisions | None,
    *,
    now: datetime,
    only_pending: bool = False,
) -> list[ImportReport]:
    """重放封存的 d006010。依期間由舊到新，重疊的日子由較新的那份覆蓋。

    `only_pending` 時只處理還沒匯入過的封存（收集器啟動時用）；呼叫端負責交易。
    壞掉的封存檔（gzip 截斷、內容被改）略過，不讓一份壞檔擋住其他的。
    """

    mapping = load_mapping(config.history.units_csv)
    done = imported_hashes(connection) if only_pending else set()
    candidates: list[tuple[str, str, Path]] = []
    for path in iter_history_archive(config.history_archive_dir):
        try:
            with gzip.open(path, "rb") as handle:
                meta = _meta(handle.read(_HEADER_BYTES))
        except (OSError, EOFError, HistoryRejected):
            continue
        candidates.append((str(meta.get("END_DATE")), str(meta.get("START_DATE")), path))
    reports = []
    for _end, _start, path in sorted(candidates):
        try:
            sha = _archived_sha(path)
        except (OSError, EOFError):
            continue
        if not path.name.startswith(f"d006010_{sha[:16]}") or sha in done:
            continue
        try:
            reports.append(
                _import_archived(connection, path, mapping=mapping, decisions=decisions, now=now)
            )
        except (HistoryRejected, OSError, EOFError):
            continue
    return reports


def import_file(
    connection: sqlite3.Connection,
    source: Path,
    config: RealtimeConfig,
    decisions: Decisions | None,
    *,
    now: datetime,
) -> ImportReport:
    """封存一份 d006010 再匯入。內容與已匯入的相同時什麼都不做。"""

    with source.open("rb") as handle:
        parse_history(handle)  # 結構有問題就在封存之前停下，不留一份收不進來的封存
    archived = archive_history(source, config.history_archive_dir)
    if archived.sha256 in imported_hashes(connection):
        row = connection.execute(
            "SELECT start_date, end_date, records, series, mapped_series, duplicates, conflicts, "
            "invalid_values, out_of_range, unmapped FROM meta_rt_history_import WHERE sha256 = ?",
            (archived.sha256,),
        ).fetchone()
        return ImportReport(
            archived.sha256,
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            0,
            row[5],
            row[6],
            row[7],
            row[8],
            tuple(tuple(item) for item in json.loads(row[9])),
            skipped=True,
        )
    with transaction(connection):
        return _import_archived(
            connection,
            archived.path,
            mapping=load_mapping(config.history.units_csv),
            decisions=decisions,
            now=now,
        )


def download(config: RealtimeConfig, *, opener=None) -> Path:
    """把 d006010 串流下載到 history/download/；超過 max_bytes 就停，不留半份檔案。"""

    import urllib.request

    from ingest.realtime.client import USER_AGENT, default_opener

    destination_dir = config.history_download_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(config.history.url, headers={"User-Agent": USER_AGENT})
    descriptor, temporary_name = tempfile.mkstemp(prefix=".d006010-", dir=destination_dir)
    temporary = Path(temporary_name)
    try:
        with (
            (opener or default_opener)(request, config.history.timeout_seconds) as response,
            open(descriptor, "wb") as out,
        ):
            total = 0
            while chunk := response.read(CHUNK_BYTES):
                total += len(chunk)
                if total > config.history.max_bytes:
                    raise HistoryRejected(
                        "HIST_TOO_LARGE",
                        f"超過 {config.history.max_bytes:,} bytes 上限，已停止下載。",
                    )
                out.write(chunk)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = destination_dir / f"d006010-{stamp}.json"
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


AUTO_STATE_NAME = "auto-check.json"


def remote_version(config: RealtimeConfig, *, opener=None) -> str | None:
    """台電目前那份 d006010 的版本（ETag，沒有就 Last-Modified）；只發 HEAD，不下載。"""

    import urllib.request

    from ingest.realtime.client import USER_AGENT, default_opener

    request = urllib.request.Request(
        config.history.url, headers={"User-Agent": USER_AGENT}, method="HEAD"
    )
    with (opener or default_opener)(request, 60) as response:
        headers = response.headers
        return headers.get("ETag") or headers.get("Last-Modified")


def _auto_state_path(config: RealtimeConfig) -> Path:
    return config.root / "history" / AUTO_STATE_NAME


def _read_auto_state(config: RealtimeConfig) -> dict[str, str]:
    try:
        payload = json.loads(_auto_state_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}


def _write_auto_state(config: RealtimeConfig, state: dict[str, str]) -> None:
    path = _auto_state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def auto_import(
    connection: sqlite3.Connection,
    config: RealtimeConfig,
    decisions: Decisions | None,
    *,
    now: datetime,
    opener=None,
) -> ImportReport | None:
    """收集器的每日檢查：台電有新版 d006010 才下載匯入；沒到時間、沒有新版時回 None。

    先記下「這次檢查過了」再連網：下載失敗時要隔 `check_hours` 才重試，
    不能每小時抓一次 200 MB。原始下載檔匯入後就刪掉，位元組留在 gzip 封存裡。
    """

    hours = config.history.check_hours
    if hours <= 0:
        return None
    state = _read_auto_state(config)
    checked = state.get("checked_at")
    if checked:
        try:
            if now - datetime.fromisoformat(checked) < timedelta(hours=hours):
                return None
        except ValueError:
            pass
    state["checked_at"] = utc_iso(now)
    _write_auto_state(config, state)
    version = remote_version(config, opener=opener)
    if not version or version == state.get("imported_version"):
        return None
    source = download(config, opener=opener)
    try:
        report = import_file(connection, source, config, decisions, now=now)
    finally:
        source.unlink(missing_ok=True)
    state["imported_version"] = version
    _write_auto_state(config, state)
    return report


def summary(connection: sqlite3.Connection) -> dict[str, object]:
    """status 用：匯入過幾份、涵蓋哪幾天。"""

    try:
        imports = connection.execute("SELECT COUNT(*) FROM meta_rt_history_import").fetchone()[0]
        first, last, days = connection.execute(
            "SELECT MIN(date), MAX(date), COUNT(*) FROM fact_rt_history_day"
        ).fetchone()
    except sqlite3.OperationalError:
        return {"imports": 0, "first_day": None, "last_day": None, "days": 0}
    return {"imports": imports, "first_day": first, "last_day": last, "days": days}


__all__ = [
    "ArchivedHistory",
    "HistoryMapping",
    "HistoryRejected",
    "ImportReport",
    "ParsedHistory",
    "archive_history",
    "import_file",
    "load_mapping",
    "parse_history",
    "replay_history",
    "summary",
    "write_history",
]
