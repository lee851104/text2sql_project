import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from align.crosswalk import parse_crosswalk
from align.pitfalls import generate_pitfalls
from eval.cases import SEMANTIC_NEGATIVE_CONTROLS
from text2sql.entities import extract_entities
from text2sql.llm import GeneratedQuery
from text2sql.semantic_guard import SemanticGuard, SemanticPitfall, load_semantic_context

ROOT = Path(__file__).parents[1]
DATA_RANGE = ("2025-01-01", "2026-07-31")
PEAK_COLUMNS = {"興達#3", "興達 (#1-#5)", "台中#1", "立霧"}


@pytest.fixture(scope="module")
def semantic_guard() -> SemanticGuard:
    with (ROOT / "taipower_align" / "crosswalk.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        pitfalls = generate_pitfalls(parse_crosswalk(csv.DictReader(handle)), ratio_max=1.06)
    return SemanticGuard(data_range=DATA_RANGE, peak_columns=PEAK_COLUMNS, pitfalls=pitfalls)


def test_trap_benchmark_hit_rate_is_at_least_ninety_five_percent(
    semantic_guard: SemanticGuard,
) -> None:
    traps = json.loads((ROOT / "benchmarks" / "trap_questions.json").read_text(encoding="utf-8"))
    hits = 0
    for item in traps:
        entities = extract_entities(item["question"], reference_date=date(2026, 7, 31))
        decision = semantic_guard.check_question(item["question"], entities)
        hits += (decision.code, decision.severity) == (
            item["expect"]["code"],
            item["expect"]["severity"],
        )
    assert hits / len(traps) >= 0.95


def test_safe_question_false_positive_rate_is_at_most_five_percent(
    semantic_guard: SemanticGuard,
) -> None:
    blocked = 0
    for question in SEMANTIC_NEGATIVE_CONTROLS:
        entities = extract_entities(question, reference_date=date(2026, 7, 31))
        decision = semantic_guard.check_question(question, entities)
        blocked += decision.severity in {"refuse", "clarify", "disclose"}
    assert blocked / len(SEMANTIC_NEGATIVE_CONTROLS) <= 0.05


def test_sql_shape_blocks_cross_date_sum_but_allows_same_day(
    semantic_guard: SemanticGuard,
) -> None:
    sql = 'SELECT SUM("尖峰出力_萬瓩") FROM v_peak LIMIT 1'
    blocked = semantic_guard.check_sql(
        "查尖峰出力統計", GeneratedQuery(sql, ()), extract_entities("查尖峰出力統計")
    )
    assert (blocked.code, blocked.severity) == ("PEAK_SUM_ACROSS_DAYS", "refuse")

    question = "2026年7月20日全部機組尖峰出力合計"
    allowed = semantic_guard.check_sql(
        question, GeneratedQuery(sql, ()), extract_entities(question)
    )
    assert allowed.code == "OK"


def test_sql_shape_detects_unit_mismatch(semantic_guard: SemanticGuard) -> None:
    query = GeneratedQuery('SELECT "裝置容量_瓩" / "尖峰出力_萬瓩" FROM v_peak LIMIT 1', ())
    decision = semantic_guard.check_sql("比率", query, extract_entities("比率"))
    assert (decision.code, decision.severity) == ("UNIT_MISMATCH", "refuse")


def test_sql_shape_discloses_unbounded_zero_filter(semantic_guard: SemanticGuard) -> None:
    query = GeneratedQuery('SELECT COUNT(*) FROM v_peak WHERE "尖峰出力_萬瓩" = 0 LIMIT 1', ())
    decision = semantic_guard.check_sql("統計", query, extract_entities("統計"))
    assert (decision.code, decision.severity) == ("ZERO_PERIOD_AMBIGUOUS", "disclose")


@pytest.mark.parametrize(
    ("question", "query", "expected"),
    [
        (
            "查類別資料",
            GeneratedQuery('SELECT "機組名" FROM v_unit WHERE "燃料" = ? LIMIT 20', ("風力",)),
            ("NO_UNIT_DETAIL", "refuse"),
        ),
        (
            "長期趨勢",
            GeneratedQuery(
                'SELECT "日期", "尖峰出力_萬瓩" FROM v_peak '
                'WHERE "是殘差欄" = 1 ORDER BY "日期" LIMIT 100',
                (),
            ),
            ("RESIDUAL_TREND", "refuse"),
        ),
        (
            "各廠總計",
            GeneratedQuery(
                'SELECT "電廠", SUM("裝置容量_萬瓩") FROM v_unit GROUP BY "電廠" LIMIT 30',
                (),
            ),
            ("PLANT_TOTAL_INCOMPLETE", "disclose"),
        ),
        (
            "容量對帳",
            GeneratedQuery(
                'SELECT "機組欄位", "對應裝置容量_萬瓩" FROM v_peak WHERE "機組欄位" = ? LIMIT 1',
                ("大潭 (#1-#9)",),
            ),
            ("KNOWN_CAPACITY_GAP", "disclose"),
        ),
    ],
)
def test_additional_sql_shapes(
    semantic_guard: SemanticGuard,
    question: str,
    query: GeneratedQuery,
    expected: tuple[str, str],
) -> None:
    decision = semantic_guard.check_sql(question, query, extract_entities(question))
    assert (decision.code, decision.severity) == expected


def test_context_is_loaded_from_database(tmp_path) -> None:
    database = tmp_path / "context.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE meta_manifest (id INTEGER, data_start TEXT, data_end TEXT)"
        )
        connection.execute("INSERT INTO meta_manifest VALUES (1, '2025-01-01', '2026-07-31')")
        connection.execute(
            """CREATE TABLE meta_pitfall (
                   id INTEGER, pitfall_code TEXT, target_kind TEXT, target_name TEXT,
                   severity TEXT, reason TEXT, suggestion TEXT, evidence TEXT)"""
        )
        connection.execute(
            "INSERT INTO meta_pitfall VALUES (1, 'RESIDUAL_TREND', 'column', ?, "
            "'refuse', 'reason', 'suggestion', '{}')",
            ("氣渦輪",),
        )
    data_range, pitfalls = load_semantic_context(database)
    assert data_range == DATA_RANGE
    assert pitfalls[0].target_name == "氣渦輪"


