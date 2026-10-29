"""把一份題目清單跑過一遍，逐題記下走哪條路、答了什麼、揭露了什麼。

執行方式：

    uv run python scripts/eval_online.py                  # 線上，需要 API key
    uv run python scripts/eval_online.py --mode offline   # 離線對照，不花錢
    uv run python scripts/eval_online.py --baseline reports/online_checklist-<時間>.json

`eval.run_eval` 是純離線的 —— 它的 routing ablation 永遠回報
`not_run_without_online_llm`，那不是「沒 key 就跳過」，是程式裡根本沒有那條路徑。
所以補完語料想知道線上模型答得對不對，目前只能一題一題手點。這支補的就是那一段。

它不碰執行中的服務，直接在行程內建 runtime 查唯讀的 power.db。好處是改完語料重跑
不必重啟服務；代價是它量到的是語料與 prompt 的效果，不是服務當下的狀態 —— 服務端
的行為仍要以服務為準。

報告會記下語料與資料庫的指紋。沒有這兩個，兩次結果對不起來就分不出是語料變了、
還是資料變了。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = PROJECT_ROOT / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from serving.runtime import build_runtime  # noqa: E402

DEFAULT_CHECKLIST = PROJECT_ROOT / "scripts" / "online_checklist.json"
CORPUS_PATH = PROJECT_ROOT / "corpus" / "training_corpus.json"
DATABASE_PATH = PROJECT_ROOT / "data" / "processed" / "power.db"
REPORT_DIR = PROJECT_ROOT / "reports"
PREVIEW_ROWS = 5
WATCHED_FIELDS = ("verdict", "source", "intent", "record_count", "error_code")
RULE = "─" * 78
MARK = {"pass": "✓", "fail": "✗", "manual": "?"}


def _fingerprint(path: Path) -> str:
    """檔案內容的短雜湊。檔案不在就說不在，不要回一個看起來像雜湊的東西。"""

    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _rows_text(rows: list[list[Any]]) -> str:
    return "\n".join(" | ".join(str(value) for value in row) for row in rows)


def _judge(
    expect: dict[str, Any] | None,
    record: dict[str, Any],
    rows: list[list[Any]],
) -> tuple[str, list[str]]:
    """比對清單給的期望。沒給期望就判 manual —— 不要用沉默假裝通過。

    contains/not_contains 只看回傳的資料列，不看 SQL：SQL 裡出現「高雄市」不代表
    答案是高雄市，把兩者混在一起比會製造假的通過。要驗 SQL 寫法請用 sql_contains。
    """

    if not expect:
        return "manual", ["清單未給 expect，需人工判定"]

    failures: list[str] = []
    haystack = _rows_text(rows)
    for needle in expect.get("contains", ()):
        if needle not in haystack:
            failures.append(f"資料列少了「{needle}」")
    for needle in expect.get("not_contains", ()):
        if needle in haystack:
            failures.append(f"資料列不該出現「{needle}」")

    sql = (record.get("sql") or "").casefold()
    for needle in expect.get("sql_contains", ()):
        if needle.casefold() not in sql:
            failures.append(f"SQL 少了「{needle}」")

    for field, label in (("source", "來源"), ("intent", "意圖"), ("error_code", "錯誤代碼")):
        wanted = expect.get(field)
        if wanted and record.get(field) != wanted:
            failures.append(f"{label}是 {record.get(field)}，預期 {wanted}")

    seen = set(record.get("disclosures") or ())
    failures.extend(
        f"少了揭露 {code}" for code in expect.get("disclosure_codes", ()) if code not in seen
    )
    return ("fail" if failures else "pass"), failures


def _run_one(pipeline: Any, item: dict[str, Any]) -> tuple[dict[str, Any], list[list[Any]]]:
    question = item["question"]
    started = perf_counter()
    try:
        response = pipeline.query(question).to_dict()
    except Exception as error:
        # 一題炸掉不該讓整輪停下 —— 剩下的題目仍然量得到，而且例外本身就是結果。
        return {
            "id": item["id"],
            "question": question,
            "ok": False,
            "crashed": f"{type(error).__name__}: {error}",
            "elapsed_seconds": round(perf_counter() - started, 2),
        }, []

    elapsed = perf_counter() - started
    data = response.get("data") or {}
    rows = [list(row) for row in data.get("rows", ())]
    record = {
        "id": item["id"],
        "question": question,
        "ok": bool(response.get("success")),
        "source": data.get("source"),
        "intent": data.get("intent"),
        "sql": data.get("sql"),
        "params": data.get("params"),
        "record_count": data.get("record_count"),
        "rows_preview": rows[:PREVIEW_ROWS],
        "rows_omitted": max(0, len(rows) - PREVIEW_ROWS),
        "disclosures": [entry.get("code") for entry in data.get("disclosures", ())],
        "error_code": response.get("error_code"),
        "error": response.get("error"),
        "severity": response.get("severity"),
        "elapsed_seconds": round(elapsed, 2),
    }
    return record, rows


def _diff(baseline: Path, records: list[dict[str, Any]]) -> list[str]:
    document = json.loads(baseline.read_text(encoding="utf-8"))
    previous = {entry["id"]: entry for entry in document.get("results", ())}
    lines: list[str] = []
    for record in records:
        before = previous.get(record["id"])
        if before is None:
            lines.append(f"  + {record['id']}：基線沒有這題")
            continue
        lines.extend(
            f"  ~ {record['id']} {field}：{before.get(field)} → {record.get(field)}"
            for field in WATCHED_FIELDS
            if before.get(field) != record.get(field)
        )
        was = sorted(before.get("disclosures") or ())
        now = sorted(record.get("disclosures") or ())
        if was != now:
            lines.append(f"  ~ {record['id']} disclosures：{was} → {now}")
    gone = set(previous) - {record["id"] for record in records}
    lines.extend(f"  - {identifier}：這次沒跑" for identifier in sorted(gone))
    return lines


def _print_record(record: dict[str, Any], failures: list[str]) -> None:
    mark = MARK[record["verdict"]]
    head = f"{mark} {record['id']:<22} {record['question']}"
    print(head)
    if crashed := record.get("crashed"):
        print(f"    例外：{crashed}")
        return
    route = record.get("source") or "—"
    intent = record.get("intent") or "—"
    detail = f"    來源={route} 意圖={intent} 筆數={record.get('record_count')}"
    print(f"{detail} 耗時={record['elapsed_seconds']}s")
    if record.get("error_code"):
        print(f"    擋下：{record['error_code']}（{record.get('severity')}）{record.get('error')}")
    if record.get("disclosures"):
        print(f"    揭露：{'、'.join(record['disclosures'])}")
    for row in record.get("rows_preview", ()):
        print(f"      {' | '.join(str(value) for value in row)}")
    if record.get("rows_omitted"):
        print(f"      …另有 {record['rows_omitted']} 列")
    for reason in failures:
        print(f"    ✗ {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="跑一份題目清單並留下可前後比對的紀錄。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--checklist", type=Path, default=DEFAULT_CHECKLIST)
    parser.add_argument("--mode", choices=("online", "offline", "auto"), default="online")
    parser.add_argument("--only", default=None, help="只跑 id 含有這段字的題目")
    parser.add_argument("--out", type=Path, default=None, help="報告輸出路徑")
    parser.add_argument("--baseline", type=Path, default=None, help="拿一份舊報告來對照")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    document = json.loads(args.checklist.read_text(encoding="utf-8"))
    items = [item for item in document["questions"] if args.only is None or args.only in item["id"]]
    if not items:
        parser.error(f"清單裡沒有符合 --only {args.only} 的題目。")

    try:
        runtime = build_runtime(mode=args.mode)
    except RuntimeError as error:
        # 這段訊息會指名要哪個環境變數，照樣印出來比包成通用錯誤有用。
        print(f"建立 runtime 失敗：{error}", file=sys.stderr)
        return 2

    print(RULE)
    print(f"模式={runtime.mode}  provider={runtime.provider}  model={runtime.model}")
    print(f"語料={_fingerprint(CORPUS_PATH)}  資料庫={_fingerprint(DATABASE_PATH)}")
    print(f"資料範圍={runtime.data_range[0]} ~ {runtime.data_range[1]}  題數={len(items)}")
    print(RULE)

    records: list[dict[str, Any]] = []
    for item in items:
        record, rows = _run_one(runtime.pipeline, item)
        verdict, failures = _judge(item.get("expect"), record, rows)
        record["verdict"] = verdict
        record["verdict_failures"] = failures
        record["why"] = item.get("why")
        records.append(record)
        _print_record(record, failures)

    tally = {name: sum(1 for r in records if r["verdict"] == name) for name in MARK}
    routes = {
        name: sum(1 for r in records if r.get("source") == name) for name in ("router", "llm")
    }
    print(RULE)
    print(f"通過 {tally['pass']}　失敗 {tally['fail']}　待人工判定 {tally['manual']}")
    print(f"規則路由 {routes['router']} 題　線上模型 {routes['llm']} 題")

    if args.baseline:
        changes = _diff(args.baseline, records)
        print(RULE)
        print(f"與 {args.baseline.name} 對照：")
        print("\n".join(changes) if changes else "  沒有差異")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or REPORT_DIR / f"online_checklist-{runtime.mode}-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "online-checklist-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": runtime.mode,
        "provider": runtime.provider,
        "model": runtime.model,
        "corpus_fingerprint": _fingerprint(CORPUS_PATH),
        "database_fingerprint": _fingerprint(DATABASE_PATH),
        "data_range": list(runtime.data_range),
        "summary": {"verdicts": tally, "sources": routes, "total": len(records)},
        "results": records,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(RULE)
    # 專案外的 --out 路徑 relative_to 會丟例外。報告已經寫出去了，不該倒在列印這一步。
    resolved = out.resolve()
    shown = (
        resolved.relative_to(PROJECT_ROOT) if resolved.is_relative_to(PROJECT_ROOT) else resolved
    )
    print(f"報告：{shown}")
    return 1 if tally["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
