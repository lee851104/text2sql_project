from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ingest.validate import PROJECT_ROOT

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")

COLLECT = "即時收集啟動.bat"
STARTUP = "開機自動啟動-即時收集.bat"
STOP = "停止即時收集.bat"

# 第一次呼叫回 1（模擬當掉），之後回 3（已有一份在跑），用來驗證「重啟一次、看到 3 就停」。
FAKE_UV = """@echo off
>>"%UV_LOG%" echo %*
if not exist "%UV_LOG%.count" (
  type nul > "%UV_LOG%.count"
  exit /b 1
)
exit /b 3
"""


def _run(tmp_path: Path, launcher: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    project = tmp_path / "OneDrive 測試" / "台電 查詢專案"
    fake_bin = tmp_path / "fake tools"
    project.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copyfile(PROJECT_ROOT / launcher, project / launcher)
    (fake_bin / "uv.cmd").write_text(FAKE_UV, encoding="ascii")
    log = tmp_path / "uv calls.log"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "UV_LOG": str(log),
        "POWERQUERY_NO_PAUSE": "1",
        "POWERQUERY_RESTART_DELAY": "0",
    }
    result = subprocess.run(
        f'cmd.exe /d /c call "{project / launcher}"',
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_collector_launcher_restarts_after_a_crash_but_not_when_already_running(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, COLLECT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == ["run --no-sync python -m ingest.realtime run"] * 2
    assert "Restarting" in result.stdout
    assert "already running" in result.stdout


def test_stop_launcher_calls_the_stop_command(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, STOP)

    assert calls == ["run --no-sync python -m ingest.realtime stop"]
    assert result.returncode == 1  # fake uv 第一次回 1，批次檔要把結束碼傳出去


def test_startup_launcher_opens_the_collector_minimised() -> None:
    content = (PROJECT_ROOT / STARTUP).read_text(encoding="utf-8")

    assert "chcp 65001" in content
    assert f'/min "%~dp0{COLLECT}"' in content


@pytest.mark.parametrize("launcher", [COLLECT, STARTUP, STOP])
def test_launchers_use_crlf(launcher: str) -> None:
    raw = (PROJECT_ROOT / launcher).read_bytes()

    assert raw.count(b"\n") == raw.count(b"\r\n")
