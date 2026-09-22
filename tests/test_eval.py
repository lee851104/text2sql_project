import json
import re
from pathlib import Path

import pytest

from eval.metrics import QueryResult, accuracy, grouped_accuracy, result_match
from eval.run_eval import _write_markdown, run_evaluation, write_reports
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

    disclose 只是掛在成功答案上的附註，SQL 產不出來附註就跟著消失。只報守門判斷的話，
    那 15 題 disclose 就算端到端全滅，指標仍然是 100%，而且驗不出退步。

    refuse／clarify 的結論就是回應本身，但出口不只 `check_question` 一個：逐檢視的期間
    由 `check_sql`（看得到表名）或 `explain_unanswerable_date`（router 接不住時）接手
    （CP-062）。所以這兩種嚴重度的兩個數字現在可以不一樣 —— 原本這裡釘的是「必須相等」，
    而那條斷言的註解正好預告了這件事：「哪天不一致，表示 refuse／clarify 也開始走到產生
    SQL 那一段了。」
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

    # 端到端可以比守門判斷高，不能比它低：後面的關卡只能補回第一關沒接到的結論，
    # 不會弄丟它已經接到的。掉下去就表示有題目在產生 SQL 的路上把結論丟了。
    for severity in ("refuse", "clarify"):
        assert (
            end_to_end["by_severity"][severity]["accuracy"]
            >= traps["by_severity"][severity]["accuracy"]
        ), severity

    # 傳達不到的題目必須列名，不能只留一個比率讓人無從追起。這裡不再限定只有 disclose
    # 會出現在名單上 —— refuse／clarify 若三關都漏接也該被列出來，而那個水準由驗收條件
    # semantic_traps_end_to_end_no_regression 把關，不必在這裡重複一次。
    for item in end_to_end["unreachable"]:
        assert {"question", "code", "severity", "outcome"} <= set(item), item

    assert end_to_end["total"] == traps["total"]
    assert "semantic_traps_end_to_end_no_regression" in report["acceptance"]


def _report_with(*, trap: float, end_to_end: float) -> dict:
    """write_reports 需要的最小報告結構。"""

    return {
        "generated_at": "2026-09-20T00:00:00+00:00",
        "mode": "offline_deterministic_rules",
        "status": "pass",
        "intent": {"golden": {"accuracy": 1.0}},
        "execution": {"accuracy": 1.0, "by_corpus_split": {"false": {"accuracy": 1.0}}},
        "safety": {
            "semantic_traps": {"accuracy": trap, "end_to_end": {"accuracy": end_to_end}},
            "semantic_false_positives": {"rate": 0.0},
            "sql_attack_blocking": {"accuracy": 1.0},
        },
    }


def _write(tmp_path: Path, report: dict) -> tuple[Path, Path]:
    history = tmp_path / "eval_history.jsonl"
    figure = tmp_path / "figures" / "eval_summary.svg"
    write_reports(
        report,
        latest_path=tmp_path / "eval_latest.json",
        history_path=history,
        figure_path=figure,
    )
    return history, figure


COMMITTED_SUMMARY = PROJECT_ROOT / "reports/eval_summary.md"
COMMITTED_REPORT = PROJECT_ROOT / "reports/eval_latest.json"
COMMITTED_HISTORY = PROJECT_ROOT / "reports/eval_history.jsonl"


def _committed_report() -> dict:
    return json.loads(COMMITTED_REPORT.read_text(encoding="utf-8"))


def test_the_history_line_records_both_trap_numbers(tmp_path: Path) -> None:
    """歷史紀錄是用來抓退步的東西。

    只記守門判斷的話，disclose 端到端壞掉的那一次，在歷史上會長得跟正常的完全一樣 ——
    當下的驗收會紅，但事後翻紀錄找不出是哪一次開始壞的。
    """

    history, _figure = _write(tmp_path, _report_with(trap=1.0, end_to_end=0.8))
    line = json.loads(history.read_text(encoding="utf-8").strip())

    assert line["semantic_trap_accuracy"] == 1.0
    assert line["semantic_trap_end_to_end"] == 0.8


