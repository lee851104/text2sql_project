"""The LLM-path benchmark: scorer rules and that every question really misses the rules."""

from __future__ import annotations

import json

import pytest

from eval.llm_eval import BENCHMARK, _rule_misses, cell_equal, result_matches
from ingest.build_db import build_database


def test_numbers_match_within_rounding_and_text_must_be_equal() -> None:
    assert cell_equal(35.2, 35.18064516129032)
    assert cell_equal("6,600,920", 6600920)
    assert not cell_equal(35.0, 35.18)
    assert cell_equal("地熱", "地熱")
    assert not cell_equal("地熱發電", "地熱")


def test_a_quantity_may_come_in_either_unit() -> None:
    columns, rows = ["kw", "wkw"], [(6600920, 660.092)]

    assert result_matches([(660.09,)], columns, rows, [["kw", "wkw"]])
    assert result_matches([("高雄市", 6600920)], columns, rows, [["kw", "wkw"]])
    assert not result_matches([(600.0,)], columns, rows, [["kw", "wkw"]])


def test_row_counts_and_every_group_must_match() -> None:
    columns = ["燃料", "n"]
    rows = [("煤", 17), ("水", 91)]
    groups = [["燃料"], ["n"]]

    assert result_matches([("水", 91), ("煤", 17)], columns, rows, groups)
    assert not result_matches([("水", 91)], columns, rows, groups)
    assert not result_matches([("水", 17), ("煤", 91)], columns, rows, groups)


@pytest.mark.integration
def test_every_benchmark_question_is_one_the_rules_cannot_answer(tmp_path) -> None:
    """規則接得住的題目量不到 LLM；規則變強之後要把題目換掉，而不是讓分數悄悄變好看。"""

    database = tmp_path / "power.db"
    build_database(database)
    items = json.loads(BENCHMARK.read_text(encoding="utf-8"))

    hits = _rule_misses(database, items)

    assert [item for item, hit in hits.items() if hit] == []
