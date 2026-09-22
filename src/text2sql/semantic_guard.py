"""Deterministic question- and SQL-level semantic safety rules."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from text2sql.aliases import resolve_peak_column
from text2sql.entities import Entities, compact_question, unparsed_date
from text2sql.generation_cost import aggregate_rows, wants_overview
from text2sql.llm import GeneratedQuery
from text2sql.router import classify_intent

# 後設問句：問的是「這個系統／資料庫有什麼」，而不是資料本身。這類沒有對應的 SQL，
# 所以在守門就澄清，不進產生流程 —— 讓它一路失敗到 GENERATION_FAILED 只會給使用者
# 一句「未能通過驗證與執行」，那對第一次打開的人沒有任何幫助。
META_QUESTION_PATTERNS: tuple[str, ...] = (
    "有哪些資料",
    "有什麼資料",
    "可以查什麼",
    "可以查詢什麼",
    "能查什麼",
    "能查詢什麼",
    "可以問什麼",
    "能問什麼",
    "怎麼用",
    "資料庫有什麼",
    "支援哪些查詢",
    "有哪些內容",
    "有什麼內容",
    "資料庫內容",
    "資料範圍",
)

# 問的是服務自己：模式、模型、金鑰。這些答案都在 /api/health，不在任何 view 裡。
# 比對前會轉小寫，所以名單一律寫小寫；資料欄位裡沒有任何 api／模型字樣，不會誤攔。
SYSTEM_STATUS_PATTERNS: tuple[str, ...] = (
    "接api",
    "串接api",
    "api嗎",
    "api key",
    "線上模式還是離線",
    "離線模式還是線上",
    "現在是什麼模式",
    "目前是什麼模式",
    "哪個模型",
    "什麼模型",
    "有沒有金鑰",
    "有設定金鑰",
)


@dataclass(frozen=True)
class SemanticDecision:
    severity: str = "pass"
    code: str = "OK"
    reason: str = ""
    suggestions: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


class PitfallLike(Protocol):
    code: str
    target_kind: str
    target_name: str
    severity: str
    reason: str
    suggestion: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class SemanticPitfall:
    code: str
    target_kind: str
    target_name: str
    severity: str
    reason: str
    suggestion: str
    evidence: dict[str, Any]


# 每個檢視自己的時間欄位與量出範圍的查詢。`meta_manifest` 的 data_range 是全域的
# （2025-01-01～2026-07-31），但 v_re_generation 實際是 2024-01～2026-07 —— 用全域那組
# 去描述再生能源的合計會講錯期間，所以逐個檢視量。
VIEW_TIME_SPANS: dict[str, tuple[str, tuple[str, ...]]] = {
    "v_peak": ('SELECT MIN("日期"), MAX("日期") FROM v_peak', ("日期",)),
    "v_system": ('SELECT MIN("日期"), MAX("日期") FROM v_system', ("日期",)),
    "v_re_generation": (
        "SELECT MIN(\"年度\" || '-' || SUBSTR('0' || \"月份\", -2)),"
        " MAX(\"年度\" || '-' || SUBSTR('0' || \"月份\", -2)) FROM v_re_generation",
        ("年度", "月份"),
    ),
    "v_generation_cost": (
        'SELECT MIN("年度"), MAX("年度") FROM v_generation_cost',
        ("年度",),
    ),
    # 大修是前瞻性排程，本來就延伸到未來（實測 2025-07～2028-06）。跟日尖峰共用一個
    # 範圍，會把答得出來的大修問句擋掉，理由還指向錯的那張表。
    "v_outage": (
        'SELECT MIN("開始日期"), MAX("結束日期") FROM v_outage WHERE "日期狀態" = \'valid\'',
        ("開始日期", "結束日期"),
    ),
}


def _view_time_spans(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """量出每個檢視實際涵蓋的時間範圍；讀不到的檢視略過，不要讓整個 guard 建不起來。"""

    spans: dict[str, tuple[str, str]] = {}
    for view, (sql, _columns) in VIEW_TIME_SPANS.items():
        try:
            row = connection.execute(sql).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0] is not None and row[1] is not None:
            spans[view] = (str(row[0]), str(row[1]))
    return spans


def load_semantic_context(
    database: Path,
) -> tuple[tuple[str, str], list[SemanticPitfall], dict[str, tuple[str, str]]]:
    """Load the dynamic date range, alignment-derived rules, and per-view time spans."""

    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        start, end = connection.execute(
            "SELECT data_start, data_end FROM meta_manifest WHERE id = 1"
        ).fetchone()
        rows = connection.execute(
            """SELECT pitfall_code, target_kind, target_name, severity,
                      reason, suggestion, evidence
               FROM meta_pitfall ORDER BY id"""
        ).fetchall()
        view_spans = _view_time_spans(connection)
    pitfalls = [
        SemanticPitfall(
            code, kind, name or "", severity, reason, suggestion or "", json.loads(data)
        )
        for code, kind, name, severity, reason, suggestion, data in rows
    ]
    return (start, end), pitfalls, view_spans


class SemanticGuard:
    def __init__(
        self,
        *,
        data_range: tuple[str, str],
        peak_columns: set[str],
        pitfalls: list[PitfallLike] | tuple[PitfallLike, ...] = (),
        view_spans: dict[str, tuple[str, str]] | None = None,
    ):
        self.data_range = data_range
        self.peak_columns = peak_columns
        self.pitfalls = tuple(pitfalls)
        # 量不到的檢視不會出現在這裡；少一張表的資訊不該讓守門失效，用到的地方各自退回。
        self.view_spans = dict(view_spans or {})

    @classmethod
    def from_database(cls, database: Path, *, peak_columns: set[str]) -> SemanticGuard:
        data_range, pitfalls, view_spans = load_semantic_context(database)
        return cls(
            data_range=data_range,
            peak_columns=peak_columns,
            pitfalls=pitfalls,
            view_spans=view_spans,
        )

    def describe_aggregate_scope(self, query: GeneratedQuery) -> SemanticDecision | None:
        """聚合沒有限制時間時，把它實際涵蓋的期間講出來。

        「離岸風力的發電量 794,751,440 度」看起來像個年度數字，實際上是 2024-01 到
        2026-07 共 31 個月的合計，而 2026 只有 7 個月 —— 拿去跟前兩年比會得到錯的結論。
        數字本身沒錯，錯在少了讀懂它需要的那一句話。

        這裡與 `check_sql` 分開，因為後者一次只回一個 decision：再生能源的查詢會先撞上
        `RENEWABLE_SELF_BUILT_ONLY`，期間就永遠輪不到。兩件事都該說，所以各自回報。
        """

        try:
            tree = parse_one(query.sql, read="sqlite")
        except ParseError:
            return None
        if not any(
            True
            for aggregate in (exp.Sum, exp.Avg, exp.Count, exp.Max, exp.Min)
            for _node in tree.find_all(aggregate)
        ):
            return None

        tables = {table.name for table in tree.find_all(exp.Table)}
        spans = {view: span for view, span in self.view_spans.items() if view in tables}
        if not spans:
            return None

        # 問句已經框了時間就不必再說一次 —— WHERE 提到任何一個時間欄位就算數。
        where = tree.args.get("where")
        if where is not None:
            constrained = {column.name for column in where.find_all(exp.Column)}
            time_columns = {
                name for view in tables for name in VIEW_TIME_SPANS.get(view, ("", ()))[1]
            }
            if constrained & time_columns:
                return None

        described = "；".join(
            f"{view} 涵蓋 {start} 至 {end}" for view, (start, end) in spans.items()
        )
        return self._decision(
            "disclose",
            "AGGREGATE_OVER_FULL_RANGE",
            f"這個彙總沒有限制時間，涵蓋資料庫內的全部期間（{described}）。",
            "要特定期間請在問句裡指明年月。",
            evidence={"spans": {view: list(span) for view, span in spans.items()}},
        )

    @staticmethod
    def _decision(
        severity: str,
        code: str,
        reason: str,
        *suggestions: str,
        evidence: dict[str, Any] | None = None,
    ) -> SemanticDecision:
        return SemanticDecision(severity, code, reason, suggestions, evidence or {})

    def _targets(self, code: str) -> tuple[PitfallLike, ...]:
        return tuple(item for item in self.pitfalls if item.code == code)

    @staticmethod
    def _target_in_question(target: str, question: str) -> bool:
        simplified = re.sub(r"\s|\([^)]*\)|發電廠$", "", target)
        return bool(simplified and simplified in question)

    def check_question(self, question: str, entities: Entities) -> SemanticDecision:
        compact = compact_question(question)

        if any(pattern in compact.lower() for pattern in SYSTEM_STATUS_PATTERNS):
            # 「目前有接 API 嗎」問的是服務自己，不是資料。答案在 /api/health，硬產
            # SQL 只會重試三次然後回一句在講 SQL 的錯誤 —— 而使用者根本沒問 SQL。
            return self._decision(
                "clarify",
                "SYSTEM_STATUS_QUESTION",
                "這個問題問的是服務本身，不用查詢資料。目前的模式、模型與資料版本"
                "顯示在頁面頂端，也可以呼叫 /api/health。",
                "資料涵蓋到什麼時候",
                "有哪些電廠",
                evidence={"see": "/api/health"},
            )

        if any(pattern in compact for pattern in META_QUESTION_PATTERNS):
            # 「這裡有什麼資料」的答案是 schema 層級的說明，不是資料列。硬產 SQL 只能去
            # 查 sqlite_master，而那正是 SqlGuard 該擋的東西 —— 為了回答這個問題而在白
            # 名單上開一個口，代價遠大於收益。改為在這裡澄清，並指向資料涵蓋說明。
            return self._decision(
                "clarify",
                "DATA_SCOPE_QUESTION",
                "這個問題不用查詢回答。資料涵蓋範圍、可回答的主題與目前答不出的項目，"
                "都列在「資料總覽」頁，或呼叫 /api/coverage。",
                "有哪些電廠",
                "有哪些燃料別",
                "資料涵蓋到什麼時候",
                evidence={"see": "/api/coverage"},
            )

        cost_types = (
            "火力",
            "核能",
            "抽蓄",
            "再生能源",
            "慣常水力",
            "風力",
            "太陽光電",
            "地熱",
            "汽電共生",
            "民營電廠",
            "燃油",
            "燃煤",
            "燃氣",
            # 成本表存的是「燃氣」「慣常水力」，但使用者講「天然氣」「水力」。
            # 不收這兩個寫法，等於對一個已經指名口徑的問句追問「請指定發電方式」。
            "天然氣",
            "水力",
            "發購電",
            "自發電力小計",
            "購入電力小計",
        )
        # 成本表只有元/度，資料庫沒有任何發電量欄位（fact_daily_peak 是功率不是能量），
        # 所以「發電量 × 成本」算不出來。這條要排在口徑追問之前：真正的阻礙是缺發電量，
        # 不是沒指定發電方式 —— 照那個追問改寫問法，改完還是答不出來。
        wants_generation = any(
            word in compact for word in ("發電量", "度數", "發了多少", "估算成本", "估算發電成本")
        )
        if "成本" in compact and wants_generation:
            return self._decision(
                "refuse",
                "NO_GENERATION_FOR_COST",
                "發電成本是元/度，但本資料集沒有發電量（每日資料是尖峰出力，"
                "屬功率不是能量），兩者相乘算不出來。",
                "2025年燃煤發電成本是多少？",
                "各種發電方式的發電成本是多少？",
                evidence={"missing": "generation_kwh", "have": "cost_per_kwh"},
            )

        aggregates = aggregate_rows()
        if "成本" in compact and aggregates and wants_overview(compact):
            # 一覽式問句由 router 答出來，但**答案要帶著限制一起送到眼前**：排掉的那幾列
            # 不是消失了，是換個問法才拿得到。這條回 disclose 而不是 clarify，所以不會
            # 攔下查詢，只會掛在成功的答案上。
            return self._decision(
                "disclose",
                "GENERATION_COST_AGGREGATES_EXCLUDED",
                "已排除彙總列（"
                + "、".join(aggregates)
                + "）。它們與明細混在同一欄，並列會被當成並列的發電方式 ——"
                "「火力發電」其實是燃煤、燃氣、燃油的彙總。要看彙總請指名去問。",
                "2025年平均發購電成本是多少？",
                "2025年火力發電成本是多少？",
                evidence={"excluded": list(aggregates), "see": "configs/generation_cost.yaml"},
            )

        if "成本" in compact and not any(name in compact for name in cost_types):
            year = entities.date_range.start[:4] if entities.date_range else "2025"
            return self._decision(
                "clarify",
                "GENERATION_COST_TYPE_REQUIRED",
                "發電成本包含多種口徑，請指定發電方式或平均發購電成本。",
                f"{year}年火力發電成本是多少？",
                f"{year}年平均發購電成本是多少？",
                evidence={"available_scope": "2023-2025 annual generation cost"},
            )

        sum_words = ("總和", "加起來", "合計", "相加", "累計", "發電量")
        cross_date_words = ("去年", "今年", "每天", "所有日期", "跨月", "跨月份")
        same_day = any(word in compact for word in ("同一天", "當日", "單日"))
        if "成本" not in compact and (
            any(word in compact for word in sum_words)
            and any(word in compact for word in cross_date_words)
            and not same_day
        ):
            return self._decision(
                "refuse",
                "PEAK_SUM_ACROSS_DAYS",
                "尖峰出力是單一時刻的功率，跨日加總不是發電量。",
                "改問指定期間的最高、最低或平均尖峰出力。",
                "如需發電量，必須改用具有時間積分意義的其他資料源。",
                evidence={"metric": "尖峰出力_萬瓩", "shape": "cross-date sum"},
            )

        unit_mismatch = (
            ("尖峰出力" in compact and "裝置容量瓩" in compact)
            or ("萬瓩" in compact and "容量瓩" in compact)
            or ("萬瓩" in compact and "瓩容量" in compact)
            or "不同功率單位" in compact
            or "未換算單位" in compact
        )
        if unit_mismatch:
            return self._decision(
                "refuse",
                "UNIT_MISMATCH",
                "裝置容量「瓩」與尖峰出力「萬瓩」相差 10,000 倍，不可直接運算。",
                "改用裝置容量_萬瓩與尖峰出力_萬瓩比較。",
                evidence={"units": ["瓩", "萬瓩"]},
            )

        # 核能不在這張清單裡：v_peak 的「核能」類別有**完整**的 6 部單機（核一/二/三
        # 各 2 部，沒有彙總欄），容量查得到。其他幾類不是沒有單機欄就是只涵蓋一部分
        # （IPP 有 9 欄單機但另有 3 欄彙總，列出來會少算而且看不出來）。
        unsupported = ("風力", "風光", "IPP", "ipp", "太陽能", "汽電共生")
        detail_words = ("每一台", "各機組", "機組主檔", "每部設備", "單機", "明細")
        # 「機組主檔」指的是 dim_unit，那裡確實沒有核能 —— 這一句仍然要擋。
        if "核能" in compact and "主檔" in compact:
            return self._decision(
                "refuse",
                "NO_UNIT_DETAIL",
                "機組主檔不含核能；核能的單機容量在每日尖峰資料的核能類別裡。",
                "各核能電廠的機組與裝置容量明細",
                evidence={"unsupported_scope": "unit master"},
            )
        if any(word in compact for word in unsupported) and any(
            word in compact for word in detail_words
        ):
            return self._decision(
                "refuse",
                "NO_UNIT_DETAIL",
                "現有機組主檔不含該類別的單機明細。",
                "改查可用的類別彙總出力。",
                evidence={"unsupported_scope": "unit detail"},
            )

        # 這些電廠在每日尖峰資料沒有自己的欄位，出力併在共用彙總欄裡。回空表會被讀成
        # 「資料缺漏」，因此明講來源限制並指向查得到的替代問法。
        daily_words = ("出力", "尖峰", "發電量", "負載")
        for rule in self._targets("PLANT_DAILY_ONLY_IN_BUCKET"):
            if self._target_in_question(rule.target_name, compact) and any(
                word in compact for word in daily_words
            ):
                return self._decision(
                    "refuse",
                    rule.code,
                    rule.reason,
                    rule.suggestion,
                    evidence={"target": rule.target_name, **rule.evidence},
                )

        trend_words = ("趨勢", "同比", "年增率", "近兩年", "去年和今年", "每月長期", "跨期", "走勢")
        residual_rules = self._targets("RESIDUAL_TREND")
        residual_hit = (
            any(self._target_in_question(rule.target_name, compact) for rule in residual_rules)
            or "殘差欄" in compact
        )
        if residual_hit and any(word in compact for word in trend_words):
            rule = next(
                (
                    item
                    for item in residual_rules
                    if self._target_in_question(item.target_name, compact)
                ),
                None,
            )
            return self._decision(
                "refuse",
                "RESIDUAL_TREND",
                rule.reason if rule else "殘差欄組成可能隨版本漂移，不適合跨期趨勢。",
                rule.suggestion if rule else "改查單日值或粒度穩定的具名機組。",
                evidence=rule.evidence if rule else {"target": "residual"},
            )

        total_words = ("完整", "全廠", "所有機組合計", "總出力", "總計")
        for rule in self._targets("PLANT_TOTAL_INCOMPLETE"):
            if self._target_in_question(rule.target_name, compact) and any(
                word in compact for word in total_words
            ):
                return self._decision(
                    "disclose",
                    rule.code,
                    rule.reason,
                    rule.suggestion,
                    evidence={"target": rule.target_name, **rule.evidence},
                )

        capacity_words = ("容量", "ratio", "實測", "對帳")
        for rule in self._targets("KNOWN_CAPACITY_GAP"):
            if self._target_in_question(rule.target_name, compact) and any(
                word in compact for word in capacity_words
            ):
                return self._decision(
                    "disclose",
                    rule.code,
                    rule.reason,
                    rule.suggestion,
                    evidence={"target": rule.target_name, **rule.evidence},
                )

        zero_words = ("零出力", "沒有出力", "零值", "有值幾天", "等於零")
        if entities.date_range is None and any(word in compact for word in zero_words):
            return self._decision(
                "disclose",
                "ZERO_PERIOD_AMBIGUOUS",
                "零出力會隨資料期間改變，未指定期間時只能以目前全部資料計算。",
                "請指定年份、月份或日期範圍。",
                evidence={"default_range": list(self.data_range)},
            )

        renewable_words = ("太陽能", "太陽光電", "光電", "風力", "地熱", "再生能源", "綠電")
        nationwide_words = ("全國", "全台", "全臺", "台灣", "臺灣", "各縣市", "全部")
        for rule in self._targets("RENEWABLE_SELF_BUILT_ONLY"):
            if (
                any(word in compact for word in renewable_words)
                and any(word in compact for word in nationwide_words)
                and any(word in compact for word in ("發電量", "度數", "發了多少", "總量"))
            ):
                return self._decision(
                    "disclose",
                    rule.code,
                    rule.reason,
                    rule.suggestion,
                    evidence=dict(rule.evidence),
                )

        resolution = resolve_peak_column(compact, self.peak_columns)
        if resolution.ambiguous:
            return self._decision(
                "clarify",
                "AMBIGUOUS_UNIT_NAME",
                "機組名稱可對應多個資料欄位，不會自行猜測。",
                "請說明要查具名燃煤機組，還是複循環彙總欄。",
                evidence={"candidates": list(resolution.candidates)},
            )

        fragment = unparsed_date(question, entities)
        if fragment is not None:
            # 直接查下去不會報錯，只會回整段期間的答案 —— 那是最糟的失敗方式：
            # 使用者問一天，拿到一整年，而且看不出來。
            return self._decision(
                "clarify",
                "UNPARSED_DATE",
                f"看不懂「{fragment}」這個日期寫法，所以沒有把它當成查詢條件。"
                "直接查下去會回整段期間的答案，而那不是你問的。",
                "2026-07-20台中#1的尖峰出力",
                "2026年7月20日台中#1的尖峰出力",
                "115年7月20日台中#1的尖峰出力",
                evidence={"fragment": fragment, "available_range": list(self.data_range)},
            )

        # 問大修就拿大修的範圍比，問尖峰就拿尖峰的範圍比。用 classify_intent 而不是
        # 在這裡再寫一份關鍵字：同一件事的判斷散在兩個地方，補一邊沒補另一邊就會出現
        # 難查的半殘狀態。
        applicable_range = self.data_range
        outage_range = self.view_spans.get("v_outage")
        if outage_range is not None and classify_intent(question, entities) == "outage":
            applicable_range = outage_range

        if (
            entities.date_range
            and (
                entities.date_range.end < applicable_range[0]
                or entities.date_range.start > applicable_range[1]
            )
            or "資料開始日前一天" in compact
        ):
            return self._decision(
                "clarify",
                "DATA_RANGE_OUT_OF_BOUNDS",
                "問句的日期超出目前資料涵蓋範圍。",
                f"請改查 {applicable_range[0]} 至 {applicable_range[1]} 之間。",
                evidence={"available_range": list(applicable_range)},
            )
        return SemanticDecision()

    def check_sql(
        self, question: str, query: GeneratedQuery, entities: Entities
    ) -> SemanticDecision:
        try:
            tree = parse_one(query.sql, read="sqlite")
        except ParseError:
            return SemanticDecision()

        for aggregate in tree.find_all(exp.Sum):
            summed = aggregate.this
            grouped_by_date = any(
                column.name == "日期"
                for group in tree.find_all(exp.Group)
                for column in group.find_all(exp.Column)
            )
            if (
                isinstance(summed, exp.Column)
                and summed.name == "尖峰出力_萬瓩"
                and not (grouped_by_date or entities.explicit_date)
            ):
                return self._decision(
                    "refuse",
                    "PEAK_SUM_ACROSS_DAYS",
                    "SQL 將尖峰時刻的瞬時功率跨日加總，結果沒有發電量意義。",
                    "改用 MAX、MIN 或 AVG，或限制在單一日期。",
                    evidence={"function": "SUM", "column": "尖峰出力_萬瓩"},
                )

        for operation in tree.find_all(exp.Binary):
            columns = {column.name for column in operation.find_all(exp.Column)}
            if {"裝置容量_瓩", "尖峰出力_萬瓩"} <= columns:
                return self._decision(
                    "refuse",
                    "UNIT_MISMATCH",
                    "SQL 在同一算式混用瓩與萬瓩。",
                    "將裝置容量換成萬瓩後再運算。",
                    evidence={"columns": sorted(columns)},
                )

        all_columns = {column.name for column in tree.find_all(exp.Column)}
        string_params = tuple(str(param) for param in query.params if isinstance(param, str))
        # 核能不在這張清單裡，理由與上面問句層那條相同：v_peak 的核能類別是完整的
        # 六部單機，沒有彙總欄，查單機明細不會少算。其餘幾類要嘛沒有單機欄，要嘛
        # 單機只涵蓋一部分（IPP 9 欄單機之外另有 3 欄彙總）。
        unsupported = ("風力", "風光", "IPP", "ipp", "太陽能", "汽電共生")
        asks_for_unit_detail = bool({"機組名", "機組欄位"} & all_columns)
        if asks_for_unit_detail and any(
            category in value for category in unsupported for value in string_params
        ):
            return self._decision(
                "refuse",
                "NO_UNIT_DETAIL",
                "SQL 試圖將只有彙總粒度的類別當成單機明細查詢。",
                "改查類別彙總出力。",
                evidence={"params": list(string_params)},
            )

        if {"機組欄位", "尖峰出力_萬瓩"} & all_columns:
            for rule in self._targets("PLANT_DAILY_ONLY_IN_BUCKET"):
                if self._target_in_question(rule.target_name, question) or any(
                    self._target_in_question(rule.target_name, value) for value in string_params
                ):
                    return self._decision(
                        "refuse",
                        rule.code,
                        rule.reason,
                        rule.suggestion,
                        evidence={"target": rule.target_name, **rule.evidence},
                    )

        trend_words = ("趨勢", "同比", "年增率", "近兩年", "長期", "跨期", "走勢")
        residual_filter = "是殘差欄" in all_columns and (
            bool(re.search(r'"是殘差欄"\s*=\s*1(?:\D|$)', query.sql)) or 1 in query.params
        )
        if residual_filter and any(word in question for word in trend_words):
            return self._decision(
                "refuse",
                "RESIDUAL_TREND",
                "SQL 對組成可能漂移的殘差欄進行跨期趨勢分析。",
                "改查單日值或粒度穩定的具名機組。",
                evidence={"filter": "是殘差欄 = 1"},
            )

        group_columns = {
            column.name
            for group in tree.find_all(exp.Group)
            for column in group.find_all(exp.Column)
        }
        incomplete_rules = self._targets("PLANT_TOTAL_INCOMPLETE")
        if "電廠" in group_columns and incomplete_rules:
            affected = [
                rule
                for rule in incomplete_rules
                if not string_params
                or any(self._target_in_question(rule.target_name, value) for value in string_params)
            ]
            if affected:
                return self._decision(
                    "disclose",
                    "PLANT_TOTAL_INCOMPLETE",
                    "電廠彙總包含無法從全系統殘差欄拆回的機組，部分廠別結果不完整。",
                    "改查具名機組，或在結果中保留此限制。",
                    evidence={"affected": [rule.target_name for rule in affected]},
                )

        capacity_columns = {"對應裝置容量_萬瓩", "裝置容量_萬瓩", "裝置容量_瓩"}
        if capacity_columns & all_columns:
            for rule in self._targets("KNOWN_CAPACITY_GAP"):
                if self._target_in_question(rule.target_name, question) or any(
                    self._target_in_question(rule.target_name, value) for value in string_params
                ):
                    return self._decision(
                        "disclose",
                        rule.code,
                        rule.reason,
                        rule.suggestion,
                        evidence={"target": rule.target_name, **rule.evidence},
                    )

        question_decision = self.check_question(question, entities)
        if question_decision.code != "OK":
            return question_decision

        # 這個檢視的涵蓋範圍只有台電自建場站，任何查詢都必須帶著範圍說明回去，
        # 否則數字看起來合理但比全國實際值小一個數量級。
        if any(table.name == "v_re_generation" for table in tree.find_all(exp.Table)):
            for rule in self._targets("RENEWABLE_SELF_BUILT_ONLY"):
                return self._decision(
                    "disclose",
                    rule.code,
                    rule.reason,
                    rule.suggestion,
                    evidence=dict(rule.evidence),
                )

        where = tree.args.get("where")
        if where is not None:
            where_columns = {column.name for column in where.find_all(exp.Column)}
            has_zero_filter = bool(
                re.search(r'"尖峰出力_萬瓩"\s*(?:=|>)\s*0(?:\D|$)', query.sql)
            ) or ("尖峰出力_萬瓩" in where_columns and 0 in query.params)
            if has_zero_filter and "日期" not in where_columns:
                return self._decision(
                    "disclose",
                    "ZERO_PERIOD_AMBIGUOUS",
                    "SQL 的零出力篩選沒有限制日期範圍。",
                    "請指定日期範圍。",
                    evidence={"available_range": list(self.data_range)},
                )
        return SemanticDecision()
