from __future__ import annotations

import json
from pathlib import Path

from text2sql.corpus import load_corpus
from text2sql.prompt import EXAMPLES_NOTE, build_prompt
from text2sql.retriever import RetrievedExample

ROOT = Path(__file__).parents[1]
CORPUS = load_corpus(ROOT / "corpus" / "training_corpus.json")
DATA_RANGE = ("2025-01-01", "2026-07-31")

HITS = [
    RetrievedExample(0.9, {"id": "closest", "question": "最像的", "sql": "SELECT 1"}),
    RetrievedExample(0.1, {"id": "farthest", "question": "最不像的", "sql": "SELECT 2"}),
    RetrievedExample(0.5, {"id": "middle", "question": "中間的", "sql": "SELECT 3"}),
]


def _payload(**kwargs: object) -> dict[str, object]:
    return json.loads(
        build_prompt(
            "台中#1昨天的出力",
            corpus=CORPUS,
            examples=HITS,
            data_range=DATA_RANGE,
            **kwargs,  # type: ignore[arg-type]
        )
    )


def test_the_closest_example_sits_next_to_the_question() -> None:
    """模型對長文中段的注意力最弱，所以最像的那一則要緊鄰問題。"""

    payload = _payload()
    assert [item["id"] for item in payload["examples"]] == ["farthest", "middle", "closest"]
    keys = list(payload)
    assert keys.index("examples") < keys.index("question"), "問題必須在範例之後"


def test_the_prompt_says_what_the_order_means() -> None:
    """排序本身沒有意義，除非講出來。"""

    payload = _payload()
    keys = list(payload)
    assert payload["examples_note"] == EXAMPLES_NOTE
    assert keys.index("examples_note") < keys.index("examples"), "說明要在資料之前"


def test_the_block_order_follows_task_schema_rules_examples_question() -> None:
    keys = list(_payload())
    for earlier, later in (
        ("task", "ddl"),
        ("ddl", "rules"),
        ("rules", "examples"),
        ("examples", "question"),
    ):
        assert keys.index(earlier) < keys.index(later), f"{earlier} 必須排在 {later} 之前"


def test_a_first_attempt_carries_no_failure_block() -> None:
    payload = _payload()
    assert "previous_attempt_sql" not in payload
    assert "previous_attempt_error" not in payload


def test_a_retry_hands_back_both_the_sql_and_the_error() -> None:
    """只給錯誤碼不夠 —— `SQL_MISSING_TABLE` 是我們自己定義的分類，不會指名道姓。"""

    payload = _payload(
        prior_sql="SELECT * FROM sqlite_master",
        prior_error="SQL_TABLE_NOT_ALLOWED: 不允許的資料表：['sqlite_master']",
    )
    assert payload["previous_attempt_sql"] == "SELECT * FROM sqlite_master"
    assert "sqlite_master" in payload["previous_attempt_error"]
    keys = list(payload)
    assert keys.index("question") < keys.index("previous_attempt_sql")


def test_a_retry_keeps_the_whole_context() -> None:
    """模型第一次寫錯，不該連參考書一起被收走。"""

    payload = _payload(prior_sql="SELECT 1", prior_error="boom")
    assert payload["ddl"] and payload["rules"]
    assert len(payload["examples"]) == len(HITS)


def test_closed_set_columns_travel_with_their_values() -> None:
    """ddl 只有欄位名時，模型不知道「燃料」裡裝的是「煤」還是「燃煤」。"""

    payload = _payload(column_values={"v_unit.燃料": ["水", "重油", "天然氣", "輕柴油", "煤"]})

    assert payload["column_values"] == {"v_unit.燃料": ["水", "重油", "天然氣", "輕柴油", "煤"]}
    assert "封閉集合" in str(payload["column_values_note"])


def test_column_values_sit_next_to_the_ddl_not_in_the_rules() -> None:
    """值是 schema 的一部分，位置要緊鄰 ddl —— 丟進 rules 會被例子隔開。"""

    keys = list(_payload(column_values={"v_unit.燃料": ["煤"]}))

    assert keys.index("ddl") < keys.index("column_values") < keys.index("rules")


def test_a_prompt_without_column_values_is_unchanged() -> None:
    """沒有值可給的時候，整份 prompt 要和先前一模一樣。"""

    assert "column_values" not in _payload()
    assert "column_values" not in _payload(column_values={})
