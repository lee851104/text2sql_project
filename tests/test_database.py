from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ingest import build_db as build_db_module
from ingest.build_db import DatabasePublishError, build_database
from ingest.validate import PROJECT_ROOT, validate_configured_files


@pytest.mark.contract
def test_full_source_fixture_matches_documented_counts() -> None:
    report = validate_configured_files()

    assert report["status"] == "pass"
    assert report["counts"] == {
        "plants": 22,
        "units": 175,
        "dates": 577,
        "daily_system": 577,
        "generation_columns": 64,
        "mapped_columns": 43,
        "daily_peak": 36_928,
        "outages": 138,
        "generation_costs": 72,
    }
    assert report["date_range"] == {"min": "2025-01-01", "max": "2026-07-31"}


@pytest.mark.integration
def test_database_contains_star_schema_and_semantic_views(tmp_path: Path) -> None:
    target = tmp_path / "power.db"
    report = build_database(target)

    assert report["table_counts"]["dim_unit"] == 175
    assert report["table_counts"]["bridge_b_column"] == 43
    assert report["table_counts"]["dim_b_column"] == 64
    assert report["table_counts"]["fact_daily_peak"] == 36_928
    assert report["table_counts"]["fact_daily_system"] == 577
    assert report["table_counts"]["dim_outage"] == 138
    assert report["table_counts"]["dim_re_site"] == 65
    assert report["table_counts"]["fact_re_monthly"] == 1_976
    # 場址主檔 93 列明細（4 列小計已剔除）加 1 筆補充檔；直接加總 97 列會多算近一倍。
    assert report["renewable"]["capacity_kw"] == 762_760
    assert report["renewable"]["unmatched"] == []

    with sqlite3.connect(target) as connection:
        views = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name"
            )
        }
        assert views == {
            "v_generation_cost",
            "v_outage",
            "v_peak",
            "v_re_generation",
            "v_system",
            "v_unit",
        }
        assert connection.execute("SELECT COUNT(*) FROM v_peak").fetchone()[0] == 36_928
        assert connection.execute("SELECT COUNT(*) FROM v_system").fetchone()[0] == 577
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dim_outage WHERE date_status = 'invalid_range'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        manifest = connection.execute(
            "SELECT data_start, data_end, data_checksum FROM meta_manifest WHERE id = 1"
        ).fetchone()
        assert manifest == (
            "2025-01-01",
            "2026-07-31",
            report["database_content_checksum"],
        )


@pytest.mark.integration
def test_database_exposes_2025_thermal_generation_cost(tmp_path: Path) -> None:
    target = tmp_path / "power.db"

    report = build_database(target)

    assert report["table_counts"]["fact_generation_cost"] == 72
    with sqlite3.connect(target) as connection:
        row = connection.execute(
            'SELECT "年度", "電力來源", "發電方式", "成本_元每度", "決算類型" '
            'FROM v_generation_cost WHERE "年度" = ? AND "發電方式" = ? LIMIT 1',
            (2025, "火力發電"),
        ).fetchone()
    assert row == (2025, "自發電力", "火力發電", 2.67, "自編決算")


@pytest.mark.integration
def test_rebuild_is_content_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "power.db"
    first = build_database(target, root=PROJECT_ROOT)
    second = build_database(target, root=PROJECT_ROOT)

    assert first["table_counts"] == second["table_counts"]
    assert first["database_content_checksum"] == second["database_content_checksum"]


@pytest.mark.integration
def test_build_accepts_reviewed_source_snapshot_with_optional_outage_removed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "power-without-outages.db"
    report = build_database(
        target,
        source_paths={"outage_csv": tmp_path / "removed-outage.csv"},
    )

    assert report["counts"]["outages"] == 0
    assert report["table_counts"]["dim_outage"] == 0
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v_outage").fetchone()[0] == 0


def test_build_rejects_unknown_source_slot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不支援的資料來源"):
        build_database(
            tmp_path / "power.db",
            source_paths={"arbitrary_sqlite": tmp_path / "unsafe.db"},
        )


@pytest.mark.integration
def test_locked_target_reports_recovery_and_removes_temporary_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "power.db"
    target.write_bytes(b"existing database remains untouched")
    attempted_sources: list[Path] = []

    def deny_replace(source: str | Path, destination: str | Path) -> None:
        attempted_sources.append(Path(source))
        assert Path(destination) == target
        raise PermissionError(5, "access denied", str(destination))

    monkeypatch.setattr(build_db_module.os, "replace", deny_replace)

    with pytest.raises(DatabasePublishError, match="Ctrl\\+C"):
        build_database(target, root=PROJECT_ROOT)

    assert target.read_bytes() == b"existing database remains untouched"
    assert len(attempted_sources) == 1
    assert not attempted_sources[0].exists()


def test_cli_formats_database_publish_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_publish(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise DatabasePublishError("請先停止服務再重試")

    monkeypatch.setattr(build_db_module, "build_database", fail_publish)

    with pytest.raises(SystemExit) as exit_info:
        build_db_module.main(
            [
                "--output",
                str(tmp_path / "power.db"),
                "--report",
                str(tmp_path / "report.json"),
            ]
        )

    assert exit_info.value.code == 1
    error = capsys.readouterr().err
    assert error == "錯誤：請先停止服務再重試\n"
    assert "Traceback" not in error
