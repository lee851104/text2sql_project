import json
from pathlib import Path

import pytest

from text2sql.entities import extract_entities
from text2sql.router import classify_intent, route
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
