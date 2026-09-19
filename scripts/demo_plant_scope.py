"""Show the same question answered under two different account scopes.

執行方式：

    uv run python scripts/demo_plant_scope.py

這支腳本不是測試，是給人看的對照：同一個問句，由全權限帳號與電廠帳號各問一次，
印出實際送進 SQLite 的 SQL 與筆數。重點在中間那段改寫後的 SQL —— 授權是在後端把
查詢包成只含該帳號可見列的子查詢，不是在提示詞裡請 AI 自己守規矩。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from ingest.validate import PROJECT_ROOT, resolve_configured_paths  # noqa: E402
from text2sql.scope_guard import ScopeGuard  # noqa: E402

QUESTION_SQL = (
    'SELECT "電廠", "機組", "裝置容量_MW" FROM v_unit WHERE "電廠" = ? ORDER BY "機組" LIMIT 200'
)
ASKED_ABOUT = "台中發電廠"
BOUND_PLANT = "林口發電廠"
RULE = "─" * 78


def _render(sql: str, params: tuple[object, ...]) -> str:
    return sql.replace("\n", " ") + f"\n  參數：{params}"


def main() -> int:
    database = resolve_configured_paths(PROJECT_ROOT)["database"].resolve()
    if not database.exists():
        print(f"找不到 {database}；請先執行 `uv run python -m ingest.build_db`。")
        return 1

    guard = ScopeGuard.from_database(database)
    names = guard.catalog.plant_names_by_id()
    bound_id = next(pid for pid, name in names.items() if name == BOUND_PLANT)

    import sqlite3

    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        print(RULE)
        print(f"問句：列出{ASKED_ABOUT}所有設備")
        print(RULE)

        for label, plant in (
            ("全權限帳號　scope: all", None),
            (f"電廠帳號　　scope: {bound_id}（{BOUND_PLANT}）", BOUND_PLANT),
        ):
            sql, params = guard.apply(QUESTION_SQL, (ASKED_ABOUT,), plant=plant)
            rows = connection.execute(sql, params).fetchall()
            print(f"\n{label}")
            print(f"  實際執行的 SQL：{_render(sql, params)}")
            print(f"  回傳筆數：{len(rows)}")
            if plant is not None and not rows:
                print(f"  → 此帳號的資料範圍只涵蓋{BOUND_PLANT}與跨廠共用欄位。")

        print(f"\n{RULE}")
        print(f"對照：同一個帳號問自己的廠（列出{BOUND_PLANT}所有設備）")
        print(RULE)
        sql, params = guard.apply(QUESTION_SQL, (BOUND_PLANT,), plant=BOUND_PLANT)
        rows = connection.execute(sql, params).fetchall()
        print(f"  回傳筆數：{len(rows)}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
