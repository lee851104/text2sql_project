import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from align.crosswalk import parse_crosswalk
from align.pitfalls import generate_pitfalls
from eval.cases import SEMANTIC_NEGATIVE_CONTROLS
from ingest.build_db import build_database
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
    data_range, pitfalls, view_spans = load_semantic_context(database)
    assert data_range == DATA_RANGE
    assert pitfalls[0].target_name == "氣渦輪"
    # 這個 fixture 沒有任何 v_* 檢視，量不到範圍就該是空的，而不是讓載入整個失敗。
    assert view_spans == {}


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


@pytest.mark.parametrize(
    "question",
    (
        "資料庫有甚麼內容",
        "資料庫有什麼內容",
        "資料庫裡有哪些內容",
        "這個資料庫有甚么資料",
        "有甚麼資料",
        "可以查甚麼",
    ),
)
def test_a_scope_question_is_clarified_whichever_variant_is_typed(
    semantic_guard: SemanticGuard, question: str
) -> None:
    """「甚麼」與「什麼」是同一句話。實測只差這一個字，一句澄清、一句回 SQL 錯誤。"""

    decision = semantic_guard.check_question(question, extract_entities(question))
    assert decision.code == "DATA_SCOPE_QUESTION"
    assert decision.severity == "clarify"


@pytest.mark.parametrize(
    "question",
    (
        "目前有接API嗎",
        "你有接 api 嗎",
        "現在是線上模式還是離線模式",
        "你用的是哪個模型",
        "有設定金鑰嗎",
    ),
)
def test_a_question_about_the_service_itself_is_not_sent_to_sql(
    semantic_guard: SemanticGuard, question: str
) -> None:
    """問的是服務自己，答案在 /api/health，不在任何 view 裡。"""

    decision = semantic_guard.check_question(question, extract_entities(question))
    assert decision.code == "SYSTEM_STATUS_QUESTION"
    assert decision.severity == "clarify"
    assert decision.evidence["see"] == "/api/health"


def test_the_new_patterns_do_not_take_any_benchmark_question(
    semantic_guard: SemanticGuard,
) -> None:
    """新規則不得攔走題庫裡任何一題 —— 包含攻擊題，那些必須照原本的方式被擋。"""

    taken = []
    for name in ("golden_questions", "eval_questions", "trap_questions", "attack_questions"):
        path = ROOT / "benchmarks" / f"{name}.json"
        for item in json.loads(path.read_text(encoding="utf-8")):
            question = item.get("question") or item.get("input")
            if not question:
                continue
            decision = semantic_guard.check_question(question, extract_entities(question))
            if decision.code == "SYSTEM_STATUS_QUESTION":
                taken.append(question)
    assert taken == []


@pytest.mark.parametrize(
    "question",
    (
        "26/7/20台中#1最高出力",
        "2026年13月40日台中#1最高出力",
        "13月5日台中#1最高出力",
    ),
)
def test_a_date_we_cannot_read_is_asked_about_not_ignored(
    semantic_guard: SemanticGuard, question: str
) -> None:
    """忽略看不懂的日期不會報錯，只會回整段期間的答案 —— 使用者問一天卻拿到一整年。"""

    decision = semantic_guard.check_question(question, extract_entities(question))
    assert decision.code == "UNPARSED_DATE"
    assert decision.severity == "clarify"
    assert decision.suggestions, "要給得出正確格式長什麼樣"


@pytest.mark.parametrize(
    "question",
    (
        "台中#1最高出力",
        "台中#1在2025年的最高出力",
        "2026年7月20日台中#1的尖峰出力",
        "115/7/20台中#1的尖峰出力",
        "有哪些電廠",
    ),
)
def test_a_readable_or_absent_date_is_left_alone(
    semantic_guard: SemanticGuard, question: str
) -> None:
    """讀得懂的、以及根本沒提日期的，都不該被這條攔下。"""

    decision = semantic_guard.check_question(question, extract_entities(question))
    assert decision.code != "UNPARSED_DATE", question


@pytest.mark.parametrize(
    "question", ("各種發電方式成本", "2025年各種發電方式的成本", "比較各種發電方式的成本")
)
def test_a_cost_overview_discloses_what_it_left_out(
    semantic_guard: SemanticGuard, question: str
) -> None:
    """答案要帶著限制一起送到眼前 —— 排掉的列不是消失了，是換個問法才拿得到。"""

    decision = semantic_guard.check_question(question, extract_entities(question))
    assert decision.code == "GENERATION_COST_AGGREGATES_EXCLUDED"
    assert decision.severity == "disclose", "disclose 不能攔下查詢"
    assert "火力發電" in decision.evidence["excluded"]
    assert decision.suggestions, "要說得出彙總怎麼問"


