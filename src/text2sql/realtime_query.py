"""Answer Chinese questions about realtime generation from realtime.db (RT-3b spec).

即時問句在既有管線之前分流：問句用詞（現在、今天、昨天⋯）決定查哪個資料庫，不是機組名稱 ——
兩套命名有約 21 個同名（例如 `台中#1`），靠名稱分流會把「現在」的問題送去 `v_peak`。

這條路徑只查 `v_rt_*`，同一句 SQL 混用 `power.db` 的檢視直接拒絕：兩邊的機組名稱沒有人工對照，
JOIN 出來只會「看起來對上了」。權限沿用 `realtime_scope.visible_unit_keys()`，與總覽頁面板同一份。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from time import perf_counter
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from ingest.realtime.config import RealtimeConfig
from ingest.realtime.maintenance import day_slots
from ingest.realtime.status import read_status
from ingest.realtime.timeutil import local_date
from text2sql.db import ReadOnlySQLite
from text2sql.entities import extract_entities
from text2sql.llm import RETRYABLE_CATEGORY, LLMProtocol, classify_error, parse_generated_query
from text2sql.pipeline import PipelineResponse
from text2sql.realtime_prompt import build_realtime_prompt
from text2sql.realtime_scope import RealtimeScope, UnitRow, visible_unit_keys
from text2sql.scope_guard import _MARKER, ScopeRewriteError, _name_original_placeholders
from text2sql.sql_guard import SqlGuard

_TEN_MINUTE_COLUMNS = {
    "資料時間",
    "日期",
    "時刻",
    "機組鍵",
    "機組類型",
    "機組名稱",
    "粒度",
    "電廠",
    "裝置容量_MW",
    "淨發電量_MW",
    "出力比",
    "備註",
    "數值狀態",
}
REALTIME_ALLOWED_COLUMNS: dict[str, set[str]] = {
    "v_rt_now": set(_TEN_MINUTE_COLUMNS),
    "v_rt_10min": set(_TEN_MINUTE_COLUMNS),
    "v_rt_daily": {
        "日期",
        "機組鍵",
        "機組類型",
        "機組名稱",
        "粒度",
        "電廠",
        "估算發電量_MWh",
        "最高出力_MW",
        "平均出力_MW",
        "最低出力_MW",
        "取樣點數",
        "應有點數",
        "資料完整度",
        "當日備註",
        "資料來源",
    },
}
REALTIME_VIEWS = frozenset(REALTIME_ALLOWED_COLUMNS)
SCOPE_KEY = "機組鍵"
OK = "正常"
COMPLETENESS_THRESHOLD = 0.9

# 強的時間詞一出現就是在問「此刻／某一天」；弱的（目前、最新）要配上出力或發電才算，
# 否則「哪一些機組目前維修中」（歲修排程）、「資料最新一天的尖峰負載」會被搶走。
STRONG_WORDS = ("現在", "即時", "此刻", "剛剛", "今天", "今日", "昨天", "昨日", "前天", "這一小時")
WEAK_WORDS = ("目前", "最新")
_HOURS_RE = re.compile(r"(?:過去|最近|近)\s*[0-9一二兩三四五六七八九十]+\s*(?:個)?小時")
OUTPUT_WORDS = (
    "出力",
    "發電",
    "發了",
    "發多少",
    "度電",
    "幾度",
    "MW",
    "瓩",
    "出力比",
    "負載率",
)
STATUS_WORDS = ("機組", "通訊異常", "停機", "歲修", "檢修", "降載", "異常", "試俥")
DATA_SCOPE_WORDS = ("資料", "涵蓋", "期間", "範圍")
ENERGY_WORDS = ("發電量", "度電", "幾度", "多少度", "MWh", "發了多少")
TOP_WORDS = ("最高", "最多", "最大", "前", "排名", "排行")
LIST_WORDS = ("哪些", "列出", "清單", "各機組", "每部", "每個機組", "排名", "排行", "前")
PEAK_WORDS = ("最高出力", "最大出力", "尖峰", "最高")
# 規則沒有模型化的問法。遇到就不硬套最接近的模板（那會給出形狀對、意思錯的答案），
# 交給 LLM；沒有 LLM 就請使用者換個問法。
UNROUTED_WORDS = (
    "最低",
    "最少",
    "最小",
    "平均",
    "比較",
    "相比",
    "差多少",
    "變化",
    "趨勢",
    "幾點",
    "什麼時候",
    "何時",
    "增加",
    "減少",
    "上週",
    "上星期",
    "小時",
    "比例",
    "占比",
    "佔比",
)
NOTE_WORDS = ("歲修", "環保停機", "檢修", "降載", "停機", "試俥", "故障", "合約", "減排")

# 問句裡的說法 → realtime.db 的機組類型。「儲能」不含「儲能負載」：放電與充電要分開問。
TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "民營電廠-燃氣": ("民營燃氣", "民營電廠燃氣", "民營電廠-燃氣", "IPP燃氣"),
    "民營電廠-燃煤": ("民營燃煤", "民營電廠燃煤", "民營電廠-燃煤", "IPP燃煤"),
    "燃氣": ("燃氣", "天然氣", "LNG"),
    "燃煤": ("燃煤", "煤"),
    "燃料油": ("燃料油", "燃油", "重油"),
    "汽電共生": ("汽電共生",),
    "太陽能": ("太陽能", "太陽光電", "光電"),
    "風力": ("風力", "風電", "風機"),
    "水力": ("水力", "水電"),
    "儲能負載": ("儲能負載", "充電"),
    "儲能": ("儲能", "放電", "電池"),
    "其它再生能源": ("其它再生能源", "其他再生能源", "地熱", "生質"),
    "核能": ("核能", "核電"),
}
# 「燃氣」「燃煤」沒說民營時，指的是全部（台電與民營）。
TYPE_FAMILIES = {"燃氣": ("燃氣", "民營電廠-燃氣"), "燃煤": ("燃煤", "民營電廠-燃煤")}
_NOTE_SUFFIX = re.compile(r"\(註\d+\)")
# 問句裡看起來像機組名的片段：`台中#1`、`大潭CC#7`、`台中1號機`（先轉成 `台中#1`）。
_UNIT_TOKEN = re.compile(r"([\u4e00-\u9fffA-Za-z&]{1,10}#\d+)")
_UNIT_ORDINAL = re.compile(r"(\d+)號機")
# 「2026年4月1日到5月10日」：共用的 extract_entities 只認得第一天，會靜默變成問一天。
_DAY_RANGE = re.compile(
    r"(?P<y1>\d{4})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日?\s*(?:到|至|~|～|-)\s*"
    r"(?:(?P<y2>\d{4})年)?(?:(?P<m2>\d{1,2})月)?(?P<d2>\d{1,2})日?"
)


def day_range(question: str) -> tuple[str, str] | None:
    found = _DAY_RANGE.search(
        question.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    )
    if found is None:
        return None
    year1, month1, day1 = int(found["y1"]), int(found["m1"]), int(found["d1"])
    year2 = int(found["y2"] or year1)
    month2 = int(found["m2"] or month1)
    try:
        start = date(year1, month1, day1)
        end = date(year2, month2, int(found["d2"]))
    except ValueError:
        return None
    return (start.isoformat(), end.isoformat()) if start <= end else None


class NotInRealtime(Exception):
    """The question names a unit or type that realtime.db does not carry."""

    def __init__(self, what: str, name: str, suggestions: Sequence[str] = ()):
        self.what = what
        self.name = name
        self.suggestions = tuple(suggestions)
        super().__init__(f"{what}：{name}")


_PLANT_SUFFIXES = ("發電廠", "電廠", "發電所")


def _normal(text: str) -> str:
    text = text.replace("＃", "#").replace("（", "(").replace("）", ")").replace(" ", "")
    return _UNIT_ORDINAL.sub(r"#\1", text)


def _matched(words: Sequence[str], question: str) -> list[str]:
    return [word for word in words if word in question]


def looks_realtime(question: str) -> bool:
    """Lexical gate: wording alone decides the realtime path (spec §4.1)."""

    text = _normal(question)
    topic = bool(_matched(OUTPUT_WORDS, text)) or any(
        alias in text for aliases in TYPE_ALIASES.values() for alias in aliases
    )
    if _matched(STRONG_WORDS, text) or _HOURS_RE.search(text):
        return topic or bool(_matched(STATUS_WORDS, text))
    if _matched(WEAK_WORDS, text) and not _matched(DATA_SCOPE_WORDS, text):
        return bool(_matched(OUTPUT_WORDS, text))
    return False


def asks_energy_on_a_day(question: str) -> bool:
    return bool(_matched(ENERGY_WORDS, _normal(question)))


@dataclass(frozen=True)
class RealtimeQuery:
    intent: str
    sql: str
    params: tuple[object, ...]
    # 揭露要知道的事：用到哪些時段、哪些類型、是不是估算。
    slots: tuple[str, str] | None = None
    day: str | None = None
    period: tuple[str, str] | None = None  # 'YYYY-MM-DD' 起訖（含）
    types: tuple[str, ...] = ()
    energy: bool = False
    expected_samples: int | None = None


@dataclass(frozen=True)
class Catalog:
    """What realtime.db knows right now; read once per question."""

    units: tuple[UnitRow, ...]
    names: dict[str, tuple[str, ...]]  # 正規化名稱 → 機組鍵
    types: tuple[str, ...]
    plants: dict[int, str]
    first_day: str | None
    rolled_up: frozenset[str]
    day_snapshots: dict[str, tuple[int, int]]  # 日期 → (收到的時段數, 應有時段數)
    # RT-2：d006010 匯入的日子、即時快照有資料的日子、最新時段裡實際出現的機組與類型。
    history_days: frozenset[str] = frozenset()
    snapshot_days: frozenset[str] = frozenset()
    latest_keys: frozenset[str] = frozenset()
    latest_types: frozenset[str] = frozenset()

    @property
    def history_range(self) -> tuple[str, str] | None:
        return (min(self.history_days), max(self.history_days)) if self.history_days else None

    def has_data(self, day: str) -> bool:
        return day in self.rolled_up or day in self.snapshot_days

    @classmethod
    def read(cls, connection: sqlite3.Connection) -> Catalog:
        rows = connection.execute(
            "SELECT unit_type, unit_name, access_scope, plant_id FROM dim_rt_unit"
        ).fetchall()
        names: dict[str, list[str]] = {}
        units = []
        for unit_type, unit_name, access_scope, plant_id in rows:
            key = f"{unit_type}|{unit_name}"
            units.append(UnitRow(key, access_scope, plant_id))
            for form in {_normal(unit_name), _normal(_NOTE_SUFFIX.sub("", unit_name))}:
                if form:
                    names.setdefault(form, []).append(key)
        days = connection.execute(
            "SELECT date, snapshots, expected FROM fact_rt_day ORDER BY date"
        ).fetchall()
        first_snapshot = connection.execute(
            "SELECT MIN(data_time) FROM fact_rt_snapshot"
        ).fetchone()
        first_candidates = [day[0] for day in days[:1]]
        if first_snapshot and first_snapshot[0]:
            first_candidates.append(str(first_snapshot[0])[:10])
        try:
            history_days = frozenset(
                row[0] for row in connection.execute("SELECT date FROM fact_rt_history_day")
            )
        except sqlite3.OperationalError:  # schema 1 的資料庫還沒有歷史表
            history_days = frozenset()
        if history_days:
            first_candidates.append(min(history_days))
        latest = connection.execute(
            """SELECT u.unit_type || '|' || u.unit_name, u.unit_type
                 FROM fact_rt_unit_10min AS f JOIN dim_rt_unit AS u ON u.id = f.unit_id
                WHERE f.data_time = (SELECT MAX(data_time) FROM fact_rt_snapshot)"""
        ).fetchall()
        return cls(
            units=tuple(units),
            names={name: tuple(keys) for name, keys in names.items()},
            types=tuple(sorted({row[0] for row in rows})),
            plants={
                int(plant_id): str(name)
                for plant_id, name in connection.execute(
                    "SELECT plant_id, plant_name FROM dim_rt_plant"
                )
            },
            first_day=min(first_candidates) if first_candidates else None,
            rolled_up=frozenset(day[0] for day in days) | history_days,
            day_snapshots={day[0]: (int(day[1]), int(day[2])) for day in days},
            history_days=history_days,
            snapshot_days=frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT substr(data_time, 1, 10) FROM fact_rt_snapshot"
                )
            ),
            latest_keys=frozenset(row[0] for row in latest),
            latest_types=frozenset(row[1] for row in latest),
        )

    def unit_keys(self, question: str) -> tuple[str, ...]:
        """Unit keys named in the question; `台中#1` never matches inside `台中#10`."""

        text = _normal(question)
        found: list[tuple[int, int, tuple[str, ...]]] = []
        for name, keys in self.names.items():
            start = text.find(name)
            while start >= 0:
                end = start + len(name)
                if not (end < len(text) and text[end].isdigit() and name[-1].isdigit()):
                    found.append((start, end, keys))
                start = text.find(name, start + 1)
        # 長的名字優先：`大潭CC#1` 不該再被當成另一部叫 `大潭` 的機組。
        found.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
        taken: list[tuple[int, int]] = []
        keys: list[str] = []
        for start, end, matched in found:
            if any(start < right and left < end for left, right in taken):
                continue
            taken.append((start, end))
            keys.extend(key for key in matched if key not in keys)
        return tuple(keys)

    def plant(self, question: str) -> str | None:
        text = _normal(question)
        for name in sorted(self.plants.values(), key=len, reverse=True):
            base = name
            for suffix in _PLANT_SUFFIXES:
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if name in text or any(base + suffix in text for suffix in _PLANT_SUFFIXES):
                return name
        return None

    def unit_token(self, question: str) -> str | None:
        """A unit-looking name in the question, matched or not."""

        text = _normal(question)
        # 時間詞與客套話不屬於名字：「現在台中#1」要切出「台中#1」。
        for word in (*STRONG_WORDS, *WEAK_WORDS, "請問", "查詢", "幫我查", "查"):
            text = text.replace(word, " ")
        found = _UNIT_TOKEN.search(text)
        return found.group(1) if found else None

    def similar_units(self, token: str, limit: int = 5) -> list[str]:
        prefix = token.split("#", 1)[0]
        names = sorted({key.split("|", 1)[1] for keys in self.names.values() for key in keys})
        return [name for name in names if prefix and name.startswith(prefix)][:limit]

    def requested_types(self, question: str) -> tuple[str, ...]:
        """Every type the question names, including ones realtime.db does not have."""

        consumed = _normal(question)
        chosen: list[str] = []
        for unit_type, aliases in TYPE_ALIASES.items():
            if any(alias in consumed for alias in aliases):
                chosen.append(unit_type)
                for alias in aliases:
                    consumed = consumed.replace(alias, "　")
        return tuple(chosen)

    def types_in(self, question: str) -> tuple[str, ...]:
        text = _normal(question)
        chosen: list[str] = []
        consumed = text
        for unit_type, aliases in TYPE_ALIASES.items():
            if any(alias in consumed for alias in aliases):
                chosen.append(unit_type)
                for alias in aliases:
                    consumed = consumed.replace(alias, "　")
        expanded: list[str] = []
        for unit_type in chosen:
            family = TYPE_FAMILIES.get(unit_type, (unit_type,))
            if unit_type in TYPE_FAMILIES and "民營" in text:
                family = (f"民營電廠-{unit_type}",)
            expanded.extend(member for member in family if member not in expanded)
        return tuple(member for member in expanded if member in self.types) or tuple(expanded)


def _in(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


_OK_SUM = 'round(SUM(CASE WHEN "數值狀態" = ? THEN "淨發電量_MW" END), 1)'
_OK_ENERGY = 'round(SUM(CASE WHEN "數值狀態" = ? THEN "淨發電量_MW" END) / 6.0, 1)'
_OK_COUNT = 'SUM(CASE WHEN "數值狀態" = ? THEN 1 ELSE 0 END)'


class RealtimeRouter:
    """Deterministic SQL for the common realtime questions (spec §5)."""

    def __init__(self, catalog: Catalog, *, latest: str, today: str, now_slot_count: int):
        self.catalog = catalog
        self.latest = latest
        self.today = today
        self.now_slot_count = now_slot_count

    def _target(
        self, question: str, *, current: bool
    ) -> tuple[str, str, tuple[object, ...], tuple[str, ...]]:
        """(kind, WHERE fragment, params, types) for the unit／plant／type the question names.

        `current` 是問「現在」：只算最新時段裡真的出現的機組與類型。d006010 匯入後，已除役的機組
        （例如 `台中#1`、`核三#2`）也在機組表裡，但不能把它們說成現在有在發電。
        """

        keys = self.catalog.unit_keys(question)
        if current and keys and self.catalog.latest_keys:
            live = tuple(key for key in keys if key in self.catalog.latest_keys)
            if not live:
                names = "、".join(dict.fromkeys(key.split("|", 1)[1] for key in keys))
                raise NotInRealtime(
                    "這部機組不在最新的即時資料裡"
                    "（可能已除役，只剩 d006010 歷史資料；可以改問某一天）",
                    names,
                )
            keys = live
        plant = (
            self.catalog.plant(question) if any(s in question for s in _PLANT_SUFFIXES) else None
        )
        if plant is not None:
            return "plant", '"電廠" = ?', (plant,), ()
        if keys:
            return "unit", f'"機組鍵" IN ({_in(keys)})', tuple(keys), ()
        token = self.catalog.unit_token(question)
        if token is not None:
            # 指名了機組卻對不上：答全系統合計就是答非所問（例如已除役的台中#1）。
            raise NotInRealtime("即時資料沒有這部機組", token, self.catalog.similar_units(token))
        requested = self.catalog.requested_types(question)
        known = (
            self.catalog.latest_types
            if current and self.catalog.latest_types
            else frozenset(self.catalog.types)
        )
        present = tuple(
            unit_type for unit_type in self.catalog.types_in(question) if unit_type in known
        )
        if requested and not present:
            raise NotInRealtime("即時資料目前沒有這個發電類型", "、".join(requested))
        if present:
            return "type", f'"機組類型" IN ({_in(present)})', present, present
        return "all", "", (), ()

    def route(
        self, question: str, *, day: str | None, period: tuple[str, str] | None = None
    ) -> RealtimeQuery | None:
        text = _normal(question)
        if _matched(UNROUTED_WORDS, text):
            return None
        kind, where, params, types = self._target(text, current=day is None and period is None)
        energy = bool(_matched(ENERGY_WORDS, text))
        if period is not None:
            return self._period(period, kind, where, params, types)
        if day is not None:
            if not energy and not _matched(PEAK_WORDS, text):
                energy = True  # 「昨天風力」沒有說量什麼時，一天最有意義的量是發電量。
            return self._day(day, kind, where, params, types, text, energy)
        if any(word in text for word in ("今天", "今日")) and (
            energy or _matched(PEAK_WORDS, text)
        ):
            return self._today(kind, where, params, types, text, energy)
        return self._now(kind, where, params, types, text)

    # --- 現在 -------------------------------------------------------------------------

    def _now(self, kind, where, params, types, text) -> RealtimeQuery | None:
        latest = (self.latest,)
        slots = (self.latest, self.latest)
        matched_notes = _matched(NOTE_WORDS, text)
        # 「環保停機」已經包含「停機」；兩個都留著會把「計劃停機」也一起撈出來。
        notes = [
            note
            for note in matched_notes
            if not any(note != other and note in other for other in matched_notes)
        ]
        if "通訊異常" in text or notes or ("異常" in text and kind in {"all", "type", "plant"}):
            conditions = ['"資料時間" = ?']
            values: list[object] = [self.latest]
            if where:
                conditions.append(where)
                values.extend(params)
            if "通訊異常" in text:
                conditions.append('"數值狀態" = ?')
                values.append("通訊異常")
            elif notes:
                conditions.append("(" + " OR ".join('"備註" LIKE ?' for _ in notes) + ")")
                values.extend(f"%{note}%" for note in notes)
            else:
                conditions.append('("數值狀態" <> ? OR "備註" <> ?)')
                values.extend((OK, ""))
            sql = (
                'SELECT "資料時間", "機組類型", "機組名稱", "電廠", "淨發電量_MW", "裝置容量_MW", '
                '"備註", "數值狀態" FROM v_rt_10min WHERE '
                + " AND ".join(conditions)
                + ' ORDER BY "機組類型", "機組名稱" LIMIT 200'
            )
            return RealtimeQuery("rt_now_abnormal", sql, tuple(values), slots=slots, types=types)

        if kind == "unit":
            sql = (
                'SELECT "資料時間", "機組類型", "機組名稱", "電廠", "淨發電量_MW", "裝置容量_MW", '
                '"出力比", "備註", "數值狀態" FROM v_rt_10min WHERE "資料時間" = ? AND '
                + where
                + ' ORDER BY "機組類型" LIMIT 20'
            )
            return RealtimeQuery("rt_now_unit", sql, latest + params, slots=slots)

        top_n = extract_entities(text).top_n
        if _matched(TOP_WORDS, text) and ("機組" in text or top_n):
            conditions = ['"資料時間" = ?', '"數值狀態" = ?']
            values = [self.latest, OK]
            if where:
                conditions.append(where)
                values.extend(params)
            sql = (
                'SELECT "機組類型", "機組名稱", "電廠", "粒度", "淨發電量_MW", "裝置容量_MW", '
                '"出力比" FROM v_rt_10min WHERE '
                + " AND ".join(conditions)
                + f' ORDER BY "淨發電量_MW" DESC LIMIT {min(top_n or 10, 50)}'
            )
            return RealtimeQuery("rt_now_top", sql, tuple(values), slots=slots, types=types)

        group = '"電廠", "機組類型"' if kind == "plant" else '"機組類型"'
        conditions = ['"資料時間" = ?']
        values = [self.latest]
        if where:
            conditions.append(where)
            values.extend(params)
        sql = (
            f'SELECT "資料時間", {group}, {_OK_SUM} AS "淨發電量_MW", '
            'round(SUM("裝置容量_MW"), 1) AS "裝置容量_MW", COUNT(*) AS "機組數", '
            'SUM(CASE WHEN "數值狀態" <> ? THEN 1 ELSE 0 END) AS "數值異常機組數" '
            "FROM v_rt_10min WHERE "
            + " AND ".join(conditions)
            + f' GROUP BY "資料時間", {group} ORDER BY "淨發電量_MW" DESC LIMIT 50'
        )
        intent = {"plant": "rt_now_plant", "type": "rt_now_type"}.get(kind, "rt_now_by_type")
        return RealtimeQuery(intent, sql, (OK, OK, *values), slots=slots, types=types)

    # --- 今天 -------------------------------------------------------------------------

    def _today(self, kind, where, params, types, text, energy) -> RealtimeQuery:
        start = f"{self.today} 00:00"
        slots = (start, self.latest)
        conditions = ['"資料時間" BETWEEN ? AND ?']
        values: list[object] = [start, self.latest]
        if where:
            conditions.append(where)
            values.extend(params)
        clause = " AND ".join(conditions)
        if not energy:
            if kind == "unit":
                sql = (
                    'SELECT "機組類型", "機組名稱", "資料時間", "淨發電量_MW" FROM v_rt_10min '
                    f'WHERE {clause} AND "數值狀態" = ? ORDER BY "淨發電量_MW" DESC LIMIT 1'
                )
                return RealtimeQuery("rt_today_peak", sql, (*values, OK), slots=slots)
            group = '"機組類型"' if kind in {"type", "all"} else '"電廠"'
            sql = (
                f'SELECT "資料時間", {group}, {_OK_SUM} AS "淨發電量_MW" FROM v_rt_10min '
                f'WHERE {clause} GROUP BY "資料時間", {group} '
                'ORDER BY "淨發電量_MW" DESC LIMIT 1'
            )
            return RealtimeQuery("rt_today_peak", sql, (OK, *values), slots=slots, types=types)
        label = (
            '"機組類型", "機組名稱"'
            if kind == "unit"
            else ('"電廠", "機組類型"' if kind == "plant" else '"機組類型"')
        )
        group = '"機組鍵", "機組類型", "機組名稱"' if kind == "unit" else label
        sql = (
            f'SELECT {label}, {_OK_ENERGY} AS "估算發電量_MWh", '
            f'{_OK_COUNT} AS "取樣點數", COUNT(DISTINCT "機組鍵") * ? AS "應有點數", '
            f'round(1.0 * {_OK_COUNT} / (COUNT(DISTINCT "機組鍵") * ?), 4) AS "資料完整度" '
            f"FROM v_rt_10min WHERE {clause} GROUP BY {group} "
            'ORDER BY "估算發電量_MWh" DESC LIMIT 50'
        )
        count = self.now_slot_count
        return RealtimeQuery(
            "rt_today_energy",
            sql,
            (OK, OK, count, OK, count, *values),
            slots=slots,
            types=types,
            energy=True,
            expected_samples=count,
        )

    # --- 一段期間（RT-2：d006010 讓「四月發了多少度」答得出來）-------------------------

    def _period(self, period, kind, where, params, types) -> RealtimeQuery:
        label = (
            '"機組類型", "機組名稱"'
            if kind == "unit"
            else ('"電廠", "機組類型"' if kind == "plant" else '"機組類型"')
        )
        group = '"機組鍵", "機組類型", "機組名稱"' if kind == "unit" else label
        conditions = ['"日期" BETWEEN ? AND ?']
        values: list[object] = [period[0], period[1]]
        if where:
            conditions.append(where)
            values.extend(params)
        sql = (
            f'SELECT {label}, round(SUM("估算發電量_MWh"), 1) AS "估算發電量_MWh", '
            'COUNT(DISTINCT "日期") AS "有資料天數", SUM("取樣點數") AS "取樣點數", '
            'SUM("應有點數") AS "應有點數", '
            'round(1.0 * SUM("取樣點數") / SUM("應有點數"), 4) AS "資料完整度" '
            f"FROM v_rt_daily WHERE {' AND '.join(conditions)} GROUP BY {group} "
            'ORDER BY "估算發電量_MWh" DESC LIMIT 50'
        )
        return RealtimeQuery(
            "rt_period_energy",
            sql,
            tuple(values),
            period=period,
            types=types,
            energy=True,
        )

    # --- 某一天 -----------------------------------------------------------------------

    def _day(self, day, kind, where, params, types, text, energy) -> RealtimeQuery:
        label = (
            '"機組類型", "機組名稱"'
            if kind == "unit"
            else ('"電廠", "機組類型"' if kind == "plant" else '"機組類型"')
        )
        group = '"機組鍵", "機組類型", "機組名稱"' if kind == "unit" else label
        conditions = ['"日期" = ?']
        values: list[object] = [day]
        if where:
            conditions.append(where)
            values.extend(params)
        clause = " AND ".join(conditions)
        if day in self.catalog.rolled_up:
            if energy:
                sql = (
                    f'SELECT "日期", {label}, round(SUM("估算發電量_MWh"), 1) AS "估算發電量_MWh", '
                    'SUM("取樣點數") AS "取樣點數", SUM("應有點數") AS "應有點數", '
                    'round(1.0 * SUM("取樣點數") / SUM("應有點數"), 4) AS "資料完整度" '
                    f'FROM v_rt_daily WHERE {clause} GROUP BY "日期", {group} '
                    'ORDER BY "估算發電量_MWh" DESC LIMIT 50'
                )
            else:
                sql = (
                    f'SELECT "日期", {label}, round(MAX("最高出力_MW"), 1) AS "最高出力_MW", '
                    'round(1.0 * SUM("取樣點數") / SUM("應有點數"), 4) AS "資料完整度" '
                    f'FROM v_rt_daily WHERE {clause} GROUP BY "日期", {group} '
                    'ORDER BY "最高出力_MW" DESC LIMIT 50'
                )
            return RealtimeQuery(
                "rt_day_energy" if energy else "rt_day_peak",
                sql,
                tuple(values),
                day=day,
                types=types,
                energy=energy,
            )
        # 當天還沒彙總（還沒過 00:15，或收集器那天之後沒跑過維護）：退回 10 分鐘明細。
        expected = len(day_slots(day, 10))
        detail_conditions = ['"資料時間" BETWEEN ? AND ?', *conditions[1:]]
        detail_values = [f"{day} 00:00", f"{day} 23:50", *values[1:]]
        detail_clause = " AND ".join(detail_conditions)
        sql = (
            f'SELECT {label}, {_OK_ENERGY} AS "估算發電量_MWh", '
            f'{_OK_COUNT} AS "取樣點數", COUNT(DISTINCT "機組鍵") * ? AS "應有點數", '
            f'round(1.0 * {_OK_COUNT} / (COUNT(DISTINCT "機組鍵") * ?), 4) AS "資料完整度" '
            f"FROM v_rt_10min WHERE {detail_clause} GROUP BY {group} "
            'ORDER BY "估算發電量_MWh" DESC LIMIT 50'
        )
        return RealtimeQuery(
            "rt_day_energy",
            sql,
            (OK, OK, expected, OK, expected, *detail_values),
            slots=(f"{day} 00:00", f"{day} 23:50"),
            day=day,
            types=types,
            energy=True,
            expected_samples=expected,
        )


def _realtime_tables(sql: str) -> tuple[str, ...] | None:
    try:
        tree = parse_one(sql, read="sqlite")
    except ParseError:
        return None
    return tuple(sorted({table.name for table in tree.find_all(exp.Table)}))


def apply_unit_scope(
    sql: str, params: Sequence[object], visible: frozenset[str] | None
) -> tuple[str, tuple[object, ...]]:
    """Wrap every v_rt_* reference in a subquery that only holds the visible unit keys.

    做法與 `ScopeGuard` 相同：改寫的是已經過守門的 SQL，所以 JOIN、子查詢與聚合都自動套用。
    """

    if visible is None:
        return sql, tuple(params)
    try:
        tree = parse_one(sql, read="sqlite")
    except ParseError as error:
        raise ScopeRewriteError(f"授權改寫無法解析 SQL：{error}") from error
    bindings = _name_original_placeholders(tree, params)
    keys = sorted(visible)
    counter = 0

    def rewrite(node: exp.Expression) -> exp.Expression:
        nonlocal counter
        if not isinstance(node, exp.Table):
            return node
        if node.name not in REALTIME_VIEWS:
            raise ScopeRewriteError(f"即時查詢不應該出現「{node.name}」。")
        names = []
        for key in keys:
            name = f"__s{counter}"
            counter += 1
            bindings[name] = key
            names.append(name)
        predicate = (
            f'"{SCOPE_KEY}" IN ({", ".join(f":{name}" for name in names)})' if names else "1 = 0"
        )
        inner = parse_one(f"SELECT * FROM {node.name} WHERE {predicate}", read="sqlite")
        return exp.Subquery(
            this=inner, alias=exp.TableAlias(this=exp.to_identifier(node.alias_or_name))
        )

    scoped = tree.transform(rewrite, copy=True)
    rendered = scoped.sql(dialect="sqlite")
    ordered = _MARKER.findall(rendered)
    missing = [name for name in ordered if name not in bindings]
    if missing:
        raise ScopeRewriteError(f"授權改寫遺失參數：{missing}")
    return _MARKER.sub("?", rendered), tuple(bindings[name] for name in ordered)


@dataclass
class _Context:
    question: str
    status: Mapping[str, Any]
    catalog: Catalog
    scope: RealtimeScope
    now: datetime
    latest: str
    today: str
    trace: list[dict[str, Any]] = field(default_factory=list)


class RealtimeQueryEngine:
    """The realtime branch of Text2SQLPipeline (spec §3)."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        max_rows: int = 200,
        timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.sql_guard = SqlGuard(max_rows=max_rows, allowed_columns=REALTIME_ALLOWED_COLUMNS)
        self.executor = ReadOnlySQLite(config.database, timeout_seconds=timeout_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))

    # --- 入口 -------------------------------------------------------------------------

    def try_query(
        self,
        question: str,
        *,
        plant: tuple[int, str] | None,
        plant_roster: Mapping[int, str] | None,
        llm: LLMProtocol | None,
        llm_enabled: bool,
        max_attempts: int = 3,
    ) -> PipelineResponse | None:
        """Answer a realtime question, or return None so the regular pipeline handles it."""

        lexical = looks_realtime(question)
        explicit_day = None
        explicit_period: tuple[str, str] | None = None
        if not lexical:
            if not asks_energy_on_a_day(question):
                return None
            entities = extract_entities(question)
            explicit_period = day_range(question)
            explicit_day = None if explicit_period else entities.explicit_date
            if explicit_day is None and explicit_period is None and entities.date_range:
                explicit_period = (entities.date_range.start, entities.date_range.end)
            if explicit_day is None and explicit_period is None:
                return None
        dated = explicit_day is not None or explicit_period is not None
        started = perf_counter()
        now = self.clock()
        if not self.config.database.is_file():
            return None if dated else _unavailable("尚未建立 realtime.db")
        try:
            status = read_status(config=self.config, now=now)
        except Exception as error:  # noqa: BLE001 - 即時狀態壞掉不能讓查詢 500。
            return None if dated else _unavailable(type(error).__name__)
        if not status.get("available") or status.get("latest_data_time") is None:
            return None if dated else _unavailable(str(status.get("state")))
        try:
            catalog = self._read_catalog()
        except sqlite3.Error as error:
            return None if dated else _unavailable(type(error).__name__)
        if dated:
            # 日期問句只有落在即時資料（含 d006010 歷史）期間內才是我們的；
            # 否則讓既有守門照舊回答或拒答。
            today = local_date(now).isoformat()
            start, end = explicit_period or (explicit_day, explicit_day)
            if catalog.first_day is None or end < catalog.first_day or start > today:
                return None

        scope = (
            RealtimeScope("plant", plant[0], plant[1])
            if plant is not None
            else RealtimeScope("all")
        )
        context = _Context(
            question=question,
            status=status,
            catalog=catalog,
            scope=scope,
            now=now,
            latest=str(status["latest_data_time"]),
            today=local_date(now).isoformat(),
        )
        _trace(context.trace, "realtime_route_gate", started, explicit_day=explicit_day)
        if scope.kind == "plant" and catalog.plants != dict(plant_roster or {}):
            return PipelineResponse(
                False,
                data={"trace": context.trace},
                error_code="RT_SCOPE_MISMATCH",
                error="即時資料與查詢資料庫的電廠名冊不一致，暫時無法提供電廠帳號的即時查詢。",
                severity="refuse",
            )
        return self._answer(context, explicit_day, explicit_period, llm, llm_enabled, max_attempts)

    def coverage(self) -> dict[str, Any]:
        """What realtime questions can reach right now, for /api/coverage."""

        if not self.config.database.is_file():
            return {"available": False}
        try:
            status = read_status(config=self.config, now=self.clock())
            catalog = self._read_catalog()
        except Exception:  # noqa: BLE001 - 涵蓋說明是補充資訊，讀不到就說沒有。
            return {"available": False}
        if not status.get("available") or status.get("latest_data_time") is None:
            return {"available": False, "state": status.get("state")}
        return {
            "available": True,
            "state": status.get("state"),
            "first_day": catalog.first_day,
            "latest_data_time": status.get("latest_data_time"),
            "detail_days": self.config.raw_days,
            "live_first_day": min(catalog.snapshot_days, default=None),
            "history_range": list(catalog.history_range) if catalog.history_range else None,
        }

    def _read_catalog(self) -> Catalog:
        uri = f"{self.config.database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            return Catalog.read(connection)
        finally:
            connection.close()

    # --- 路由與生成 -------------------------------------------------------------------

    def _resolve_day(self, context: _Context, explicit_day: str | None) -> str | None:
        text = context.question
        today = date.fromisoformat(context.today)
        if explicit_day is not None:
            return explicit_day
        if "前天" in text:
            return (today - timedelta(days=2)).isoformat()
        if "昨天" in text or "昨日" in text:
            return (today - timedelta(days=1)).isoformat()
        explicit = extract_entities(text, reference_date=today).explicit_date
        if explicit is not None and explicit != context.today:
            return explicit
        return None

    def _no_data(self, context: _Context, asked: str) -> PipelineResponse:
        history = context.catalog.history_range
        covered = f"即時收集從 {min(context.catalog.snapshot_days, default='（尚無）')} 開始"
        if history:
            covered += f"；台電 d006010 歷史資料涵蓋 {history[0]}～{history[1]}"
        return PipelineResponse(
            False,
            data={"trace": context.trace},
            error_code="RT_NO_DATA_FOR_DAY",
            error=f"{asked} 沒有即時或歷史發電資料（{covered}）。中間的空檔目前補不回來。",
            severity="refuse",
            evidence={"asked": asked, "history": history},
        )

    def _answer(
        self,
        context: _Context,
        explicit_day: str | None,
        explicit_period: tuple[str, str] | None,
        llm: LLMProtocol | None,
        llm_enabled: bool,
        max_attempts: int,
    ) -> PipelineResponse:
        day = self._resolve_day(context, explicit_day)
        first_day = context.catalog.first_day
        if day is not None and (first_day is None or day < first_day):
            return PipelineResponse(
                False,
                data={"trace": context.trace},
                error_code="RT_BEFORE_FIRST_DAY",
                error=(
                    f"即時資料（含 d006010 歷史資料）從 {first_day} 開始，{day} 沒有資料。"
                    "收集器在電腦開機、登入後才會開始抓取，錯過的時段目前補不回來。"
                ),
                severity="refuse",
                suggestions=(f"{first_day}的風力發電量是多少？" if first_day else "",),
                evidence={"first_day": first_day, "requested_day": day},
            )
        if day is not None and day > context.today:
            return PipelineResponse(
                False,
                data={"trace": context.trace},
                error_code="RT_FUTURE_DAY",
                error=f"{day} 還沒到，沒有即時資料。",
                severity="refuse",
            )
        if day is not None and day < context.today and not context.catalog.has_data(day):
            return self._no_data(context, day)
        period = explicit_period
        if period is not None:
            period = (max(period[0], first_day or period[0]), min(period[1], context.today))
            if not any(
                context.catalog.has_data(item) for item in _days_between(period[0], period[1])
            ):
                return self._no_data(context, f"{period[0]}～{period[1]}")

        started = perf_counter()
        elapsed = len([slot for slot in day_slots(context.today, 10) if slot <= context.latest])
        router = RealtimeRouter(
            context.catalog,
            latest=context.latest,
            today=context.today,
            now_slot_count=max(elapsed, 1),
        )
        try:
            routed = router.route(_normal(context.question), day=day, period=period)
        except NotInRealtime as missing:
            return PipelineResponse(
                False,
                data={"trace": context.trace},
                error_code="RT_NOT_IN_REALTIME",
                error=(
                    f"{missing.what}（{missing.name}）。即時資料是台電 d006001 目前列出的機組，"
                    "已除役或併在彙總列裡的不會單獨出現。"
                ),
                severity="clarify",
                suggestions=tuple(f"現在{name}出力多少？" for name in missing.suggestions[:3]),
                evidence={"name": missing.name, "similar": list(missing.suggestions)},
            )
        _trace(context.trace, "realtime_route", started, intent=routed.intent if routed else None)

        candidates: list[tuple[str, RealtimeQuery]] = []
        if routed is not None:
            candidates.append(("router", routed))
        prior_error: str | None = None
        prior_sql: str | None = None
        llm_error: str | None = None
        attempts = 1 if routed is not None else (max_attempts if llm_enabled and llm else 0)
        for attempt in range(1, attempts + 1):
            if routed is not None:
                source, query = candidates[0]
            else:
                started = perf_counter()
                prompt = build_realtime_prompt(
                    context.question,
                    latest=context.latest,
                    today=context.today,
                    day=day,
                    unit_types=context.catalog.types,
                    prior_error=prior_error,
                    prior_sql=prior_sql,
                )
                try:
                    generated = parse_generated_query(llm.generate(prompt))  # type: ignore[union-attr]
                except Exception as error:
                    category = classify_error(error)
                    prior_error = f"LLM_OUTPUT_ERROR: {type(error).__name__}"
                    prior_sql = None
                    _trace(
                        context.trace,
                        "realtime_generate",
                        started,
                        attempt=attempt,
                        category=category,
                    )
                    if category == RETRYABLE_CATEGORY:
                        continue
                    # 限流、服務中斷不重送；但要留在 evidence，不能躲在「換個問法」後面。
                    llm_error = category
                    break
                _trace(context.trace, "realtime_generate", started, attempt=attempt)
                source = "llm"
                # 模型寫的 SQL 沒告訴我們加總了哪些類型；綁定的參數與問句裡的類型就是線索。
                named = {param for param in generated.params if param in context.catalog.types}
                query = RealtimeQuery(
                    "rt_llm",
                    generated.sql,
                    generated.params,
                    slots=(f"{day} 00:00", f"{day} 23:50") if day else None,
                    day=day,
                    types=tuple(sorted(named | set(context.catalog.types_in(context.question)))),
                )
            outcome = self._execute(context, source, query)
            if isinstance(outcome, PipelineResponse):
                return outcome
            prior_error, prior_sql = outcome

        return PipelineResponse(
            False,
            data={"trace": context.trace},
            error_code="RT_CLARIFY",
            error=(
                "這句看起來是在問即時發電，但我接不住這種問法。"
                "可以指名機組、發電類型或電廠，並說明要現在的出力還是今天的發電量。"
            ),
            severity="clarify",
            suggestions=("現在風力發電多少 MW？", "今天太陽能估計發了多少電？"),
            evidence={
                "attempts": attempts,
                "last_error": prior_error,
                "latest": context.latest,
                **({"llm_error": llm_error} if llm_error else {}),
            },
        )

    def _execute(
        self, context: _Context, source: str, query: RealtimeQuery
    ) -> PipelineResponse | tuple[str, str]:
        """Guard, scope and run one candidate; a (error, sql) tuple asks for another attempt."""

        started = perf_counter()
        tables = _realtime_tables(query.sql)
        if (
            tables
            and any(table in REALTIME_VIEWS for table in tables)
            and any(table not in REALTIME_VIEWS for table in tables)
        ):
            _trace(context.trace, "realtime_sql_guard", started, code="RT_CROSS_DATABASE")
            return PipelineResponse(
                False,
                data={"trace": context.trace},
                error_code="RT_CROSS_DATABASE",
                error=(
                    "即時資料（v_rt_*）與歷史快照的檢視在不同資料庫，兩邊的機組名稱也沒有對照，"
                    "不能放在同一句查詢裡。請分開問。"
                ),
                severity="refuse",
                evidence={"tables": list(tables)},
            )
        guard = self.sql_guard.validate(query.sql, query.params)
        _trace(context.trace, "realtime_sql_guard", started, source=source, code=guard.code)
        if not guard.allowed:
            return f"{guard.code}: {guard.reason}", query.sql

        started = perf_counter()
        visible = visible_unit_keys(context.scope, context.catalog.units)
        try:
            sql, params = apply_unit_scope(query.sql, query.params, visible)
        except ScopeRewriteError as error:
            return PipelineResponse(
                False,
                data={"trace": context.trace},
                error_code="SCOPE_DENIED",
                error=str(error),
                severity="refuse",
            )
        _trace(context.trace, "realtime_scope", started, scope=context.scope.label)

        started = perf_counter()
        try:
            columns, rows = self.executor.execute(sql, params)
        except Exception as error:
            _trace(context.trace, "realtime_execute", started, error=type(error).__name__)
            return f"SQL_EXECUTION_ERROR: {type(error).__name__}: {error}", query.sql
        _trace(context.trace, "realtime_execute", started, record_count=len(rows))
        disclosures = self._disclosures(context, query, columns, rows)
        return PipelineResponse(
            True,
            data={
                "question": context.question,
                "intent": query.intent,
                "source": source,
                "sql": query.sql,
                "params": list(query.params),
                "tables": list(guard.tables),
                "columns": columns,
                "rows": [list(row) for row in rows],
                "record_count": len(rows),
                "scope": (
                    {"plant": context.scope.plant_name} if context.scope.kind == "plant" else None
                ),
                "disclosures": disclosures,
                "realtime": {
                    "data_time": context.latest,
                    "state": context.status.get("state"),
                    "lag_minutes": context.status.get("lag_minutes"),
                    "first_day": context.catalog.first_day,
                },
                "trace": context.trace,
            },
        )

    # --- 揭露 -------------------------------------------------------------------------

    def _disclosures(
        self,
        context: _Context,
        query: RealtimeQuery,
        columns: Sequence[str],
        rows: Sequence[Sequence[object]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        def add(code: str, reason: str, **evidence: object) -> None:
            items.append({"code": code, "reason": reason, "evidence": evidence})

        status = context.status
        catalog = context.catalog
        if query.period is not None:
            days = _days_between(*query.period)
            history = [day for day in days if day in catalog.history_days]
            missing = [day for day in days if not catalog.has_data(day)]
            add(
                "RT_DATA_TIME",
                f"資料期間 {query.period[0]}～{query.period[1]}（台灣時間），共 {len(days)} 天。",
                period=list(query.period),
            )
            if history:
                add(
                    "RT_HISTORY_SOURCE",
                    f"其中 {len(history)} 天來自台電 d006010 歷史資料（每月更新、約晚四個月），"
                    "不是收集器即時抓到的。",
                    days=len(history),
                )
            if missing:
                add(
                    "RT_PERIOD_GAPS",
                    f"期間內有 {len(missing)} 天完全沒有資料（{missing[0]} 起），"
                    "加總只含有資料的日子。",
                    missing_days=len(missing),
                )
        elif query.day is None:
            add(
                "RT_DATA_TIME",
                f"資料時間 {context.latest}（台灣時間）；台電每 10 分鐘更新一次。",
                data_time=context.latest,
            )
        else:
            received = context.catalog.day_snapshots.get(query.day)
            note = f"，當天收到 {received[0]}／{received[1]} 個時段" if received else ""
            add("RT_DATA_TIME", f"資料日期 {query.day}（台灣時間）{note}。", day=query.day)
            if query.day in catalog.history_days and query.day not in catalog.snapshot_days:
                add(
                    "RT_HISTORY_SOURCE",
                    "這天的數字來自台電 d006010 歷史資料（每月更新、約晚四個月），"
                    "不是收集器即時抓到的。",
                )
        # 落後與收集器停止只跟「現在／今天」有關；問過去某一天或一段期間時說這個只是雜訊。
        about_now = query.period is None and (query.day is None or query.day == context.today)
        if not about_now:
            pass
        elif status.get("state") == "stale":
            lag = status.get("lag_minutes")
            minutes = round(lag) if isinstance(lag, int | float) else "?"
            add("RT_STALE", f"資料落後 {minutes} 分鐘（最新時段 {context.latest}）。")
        elif status.get("state") == "stopped":
            add(
                "RT_COLLECTOR_STOPPED",
                f"收集器目前沒有在執行；數字是停止前的最後一筆（{context.latest}），不是此刻的值。",
            )

        warned = self._warned_snapshots(query)
        if warned:
            if context.scope.kind == "plant":
                add("RT_QUALITY_WARN", f"用到的 {warned} 個時段有驗證警告；細節請洽管理員。")
            else:
                add(
                    "RT_QUALITY_WARN",
                    f"用到的 {warned} 個時段有驗證警告（例如小計對不上），數字可能不完整。",
                )

        index = {name: position for position, name in enumerate(columns)}
        if "粒度" in index and _matched(LIST_WORDS, context.question):
            buckets = {str(row[index["粒度"]]) for row in rows} & {"彙總", "未定"}
            if buckets:
                add(
                    "RT_BUCKET_ROWS",
                    "結果含「彙總」或「未定」的列：一列可能代表多個設施（例如「其它購電太陽能」），"
                    "不能當成單一機組排名。",
                    grains=sorted(buckets),
                )
        history_used = (
            query.period is not None
            and any(day in catalog.history_days for day in _days_between(*query.period))
        ) or (query.day is not None and query.day in catalog.history_days)
        if history_used and "機組名稱" not in columns:
            # 與 RENEWABLE_SELF_BUILT_ONLY 同型的陷阱：d006010 沒有民營電廠與購電彙總列，
            # 類型合計看起來正常，其實只是台電自有的那一部分。
            add(
                "RT_HISTORY_PARTIAL_SCOPE",
                "d006010 歷史資料只有台電自有與部分機組（沒有民營電廠、購電太陽能與風力等彙總列），"
                "類型或電廠合計會比同類型的即時合計小很多，不能當成全國數字。",
            )
        energy = query.energy or any("發電量_MWh" in str(column) for column in columns)
        if energy:
            add(
                "RT_ENERGY_ESTIMATE",
                "發電量是每 10 分鐘的瞬間淨出力相加後除以 6 的估算值（MWh；1 MWh＝1,000 度），"
                "不是電表讀數；只計入數值正常的時段。",
            )
        if "資料完整度" in index:
            low = [
                row
                for row in rows
                if isinstance(row[index["資料完整度"]], int | float)
                and row[index["資料完整度"]] < COMPLETENESS_THRESHOLD
            ]
            if low:
                worst = min(float(row[index["資料完整度"]]) for row in low)
                add(
                    "RT_INCOMPLETE",
                    f"有 {len(low)} 列的資料完整度低於 {COMPLETENESS_THRESHOLD:.0%}"
                    f"（最低 {worst:.1%}）：缺的時段沒有算進去，估算值會偏低。"
                    "缺口可能來自收集器沒在跑、抓取失敗或台電回傳通訊異常。",
                    rows=len(low),
                    minimum=worst,
                )
        types_in_rows = (
            {str(row[index["機組類型"]]) for row in rows} if "機組類型" in index else set()
        )
        if {"儲能", "儲能負載"} <= (types_in_rows | set(query.types)) and not (
            "機組類型" in index and len(rows) > 1
        ):
            add(
                "RT_STORAGE_NETTING",
                "加總同時含儲能（放電）與儲能負載（充電）：兩者方向相反，合計會相抵。",
            )
        if (
            query.slots
            and query.slots[0].startswith(context.today)
            and query.slots[0] != query.slots[1]
        ):
            add(
                "RT_TODAY_PARTIAL",
                f"今天還沒過完：只算到 {context.latest[11:]}。",
                until=context.latest,
            )
        if context.scope.kind == "plant":
            add(
                "RT_SCOPE_PLANT",
                f"只含{context.scope.plant_name}的機組與跨廠共用（shared）的列；歸屬未定的機組不列入。",
            )
        return items

    def _warned_snapshots(self, query: RealtimeQuery) -> int:
        if query.period is not None:
            start, end = f"{query.period[0]} 00:00", f"{query.period[1]} 23:50"
        elif query.slots is None and query.day is None:
            return 0
        else:
            start, end = query.slots or (f"{query.day} 00:00", f"{query.day} 23:50")
        uri = f"{self.config.database.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            try:
                return int(
                    connection.execute(
                        """SELECT COUNT(*) FROM fact_rt_snapshot
                            WHERE data_time BETWEEN ? AND ? AND quality = 'warn'""",
                        (start, end),
                    ).fetchone()[0]
                )
            finally:
                connection.close()
        except sqlite3.Error:
            return 0


def _days_between(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]


def _trace(trace: list[dict[str, Any]], stage: str, started: float, **details: Any) -> None:
    trace.append(
        {"stage": stage, "elapsed_ms": round((perf_counter() - started) * 1000, 3), **details}
    )


def _unavailable(detail: str) -> PipelineResponse:
    return PipelineResponse(
        False,
        data={"trace": []},
        error_code="RT_UNAVAILABLE",
        error=(
            "目前讀不到即時資料：收集器可能還沒啟動過，或正在重建資料庫。"
            "請先執行「即時收集啟動.bat」，等第一筆資料進來後再問。"
        ),
        severity="error",
        evidence={"detail": detail},
    )


__all__ = [
    "REALTIME_ALLOWED_COLUMNS",
    "REALTIME_VIEWS",
    "Catalog",
    "RealtimeQuery",
    "RealtimeQueryEngine",
    "RealtimeRouter",
    "apply_unit_scope",
    "asks_energy_on_a_day",
    "looks_realtime",
]
