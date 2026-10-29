"""Checks that turn silent LLM mistakes into retries (each case is a real CP-088 failure)."""

from __future__ import annotations

import json

from text2sql.answer_checks import check_result, check_sql, coerce_numeric_params
from text2sql.llm import FakeLLM
from text2sql.pipeline import Text2SQLPipeline
from text2sql.sql_guard import SqlGuard

COUNTIES = {"v_unit.縣市": ["新北市林口區", "新北市新店區", "高雄市小港區"]}


def test_a_value_outside_the_closed_set_is_refused_with_the_real_values() -> None:
    issue = check_sql(
        'SELECT DISTINCT "電廠" FROM v_unit WHERE "縣市" = ? LIMIT 200', ("新北市",), COUNTIES
    )

    assert issue is not None and issue.code == "SQL_VALUE_NOT_IN_SET"
    assert "新北市林口區" in issue.reason


def test_values_in_the_closed_set_and_open_columns_pass() -> None:
    assert check_sql('SELECT 1 FROM v_unit WHERE "縣市" = ? LIMIT 1', ("基隆市",), {}) is None
    assert (
        check_sql('SELECT 1 FROM v_unit WHERE "縣市" IN (?) LIMIT 1', ("新北市林口區",), COUNTIES)
        is None
    )


def test_like_without_a_wildcard_is_refused() -> None:
    sql = 'SELECT SUM("裝置容量_萬瓩") FROM v_unit WHERE "縣市" LIKE ? LIMIT 1'

    assert check_sql(sql, ("高雄市",), COUNTIES).code == "SQL_LIKE_WITHOUT_WILDCARD"
    assert check_sql(sql, ("高雄市%",), COUNTIES) is None


def test_numeric_strings_in_comparisons_become_numbers_but_like_is_left_alone() -> None:
    sql = 'SELECT "機組名" FROM v_unit WHERE "裝置容量_萬瓩" > ? AND "縣市" LIKE ? LIMIT 200'

    assert coerce_numeric_params(sql, ("80", "12%")) == (80, "12%")
    assert coerce_numeric_params(sql, ("80.5", "x")) == (80.5, "x")


def test_parameters_keep_their_positions_when_coerced() -> None:
    sql = 'SELECT 1 FROM v_peak WHERE "日期" BETWEEN ? AND ? AND "尖峰出力_萬瓩" > ? LIMIT 5'

    assert coerce_numeric_params(sql, ("2026-07-01", "2026-07-31", "50")) == (
        "2026-07-01",
        "2026-07-31",
        50,
    )


def test_an_empty_or_all_null_result_is_suspicious() -> None:
    assert check_result("SELECT 1 LIMIT 5", [], asks_top_n=False, max_rows=200).code == (
        "RESULT_EMPTY"
    )
    assert check_result("SELECT SUM(x) LIMIT 1", [(None,)], asks_top_n=False, max_rows=200)


def test_a_grouped_listing_cut_at_its_limit_is_suspicious() -> None:
    sql = 'SELECT "縣市", COUNT(*) FROM v_unit GROUP BY "縣市" ORDER BY "縣市" LIMIT 10'

    issue = check_result(sql, [("x", 1)] * 10, asks_top_n=False, max_rows=200)

    assert issue is not None and issue.code == "RESULT_TRUNCATED"


def test_intentional_rankings_and_single_answers_are_not_truncation() -> None:
    ranking = 'SELECT "日期" FROM v_system ORDER BY "備轉容量率_pct" LIMIT 3'
    single = 'SELECT "日期" FROM v_system ORDER BY "日期" DESC LIMIT 1'

    assert check_result(ranking, [(1,)] * 3, asks_top_n=False, max_rows=200) is None
    assert check_result(single, [(1,)], asks_top_n=False, max_rows=200) is None
    grouped = "SELECT a, COUNT(*) FROM v GROUP BY a LIMIT 5"
    assert check_result(grouped, [(1, 1)] * 5, asks_top_n=True, max_rows=200) is None


def _pipeline(llm: FakeLLM, run_sql, tmp_path) -> Text2SQLPipeline:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps({"version": "t", "ddl": [], "documentation": [], "examples": []}),
        encoding="utf-8",
    )
    return Text2SQLPipeline(
        llm=llm,
        sql_guard=SqlGuard(),
        run_sql=run_sql,
        corpus_path=corpus,
        data_range=("2025-01-01", "2026-07-31"),
        peak_columns=set(),
        column_values=COUNTIES,
    )


def test_the_pipeline_sends_a_silent_mistake_back_and_runs_the_corrected_sql(tmp_path) -> None:
    wrong = {
        "sql": 'SELECT DISTINCT "電廠" FROM v_unit WHERE "縣市" = ? LIMIT 200',
        "params": ["新北市"],
    }
    right = {
        "sql": 'SELECT DISTINCT "電廠" FROM v_unit WHERE "縣市" LIKE ? LIMIT 200',
        "params": ["新北市%"],
    }
    llm = FakeLLM([json.dumps(wrong, ensure_ascii=False), json.dumps(right, ensure_ascii=False)])
    executed: list[tuple] = []

    def run_sql(sql, params):
        executed.append(params)
        return ["電廠"], [("林口發電廠",), ("桂山發電廠",)]

    response = _pipeline(llm, run_sql, tmp_path).query("新北市有哪些電廠")

    assert response.success
    assert executed == [("新北市%",)], "被擋下的那一版不該執行"
    assert "SQL_VALUE_NOT_IN_SET" in llm.calls[1]


def test_rule_sql_is_never_second_guessed(tmp_path) -> None:
    """規則路由的 SQL 是人寫、測過的：0 筆就是 0 筆，不重試。"""

    llm = FakeLLM([])
    pipeline = _pipeline(llm, lambda _sql, _params: (["日期"], []), tmp_path)

    response = pipeline.query("2026年7月20日備轉容量率")

    assert llm.calls == []
    assert response.data["source"] == "router"