def test_the_summary_chart_separates_the_guard_from_what_the_user_sees(tmp_path: Path) -> None:
    _history, figure = _write(tmp_path, _report_with(trap=1.0, end_to_end=0.8))
    svg = figure.read_text(encoding="utf-8")

    assert "語意守門" in svg
    assert "語意端到端" in svg
    assert "80.0%" in svg, "端到端的數字要真的畫出來，不能只有標籤"


def test_the_summary_chart_is_tall_enough_for_every_bar(tmp_path: Path) -> None:
    """加一條就會超出畫布，而超出的部分不會報錯，只會被裁掉。"""

    _history, figure = _write(tmp_path, _report_with(trap=1.0, end_to_end=1.0))
    svg = figure.read_text(encoding="utf-8")

    height = int(re.search(r'height="(\d+)"', svg).group(1))
    bottoms = [int(y) + 22 for y in re.findall(r'<rect x="\d+" y="(\d+)"', svg)]
    assert bottoms, "沒有畫出任何長條"
    assert max(bottoms) <= height, f"最後一條畫到 {max(bottoms)}，畫布只有 {height}"


def test_the_summary_table_reads_every_number_from_the_report(tmp_path: Path) -> None:
    """表格裡不能有手寫的數字。

    改掉報告裡的值，表格就該跟著變。不會變的那幾個就是寫死的，而寫死的數字遲早會跟
    JSON 對不起來 —— 到那時它看起來仍然完全正常。
    """

    report = _committed_report()
    report["safety"]["semantic_traps"]["end_to_end"]["accuracy"] = 0.8
    report["execution"]["rule_coverage"] = 0.42
    summary = tmp_path / "eval_summary.md"

    _write_markdown(report, summary, history_path=COMMITTED_HISTORY)
    text = summary.read_text(encoding="utf-8")

    assert "80.0%" in text
    assert "42.0%" in text, "規則覆蓋要跟著報告走 —— 它決定了這些數字算不算 LLM 的成績"


def test_the_summary_table_says_the_online_column_is_empty(tmp_path: Path) -> None:
    """線上模式沒有量過，表格就得明講。

    只寫離線那一欄的數字而不提線上，審查者會把它當成整個系統的準確率。缺口要列在表上，
    不是躲在最後一段文字裡。
    """

    summary = tmp_path / "eval_summary.md"
    _write_markdown(_committed_report(), summary, history_path=COMMITTED_HISTORY)
    text = summary.read_text(encoding="utf-8")

    assert "未量測" in text
    assert "not_run_without_online_llm" in text, "未量測要附上出處，不能只是一句話"


def test_write_reports_emits_the_summary_beside_the_json(tmp_path: Path) -> None:
    report = _committed_report()
    summary = tmp_path / "eval_summary.md"
    write_reports(
        report,
        latest_path=tmp_path / "eval_latest.json",
        history_path=tmp_path / "eval_history.jsonl",
        figure_path=tmp_path / "figures" / "eval_summary.svg",
        summary_path=summary,
    )
    text = summary.read_text(encoding="utf-8")

    assert "# 離線評測摘要" in text
    # 摘要寫在 history 之後，所以剛剛追加的那一筆必須出現在「最近幾次」裡。
    assert report["generated_at"][:19].replace("T", " ") in text


def test_the_committed_summary_matches_the_committed_report(tmp_path: Path) -> None:
    """版控裡的表必須是那份報告產出來的。

    這張表是審查看到的第一頁。手改它、或重跑評測後只 commit JSON 沒 commit 表，
    審查讀到的就是一個不再為真的數字 —— 而且它看起來完全正常。
    """

    assert COMMITTED_SUMMARY.exists(), "reports/eval_summary.md 不存在；跑 `make eval` 產生它。"
    regenerated = tmp_path / "eval_summary.md"
    _write_markdown(_committed_report(), regenerated, history_path=COMMITTED_HISTORY)

    # 換行風格交給 git（core.autocrlf），這裡只比內容。
    assert (
        COMMITTED_SUMMARY.read_text(encoding="utf-8").splitlines()
        == regenerated.read_text(encoding="utf-8").splitlines()
    ), "reports/eval_summary.md 與 eval_latest.json 不同步；跑 `make eval` 重新產生。"