def test_unspecified_generation_cost_requires_clarification(
    semantic_guard: SemanticGuard,
) -> None:
    question = "2025年發電的成本是多少"

    decision = semantic_guard.check_question(question, extract_entities(question))

    assert (decision.code, decision.severity) == ("GENERATION_COST_TYPE_REQUIRED", "clarify")
    assert "2025年火力發電成本是多少？" in decision.suggestions


def _bucket_only_guard() -> SemanticGuard:
    rule = SemanticPitfall(
        code="PLANT_DAILY_ONLY_IN_BUCKET",
        target_kind="plant",
        target_name="高屏發電廠",
        severity="refuse",
        reason=(
            "高屏發電廠在每日尖峰資料中沒有自己的欄位，出力併在「其他小水力」這個 10 廠合計欄位裡。"
        ),
        suggestion="改查高屏發電廠的機組裝置容量與歲修排程。",
        evidence={"bucket": "其他小水力", "member_plants": 10},
    )
    return SemanticGuard(data_range=DATA_RANGE, peak_columns=PEAK_COLUMNS, pitfalls=[rule])


def test_plant_without_its_own_daily_column_is_refused_with_the_source_limit() -> None:
    guard = _bucket_only_guard()
    question = "高屏發電廠昨天的尖峰出力是多少？"

    decision = guard.check_question(question, extract_entities(question))

    assert (decision.severity, decision.code) == ("refuse", "PLANT_DAILY_ONLY_IN_BUCKET")
    assert "其他小水力" in decision.reason
    assert decision.evidence["member_plants"] == 10


def test_bucket_only_rule_does_not_block_that_plant_s_equipment_questions() -> None:
    guard = _bucket_only_guard()
    question = "高屏發電廠有哪些機組？裝置容量多少？"

    decision = guard.check_question(question, extract_entities(question))

    assert decision.severity == "pass"


def test_bucket_only_rule_also_blocks_sql_that_filters_on_that_plant() -> None:
    guard = _bucket_only_guard()
    sql = 'SELECT "機組欄位", "尖峰出力_萬瓩" FROM v_peak WHERE "電廠" = ? LIMIT 10'

    decision = guard.check_sql(
        "查一下這個廠的出力",
        GeneratedQuery(sql, ("高屏發電廠",)),
        extract_entities("查一下這個廠的出力"),
    )

    assert (decision.severity, decision.code) == ("refuse", "PLANT_DAILY_ONLY_IN_BUCKET")


RENEWABLE_PITFALL = SemanticPitfall(
    code="RENEWABLE_SELF_BUILT_ONLY",
    target_kind="global",
    target_name="",
    severity="disclose",
    reason="v_re_generation 只涵蓋台電自建的再生能源場站，約為全國風光地熱的 3～4%。",
    suggestion="回答時必須註明僅限台電自建場站。",
    evidence={"view": "v_re_generation", "capacity_kw": 762760},
)


@pytest.fixture(scope="module")
def renewable_guard() -> SemanticGuard:
    return SemanticGuard(
        data_range=DATA_RANGE, peak_columns=PEAK_COLUMNS, pitfalls=[RENEWABLE_PITFALL]
    )


class TestRenewableScopeDisclosure:
    """這份資料只涵蓋台電自建場站，答案若不附範圍就會小一個數量級。"""

    @pytest.mark.parametrize(
        "question",
        [
            "2025年全台太陽能發電量是多少",
            "全國風力發了多少度",
            "台灣各縣市再生能源總量",
        ],
    )
    def test_nationwide_renewable_question_discloses_the_scope(
        self, renewable_guard: SemanticGuard, question: str
    ) -> None:
        decision = renewable_guard.check_question(question, extract_entities(question))
        assert decision.severity == "disclose"
        assert decision.code == "RENEWABLE_SELF_BUILT_ONLY"

    def test_any_query_on_the_view_carries_the_scope_note(
        self, renewable_guard: SemanticGuard
    ) -> None:
        query = GeneratedQuery(
            sql='SELECT "發電站", SUM("發電量_度") FROM v_re_generation '
            'WHERE "年度" = ? GROUP BY 1 LIMIT 20',
            params=[2025],
        )
        decision = renewable_guard.check_sql("查詢", query, extract_entities("2025年"))
        assert decision.severity == "disclose"
        assert decision.code == "RENEWABLE_SELF_BUILT_ONLY"

    @pytest.mark.parametrize(
        "question",
        ["台電自建風力2025年發了多少度", "2026年7月備轉容量率最低是哪一天"],
    )
    def test_questions_within_scope_are_not_flagged(
        self, renewable_guard: SemanticGuard, question: str
    ) -> None:
        decision = renewable_guard.check_question(question, extract_entities(question))
        assert decision.severity == "pass"

    def test_other_views_are_untouched(self, renewable_guard: SemanticGuard) -> None:
        query = GeneratedQuery(
            sql='SELECT "日期", "尖峰負載_萬瓩" FROM v_system WHERE "日期" = ? LIMIT 1',
            params=["2026-07-05"],
        )
        decision = renewable_guard.check_sql("查詢", query, extract_entities("2026年7月5日"))
        assert decision.severity == "pass"
