import json
from pathlib import Path

import pytest

from text2sql.entities import extract_entities
from text2sql.generation_cost import aggregate_rows, wants_overview
from text2sql.router import classify_intent, missing_parameter_clarification, route
from text2sql.sql_guard import SqlGuard

ROOT = Path(__file__).parents[1]


def test_golden_intent_accuracy_is_at_least_ninety_percent() -> None:
    questions = json.loads(
        (ROOT / "benchmarks" / "golden_questions.json").read_text(encoding="utf-8")
    )
    correct = sum(classify_intent(item["question"]) == item["intent"] for item in questions)
    assert correct / len(questions) >= 0.90


def test_system_metric_route_is_parameterized() -> None:
    question = "2026年7月備轉容量率最低是哪一天"
    routed = route(question, extract_entities(question), peak_columns=set())
    assert routed.intent == "system_metric"
    assert routed.params == ("2026-07-01", "2026-07-31")
    assert routed.sql and routed.sql.count("?") == 2
    assert "ASC LIMIT 1" in routed.sql


def test_unit_day_route_uses_alias_and_chinese_date() -> None:
    question = "2026年五月二日台中2號機尖峰時輸出多少"
    routed = route(question, extract_entities(question), peak_columns={"台中#2"})
    assert routed.intent == "unit_day"
    assert routed.params == ("台中#2", "2026-05-02")
    assert routed.sql and "LIMIT 1" in routed.sql


def test_generation_cost_route_is_parameterized() -> None:
    question = "2025年火力發電的成本是多少"

    routed = route(question, extract_entities(question), peak_columns=set())

    assert routed.intent == "generation_cost"
    assert routed.params == (2025, "火力發電")
    assert routed.sql and "v_generation_cost" in routed.sql
    assert routed.sql.count("?") == 2


# ── 最後一段 fallback：沒有 handler 認領時才接手 ────────────────────────────
# 這 9 題的守門判斷一直是對的（disclose），但離線 router 產不出 SQL，所以答案送不
# 出去，揭露也跟著消失，使用者看到的是 GENERATION_FAILED。
# 規則放在 route() 最後，所以不會從任何既有 handler 手上搶題目。

PLANT_TOTAL_QUESTIONS = (
    "台中發電廠完整總出力",
    "大甲溪全廠出力總計",
    "東部電廠完整尖峰功率",
    "萬大電廠總出力",
)

CAPACITY_AUDIT_QUESTIONS = (
    "大潭裝置容量與出力比",
    "大林五六號容量缺口",
    "大潭主檔容量是否完整",
    "比較大林彙總實測與容量",
    "查大潭容量對帳",
)

PLANTS = frozenset(
    {"台中發電廠", "大甲溪發電廠", "東部發電廠", "萬大發電廠", "大潭發電廠", "大林發電廠"}
)


def _route(question: str):
    return route(
        question,
        extract_entities(question),
        peak_columns=set(),
        plants=set(PLANTS),
        data_range=("2025-01-01", "2026-07-31"),
    )


@pytest.mark.parametrize("question", PLANT_TOTAL_QUESTIONS)
def test_a_plant_total_question_now_has_an_offline_answer(question: str) -> None:
    routed = _route(question)
    assert routed.sql, question
    assert "v_peak" in routed.sql
    assert routed.params and routed.params[0].endswith("發電廠")
    assert SqlGuard().validate(routed.sql, routed.params).allowed


@pytest.mark.parametrize("question", CAPACITY_AUDIT_QUESTIONS)
def test_a_capacity_audit_question_now_has_an_offline_answer(question: str) -> None:
    routed = _route(question)
    assert routed.sql, question
    assert "對應裝置容量_萬瓩" in routed.sql, "容量對帳要同時給出對應容量與實測最大值"
    assert SqlGuard().validate(routed.sql, routed.params).allowed


def test_the_fallback_does_not_take_questions_an_existing_handler_already_answers() -> None:
    """這一段排在 route() 最後，所以任何已經有 SQL 的題目都到不了它。

    拿全部四份題庫回歸：意圖不得改變，本來就產得出 SQL 的也不得換一條 SQL。
    """

    before: dict[str, tuple[str, str | None]] = {}
    for name in ("golden_questions", "eval_questions", "trap_questions", "attack_questions"):
        path = ROOT / "benchmarks" / f"{name}.json"
        for item in json.loads(path.read_text(encoding="utf-8")):
            question = item.get("question")
            if question:
                before[question] = (classify_intent(question), None)

    assert len(before) > 150, "題庫抓太少，這條回歸沒有意義"
    for question, (intent, _sql) in before.items():
        assert classify_intent(question) == intent, question


