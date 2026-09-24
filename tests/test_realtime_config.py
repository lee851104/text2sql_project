from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from ingest.realtime.config import RealtimeConfigError, load_config
from ingest.realtime.timeutil import TAIPEI, floor_slot, format_slot, parse_slot, to_taipei

REALTIME_YAML = """\
source:
  url: https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json
  timeout_seconds: 20
  max_bytes: 1000000
schedule:
  slot_minutes: 10
  first_probe_seconds: 320
  retry_seconds: 60
  backoff_after_failures: [10, 20]
  backoff_seconds: [120, 300]
  retry_after_cap_seconds: 1800
  loop_max_sleep_seconds: 30
  resume_gap_seconds: 120
validation:
  future_tolerance_seconds: 900
  subtotal_tolerance_mw: 0.1
  detail_rows_expected: [100, 400]
  known_types: [燃氣, 儲能負載]
  unreliable_notes: [通訊異常]
retention:
  raw_days: 14
rollup:
  day_closed_after: "00:15"
status:
  stale_after_minutes: 30
"""

CONFIG_YAML = """\
paths:
  plants_csv: taipower_align/plants.csv
  realtime_database: data/processed/realtime.db
  realtime_root: data/realtime
  realtime_units_csv: taipower_align/realtime_units.csv
"""


def _write_root(root: Path, realtime_yaml: str = REALTIME_YAML) -> Path:
    (root / "configs").mkdir(parents=True)
    (root / "configs/realtime.yaml").write_text(realtime_yaml, encoding="utf-8")
    (root / "configs/config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    return root


def test_slots_are_utc_plus_8_and_floor_to_ten_minutes() -> None:
    moment = datetime(2026, 9, 24, 0, 47, 31, tzinfo=UTC)  # 08:47:31 in Taipei
    assert format_slot(floor_slot(moment, 10)) == "2026-09-24 08:40"
    assert parse_slot("2026-09-24 08:40") == datetime(2026, 9, 24, 8, 40, tzinfo=TAIPEI)


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="時區"):
        to_taipei(datetime(2026, 9, 24, 8, 40))


def test_load_config_reads_both_files(tmp_path: Path) -> None:
    config = load_config(_write_root(tmp_path))

    assert config.schedule.backoff_after_failures == (10, 20)
    assert config.validation.known_types == frozenset({"燃氣", "儲能負載"})
    assert config.day_closed_after == time(0, 15)
    assert config.database == tmp_path.resolve() / "data/processed/realtime.db"
    assert config.archive_dir == tmp_path.resolve() / "data/realtime/archive"
    assert config.slots_per_day == 144


def test_unquoted_closing_time_is_explained(tmp_path: Path) -> None:
    # PyYAML 把沒加引號的 12:30 讀成六十進位整數 750；00:15 則剛好還是字串。
    yaml_text = REALTIME_YAML.replace('day_closed_after: "00:15"', "day_closed_after: 12:30")
    with pytest.raises(RealtimeConfigError, match="加引號"):
        load_config(_write_root(tmp_path, yaml_text))


def test_malformed_closing_time_is_refused(tmp_path: Path) -> None:
    yaml_text = REALTIME_YAML.replace('day_closed_after: "00:15"', 'day_closed_after: "late"')
    with pytest.raises(RealtimeConfigError, match="HH:MM"):
        load_config(_write_root(tmp_path, yaml_text))


def test_backoff_lists_must_pair_up(tmp_path: Path) -> None:
    yaml_text = REALTIME_YAML.replace("backoff_seconds: [120, 300]", "backoff_seconds: [120]")
    with pytest.raises(RealtimeConfigError, match="長度必須相同"):
        load_config(_write_root(tmp_path, yaml_text))


def test_missing_key_names_the_key(tmp_path: Path) -> None:
    yaml_text = REALTIME_YAML.replace("  retry_seconds: 60\n", "")
    with pytest.raises(RealtimeConfigError, match="retry_seconds"):
        load_config(_write_root(tmp_path, yaml_text))
