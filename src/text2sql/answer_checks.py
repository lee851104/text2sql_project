"""Turn silent LLM mistakes into retries (CP-089).

`SqlGuard` 管的是「安不安全」；這裡管的是「看起來正常、其實答錯」。評測（`eval.llm_eval`）裡本機模型
五題靜默答錯，四題是同一類可以機械判斷的錯：

- 值不在封閉集合：`縣市 = '新北市'`，資料裡是「新北市林口區」，回 0 筆而且不報錯；
- `LIKE` 沒有萬用字元：`LIKE '高雄市'` 等於 `=`，同樣 0 筆；
- 數字被當成字串：`裝置容量_萬瓩 > '80'`，SQLite 拿數值和文字比，永遠為假；
- 結果剛好等於 LIMIT：分組結果被截斷，看起來像完整的表。

只檢查 LLM 寫的 SQL：規則路由的 SQL 是人寫、測過的。發現問題就把原因交回給模型重寫，
與 `SqlGuard` 擋下時走同一條重試路徑；重試用完仍然 0 筆時照常回答（真的可能沒有資料）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from text2sql.scope_guard import ScopeRewriteError, _name_original_placeholders

_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In)
_EQUALITIES = (exp.EQ, exp.NEQ, exp.In)
MAX_LISTED_VALUES = 25


@dataclass(frozen=True)
class CheckIssue:
    code: str
    reason: str

    def as_prior_error(self) -> str:
        return f"{self.code}: {self.reason}"


@dataclass(frozen=True)
class _Binding:
    node: exp.Expression  # 包住這個 placeholder 的比較
    columns: tuple[str, ...]  # 這個比較裡出現的欄位名
    index: int  # 對應 params 的位置


def _bindings(tree: exp.Expression, params: Sequence[object]) -> list[_Binding]:
    """Each placeholder, the comparison it sits in, and its position in `params`."""

    names = list(_name_original_placeholders(tree, params))
    found: list[_Binding] = []
    for placeholder in tree.find_all(exp.Placeholder):
        comparison = placeholder.find_ancestor(*_COMPARISONS, exp.Like)
        if comparison is None:
            continue
        columns = tuple(column.name for column in comparison.find_all(exp.Column))
        found.append(_Binding(comparison, columns, names.index(placeholder.this)))
    return found


def _closed_set(
    column: str, tables: Sequence[str], column_values: Mapping[str, Sequence[str]]
) -> Sequence[str] | None:
    for table in tables:
        values = column_values.get(f"{table}.{column}")
        if values is not None:
            return values
    return None


def coerce_numeric_params(sql: str, params: Sequence[object]) -> tuple[object, ...]:
    """`> '80'` 改成 `> 80`：只動純數字字串、而且用在比較（不含 LIKE）裡的參數。"""

    try:
        tree = parse_one(sql, read="sqlite")
        bindings = _bindings(tree, params)
    except (ParseError, ScopeRewriteError):
        return tuple(params)
    coerced = list(params)
    for binding in bindings:
        value = coerced[binding.index]
        if isinstance(binding.node, exp.Like) or not isinstance(value, str):
            continue
        text = value.strip()
        if _NUMBER.match(text):
            coerced[binding.index] = float(text) if "." in text else int(text)
    return tuple(coerced)


def check_sql(
    sql: str, params: Sequence[object], column_values: Mapping[str, Sequence[str]]
) -> CheckIssue | None:
    """Problems visible before running the SQL."""

    try:
        tree = parse_one(sql, read="sqlite")
        bindings = _bindings(tree, params)
    except (ParseError, ScopeRewriteError):
        return None  # 解析不了的 SQL 早被 SqlGuard 擋下
    tables = [table.name for table in tree.find_all(exp.Table)]
    for binding in bindings:
        value = params[binding.index]
        if not isinstance(value, str):
            continue
        if isinstance(binding.node, exp.Like):
            if "%" not in value and "_" not in value:
                return CheckIssue(
                    "SQL_LIKE_WITHOUT_WILDCARD",
                    f"LIKE 的參數 {value!r} 沒有 % 或 _，等於完全相符；"
                    f"部分比對請寫成 '%{value}%'。",
                )
            continue
        if not isinstance(binding.node, _EQUALITIES):
            continue
        for column in binding.columns:
            allowed = _closed_set(column, tables, column_values)
            if allowed is None or value in allowed:
                continue
            listed = "、".join(list(allowed)[:MAX_LISTED_VALUES])
            hint = [item for item in allowed if value in item or item in value]
            suggestion = f"；最接近的是 {'、'.join(hint[:5])}（部分比對可用 LIKE）" if hint else ""
            return CheckIssue(
                "SQL_VALUE_NOT_IN_SET",
                f"「{column}」沒有 {value!r} 這個值。可用的值：{listed}{suggestion}",
            )
    return None


def _limit_of_a_listing(sql: str) -> int | None:
    """LIMIT of a query meant to list everything; None for intentional rankings.

    `ORDER BY … LIMIT 1`（最高的是哪一天）或沒有 GROUP BY 的排序是刻意取前幾名，結果剛好
    等於 LIMIT 是正常的；分組或沒排序的查詢被 LIMIT 切掉才是截斷。
    """

    try:
        tree = parse_one(sql, read="sqlite")
    except ParseError:
        return None
    expression = getattr(tree.args.get("limit"), "expression", None)
    if not (isinstance(expression, exp.Literal) and expression.is_int):
        return None
    limit = int(expression.this)
    ranking = tree.args.get("order") is not None and tree.args.get("group") is None
    return None if limit <= 1 or ranking else limit


def check_result(
    sql: str, rows: Sequence[Sequence[object]], *, asks_top_n: bool, max_rows: int
) -> CheckIssue | None:
    """Problems visible only after running it."""

    if not rows or all(all(value is None for value in row) for row in rows):
        return CheckIssue(
            "RESULT_EMPTY",
            "結果是 0 筆（或聚合結果全是 NULL）。請檢查條件值是否真的存在於資料、"
            "數字是否以數值比較、部分比對是否用了 LIKE 與 %；確定真的沒有資料就照原樣回傳。",
        )
    limit = _limit_of_a_listing(sql)
    if limit is not None and len(rows) == limit and limit < max_rows and not asks_top_n:
        return CheckIssue(
            "RESULT_TRUNCATED",
            f"結果剛好 {limit} 筆、等於 LIMIT，可能被截斷；"
            f"問題沒有要前幾名時，請把 LIMIT 提高到 {max_rows}。",
        )
    return None


__all__ = ["CheckIssue", "check_result", "check_sql", "coerce_numeric_params"]