@pytest.mark.parametrize("question", ("發電成本", "成本多少", "2025年成本"))
def test_a_cost_question_with_no_kind_and_no_overview_still_asks(
    semantic_guard: SemanticGuard, question: str
) -> None:
    """既不指名、也不是要一覽的，仍然要問清楚是哪一種口徑。"""

    decision = semantic_guard.check_question(question, extract_entities(question))
    assert decision.code == "GENERATION_COST_TYPE_REQUIRED"
    assert decision.severity == "clarify"


# ---------------------------------------------------------------------------
# 大修排程的涵蓋期間與日尖峰不同
#
# meta_manifest 的 data_end 來自日尖峰資料（2026-07-31），但 dim_outage 是**前瞻性
# 排程**，實測涵蓋到 2028-06。拿日尖峰的範圍去擋大修問句，會把「下個月有哪些機組要
# 大修」這種完全答得出來的題目擋掉，而且理由寫成「日期超出資料涵蓋範圍」—— 指向錯
# 的那張表。
# ---------------------------------------------------------------------------

OUTAGE_RANGE = ("2025-07-01", "2028-06-23")


def _guard_with_outage_range() -> SemanticGuard:
    return SemanticGuard(
        data_range=DATA_RANGE,
        peak_columns=PEAK_COLUMNS,
        pitfalls=(),
        outage_range=OUTAGE_RANGE,
    )


@pytest.mark.parametrize(
    "question",
    [
        "下個月有哪些機組要大修",
        "明年有哪些機組安排大修",
        "未來兩年有哪些機組會進行大修",
        "下個月大修的燃煤機組總裝置容量是多少",
    ],
)
def test_a_future_overhaul_question_is_not_blocked_by_the_peak_data_range(question: str) -> None:
    decision = _guard_with_outage_range().check_question(question, extract_entities(question))
    assert decision.code != "DATA_RANGE_OUT_OF_BOUNDS", question


def test_an_overhaul_question_beyond_even_the_schedule_is_still_blocked() -> None:
    """放寬的是「換一張表的範圍」，不是「不檢查」。2030 年仍然該擋。"""

    question = "2030年有哪些機組安排大修"
    decision = _guard_with_outage_range().check_question(question, extract_entities(question))
    assert decision.code == "DATA_RANGE_OUT_OF_BOUNDS"


def test_a_peak_question_still_uses_the_peak_range() -> None:
    """非大修的問句不受影響，仍然以日尖峰的範圍為準。"""

    question = "2027年3月台中#1的尖峰出力"
    decision = _guard_with_outage_range().check_question(question, extract_entities(question))
    assert decision.code == "DATA_RANGE_OUT_OF_BOUNDS"


@pytest.fixture(scope="module")
def built_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """用版控裡的 taipower_align/ 現建一份快照（約 3 秒）。

    原本這裡讀 data/processed/power.db，讀不到就 skip —— 那個檔不進版控，所以 CI 從來
    沒有真的跑過這個測試（CP-064 是同一個毛病的另一半）。
    """

    database = tmp_path_factory.mktemp("semantic-guard") / "power.db"
    build_database(database)
    return database


def test_the_outage_range_is_read_from_the_database(built_database: Path) -> None:
    guard = SemanticGuard.from_database(built_database, peak_columns=PEAK_COLUMNS)
    assert guard.outage_range is not None
    assert guard.outage_range[1] > guard.data_range[1], "大修排程應該比日尖峰更晚結束"


def test_nuclear_unit_capacity_is_answerable_but_the_unit_master_is_still_refused() -> None:
    """核能在 v_peak 有完整的 6 部單機，問容量明細答得出來；
    但問「機組主檔」仍該擋 —— dim_unit 確實沒有核能。"""

    guard = SemanticGuard(data_range=DATA_RANGE, peak_columns=PEAK_COLUMNS, pitfalls=())
    answerable = guard.check_question(
        "各核能電廠的機組與裝置容量明細", extract_entities("各核能電廠的機組與裝置容量明細")
    )
    assert answerable.code != "NO_UNIT_DETAIL"

    refused = guard.check_question("核能機組主檔細節", extract_entities("核能機組主檔細節"))
    assert refused.code == "NO_UNIT_DETAIL"


