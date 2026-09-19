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
from text2sql.entities import extract_entities
from text2sql.retriever import TfidfRetriever
from text2sql.router import classify_intent, route
from text2sql.semantic_guard import SemanticGuard
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


def _safety_metrics(
    benchmark_dir: Path,
    semantic_guard: SemanticGuard,
    *,
    reference_date: date,
) -> dict[str, Any]:
    sql_guard = SqlGuard()
    attacks = _load(benchmark_dir / "attack_questions.json")
    attack_metrics = accuracy(not sql_guard.validate(item["input"]).allowed for item in attacks)

    traps = _load(benchmark_dir / "trap_questions.json")
    trap_outcomes = []
    severity_outcomes: dict[str, list[bool]] = {}
    for item in traps:
        entities = extract_entities(item["question"], reference_date=reference_date)
        decision = semantic_guard.check_question(item["question"], entities)
        passed = (decision.code, decision.severity) == (
            item["expect"]["code"],
            item["expect"]["severity"],
        )
        trap_outcomes.append(passed)
        severity_outcomes.setdefault(item["expect"]["severity"], []).append(passed)
    trap_metrics = accuracy(trap_outcomes)
    trap_metrics["by_severity"] = {
        severity: accuracy(values) for severity, values in sorted(severity_outcomes.items())
    }

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
        reference_date=reference_date,
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
        "semantic_false_positive_at_most_5pct": safety["semantic_false_positives"]["rate"] <= 0.05,
    }
    report["status"] = "pass" if all(report["acceptance"].values()) else "fail"
    return report


def _write_svg(report: dict[str, Any], path: Path) -> None:
    values = (
        ("意圖", report["intent"]["golden"]["accuracy"]),
        ("執行", report["execution"]["accuracy"]),
        ("語意", report["safety"]["semantic_traps"]["accuracy"]),
        ("SQL攻擊", report["safety"]["sql_attack_blocking"]["accuracy"]),
    )
    bars = []
    for index, (label, value) in enumerate(values):
        y = 38 + index * 45
        bars.append(
            f'<text x="10" y="{y + 15}" font-size="13">{label}</text>'
            f'<rect x="85" y="{y}" width="{value * 300:.1f}" height="22" fill="#1a5fb4"/>'
            f'<text x="395" y="{y + 15}" font-size="13">{value:.1%}</text>'
        )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="470" height="230" role="img" '
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
        f"語意陷阱 {report['safety']['semantic_traps']['accuracy']:.1%}。"
    )
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
