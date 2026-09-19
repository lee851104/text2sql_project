"""What the database covers, and what it cannot answer.

涵蓋範圍的「多少」全部從資料庫查出來：期間、筆數、電廠、燃料別、已知陷阱。寫死的
數字會在資料換版後變成謊話，而這個系統的主張正是「主動揭露限制」—— 一份過期的限制
說明比沒有更糟，因為它讓使用者相信一件不再為真的事。

只有兩種東西算不出來，放在 `configs/coverage.yaml`：每個檢視「回答什麼問題」，以及
「目前答不出什麼」。後者每一條都附可驗證的條件（見 `verify_limitations`），所以資料
長出新欄位時，是測試先紅，而不是使用者先被誤導。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class CoverageConfigurationError(ValueError):
    """The coverage document is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Limitation:
    """One question the data cannot answer, plus the condition that keeps it true."""

    topic: str
    detail: str
    absent: Mapping[str, tuple[str, ...]]


def load_coverage_document(path: Path) -> tuple[dict[str, dict[str, str]], tuple[Limitation, ...]]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CoverageConfigurationError(f"無法讀取 {path}：{error}") from error
    except yaml.YAMLError as error:
        raise CoverageConfigurationError(f"{path} 不是合法的 YAML：{error}") from error
    if not isinstance(payload, dict):
        raise CoverageConfigurationError(f"{path} 必須是 mapping。")

    raw_views = payload.get("views")
    if not isinstance(raw_views, dict) or not raw_views:
        raise CoverageConfigurationError("coverage.yaml 必須有非空的 views。")
    views: dict[str, dict[str, str]] = {}
    for name, body in raw_views.items():
        if not isinstance(body, dict) or not body.get("title") or not body.get("answers"):
            raise CoverageConfigurationError(f"檢視「{name}」缺少 title 或 answers。")
        views[str(name)] = {"title": str(body["title"]), "answers": str(body["answers"])}

    raw_limits = payload.get("limitations")
    if not isinstance(raw_limits, list) or not raw_limits:
        raise CoverageConfigurationError("coverage.yaml 必須有非空的 limitations。")
    limitations: list[Limitation] = []
    for index, item in enumerate(raw_limits, start=1):
        if not isinstance(item, dict) or not item.get("topic") or not item.get("detail"):
            raise CoverageConfigurationError(f"第 {index} 條限制缺少 topic 或 detail。")
        absent = item.get("absent")
        if not isinstance(absent, dict) or not absent:
            raise CoverageConfigurationError(
                f"限制「{item['topic']}」沒有 absent 條件；無法驗證的限制說明會悄悄過期。"
            )
        checks: dict[str, tuple[str, ...]] = {}
        for view, terms in absent.items():
            if not isinstance(terms, list) or not terms:
                raise CoverageConfigurationError(
                    f"限制「{item['topic']}」對 {view} 的 absent 必須是非空清單。"
                )
            checks[str(view)] = tuple(str(term) for term in terms)
        limitations.append(
            Limitation(
                topic=str(item["topic"]),
                detail=" ".join(str(item["detail"]).split()),
                absent=checks,
            )
        )
    return views, tuple(limitations)


def verify_limitations(
    limitations: Sequence[Limitation], allowed_columns: Mapping[str, set[str]]
) -> tuple[str, ...]:
    """Report limitations that no longer hold, instead of trusting the text.

    回傳「已經不成立」的說明。某個關鍵字出現在該檢視的欄位裡，就表示那個問題現在
    答得出來了，說明必須更新。檢視本身不存在也算失敗 —— 條件無法驗證等於沒有條件。
    """

    stale: list[str] = []
    for limitation in limitations:
        for view, terms in limitation.absent.items():
            columns = allowed_columns.get(view)
            if columns is None:
                stale.append(f"「{limitation.topic}」引用了不存在的檢視 {view}")
                continue
            for term in terms:
                hits = sorted(column for column in columns if term in column)
                if hits:
                    stale.append(f"「{limitation.topic}」已不成立：{view} 有欄位 {hits}")
    return tuple(stale)


def _scalar(connection: sqlite3.Connection, sql: str) -> Any:
    row = connection.execute(sql).fetchone()
    return row[0] if row else None


def describe_coverage(
    database: Path,
    *,
    views: Mapping[str, Mapping[str, str]],
    limitations: Sequence[Limitation],
) -> dict[str, Any]:
    """Build the coverage payload, measuring everything measurable from the database."""

    uri = f"{Path(database).resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        available = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
        date_range = {
            "start": _scalar(connection, 'SELECT MIN("日期") FROM v_system'),
            "end": _scalar(connection, 'SELECT MAX("日期") FROM v_system'),
        }
        fuels = [str(row[0]) for row in connection.execute('SELECT DISTINCT "燃料" FROM v_unit')]
        counts = {
            "peak_rows": _scalar(connection, "SELECT COUNT(*) FROM v_peak"),
            "units": _scalar(connection, "SELECT COUNT(*) FROM v_unit"),
            "plants_with_units": _scalar(connection, 'SELECT COUNT(DISTINCT "電廠") FROM v_unit'),
            "outages": _scalar(connection, "SELECT COUNT(*) FROM v_outage"),
        }
        try:
            counts["plants_in_roster"] = _scalar(connection, "SELECT COUNT(*) FROM dim_plant_scope")
        except sqlite3.OperationalError:
            counts["plants_in_roster"] = None

        pitfalls: list[dict[str, Any]] = []
        try:
            rows = connection.execute(
                """SELECT pitfall_code, severity, COUNT(*), GROUP_CONCAT(target_name, '、')
                     FROM meta_pitfall GROUP BY pitfall_code, severity ORDER BY pitfall_code"""
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for code, severity, total, targets in rows:
            pitfalls.append(
                {
                    "code": str(code),
                    "severity": str(severity),
                    "count": int(total),
                    "targets": [item for item in str(targets or "").split("、") if item],
                }
            )

    answerable = [
        {"view": name, "title": body["title"], "answers": body["answers"]}
        for name, body in views.items()
        if name in available
    ]
    return {
        "date_range": date_range,
        "counts": counts,
        "fuels": sorted(fuels),
        "answerable": answerable,
        "limitations": [{"topic": item.topic, "detail": item.detail} for item in limitations],
        "pitfalls": pitfalls,
    }
