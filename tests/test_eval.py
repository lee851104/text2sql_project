from pathlib import Path

import pytest

from eval.metrics import QueryResult, accuracy, grouped_accuracy, result_match
from eval.run_eval import run_evaluation
from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT


def test_result_match_uses_column_and_row_sets_not_order() -> None:
    left = QueryResult(("date", "value"), (("2026-01-02", 2), ("2026-01-01", 1)))
    right = QueryResult(("value", "date"), ((1, "2026-01-01"), (2, "2026-01-02")))
    assert result_match(left, right)
    assert not result_match(left, QueryResult(("date", "other"), left.rows))


def test_metrics_report_counts_and_groups() -> None:
    assert accuracy([True, False, True]) == {"passed": 2, "total": 3, "accuracy": 2 / 3}
    outcomes = [
        {"split": "in", "passed": True},
        {"split": "out", "passed": False},
        {"split": "out", "passed": True},
    ]
    assert grouped_accuracy(outcomes, group_key="split")["out"]["accuracy"] == 0.5


@pytest.mark.integration
def test_complete_offline_evaluation_meets_acceptance(tmp_path: Path) -> None:
    database = tmp_path / "power.db"
    build_database(database)
    report = run_evaluation(
        database=database,
        benchmark_dir=PROJECT_ROOT / "benchmarks",
        corpus_path=PROJECT_ROOT / "corpus/training_corpus.json",
    )
    assert report["status"] == "pass"
    assert report["execution"]["by_corpus_split"]["false"]["accuracy"] >= 0.60
    assert report["safety"]["semantic_false_positives"]["rate"] <= 0.05
    assert set(report["ablation"]) == {"rag", "semantic_guard", "max_attempts", "routing"}


@pytest.mark.integration
def test_the_trap_metric_separates_the_guard_call_from_what_the_user_sees(tmp_path: Path) -> None:
    """守門判斷正確不等於使用者看得到。

    refuse／clarify 一判就短路回傳，守門的結論**就是**回應本身。disclose 不是 ——
    它只是掛在成功答案上的附註，SQL 產不出來，附註就跟著消失。只報守門判斷的話，
    那 15 題 disclose 就算端到端全滅，指標仍然是 100%，而且驗不出退步。
    """

    database = tmp_path / "power.db"
    build_database(database)
    report = run_evaluation(
        database=database,
        benchmark_dir=PROJECT_ROOT / "benchmarks",
        corpus_path=PROJECT_ROOT / "corpus/training_corpus.json",
    )
    traps = report["safety"]["semantic_traps"]
    end_to_end = traps["end_to_end"]

    # 短路的兩種嚴重度：守門結論就是回應，所以兩個數字必須永遠一致。
    # 哪天不一致，表示 refuse／clarify 也開始走到產生 SQL 那一段了。
    for severity in ("refuse", "clarify"):
        assert end_to_end["by_severity"][severity] == traps["by_severity"][severity], severity

    # 傳達不到的題目必須列名，不能只留一個比率讓人無從追起。
    for item in end_to_end["unreachable"]:
        assert {"question", "code", "severity", "outcome"} <= set(item), item
        assert item["severity"] == "disclose", "只有 disclose 會在產生 SQL 失敗時弄丟結論"

    assert end_to_end["total"] == traps["total"]
    assert "semantic_traps_end_to_end_no_regression" in report["acceptance"]