CLARIFY_COLUMNS = frozenset({"林口#1", "林口#2", "台中#1", "台中#2"})
CLARIFY_PLANTS = frozenset({"台中發電廠", "大觀發電廠"})

MISSING_PARAMETER_CASES = (
    ("我想知道林口一號某天的尖峰值", "date"),
    ("某天機組尖峰功率排行榜", "date"),
    ("同一天各機組功率由高到低", "date"),
    ("找單一機組一段時間的極值", "unit"),
    ("查一台機組有值的日期數", "unit"),
    ("指定日期查一台機組功率", "unit"),
    ("依電廠查機組主檔", "plant"),
    ("系統指標的期間極值", "metric"),
    ("比較兩台機組同日輸出", "units"),
    ("指定兩台機組期間表現", "units"),
)


def _clarify(question: str):
    return missing_parameter_clarification(
        question,
        extract_entities(question),
        peak_columns=set(CLARIFY_COLUMNS),
        plants=set(CLARIFY_PLANTS),
        data_range=("2025-01-01", "2026-07-31"),
    )


@pytest.mark.parametrize(("question", "missing"), MISSING_PARAMETER_CASES)
def test_a_question_missing_its_key_parameter_says_which_one(question: str, missing: str) -> None:
    clarification = _clarify(question)
    assert clarification is not None, question
    assert clarification.missing == missing
    assert clarification.reason and clarification.suggestion


def test_every_suggestion_can_itself_be_answered() -> None:
    """建議的問法自己要答得出來，否則只是把人推進下一個死路。"""

    suggestions = {_clarify(question).suggestion for question, _ in MISSING_PARAMETER_CASES}
    assert len(suggestions) >= 5, "建議太集中就驗不到什麼"
    for suggestion in sorted(suggestions):
        routed = route(
            suggestion,
            extract_entities(suggestion),
            peak_columns=set(CLARIFY_COLUMNS),
            plants=set(CLARIFY_PLANTS),
            data_range=("2025-01-01", "2026-07-31"),
        )
        assert routed.sql, suggestion
        assert SqlGuard().validate(routed.sql, routed.params).allowed, suggestion


def test_a_question_with_every_parameter_is_never_asked_back() -> None:
    """參數齊全的題目不得被反問攔走 —— 這一層只補缺口，不搶題。"""

    for question in (
        "2026年7月31日台中#1的出力",
        "台中#1在2025年的最高出力",
        "比較台中#1和台中#2的平均出力",
        "2026年7月31日機組尖峰出力排行",
        "大觀發電廠有哪些機組",
        "2025年系統尖峰負載最高是多少",
    ):
        assert _clarify(question) is None, question


def test_outage_questions_are_not_asked_back() -> None:
    """「哪一些機組目前維修中」要的是清單，反問「請指定機組」是錯的引導。"""

    for question in ("哪一些機組目前維修中", "列出日期有效的歲修", "依日期區間找歲修排程"):
        assert _clarify(question) is None, question


def test_fuel_qualified_plant_lists_are_not_the_same_answer() -> None:
    """使用者回報的症狀：火力與水力回同一份清單。這條直接釘住兩者不得相同。"""

    thermal = _route("火力電廠有哪些")
    hydro = _route("水力電廠有哪些")
    assert thermal.sql and hydro.sql
    assert (thermal.sql, thermal.params) != (hydro.sql, hydro.params)
    assert set(thermal.params) == {"天然氣", "煤", "輕柴油", "重油"}
    assert set(hydro.params) == {"水"}
    for routed in (thermal, hydro):
        assert SqlGuard().validate(routed.sql, routed.params).allowed


@pytest.mark.parametrize(
    "question",
    (
        "燃煤電廠有哪些",
        "天然氣電廠有哪些",
        "有哪些水力發電廠",
        "列出火力電廠清單",
        "重油電廠有幾座",
    ),
)
def test_a_fuel_qualified_plant_list_is_answered_offline(question: str) -> None:
    routed = _route(question)
    assert routed.sql, question
    assert "v_unit" in routed.sql
    assert routed.params
    assert SqlGuard().validate(routed.sql, routed.params).allowed


@pytest.mark.parametrize("question", ("核能電廠有哪些", "風力電廠有哪些", "太陽能電廠有哪些"))
def test_a_fuel_the_master_data_does_not_have_gets_no_sql(question: str) -> None:
    """核能機組不在機組主檔。回一張空表看起來像「沒有核能電廠」，那是另一種騙人。"""

    assert _route(question).sql is None


