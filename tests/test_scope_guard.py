"""Authorisation coverage: the scope roster, its build-time pins, and the SQL rewrite."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from ingest.build_db import ScopeAlignmentError, build_database
from ingest.validate import PROJECT_ROOT, resolve_configured_paths
from text2sql.scope_guard import ScopeGuard, UnknownPlantError

SHARED_COLUMNS = {"其他小水力", "太陽能發電", "氣渦輪", "汽電共生", "離島", "風力發電"}

# 這八個電廠在每日尖峰資料沒有自己的欄位，出力只存在於共用彙總欄。
BUCKET_ONLY_PLANTS = {
    "協和電廠－珠山分廠",
    "塔山發電廠",
    "尖山發電廠",
    "曾文發電廠",
    "桂山發電廠",
    "石門發電廠",
    "蘭陽發電廠",
    "高屏發電廠",
}


@pytest.fixture(scope="module")
def scoped_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("scope") / "power.db"
    build_database(target)
    return target


@pytest.fixture(scope="module")
def guard(scoped_database: Path) -> ScopeGuard:
    return ScopeGuard.from_database(scoped_database)


def _rewrite_rows(
    database: Path, guard: ScopeGuard, sql: str, params: tuple[object, ...], plant: str | None
) -> list[tuple[object, ...]]:
    scoped_sql, scoped_params = guard.apply(sql, params, plant=plant)
    with sqlite3.connect(database) as connection:
        return connection.execute(scoped_sql, scoped_params).fetchall()


def _scope_csv(name: str) -> list[dict[str, str]]:
    path = resolve_configured_paths(PROJECT_ROOT)[name]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.mark.integration
def test_every_governed_row_has_an_authorisation_owner(scoped_database: Path) -> None:
    with sqlite3.connect(scoped_database) as connection:
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "dim_plant_scope",
                "b_column_scope",
                "bridge_b_column_plant",
                "outage_scope",
            )
        }
        unscoped_columns = connection.execute(
            """SELECT COUNT(*) FROM dim_b_column
                WHERE id NOT IN (SELECT b_column_id FROM b_column_scope)"""
        ).fetchone()[0]
        unscoped_outages = connection.execute(
            """SELECT COUNT(*) FROM dim_outage
                WHERE id NOT IN (SELECT outage_id FROM outage_scope)"""
        ).fetchone()[0]
        unscoped_units = connection.execute(
            "SELECT COUNT(*) FROM dim_unit WHERE plant_id IS NULL"
        ).fetchone()[0]

    assert counts == {
        "dim_plant_scope": 34,
        "b_column_scope": 64,
        "bridge_b_column_plant": 72,
        "outage_scope": 138,
    }
    assert (unscoped_columns, unscoped_outages, unscoped_units) == (0, 0, 0)


@pytest.mark.integration
def test_plant_identifiers_match_the_star_schema(scoped_database: Path) -> None:
    with sqlite3.connect(scoped_database) as connection:
        drifted = connection.execute(
            """SELECT p.id FROM dim_plant AS p
                 JOIN dim_plant_scope AS s ON s.plant_id = p.id
                WHERE s.plant_name <> p.plant_name"""
        ).fetchall()
    assert drifted == []


@pytest.mark.integration
def test_plant_account_reads_only_its_own_and_shared_daily_columns(
    scoped_database: Path, guard: ScopeGuard
) -> None:
    sql = 'SELECT DISTINCT "機組欄位" FROM v_peak LIMIT 100'

    taichung = {row[0] for row in _rewrite_rows(scoped_database, guard, sql, (), "台中發電廠")}
    linkou = {row[0] for row in _rewrite_rows(scoped_database, guard, sql, (), "林口發電廠")}

    assert {"台中#1", "台中#10"} <= taichung
    assert taichung >= SHARED_COLUMNS
    assert not any(column.startswith("林口") for column in taichung)
    assert (taichung & linkou) == SHARED_COLUMNS


@pytest.mark.integration
def test_bucket_only_plant_still_reads_the_shared_bucket(
    scoped_database: Path, guard: ScopeGuard
) -> None:
    sql = 'SELECT DISTINCT "機組欄位" FROM v_peak LIMIT 100'

    visible = {row[0] for row in _rewrite_rows(scoped_database, guard, sql, (), "高屏發電廠")}

    assert visible == SHARED_COLUMNS


@pytest.mark.integration
def test_system_wide_views_are_never_restricted(scoped_database: Path, guard: ScopeGuard) -> None:
    sql = "SELECT COUNT(*) FROM v_system LIMIT 1"
    cost_sql = "SELECT COUNT(*) FROM v_generation_cost LIMIT 1"

    scoped_sql, scoped_params = guard.apply(sql, (), plant="高屏發電廠")

    assert scoped_sql == sql
    assert scoped_params == ()
    assert _rewrite_rows(scoped_database, guard, sql, (), "高屏發電廠") == [(577,)]
    assert _rewrite_rows(scoped_database, guard, cost_sql, (), "高屏發電廠") == [(72,)]


@pytest.mark.integration
def test_rewrite_keeps_original_parameters_bound_to_their_own_placeholders(
    scoped_database: Path, guard: ScopeGuard
) -> None:
    sql = (
        'SELECT "日期", "尖峰出力_萬瓩" FROM v_peak '
        'WHERE "日期" >= ? AND "日期" <= ? ORDER BY "日期" LIMIT 5'
    )

    scoped_sql, scoped_params = guard.apply(sql, ("2026-07-01", "2026-07-03"), plant="林口發電廠")
    rows = _rewrite_rows(scoped_database, guard, sql, ("2026-07-01", "2026-07-03"), "林口發電廠")

    assert scoped_params[-2:] == ("2026-07-01", "2026-07-03")
    assert scoped_sql.count("?") == len(scoped_params)
    assert {row[0] for row in rows} <= {"2026-07-01", "2026-07-02", "2026-07-03"}


@pytest.mark.integration
def test_join_and_alias_are_restricted_on_both_sides(
    scoped_database: Path, guard: ScopeGuard
) -> None:
    sql = (
        'SELECT u."電廠", o."開始日期" FROM v_unit AS u '
        'JOIN v_outage AS o ON o."機組名" = u."機組名" LIMIT 20'
    )

    rows = _rewrite_rows(scoped_database, guard, sql, (), "蘭陽發電廠")

    assert rows
    assert {row[0] for row in rows} == {"蘭陽發電廠"}


@pytest.mark.integration
def test_plant_without_outages_returns_no_rows_rather_than_invalid_sql(
    scoped_database: Path, guard: ScopeGuard
) -> None:
    sql = 'SELECT "機組名" FROM v_outage LIMIT 20'

    rows = _rewrite_rows(scoped_database, guard, sql, (), "曾文發電廠")

    assert rows == []


@pytest.mark.integration
def test_all_plants_scope_executes_the_query_unchanged(guard: ScopeGuard) -> None:
    sql = 'SELECT "日期" FROM v_peak WHERE "機組欄位" = ? LIMIT 5'

    assert guard.apply(sql, ("台中#1",), plant=None) == (sql, ("台中#1",))


@pytest.mark.integration
def test_unknown_plant_is_refused(guard: ScopeGuard) -> None:
    with pytest.raises(UnknownPlantError, match="授權名冊"):
        guard.apply('SELECT "日期" FROM v_peak LIMIT 1', (), plant="不存在發電廠")


@pytest.mark.integration
def test_bucket_only_plants_become_refusal_rules(scoped_database: Path) -> None:
    with sqlite3.connect(scoped_database) as connection:
        rules = dict(
            connection.execute(
                """SELECT target_name, severity FROM meta_pitfall
                    WHERE pitfall_code = 'PLANT_DAILY_ONLY_IN_BUCKET'"""
            )
        )

    assert set(rules) == BUCKET_ONLY_PLANTS
    assert set(rules.values()) == {"refuse"}


@pytest.mark.integration
def test_build_fails_when_a_daily_column_identifier_drifts(tmp_path: Path) -> None:
    rows = _scope_csv("daily_scope_csv")
    rows[1]["b_column"] = "被改名的欄位"
    tampered = _write_csv(tmp_path / "daily_plant_scope.csv", rows)

    with pytest.raises(ScopeAlignmentError, match="每日欄位編號已漂移"):
        build_database(tmp_path / "power.db", source_paths={"daily_scope_csv": tampered})


@pytest.mark.integration
def test_build_fails_when_a_daily_column_has_no_authorisation_decision(tmp_path: Path) -> None:
    rows = [row for row in _scope_csv("daily_scope_csv") if row["b_column"] != "台中#1"]
    incomplete = _write_csv(tmp_path / "daily_plant_scope.csv", rows)

    with pytest.raises(ScopeAlignmentError, match="沒有授權範圍設定"):
        build_database(tmp_path / "power.db", source_paths={"daily_scope_csv": incomplete})


@pytest.mark.integration
def test_build_fails_when_a_plant_identifier_drifts(tmp_path: Path) -> None:
    rows = _scope_csv("plants_csv")
    rows[0]["plant_name"] = "改名發電廠"
    tampered = _write_csv(tmp_path / "plants.csv", rows)

    with pytest.raises(ScopeAlignmentError, match="電廠編號已漂移"):
        build_database(tmp_path / "power.db", source_paths={"plants_csv": tampered})


@pytest.mark.integration
def test_build_fails_when_an_outage_event_has_no_plant(tmp_path: Path) -> None:
    rows = _scope_csv("outage_scope_csv")[:-1]
    incomplete = _write_csv(tmp_path / "outage_plant_map.csv", rows)

    with pytest.raises(ScopeAlignmentError, match="沒有電廠歸屬"):
        build_database(tmp_path / "power.db", source_paths={"outage_scope_csv": incomplete})
