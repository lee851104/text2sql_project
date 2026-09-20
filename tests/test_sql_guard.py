import json
from pathlib import Path

import pytest

from text2sql.sql_guard import SqlGuard

ROOT = Path(__file__).parents[1]


def test_all_attack_benchmark_queries_are_blocked() -> None:
    attacks = json.loads(
        (ROOT / "benchmarks" / "attack_questions.json").read_text(encoding="utf-8")
    )
    guard = SqlGuard()
    results = {item["id"]: guard.validate(item["input"]) for item in attacks}
    assert len(results) == 16
    assert all(not result.allowed for result in results.values())


@pytest.mark.parametrize(
    ("sql", "params", "code"),
    [
        ('SELECT "不存在" FROM v_peak LIMIT 1', (), "SQL_COLUMN_NOT_ALLOWED"),
        ('SELECT "日期" FROM v_peak WHERE "機組欄位" = ? LIMIT 1', (), "SQL_PARAMETER_MISMATCH"),
        ('SELECT "日期" FROM v_peak LIMIT 201', (), "SQL_LIMIT_EXCEEDED"),
        (
            "SELECT load_extension(?) FROM v_peak LIMIT 1",
            ("evil",),
            "SQL_FUNCTION_NOT_ALLOWED",
        ),
        (
            'SELECT "日期" FROM v_peak WHERE "機組欄位" = \'台中#1\' LIMIT 1',
            (),
            "SQL_LITERAL_NOT_PARAMETERIZED",
        ),
        # LIMIT 管的是列數，管不到一列有多大：randomblob(1e9) 一列就配走 1 GB，
        # 而且只花 2.99 秒 —— 比 db.py 的 5 秒上限還快，超時那道攔不到。
        ("SELECT randomblob(1000000000) FROM v_unit LIMIT 1", (), "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT zeroblob(1000000000) FROM v_unit LIMIT 1", (), "SQL_FUNCTION_NOT_ALLOWED"),
        # 包一層 sqlglot 認得的函式也不行：裡面那個還是 Anonymous。
        ("SELECT hex(randomblob(500000000)) FROM v_unit LIMIT 1", (), "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT printf(?) FROM v_unit LIMIT 1", ("%d",), "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT sqlite_version() FROM v_unit LIMIT 1", (), "SQL_FUNCTION_NOT_ALLOWED"),
    ],
)
def test_guard_rejects_specific_unsafe_shapes(
    sql: str, params: tuple[object, ...], code: str
) -> None:
    result = SqlGuard().validate(sql, params)
    assert not result.allowed
    assert result.code == code


def test_guard_accepts_parameterized_view_query() -> None:
    sql = (
        'SELECT "日期", "尖峰出力_萬瓩" FROM v_peak '
        'WHERE "機組欄位" = ? ORDER BY "日期" DESC LIMIT 20'
    )
    result = SqlGuard().validate(sql, ("台中#1",))
    assert result.allowed
    assert result.tables == ("v_peak",)


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            'SELECT "日期" FROM v_peak WHERE "電廠" = ? AND "日期" > ? LIMIT 200',
            ("台中", "2026-01"),
        ),
        ('SELECT COUNT(*) AS "數量" FROM v_unit WHERE "燃料" = ? LIMIT 1', ("燃煤",)),
        (
            'SELECT SUBSTR("日期", 1, 7) AS "月份", AVG("尖峰負載_萬瓩") AS "平均值" '
            "FROM v_system GROUP BY 1 LIMIT 20",
            (),
        ),
        ('SELECT MAX("尖峰出力_萬瓩") AS "最大值" FROM v_peak LIMIT 1', ()),
        ('SELECT ROUND(AVG("備轉容量率_pct"), 2) AS "平均值" FROM v_system LIMIT 1', ()),
        ('SELECT "日期" FROM v_peak WHERE "電廠" = ? OR "電廠" = ? LIMIT 10', ("甲", "乙")),
    ],
)
def test_function_allowlist_keeps_ordinary_queries_working(
    sql: str, params: tuple[object, ...]
) -> None:
    """白名單擋的是 sqlglot 不認得的函式，不能連 AND／OR 這種節點一起擋掉。

    `find_all(exp.Func)` 抓到的不只真函式 —— `And`／`Or` 也是 Func 的子類。
    要是照名字做白名單，`WHERE a = ? AND b = ?` 會被自己的守門擋下來。
    """

    result = SqlGuard().validate(sql, params)
    assert result.allowed, result.code


def test_guard_accepts_generation_cost_view_query() -> None:
    sql = (
        'SELECT "年度", "成本_元每度" FROM v_generation_cost '
        'WHERE "年度" = ? AND "發電方式" = ? LIMIT 20'
    )

    result = SqlGuard().validate(sql, (2025, "火力發電"))

    assert result.allowed
    assert result.tables == ("v_generation_cost",)
