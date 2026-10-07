from __future__ import annotations

import os
import shutil
import subprocess
import time
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


def test_startup_launcher_really_starts_the_collector_from_a_big5_console(tmp_path: Path) -> None:
    """登入時「啟動」資料夾的捷徑在 Big5 主控台執行；2026-10-07 實際登入時收集器沒有被叫起來。

    `chcp 65001` 之後緊接一行中文註解，切換字碼頁後 cmd 讀檔位置錯位，`start` 那行被讀壞
    （`'-' is not recognized`）。這裡用假的收集器啟動檔，只確認它真的被叫起來。
    """

    project = tmp_path / "台電 查詢專案"
    project.mkdir()
    shutil.copyfile(PROJECT_ROOT / STARTUP, project / STARTUP)
    marker = tmp_path / "collector started.txt"
    # `start` 用 cmd /K 開新視窗；結尾的 exit 讓那個視窗自己關掉。
    (project / COLLECT).write_bytes(f'@echo off\r\n>"{marker}" echo started\r\nexit\r\n'.encode())

    log = tmp_path / "startup.log"
    # 要有真正的主控台 chcp 才會生效；沒有主控台的子行程重現不了這個錯誤。視窗隱藏，輸出寫進檔案。
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    result = subprocess.run(
        f'cmd.exe /d /c "chcp 950 >nul & call "{project / STARTUP}" >"{log}" 2>&1"',
        cwd=tmp_path,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startup,
        timeout=60,
        check=False,
    )
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.2)
    output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    assert result.returncode == 0, output
    assert "is not recognized" not in output
    assert marker.exists(), output


@pytest.mark.parametrize("launcher", [COLLECT, STARTUP, STOP])
def test_launchers_use_crlf(launcher: str) -> None:
    raw = (PROJECT_ROOT / launcher).read_bytes()

    assert raw.count(b"\n") == raw.count(b"\r\n")
