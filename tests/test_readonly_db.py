import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

from text2sql.db import (
    DoubleQuotedStringGuardUnavailable,
    QueryResultTooLargeError,
    ReadOnlySQLite,
    _reject_double_quoted_strings,
)


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


def test_one_oversized_value_is_refused_before_it_is_materialised(tmp_path) -> None:
    """LIMIT 管列數，管不到一列有多大。

    `randomblob(1000000000)` 一列就配走 1 GB，而且只要 2.99 秒 —— 比 5 秒的超時上限還快，
    所以那道攔不到。上限設在連線上，SQLite 會在配置記憶體之前就回絕。
    """

    database = tmp_path / "sample.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('ok')")

    executor = ReadOnlySQLite(database, max_value_bytes=1_000)
    assert executor.execute("SELECT length(randomblob(500)) FROM sample LIMIT 1", ())[1] == [(500,)]

    with pytest.raises(QueryResultTooLargeError):
        executor.execute("SELECT randomblob(1000000000) FROM sample LIMIT 1", ())


def test_max_value_bytes_must_be_positive(tmp_path) -> None:
    with pytest.raises(ValueError, match="max_value_bytes"):
        ReadOnlySQLite(tmp_path / "sample.db", max_value_bytes=0)


def test_an_interpreter_that_cannot_turn_off_dqs_is_refused_at_startup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """以前在 Python 3.11 上這道防護會靜默跳過；現在建執行器時就拒絕。"""

    monkeypatch.delattr(sqlite3, "SQLITE_DBCONFIG_DQS_DML", raising=False)

    with pytest.raises(DoubleQuotedStringGuardUnavailable, match="3.12"):
        ReadOnlySQLite(tmp_path / "sample.db")


def test_a_connection_without_setconfig_is_refused() -> None:
    class OldConnection:
        pass

    with pytest.raises(DoubleQuotedStringGuardUnavailable):
        _reject_double_quoted_strings(OldConnection())


def test_the_project_requires_the_python_that_has_the_guard() -> None:
    """requires-python、.python-version 與 db.py 的需求必須講同一件事。"""

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.12"
    assert (root / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    assert sys.version_info >= (3, 12)
