"""RealtimePanel: the realtime block on the overview page (RT-3a spec §4, §8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from realtime_panel_support import NOW, build_standard_database
from realtime_support import make_config, write_project

from ingest.realtime.lock import SingleInstanceLock
from serving import realtime_panel
from serving.realtime_panel import PUBLIC_STATUS_FIELDS, RealtimePanel

UNAVAILABLE = {"available": False, "state": "unavailable"}


def _panel(tmp_path: Path, **build: object) -> RealtimePanel:
    config = make_config(tmp_path)
    build_standard_database(config, **build)
    return RealtimePanel(config)


# ── 狀態（/api/health 的 realtime 欄位）──────────────────────────────────


def test_status_without_a_database_is_unavailable(tmp_path: Path) -> None:
    assert RealtimePanel(make_config(tmp_path)).status(now=NOW) == UNAVAILABLE


def test_status_shows_the_state_but_no_generation_numbers(tmp_path: Path) -> None:
    panel = _panel(tmp_path)

    with SingleInstanceLock(panel.config.lock_path):
        status = panel.status(now=NOW)

    assert set(status) == set(PUBLIC_STATUS_FIELDS)
    assert status == {
        "available": True,
        "state": "healthy",
        "collector_running": True,
        "latest_data_time": "2026-10-05 15:20",
        "lag_minutes": 7.4,
    }


def test_status_reports_a_stopped_collector(tmp_path: Path) -> None:
    status = _panel(tmp_path).status(now=NOW)

    assert status["state"] == "stopped"
    assert status["collector_running"] is False


def test_status_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _panel(tmp_path)

    def broken(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(realtime_panel, "read_status", broken)

    assert panel.status(now=NOW) == UNAVAILABLE


def test_a_panel_without_settings_shows_no_data() -> None:
    assert RealtimePanel(None).status(now=NOW) == UNAVAILABLE


def test_from_project_reads_the_project_settings(tmp_path: Path) -> None:
    root = write_project(tmp_path)

    panel = RealtimePanel.from_project(root)

    assert panel.config is not None
    assert panel.config.database == root.resolve() / "data/processed/realtime.db"


def test_a_broken_realtime_config_does_not_stop_the_service(tmp_path: Path) -> None:
    root = write_project(tmp_path)
    (root / "configs/realtime.yaml").write_text("source: [\n", encoding="utf-8")

    panel = RealtimePanel.from_project(root)

    assert panel.config is None
    assert panel.status(now=NOW) == UNAVAILABLE
