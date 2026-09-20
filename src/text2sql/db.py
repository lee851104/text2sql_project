"""Read-only, time-bounded SQLite execution adapter."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from time import monotonic


class QueryTimeoutError(TimeoutError):
    """Raised when SQLite exceeds the configured execution deadline."""


def _reject_double_quoted_strings(connection: sqlite3.Connection) -> None:
    """Turn off SQLite's double-quoted-string fallback for this connection.

    預設行為是相容性遺毒：`SELECT "打錯的欄位名"` 不會報錯，而是把那個名字當成字串回
    傳。實測本專案的連線也是這樣 —— `SELECT "不存在的欄位" FROM v_unit` 回一整欄
    `不存在的欄位`，一個字都沒錯。**安靜的錯誤比報錯危險得多**，而且對 Text2SQL 來說
    特別致命：模型或規則寫出一個不存在的欄位，使用者會收到一張看起來正常的假資料表。

    這裡是深度防禦。第一道仍然是 `SqlGuard` 的欄位 allowlist；這一道守的是
    「allowlist 與實際 schema 不同步」那種情況（例如資料換版後欄位改名）——
    那時 allowlist 會放行，而 DB 沒有那一欄。

    `setconfig` 是 Python 3.12 才有的，舊版就只能靠 allowlist 與評測抓。
    """

    if not hasattr(connection, "setconfig"):
        return
    for option in ("SQLITE_DBCONFIG_DQS_DML", "SQLITE_DBCONFIG_DQS_DDL"):
        flag = getattr(sqlite3, option, None)
        if flag is not None:
            connection.setconfig(flag, False)


class ReadOnlySQLite:
    def __init__(self, database: Path, *, timeout_seconds: float = 5.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必須大於 0")
        self.database = database.resolve()
        self.timeout_seconds = timeout_seconds

    def execute(
        self, sql: str, params: tuple[object, ...]
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        if not self.database.is_file():
            raise FileNotFoundError(self.database)

        deadline = monotonic() + self.timeout_seconds
        timed_out = False

        def within_deadline() -> int:
            nonlocal timed_out
            timed_out = monotonic() > deadline
            return int(timed_out)

        uri = f"{self.database.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.execute("PRAGMA query_only = ON")
                _reject_double_quoted_strings(connection)
                connection.set_progress_handler(within_deadline, 1_000)
                cursor = connection.execute(sql, params)
                columns = [item[0] for item in cursor.description or ()]
                rows = [tuple(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError as error:
            if timed_out:
                raise QueryTimeoutError(
                    f"SQLite 查詢超過 {self.timeout_seconds:g} 秒上限。"
                ) from error
            raise
        return columns, rows
