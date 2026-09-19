"""Entry-level questions: what is in here, and what may I ask."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest.validate import PROJECT_ROOT
from text2sql.entities import extract_entities
from text2sql.router import DATA_SCOPE_QUESTIONS, classify_intent, data_scope_topic
from text2sql.semantic_guard import META_QUESTION_PATTERNS, SemanticGuard

SCOPE_QUESTIONS = ("有哪些電廠", "有哪些燃料別", "資料涵蓋到什麼時候", "總共有幾台機組")

DATA_RANGE = ("2025-01-01", "2026-07-31")
PEAK_COLUMNS = frozenset({"台中#1", "林口#1"})


def _guard() -> SemanticGuard:
    return SemanticGuard(data_range=DATA_RANGE, peak_columns=PEAK_COLUMNS, pitfalls=())


# 這些題目在改動前分別屬於 plant_units、unit_extreme 與 other。用「包含比對」實作
# data_scope 時三題全被偷走，離線執行率從 100% 掉到 95%。
QUESTIONS_THAT_WERE_STOLEN = {
    "大觀發電廠有哪些設備": "plant_units",
    "碧海在資料期間的峰值日期": "unit_extreme",
    "資料涵蓋的最早與最晚日期": "other",
    "天然氣機組共有幾台？": "fuel_stats",
}


@pytest.mark.parametrize("question", SCOPE_QUESTIONS)
def test_a_scope_question_gets_its_own_intent(question: str) -> None:
    assert classify_intent(question) == "data_scope"
    assert data_scope_topic(question) is not None


@pytest.mark.parametrize(("question", "intent"), sorted(QUESTIONS_THAT_WERE_STOLEN.items()))
def test_scope_matching_does_not_steal_from_other_intents(question: str, intent: str) -> None:
    """完全比對而非包含比對，是為了守住這一條。"""

    assert data_scope_topic(question) is None
    assert classify_intent(question) == intent


def test_a_scope_question_tolerates_trailing_punctuation() -> None:
    assert classify_intent("有哪些電廠？") == "data_scope"
    assert classify_intent("有哪些電廠 ") == "data_scope"


def test_a_question_with_extra_qualifiers_is_not_a_scope_question() -> None:
    """多了修飾就表示要問別的東西，交給既有意圖。"""

    assert data_scope_topic("台中發電廠有哪些機組") is None
    assert data_scope_topic("2026年資料涵蓋到什麼時候") is None


@pytest.mark.parametrize(
    "question",
    ["資料庫有哪些資料可以查詢", "我可以問什麼", "這個系統能查什麼", "支援哪些查詢"],
)
def test_a_meta_question_is_clarified_rather_than_routed(question: str) -> None:
    """後設問句沒有對應的 SQL；硬產只能查 sqlite_master，而那正是守門該擋的。"""

    decision = _guard().check_question(question, extract_entities(question))

    assert decision.severity == "clarify"
    assert decision.code == "DATA_SCOPE_QUESTION"
    assert decision.suggestions, "澄清必須給得出下一步，不能只說答不出來"


def test_meta_patterns_do_not_swallow_ordinary_questions() -> None:
    guard = _guard()

    for question in ("有哪些電廠", "列出台中發電廠所有設備", "2026年三月有哪些機組在歲修？"):
        decision = guard.check_question(question, extract_entities(question))
        assert decision.code != "DATA_SCOPE_QUESTION", question


def test_every_scope_question_is_also_in_the_corpus() -> None:
    """離線靠 router，線上靠語料檢索；同一批問句兩邊都要有，否則模式一換就答不出來。"""

    corpus = json.loads(
        (PROJECT_ROOT / "corpus" / "training_corpus.json").read_text(encoding="utf-8")
    )
    questions = {example["question"] for example in corpus["examples"]}

    for question in SCOPE_QUESTIONS:
        assert question in questions, question


def test_the_accepted_forms_are_distinct_across_topics() -> None:
    seen: set[str] = set()
    for accepted in DATA_SCOPE_QUESTIONS.values():
        assert not (seen & accepted), "同一句話不能對應到兩個主題"
        seen |= accepted


def test_meta_patterns_are_not_empty() -> None:
    assert META_QUESTION_PATTERNS


@pytest.mark.integration
def test_the_examples_lead_with_entry_level_questions() -> None:
    """第一次打開的人需要的是邊界，不是進階問法。"""

    from serving.app import create_app

    application = create_app()
    routes = {route.path: route for route in application.routes}
    assert "/api/examples" in routes

    from fastapi.testclient import TestClient

    with TestClient(application, base_url="http://testserver") as client:
        examples = client.get("/api/examples").json()["data"]

    assert examples[:3] == ["有哪些電廠", "資料涵蓋到什麼時候", "有哪些燃料別"]


def test_the_corpus_index_was_rebuilt_after_adding_examples() -> None:
    """語料與索引不同步時，檢索拿不到新加的例子。"""

    corpus = json.loads(
        (PROJECT_ROOT / "corpus" / "training_corpus.json").read_text(encoding="utf-8")
    )
    index = json.loads((PROJECT_ROOT / "corpus" / "index.json").read_text(encoding="utf-8"))
    indexed = {
        str(document.get("id")) for document in index.get("documents", index.get("entries", []))
    }

    for example in corpus["examples"]:
        assert str(example["id"]) in indexed, example["id"]


def test_corpus_path_exists() -> None:
    assert (PROJECT_ROOT / "corpus" / "index.json").exists()


def test_scope_questions_are_short_complete_sentences() -> None:
    """完全比對只在問句本身就是那句話時成立；別把長句塞進接受清單。"""

    for accepted in DATA_SCOPE_QUESTIONS.values():
        for form in accepted:
            assert len(form) <= 12, form
            assert Path(form).name == form