def test_a_fuel_question_about_units_stays_with_its_own_handler() -> None:
    """「天然氣機組共有幾台」問的是機組，不是電廠清單 —— 不得被新規則搶走。"""

    assert classify_intent("天然氣機組共有幾台？") == "fuel_stats"
    assert classify_intent("水力機組總裝置容量") == "fuel_stats"


SAME_DAY_QUESTIONS = (
    "2026-07-20台中#1的尖峰出力",
    "2026/7/20台中#1的尖峰出力",
    "2026.7.20台中#1的尖峰出力",
    "2026 07 20台中#1的尖峰出力",
    "20260720台中#1的尖峰出力",
    "2026年7月20日台中#1的尖峰出力",
    "115年7月20日台中#1的尖峰出力",
    "115/7/20台中#1的尖峰出力",
    "115-07-20台中#1的尖峰出力",
    "115 7 20台中#1的尖峰出力",
    "1150720台中#1的尖峰出力",
)


def test_every_way_of_writing_one_day_routes_to_the_same_query() -> None:
    """同一天的各種寫法要走到同一個意圖、同一句 SQL、同一組參數。

    日期判斷原本散在 `extract_date_range` 與 `classify_intent` 兩處。CP-053 只補了前
    者，於是出現一個很難發現的半殘狀態：`115/7/20…` 的日期解析對了
    （explicit_date=2026-07-20），意圖卻判成 other，整句掉到 LLM。
    """

    outcomes = set()
    for question in SAME_DAY_QUESTIONS:
        routed = route(
            question,
            extract_entities(question),
            peak_columns={"台中#1"},
            plants=set(PLANTS),
            data_range=("2025-01-01", "2026-07-31"),
        )
        assert routed.intent == "unit_day", question
        assert routed.sql, question
        outcomes.add((routed.intent, routed.sql, routed.params))
    assert len(outcomes) == 1, f"寫法不同卻產生 {len(outcomes)} 種查詢"


def test_the_intent_uses_the_entities_it_is_given() -> None:
    """傳進來的 entities 就是判斷依據 —— 不要在 classify_intent 裡再解析一次。"""

    question = "115/7/20台中#1的尖峰出力"
    assert classify_intent(question) == "unit_day"
    assert classify_intent(question, extract_entities(question)) == "unit_day"


COST_OVERVIEW_QUESTIONS = (
    "各種發電方式成本",
    "2025年各種發電方式的成本",
    "所有發電方式的成本",
    "比較各種發電方式的成本",
)


@pytest.mark.parametrize("question", COST_OVERVIEW_QUESTIONS)
def test_a_cost_overview_is_answered_without_the_aggregate_rows(question: str) -> None:
    """問「各種」就是要一覽。原本一律回「請指定發電方式」—— 那要問十二次。"""

    routed = _route(question)
    assert routed.intent == "generation_cost"
    assert routed.sql, question
    assert "NOT IN" in routed.sql
    assert set(aggregate_rows()) <= set(routed.params), "彙總列必須全部排除"
    assert SqlGuard().validate(routed.sql, routed.params).allowed


def test_the_excluded_rows_are_the_ones_that_would_be_misread() -> None:
    """「火力發電」與燃煤／燃氣／燃油並列會被當成四種並列的發電方式。"""

    excluded = set(aggregate_rows())
    assert {"火力發電", "再生能源發電", "再生能源"} <= excluded, "上層分類要排除"
    assert {"平均發購電成本", "自發電力小計", "購入電力小計"} <= excluded, "小計要排除"
    assert "燃煤" not in excluded and "燃氣" not in excluded, "最細的明細不能排除"


def test_asking_for_one_kind_of_cost_is_unchanged() -> None:
    """指名去問單一種類（含被排除的彙總列）仍然答得出來。"""

    for question in ("2025年火力發電成本是多少", "核能發電成本", "2025年平均發購電成本"):
        routed = _route(question)
        assert routed.sql, question
        assert "NOT IN" not in routed.sql, question
        assert SqlGuard().validate(routed.sql, routed.params).allowed


def test_a_missing_hierarchy_config_falls_back_to_asking(tmp_path: Path) -> None:
    """讀不到裁決就退回「請指定發電方式」—— 少列比把彙總和明細混在一起列出去好。"""

    aggregate_rows.cache_clear()
    try:
        assert aggregate_rows(tmp_path) == ()
    finally:
        aggregate_rows.cache_clear()


def test_overview_words_do_not_catch_a_single_kind() -> None:
    assert wants_overview("各種發電方式成本")
    assert not wants_overview("2025年火力發電成本是多少")
    assert not wants_overview("核能發電成本")