# ---------------------------------------------------------------------------
# 「發電量 × 成本」：擋，但要用對的理由
#
# 成本表只有元/度，資料庫沒有任何發電量欄位（fact_daily_peak 是功率不是能量）。
# 所以「去年燃煤發電量乘以發電成本」算不出來。
#
# 原本這幾題有兩種下場，兩種都不對：
#   1. 回 GENERATION_COST_TYPE_REQUIRED「請指定發電方式」—— 理由指向錯的東西，
#      使用者照建議改問了還是答不出來
#   2. 成功但只回成本表，沒有乘上發電量 —— 數字看起來正常，答的卻是另一個問題
# ---------------------------------------------------------------------------


def _cost_guard() -> SemanticGuard:
    return SemanticGuard(data_range=DATA_RANGE, peak_columns=PEAK_COLUMNS, pitfalls=())


@pytest.mark.parametrize(
    "question",
    [
        "去年燃煤發電量乘以發電成本是多少",
        "去年天然氣發電量乘以發電成本是多少",
        "去年各種能源的發電量與估算成本是多少",
        "哪種能源去年估算發電成本最高",
        "各火力電廠去年發電量及估算成本是多少",
        "林口電廠去年發電量及估算成本是多少",
    ],
)
def test_cost_times_generation_is_refused_for_the_right_reason(question: str) -> None:
    decision = _cost_guard().check_question(question, extract_entities(question))
    assert decision.code == "NO_GENERATION_FOR_COST", question
    assert decision.severity == "refuse"


@pytest.mark.parametrize(
    "question",
    ["天然氣發電成本是多少", "水力發電成本是多少"],
)
def test_a_named_cost_type_is_not_asked_back(question: str) -> None:
    """成本表用「燃氣」「慣常水力」，但使用者講「天然氣」「水力」。

    問句已經指名了口徑，再追問「請指定發電方式」等於沒有回答。
    """

    decision = _cost_guard().check_question(question, extract_entities(question))
    assert decision.code != "GENERATION_COST_TYPE_REQUIRED", question


@pytest.mark.parametrize(
    "question",
    ["2025年火力發電成本是多少", "各種發電方式的發電成本是多少", "燃煤發電成本是多少"],
)
def test_a_pure_cost_question_is_unaffected(question: str) -> None:
    decision = _cost_guard().check_question(question, extract_entities(question))
    assert decision.code != "NO_GENERATION_FOR_COST", question


# ── 聚合的時間範圍：數字對，但少了讀懂它需要的那句話 ────────────────────


def _scope_guard() -> SemanticGuard:
    return SemanticGuard(
        data_range=DATA_RANGE,
        peak_columns=set(),
        view_spans={"v_re_generation": ("2024-01", "2026-07")},
    )


def test_an_unbounded_aggregate_says_what_period_it_covers() -> None:
    """「離岸風力 794,751,440 度」看起來像年度數字，其實是 31 個月的合計。"""

    query = GeneratedQuery(
        'SELECT "能源別", SUM("發電量_度") FROM v_re_generation GROUP BY "能源別" LIMIT 20', ()
    )

    decision = _scope_guard().describe_aggregate_scope(query)

    assert decision is not None
    assert decision.severity == "disclose"
    assert decision.code == "AGGREGATE_OVER_FULL_RANGE"
    assert "2024-01" in decision.reason and "2026-07" in decision.reason


def test_an_aggregate_already_bounded_by_time_says_nothing() -> None:
    """問句自己框了時間就不必再說一次。"""

    query = GeneratedQuery(
        'SELECT SUM("發電量_度") FROM v_re_generation WHERE "年度" = ? LIMIT 1', (2025,)
    )

    assert _scope_guard().describe_aggregate_scope(query) is None


def test_a_query_without_aggregation_says_nothing() -> None:
    query = GeneratedQuery('SELECT "發電站" FROM v_re_generation LIMIT 5', ())

    assert _scope_guard().describe_aggregate_scope(query) is None


def test_a_view_with_no_measured_span_says_nothing() -> None:
    """量不到範圍的檢視不要硬掰一個期間出來。"""

    query = GeneratedQuery("SELECT COUNT(*) FROM v_unit LIMIT 1", ())

    assert _scope_guard().describe_aggregate_scope(query) is None
