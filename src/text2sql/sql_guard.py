"""AST-based read-only SQL validation for the approved semantic views."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import ParseError

ALLOWED_COLUMNS = {
    "v_unit": {
        "機組名",
        "電廠",
        "縣市",
        "裝置容量_瓩",
        "裝置容量_萬瓩",
        "燃料",
        "商轉日期",
        "商轉日期精度",
    },
    "v_peak": {
        "日期",
        "機組欄位",
        "尖峰出力_萬瓩",
        "電廠",
        "粒度",
        "類別",
        "涵蓋機組數",
        "對應裝置容量_萬瓩",
        "容量來源",
        "是殘差欄",
        "是彙總欄",
        "有機組主檔",
    },
    "v_system": {
        "日期",
        "淨尖峰供電能力_萬瓩",
        "尖峰負載_萬瓩",
        "備轉容量_萬瓩",
        "備轉容量率_pct",
        "工業用電_百萬度",
        "民生用電_百萬度",
    },
    "v_outage": {
        "歲修編號",
        "機組名",
        "電廠",
        "燃料",
        "開始日期",
        "結束日期",
        "原因",
        "日期狀態",
        "對齊狀態",
    },
    "v_re_generation": {
        "年度",
        "月份",
        "發電站",
        "能源別",
        "縣市",
        "發電量_度",
        "裝置容量_瓩",
        "場址數",
        "數值狀態",
        "主檔來源",
    },
    "v_generation_cost": {
        "年度",
        "電力來源",
        "發電方式",
        "成本_元每度",
        "決算類型",
    },
}
# sqlglot 解析時，它認得的函式會有專屬節點（`Count`、`Avg`、`Substring`…），
# 只有它不認得的才落成 `Anonymous` —— 而 SQLite 那些危險的專有函式全在後者：
# `randomblob`、`zeroblob`、`load_extension`、`readfile`、`writefile`、`printf`。
# 這條界線就是白名單的分界：不認得的一律擋，要放行得逐一寫進來。
#
# 原本這裡是三個名字的黑名單（load_extension／readfile／writefile），漏掉了
# `randomblob(1000000000)` —— 它能在 2.99 秒內配出 1 GB，比 db.py 的 5 秒上限還快，
# 超時那道攔不到。黑名單補上 randomblob 還會漏下一個，所以改成白名單，跟這個檔案
# 對資料表與欄位的做法一致。
#
# 專案現有的 102 段 SQL（語料、題庫、router 手寫）用到的函式全都是 sqlglot 認得的，
# 所以這份白名單目前是空的；真的需要某個 SQLite 專有函式時再具名加入。
ALLOWED_UNKNOWN_FUNCTIONS: frozenset[str] = frozenset()
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SqlGuardResult:
    allowed: bool
    code: str
    reason: str
    tables: tuple[str, ...] = ()


class SqlGuard:
    def __init__(
        self,
        *,
        max_rows: int = 200,
        allowed_columns: dict[str, set[str]] | None = None,
        allowed_unknown_functions: frozenset[str] | None = None,
    ):
        self.max_rows = max_rows
        self.allowed_columns = allowed_columns or ALLOWED_COLUMNS
        self.allowed_unknown_functions = (
            ALLOWED_UNKNOWN_FUNCTIONS
            if allowed_unknown_functions is None
            else allowed_unknown_functions
        )

    @staticmethod
    def _reject(code: str, reason: str) -> SqlGuardResult:
        return SqlGuardResult(False, code, reason)

    def validate(self, sql: str, params: tuple[object, ...] = ()) -> SqlGuardResult:
        if "--" in sql or "/*" in sql or "*/" in sql:
            return self._reject("SQL_COMMENT", "SQL 不允許註解。")
        if FORBIDDEN_KEYWORDS.search(sql):
            return self._reject("SQL_NOT_READ_ONLY", "只允許 SELECT 查詢。")
        try:
            statements = [statement for statement in parse(sql, read="sqlite") if statement]
        except ParseError as error:
            return self._reject("SQL_PARSE_ERROR", str(error))
        if len(statements) != 1:
            return self._reject("SQL_MULTI_STATEMENT", "只允許單一 SQL 敘述。")
        tree = statements[0]
        if not isinstance(tree, exp.Select):
            return self._reject("SQL_NOT_SELECT", "SQL 最外層必須是 SELECT。")
        if tree.args.get("with_") is not None:
            return self._reject("SQL_WITH_BLOCKED", "保守模式不允許 WITH。")

        tables = tuple(sorted({table.name for table in tree.find_all(exp.Table)}))
        if not tables:
            return self._reject("SQL_MISSING_TABLE", "查詢必須從語意檢視讀取資料。")
        invalid_tables = [table for table in tables if table not in self.allowed_columns]
        if invalid_tables:
            return self._reject("SQL_TABLE_NOT_ALLOWED", f"不允許的資料表：{invalid_tables}")

        for function in tree.find_all(exp.Anonymous):
            if function.name.casefold() not in self.allowed_unknown_functions:
                return self._reject("SQL_FUNCTION_NOT_ALLOWED", f"不允許的函式：{function.name}")

        aliases = {item.alias for item in tree.expressions if item.alias}
        allowed = set().union(*(self.allowed_columns[table] for table in tables)) | aliases
        invalid_columns = {
            column.name
            for column in tree.find_all(exp.Column)
            if column.name != "*" and column.name not in allowed
        }
        if invalid_columns:
            return self._reject(
                "SQL_COLUMN_NOT_ALLOWED", f"不允許的欄位：{sorted(invalid_columns)}"
            )

        string_literals = [literal for literal in tree.find_all(exp.Literal) if literal.is_string]
        if string_literals:
            return self._reject("SQL_LITERAL_NOT_PARAMETERIZED", "字串字面值必須使用參數。")
        placeholders = list(tree.find_all(exp.Placeholder))
        if len(placeholders) != len(params):
            return self._reject("SQL_PARAMETER_MISMATCH", "SQL placeholder 與參數數量不一致。")

        limit = tree.args.get("limit")
        if limit is None:
            return self._reject("SQL_LIMIT_REQUIRED", "查詢必須有 LIMIT。")
        limit_expression = limit.expression
        if not isinstance(limit_expression, exp.Literal) or not limit_expression.is_int:
            return self._reject("SQL_LIMIT_INVALID", "LIMIT 必須是整數常數。")
        if int(limit_expression.this) > self.max_rows:
            return self._reject("SQL_LIMIT_EXCEEDED", f"LIMIT 不得超過 {self.max_rows}。")
        return SqlGuardResult(True, "OK", "SQL 安全驗證通過。", tables)


def validate_sql(sql: str, params: tuple[Any, ...] = (), *, max_rows: int = 200) -> SqlGuardResult:
    return SqlGuard(max_rows=max_rows).validate(sql, params)
