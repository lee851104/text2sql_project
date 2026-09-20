import sqlite3

import pytest

from text2sql.db import ReadOnlySQLite


def test_readonly_sqlite_executes_select_and_rejects_writes(tmp_path) -> None:
    database = tmp_path / "sample.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('ok')")

    executor = ReadOnlySQLite(database)
    columns, rows = executor.execute("SELECT value FROM sample LIMIT 1", ())
    assert columns == ["value"]
    assert rows == [("ok",)]
    with pytest.raises(sqlite3.OperationalError):
        executor.execute("DELETE FROM sample", ())


def test_a_misspelled_column_raises_instead_of_returning_its_own_name(tmp_path) -> None:
    """SQLite 預設把 `SELECT "打錯的欄位"` 當字串回傳，一個字都不報錯。

    對 Text2SQL 來說這是最危險的失敗方式：使用者會收到一張看起來正常的假資料表。
    """

    database = tmp_path / "sample.db"
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE sample ("電廠" TEXT)')
        connection.execute("INSERT INTO sample VALUES ('台中發電廠')")

    executor = ReadOnlySQLite(database)
    columns, rows = executor.execute('SELECT "電廠" FROM sample LIMIT 1', ())
    assert (columns, rows) == (["電廠"], [("台中發電廠",)]), "中文欄位名仍要能用"

    with pytest.raises(sqlite3.OperationalError):
        executor.execute('SELECT "不存在的欄位" FROM sample LIMIT 1', ())
