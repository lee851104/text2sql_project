from __future__ import annotations

from pathlib import Path

import pytest
from realtime_support import make_config

from ingest.realtime.decisions import DecisionFileError, UnitDecision, load_decisions

HEADER = "unit_type,unit_name,grain,access_scope,plant_id,note\n"


def _load(tmp_path: Path, body: str):
    config = make_config(tmp_path)
    config.units_csv.write_text(HEADER + body, encoding="utf-8")
    return load_decisions(config.units_csv, config.plants_csv)


def test_valid_rows_become_decisions(tmp_path: Path) -> None:
    decisions = _load(
        tmp_path,
        "燃氣,大潭CC#1,unit,plant,8,\n"
        "太陽能,其它購電太陽能,bucket,shared,,購電太陽能全部併在這一列\n",
    )

    assert decisions.units[("燃氣", "大潭CC#1")] == UnitDecision("unit", "plant", 8, "")
    assert decisions.units[("太陽能", "其它購電太陽能")].access_scope == "shared"
    assert decisions.plants[12] == "明潭發電廠"
    assert decisions.warnings == ()
    assert len(decisions.sha256) == 64


def test_missing_file_means_everything_undecided(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    decisions = load_decisions(config.units_csv, config.plants_csv)

    assert decisions.units == {}
    assert "未定" in decisions.warnings[0]
    assert decisions.sha256 == ""


def test_wrong_header_invalidates_the_whole_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.units_csv.write_text("type,name\n燃氣,大潭CC#1\n", encoding="utf-8")

    with pytest.raises(DecisionFileError, match="欄位必須是"):
        load_decisions(config.units_csv, config.plants_csv)


def test_duplicate_rows_invalidate_the_whole_file(tmp_path: Path) -> None:
    with pytest.raises(DecisionFileError, match="重複"):
        _load(tmp_path, "燃氣,大潭CC#1,unit,plant,8,\n燃氣,大潭CC#1,unit,shared,,\n")


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ("燃氣,大潭CC#1,unit,plant,99,", "plant_id"),
        ("燃氣,大潭CC#1,single,plant,8,", "grain"),
        ("燃氣,大潭CC#1,unit,everyone,,", "access_scope"),
        ("燃氣,大潭CC#1,unit,shared,8,", "shared"),
    ],
)
def test_invalid_row_is_skipped_with_a_warning(tmp_path: Path, row: str, reason: str) -> None:
    decisions = _load(tmp_path, row + "\n")

    assert decisions.units == {}
    assert reason in decisions.warnings[0]
