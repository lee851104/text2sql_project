import json
from pathlib import Path

import pytest

from text2sql.entities import extract_entities
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
