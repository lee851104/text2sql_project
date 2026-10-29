"""Evaluate the LLM path on questions the offline rules cannot answer.

離線評測（`run_eval`）只量得到規則路徑；規則接不住的那一小部分交給 LLM，準確率從來沒有量過。
這裡用 `benchmarks/llm_questions.json`：每一題都**先確認規則接不住**（接得住的題目量不到 LLM），
再讓指定的 provider 回答，執行它的 SQL，與人工核對過的 gold SQL 比結果。

比對不看欄位名稱與順序，只看值：gold 的每一列都要在預測結果裡找到一列，對每個 `match` 群組
至少包含其中一個 gold 值（同一個量用瓩或萬瓩都算對），而且列數要相同。數字容許四捨五入的誤差。

    uv run --extra local python -m eval.llm_eval --timeout 900
    uv run --extra local python -m eval.llm_eval --no-rag --label no_rag   # 關掉檢索的對照
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingest.validate import PROJECT_ROOT
from serving.runtime import PROVIDER_ENV, build_runtime

BENCHMARK = PROJECT_ROOT / "benchmarks" / "llm_questions.json"


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def cell_equal(predicted: object, gold: object) -> bool:
    expected, actual = _number(gold), _number(predicted)
    if expected is not None and actual is not None:
        # 模型常把平均值四捨五入到一兩位小數；容許 0.05 或千分之一，取大的。
        return math.isclose(actual, expected, rel_tol=1e-3, abs_tol=0.05)
    return str(predicted).strip() == str(gold).strip()


def row_matches(predicted: Sequence[object], gold: dict[str, object], groups) -> bool:
    return all(
        any(cell_equal(cell, gold[column]) for column in group for cell in predicted)
        for group in groups
    )


def result_matches(
    predicted_rows: Sequence[Sequence[object]],
    gold_columns: Sequence[str],
    gold_rows: Sequence[Sequence[object]],
    groups: Sequence[Sequence[str]],
) -> bool:
    if len(predicted_rows) != len(gold_rows):
        return False
    remaining = list(predicted_rows)
    for gold_row in gold_rows:
        gold = dict(zip(gold_columns, gold_row, strict=True))
        match = next((row for row in remaining if row_matches(row, gold, groups)), None)
        if match is None:
            return False
        remaining.remove(match)
    return True


def _rule_misses(database: Path, items: Sequence[dict[str, Any]]) -> dict[str, bool]:
    """Which questions never reach the LLM: the router answers them, or a guard stops them first.

    只看「路由有沒有接住」不夠：語意守門在路由之前就拒答的題目（例如沒指定電力來源的成本題）
    也到不了 LLM，放進題庫只會讓每個 provider 都平白少一題。
    """

    runtime = build_runtime(database=database, mode="offline")
    hits: dict[str, bool] = {}
    for item in items:
        response = runtime.pipeline.query(item["question"])
        trace = (response.data or {}).get("trace", [])
        route = next((step for step in trace if step.get("stage") == "route"), None)
        hits[item["id"]] = route is None or bool(route.get("matched"))
    return hits


def evaluate(
    provider: str,
    *,
    database: Path,
    model: str | None = None,
    timeout_seconds: float | None = None,
    extra_body: dict[str, Any] | None = None,
    label: str | None = None,
    rag: bool = True,
) -> dict[str, Any]:
    items = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    rule_hits = _rule_misses(database, items)
    os.environ[PROVIDER_ENV] = provider
    runtime = build_runtime(
        database=database,
        mode="local",
        model=model,
        request_timeout_seconds=timeout_seconds,
        extra_body=extra_body,
        # 0 筆範例 = prompt 只剩 schema、欄位值與領域規則，用來單獨量檢索的效果。
        retrieval_top_k=None if rag else 0,
    )
    results: list[dict[str, Any]] = []
    for item in items:
        gold_columns, gold_rows = runtime.executor.execute(item["gold_sql"], ())
        started = time.monotonic()
        response = runtime.pipeline.query(item["question"])
        elapsed = round(time.monotonic() - started, 1)
        data = response.data or {}
        attempts = sum(1 for step in data.get("trace", []) if step.get("stage") == "generate")
        if response.success:
            correct = result_matches(data["rows"], gold_columns, gold_rows, item["match"])
            outcome = "correct" if correct else "wrong_result"
        else:
            outcome = "no_answer"
        results.append(
            {
                "id": item["id"],
                "question": item["question"],
                "outcome": outcome,
                "rule_hit": rule_hits[item["id"]],
                "source": data.get("source"),
                "error_code": response.error_code,
                "llm_error": (response.evidence or {}).get("llm_error"),
                "attempts": attempts,
                "seconds": elapsed,
                "sql": data.get("sql"),
                "params": data.get("params"),
                "rows": len(data.get("rows") or []),
                "gold_rows": len(gold_rows),
            }
        )
        print(
            f"[{len(results):2d}/{len(items)}] {outcome:12s} {elapsed:6.1f}s  {item['question']}",
            flush=True,
        )
    measured = [result for result in results if not result["rule_hit"]]
    counts = Counter(result["outcome"] for result in measured)
    seconds = sorted(result["seconds"] for result in measured)
    return {
        "provider": provider,
        "model": runtime.model,
        "timeout_seconds": timeout_seconds,
        "label": label,
        "extra_body": getattr(runtime.pipeline.llm, "extra_body", None),
        "rag": rag,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "questions": len(items),
        "measured": len(measured),
        "rule_hits_excluded": len(results) - len(measured),
        "correct": counts["correct"],
        "wrong_result": counts["wrong_result"],
        "no_answer": counts["no_answer"],
        "accuracy": round(counts["correct"] / len(measured), 4) if measured else None,
        "median_seconds": seconds[len(seconds) // 2] if seconds else None,
        "max_seconds": seconds[-1] if seconds else None,
        "results": results,
    }


def write_reports(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"llm_eval_{report['provider']}" + (f"_{report['label']}" if report.get("label") else "")
    json_path = directory / f"{name}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# LLM 路徑評測：{report['provider']}（{report['model']}）"
        + (f"・{report['label']}" if report.get("label") else ""),
        "",
        f"> provider 額外參數：`{json.dumps(report.get('extra_body') or {}, ensure_ascii=False)}`；"
        f"RAG 範例：{'開' if report.get('rag', True) else '關'}",
        "",
        f"> 產生時間 {report['generated_at']}。題庫 `benchmarks/llm_questions.json`："
        "只收離線規則接不住、但資料答得出來的問題；以 gold SQL 的執行結果比對（不看欄名與順序）。",
        "",
        "| 指標 | 值 |",
        "|---|---:|",
        f"| 計分題數（排除規則接得住的 {report['rule_hits_excluded']} 題） "
        f"| {report['measured']} |",
        f"| 答對 | {report['correct']} |",
        f"| 答錯（有回答但結果不符） | {report['wrong_result']} |",
        f"| 沒有回答（守門擋下、反問或 LLM 失敗） | {report['no_answer']} |",
        f"| 準確率 | {report['accuracy']:.1%} |"
        if report["accuracy"] is not None
        else "| 準確率 | - |",
        f"| 每題秒數（中位數／最大） | {report['median_seconds']}／{report['max_seconds']} |",
        "",
        "| 題目 | 結果 | 秒數 | 嘗試 | 錯誤碼 |",
        "|---|---|---:|---:|---|",
    ]
    for result in report["results"]:
        label = "（規則接住，不計分）" if result["rule_hit"] else result["outcome"]
        lines.append(
            f"| {result['question']} | {label} | {result['seconds']} | {result['attempts']} | "
            f"{result['error_code'] or ''} |"
        )
    markdown_path = directory / f"{name}.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="評測規則接不住的問題上，LLM 路徑的準確率")
    parser.add_argument(
        "--provider",
        default="ollama",
        help="configs/llm.yaml 的 provider；預設 ollama（本機、不需要 key、不連外網）",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--extra-body",
        default=None,
        help='覆寫 provider 的 extra_body（JSON），例如 "{}" 把 ollama 的推理打開',
    )
    parser.add_argument("--label", default=None, help="報告檔名後綴，例如 thinking")
    parser.add_argument(
        "--no-rag",
        action="store_true",
        help="不給模型任何檢索範例，對照 RAG 本身對準確率的影響",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="每次呼叫 LLM 的逾時秒數；預設用 llm.yaml。本機模型慢，量準確率時要放寬，速度另外看",
    )
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data/processed/power.db")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports")
    args = parser.parse_args(argv)
    report = evaluate(
        args.provider,
        database=args.database,
        model=args.model,
        timeout_seconds=args.timeout,
        extra_body=json.loads(args.extra_body) if args.extra_body is not None else None,
        label=args.label,
        rag=not args.no_rag,
    )
    json_path, markdown_path = write_reports(report, args.out)
    print(
        f"{report['provider']}：{report['correct']}/{report['measured']} 答對；"
        f"報告 {json_path.name}、{markdown_path.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
