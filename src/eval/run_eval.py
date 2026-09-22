"""Run the fully offline benchmark suite and emit reproducible reports."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from eval.cases import SEMANTIC_NEGATIVE_CONTROLS
from eval.metrics import QueryResult, accuracy, grouped_accuracy, result_match
from ingest.validate import PROJECT_ROOT
from text2sql.corpus import load_corpus
from text2sql.db import ReadOnlySQLite
from text2sql.entities import Entities, extract_entities
from text2sql.llm import GeneratedQuery
from text2sql.retriever import TfidfRetriever
from text2sql.router import classify_intent, route
from text2sql.semantic_guard import SemanticDecision, SemanticGuard
from text2sql.sql_guard import SqlGuard


def _load(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} 必須是 JSON array。")
    return payload


def _query_result(
    executor: ReadOnlySQLite, sql: str, params: tuple[object, ...] = ()
) -> QueryResult:
    columns, rows = executor.execute(sql, params)
    return QueryResult(tuple(columns), tuple(rows))


def _catalog(executor: ReadOnlySQLite) -> tuple[set[str], set[str]]:
    _, peak_rows = executor.execute('SELECT DISTINCT "機組欄位" FROM v_peak LIMIT 200', ())
    _, plant_rows = executor.execute('SELECT DISTINCT "電廠" FROM v_unit LIMIT 200', ())
    return {str(row[0]) for row in peak_rows}, {str(row[0]) for row in plant_rows}


def _intent_metrics(benchmark_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, filename in (
        ("golden", "golden_questions.json"),
        ("eval", "eval_questions.json"),
    ):
        items = _load(benchmark_dir / filename)
        outcomes = [classify_intent(item["question"]) == item["intent"] for item in items]
        result[name] = accuracy(outcomes)
    return result


# 端到端基準線。守門判斷正確不代表使用者看得到 —— disclose 的結論只是掛在成功答案上的
# 附註，SQL 產不出來，揭露就跟著消失。
#
# 這個數字一度是 36/45：9 題 disclose 卡在 NO_OFFLINE_CANDIDATE，因為離線 router 沒有
# 規則接「電廠總出力」「容量缺口」這類問法。補上 route() 最後那段 fallback 之後回到
# 45/45，門檻也跟著調到 1.0 —— 基準線是用來卡住已經達到的水準，不是拿來長期容忍缺口。
TRAP_END_TO_END_BASELINE = 1.0


def _execution_metrics(
    benchmark_dir: Path,
    executor: ReadOnlySQLite,
    *,
    data_range: tuple[str, str],
    peak_columns: set[str],
    plants: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    guard = SqlGuard()
    reference_date = date.fromisoformat(data_range[1])
    outcomes: list[dict[str, Any]] = []
    for item in _load(benchmark_dir / "eval_questions.json"):
        entities = extract_entities(item["question"], reference_date=reference_date)
        candidate = route(
            item["question"],
            entities,
            peak_columns=peak_columns,
            plants=plants,
            data_range=data_range,
        )
        outcome = {
            "id": item["id"],
            "intent": item["intent"],
            "in_corpus": item["in_corpus"],
            "routed_intent": candidate.intent,
            "passed": False,
        }
        if not candidate.sql:
            outcome["failure"] = "NO_OFFLINE_CANDIDATE"
            outcomes.append(outcome)
            continue
        guard_result = guard.validate(candidate.sql, candidate.params)
        if not guard_result.allowed:
            outcome["failure"] = guard_result.code
            outcomes.append(outcome)
            continue
        try:
            candidate_result = _query_result(executor, candidate.sql, candidate.params)
            expected_result = _query_result(executor, item["sql"])
            outcome["passed"] = result_match(candidate_result, expected_result)
            if not outcome["passed"]:
                outcome["failure"] = "RESULT_MISMATCH"
        except Exception as error:
            outcome["failure"] = f"EXECUTION_ERROR:{type(error).__name__}"
        outcomes.append(outcome)
    metrics = accuracy(item["passed"] for item in outcomes)
    metrics["by_corpus_split"] = grouped_accuracy(outcomes, group_key="in_corpus")
    metrics["rule_coverage"] = sum(
        "failure" not in item or item["failure"] != "NO_OFFLINE_CANDIDATE" for item in outcomes
    ) / len(outcomes)
    return metrics, outcomes


def _answer_reaches_user(
    question: str,
    entities: Entities,
    *,
    executor: ReadOnlySQLite,
    sql_guard: SqlGuard,
    peak_columns: set[str],
    plants: set[str],
    data_range: tuple[str, str],
) -> tuple[bool, str]:
    """走一次離線路徑，回報答案送不送得出去。

    refuse／clarify 的結論就是回應本身，不必走到這裡。disclose 不同 —— 它是掛在成功
    答案上的附註，所以答案產不出來，揭露也就沒有人看得到。
    """

    candidate = route(
        question,
        entities,
        peak_columns=peak_columns,
        plants=plants,
        data_range=data_range,
    )
    if not candidate.sql:
        return False, "NO_OFFLINE_CANDIDATE"
    guard_result = sql_guard.validate(candidate.sql, candidate.params)
    if not guard_result.allowed:
        return False, guard_result.code
    try:
        _query_result(executor, candidate.sql, candidate.params)
    except Exception as error:  # 執行層的例外照樣代表使用者拿不到答案
        return False, f"EXECUTION_ERROR:{type(error).__name__}"
    return True, "OK"


def _delivered_decision(
    question: str,
    entities: Entities,
    decision: SemanticDecision,
    *,
    semantic_guard: SemanticGuard,
    peak_columns: set[str],
    plants: set[str],
    data_range: tuple[str, str],
) -> SemanticDecision:
    """離線路徑最終回給使用者的那個守門結論。

    `check_question` 不是唯一的出口。它跑在 route 之前，看不到最後會查哪個檢視，所以
    「2024年台中出力」這種「有的檢視有、有的沒有」的期間，得由 `check_sql`（看得到表名）
    或 `explain_unanswerable_date`（router 接不住時）接手。只看第一關，會把後面兩關確實
    擋下的題目誤記成使用者看不到。
    """

    if decision.code != "OK":
        return decision
    candidate = route(
        question, entities, peak_columns=peak_columns, plants=plants, data_range=data_range
    )
    if candidate.sql:
        return semantic_guard.check_sql(
            question, GeneratedQuery(candidate.sql, candidate.params), entities
        )
    return semantic_guard.explain_unanswerable_date(entities) or SemanticDecision()


def _safety_metrics(
    benchmark_dir: Path,
    semantic_guard: SemanticGuard,
    executor: ReadOnlySQLite,
    *,
    reference_date: date,
    data_range: tuple[str, str],
    peak_columns: set[str],
    plants: set[str],
) -> dict[str, Any]:
    sql_guard = SqlGuard()
    attacks = _load(benchmark_dir / "attack_questions.json")
    attack_metrics = accuracy(not sql_guard.validate(item["input"]).allowed for item in attacks)

    traps = _load(benchmark_dir / "trap_questions.json")
    trap_outcomes = []
    severity_outcomes: dict[str, list[bool]] = {}
    reached_outcomes = []
    reached_by_severity: dict[str, list[bool]] = {}
    unreachable: list[dict[str, str]] = []
    for item in traps:
        entities = extract_entities(item["question"], reference_date=reference_date)
        decision = semantic_guard.check_question(item["question"], entities)
        severity = item["expect"]["severity"]
        passed = (decision.code, decision.severity) == (item["expect"]["code"], severity)
        trap_outcomes.append(passed)
        severity_outcomes.setdefault(severity, []).append(passed)

        # 守門判斷對，不代表使用者看得到。refuse／clarify 的結論就是回應本身，但出口不
        # 只 check_question 一個 —— 逐檢視的期間是後面兩關擋的，所以要問到底。
        # disclose 不同：它掛在成功答案上，答案產不出來附註就沒人看得到。
        if severity in {"refuse", "clarify"}:
            delivered_decision = _delivered_decision(
                item["question"],
                entities,
                decision,
                semantic_guard=semantic_guard,
                peak_columns=peak_columns,
                plants=plants,
                data_range=data_range,
            )
            outcome = delivered_decision.code
            reached = (delivered_decision.code, delivered_decision.severity) == (
                item["expect"]["code"],
                severity,
            )
        else:
            delivered, outcome = _answer_reaches_user(
                item["question"],
                entities,
                executor=executor,
                sql_guard=sql_guard,
                peak_columns=peak_columns,
                plants=plants,
                data_range=data_range,
            )
            reached = passed and delivered
        reached_outcomes.append(reached)
        reached_by_severity.setdefault(severity, []).append(reached)
        if not reached:
            unreachable.append(
                {
                    "question": item["question"],
                    "code": item["expect"]["code"],
                    "severity": severity,
                    "outcome": outcome,
                }
            )

    trap_metrics = accuracy(trap_outcomes)
    trap_metrics["by_severity"] = {
        severity: accuracy(values) for severity, values in sorted(severity_outcomes.items())
    }
    trap_metrics["end_to_end"] = accuracy(reached_outcomes)
    trap_metrics["end_to_end"]["by_severity"] = {
        severity: accuracy(values) for severity, values in sorted(reached_by_severity.items())
    }
    trap_metrics["end_to_end"]["unreachable"] = unreachable

    false_positives = []
    for question in SEMANTIC_NEGATIVE_CONTROLS:
        entities = extract_entities(question, reference_date=reference_date)
        decision = semantic_guard.check_question(question, entities)
        false_positives.append(decision.severity != "pass")
    false_positive_count = sum(false_positives)
    false_positive_rate = false_positive_count / len(false_positives)
    return {
        "sql_attack_blocking": attack_metrics,
        "semantic_traps": trap_metrics,
        "semantic_false_positives": {
            "count": false_positive_count,
            "total": len(false_positives),
            "rate": false_positive_rate,
        },
    }


def _rag_ablation(corpus_path: Path, eval_items: list[dict[str, Any]]) -> dict[str, Any]:
    retriever = TfidfRetriever(load_corpus(corpus_path)["examples"])
    enabled = []
    disabled = []
    for item in eval_items:
        retrieved = retriever.retrieve(item["question"], top_k=1)
        predicted = retrieved[0].example["intent"] if retrieved else "other"
        enabled.append(predicted == item["intent"])
        disabled.append(item["intent"] == "other")
    return {
        "metric": "top1_intent_accuracy",
        "enabled": accuracy(enabled),
        "disabled_default_other": accuracy(disabled),
        "note": "這是檢索器本身的離線指標，不假裝是線上 LLM 準確率。",
    }


def _ablation(
    *,
    corpus_path: Path,
    eval_items: list[dict[str, Any]],
    execution: dict[str, Any],
    safety: dict[str, Any],
) -> dict[str, Any]:
    execution_accuracy = execution["accuracy"]
    trap_accuracy = safety["semantic_traps"]["accuracy"]
    return {
        "rag": _rag_ablation(corpus_path, eval_items),
        "semantic_guard": {
            "metric": "trap_handling_accuracy",
            "enabled": trap_accuracy,
            "disabled": 0.0,
        },
        "max_attempts": {
            "metric": "offline_rule_execution_accuracy",
            "1": execution_accuracy,
            "2": execution_accuracy,
            "3": execution_accuracy,
            "note": "規則路由在第一次就產生候選；需線上 LLM 測試才能估計重生收益。",
        },
        "routing": {
            "metric": "offline_execution_accuracy",
            "enabled": execution_accuracy,
            "disabled": None,
            "disabled_status": "not_run_without_online_llm",
            "rule_coverage": execution["rule_coverage"],
        },
    }


def run_evaluation(
    *,
    database: Path,
    benchmark_dir: Path,
    corpus_path: Path,
) -> dict[str, Any]:
    executor = ReadOnlySQLite(database)
    peak_columns, plants = _catalog(executor)
    semantic_guard = SemanticGuard.from_database(database, peak_columns=peak_columns)
    data_range = semantic_guard.data_range
    reference_date = date.fromisoformat(data_range[1])

    execution, outcomes = _execution_metrics(
        benchmark_dir,
        executor,
        data_range=data_range,
        peak_columns=peak_columns,
        plants=plants,
    )
    safety = _safety_metrics(
        benchmark_dir,
        semantic_guard,
        executor,
        reference_date=reference_date,
        data_range=data_range,
        peak_columns=peak_columns,
        plants=plants,
    )
    eval_items = _load(benchmark_dir / "eval_questions.json")
    try:
        database_label = database.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        database_label = database.name
    report = {
        "schema_version": "eval-report-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "offline_deterministic_rules",
        "database": database_label,
        "data_range": {"start": data_range[0], "end": data_range[1]},
        "intent": _intent_metrics(benchmark_dir),
        "execution": execution,
        "safety": safety,
        "ablation": _ablation(
            corpus_path=corpus_path,
            eval_items=eval_items,
            execution=execution,
            safety=safety,
        ),
        "failures": [item for item in outcomes if not item["passed"]],
    }
    report["acceptance"] = {
        "intent_at_least_90pct": report["intent"]["golden"]["accuracy"] >= 0.90,
        "out_of_corpus_execution_at_least_60pct": execution["by_corpus_split"]["false"]["accuracy"]
        >= 0.60,
        "attack_blocking_100pct": safety["sql_attack_blocking"]["accuracy"] == 1.0,
        "semantic_traps_at_least_95pct": safety["semantic_traps"]["accuracy"] >= 0.95,
        "semantic_traps_end_to_end_no_regression": safety["semantic_traps"]["end_to_end"][
            "accuracy"
        ]
        >= TRAP_END_TO_END_BASELINE,
        "semantic_false_positive_at_most_5pct": safety["semantic_false_positives"]["rate"] <= 0.05,
    }
    report["status"] = "pass" if all(report["acceptance"].values()) else "fail"
    return report


def _write_svg(report: dict[str, Any], path: Path) -> None:
    traps = report["safety"]["semantic_traps"]
    # 守門判斷與端到端分成兩條。只畫前者的話，disclose 端到端壞掉時這張圖還是滿格 ——
    # 那正是這兩個數字要拆開的理由，圖表沒有理由自成例外。
    values = (
        ("意圖", report["intent"]["golden"]["accuracy"]),
        ("執行", report["execution"]["accuracy"]),
        ("語意守門", traps["accuracy"]),
        ("語意端到端", traps["end_to_end"]["accuracy"]),
        ("SQL攻擊", report["safety"]["sql_attack_blocking"]["accuracy"]),
    )
    bar_x = 110
    bar_width = 300
    height = 38 + len(values) * 45 + 10
    bars = []
    for index, (label, value) in enumerate(values):
        y = 38 + index * 45
        bars.append(
            f'<text x="10" y="{y + 15}" font-size="13">{label}</text>'
            f'<rect x="{bar_x}" y="{y}" width="{value * bar_width:.1f}" height="22" '
            'fill="#1a5fb4"/>'
            f'<text x="{bar_x + bar_width + 10}" y="{y + 15}" font-size="13">{value:.1%}</text>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" role="img" '
        'aria-label="PowerQuery TW offline evaluation">'
        '<rect width="100%" height="100%" fill="white"/>'
        '<text x="10" y="22" font-size="16" font-weight="bold">Offline evaluation</text>'
        + "".join(bars)
        + "</svg>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def write_reports(
    report: dict[str, Any], *, latest_path: Path, history_path: Path, figure_path: Path
) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    history = {
        "generated_at": report["generated_at"],
        "mode": report["mode"],
        "status": report["status"],
        "intent_accuracy": report["intent"]["golden"]["accuracy"],
        "execution_accuracy": report["execution"]["accuracy"],
        "out_of_corpus_accuracy": report["execution"]["by_corpus_split"]["false"]["accuracy"],
        "semantic_trap_accuracy": report["safety"]["semantic_traps"]["accuracy"],
        # 守門判斷對，不代表使用者看得到。少了這一欄，disclose 端到端壞掉的那一次在
        # 歷史上會長得跟正常的一模一樣，事後就找不出是哪一次開始壞的。
        "semantic_trap_end_to_end": report["safety"]["semantic_traps"]["end_to_end"]["accuracy"],
        "semantic_false_positive_rate": report["safety"]["semantic_false_positives"]["rate"],
    }
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history, ensure_ascii=False, separators=(",", ":")) + "\n")
    _write_svg(report, figure_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data/processed/power.db")
    parser.add_argument("--benchmarks", type=Path, default=PROJECT_ROOT / "benchmarks")
    parser.add_argument("--corpus", type=Path, default=PROJECT_ROOT / "corpus/training_corpus.json")
    parser.add_argument("--latest", type=Path, default=PROJECT_ROOT / "reports/eval_latest.json")
    parser.add_argument("--history", type=Path, default=PROJECT_ROOT / "reports/eval_history.jsonl")
    parser.add_argument(
        "--figure", type=Path, default=PROJECT_ROOT / "reports/figures/eval_summary.svg"
    )
    args = parser.parse_args(argv)
    report = run_evaluation(
        database=args.database,
        benchmark_dir=args.benchmarks,
        corpus_path=args.corpus,
    )
    write_reports(
        report,
        latest_path=args.latest,
        history_path=args.history,
        figure_path=args.figure,
    )
    print(
        f"離線評測 {report['status']}：意圖 {report['intent']['golden']['accuracy']:.1%}，"
        f"執行 {report['execution']['accuracy']:.1%}，"
        f"語意陷阱 {report['safety']['semantic_traps']['accuracy']:.1%}"
        f"（端到端 {report['safety']['semantic_traps']['end_to_end']['accuracy']:.1%}）。"
    )
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
