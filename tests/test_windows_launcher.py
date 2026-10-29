from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from ingest.validate import PROJECT_ROOT

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")


def _run_launcher(
    launcher: Path,
    *,
    fake_bin: Path,
    log: Path,
    cwd: Path,
    skip_existing_check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "UV_LOG": str(log),
            "POWERQUERY_SKIP_BROWSER": "1",
            "POWERQUERY_NO_PAUSE": "1",
        }
    )
    if skip_existing_check:
        environment["POWERQUERY_SKIP_EXISTING_CHECK"] = "1"
    else:
        environment.pop("POWERQUERY_SKIP_EXISTING_CHECK", None)
    return subprocess.run(
        f'cmd.exe /d /c call "{launcher}"',
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _dependency_stamp(project: Path) -> str:
    hashes = [
        hashlib.sha256((project / filename).read_bytes()).hexdigest().upper()
        for filename in ("pyproject.toml", "uv.lock")
    ]
    return ":".join(hashes)


def test_double_click_launcher_uses_its_own_unicode_directory_and_skips_redundant_sync(
    tmp_path: Path,
) -> None:
    project = tmp_path / "OneDrive 測試" / "台電 查詢專案"
    elsewhere = tmp_path / "different working directory"
    fake_bin = tmp_path / "fake tools"
    project.mkdir(parents=True)
    elsewhere.mkdir()
    fake_bin.mkdir()

    launcher = project / "啟動.bat"
    shutil.copyfile(PROJECT_ROOT / "啟動.bat", launcher)
    (project / "scripts").mkdir()
    for helper in ("launcher_dependency_state.ps1", "launcher_health.ps1"):
        shutil.copyfile(PROJECT_ROOT / "scripts" / helper, project / "scripts" / helper)
    (project / "pyproject.toml").write_text("[project]\nname='launcher-test'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    log = tmp_path / "uv calls.log"
    (fake_bin / "uv.cmd").write_text(
        """@echo off
>>"%UV_LOG%" echo %*
if "%~1"=="sync" (
  if not exist ".venv\\Scripts" mkdir ".venv\\Scripts"
  type nul > ".venv\\Scripts\\python.exe"
  type nul > ".venv\\Scripts\\powerquery.exe"
  exit /b 0
)
if "%~5"=="ingest.build_db" (
  if not exist "data\\processed" mkdir "data\\processed"
  >"data\\processed\\power.db" echo test database
)
exit /b 0
""",
        encoding="ascii",
    )

    first = _run_launcher(launcher, fake_bin=fake_bin, log=log, cwd=elsewhere)
    assert first.returncode == 0, first.stdout + first.stderr
    stamp_path = project / ".venv" / ".powerquery-sync-state"
    assert stamp_path.is_file(), first.stdout + first.stderr
    first_stamp = stamp_path.read_text(encoding="ascii").strip()
    second = _run_launcher(launcher, fake_bin=fake_bin, log=log, cwd=elsewhere)
    (project / "pyproject.toml").write_text(
        "[project]\nname='launcher-test'\n# dependency input changed\n",
        encoding="utf-8",
    )
    third = _run_launcher(launcher, fake_bin=fake_bin, log=log, cwd=elsewhere)
    third_stamp = stamp_path.read_text(encoding="ascii").strip()

    assert second.returncode == 0, second.stdout + second.stderr
    assert third.returncode == 0, third.stdout + third.stderr
    assert re.fullmatch(r"[0-9A-F]{64}:[0-9A-F]{64}", first_stamp)
    assert first_stamp != ":"
    assert first_stamp != third_stamp
    assert third_stamp == _dependency_stamp(project)
    assert (project / "data" / "processed" / "power.db").is_file(), (
        first.stdout + first.stderr + second.stdout + second.stderr + third.stdout + third.stderr
    )
    assert not (elsewhere / "data").exists()

    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("sync --extra dev --extra online") == 2
    assert calls.count("run --no-sync python -m project_tasks init-dirs") == 3
    assert calls.count("run --no-sync python -m ingest.build_db") == 1
    assert calls.count("run --no-sync powerquery --serve --host 127.0.0.1 --port 8765") == 3
    assert "dependency sync skipped" in second.stdout


def test_launcher_contains_health_gated_browser_start_and_actionable_failures() -> None:
    content = (PROJECT_ROOT / "啟動.bat").read_text(encoding="utf-8")
    health_helper = (PROJECT_ROOT / "scripts" / "launcher_health.ps1").read_text(encoding="utf-8")

    assert 'pushd "%~dp0"' in content
    assert "where uv" in content
    assert 'launcher_health.ps1" check' in content
    assert 'launcher_health.ps1" wait-and-open' in content
    assert "api/health" in health_helper
    assert "HttpWebRequest" in health_helper
    assert "ProcessStartInfo" in health_helper
    assert "uv run --no-sync powerquery --serve --host 127.0.0.1 --port 8765" in content
    assert "pause" in content
    assert "[ERROR]" in content
    assert content.index('launcher_health.ps1" check') < content.index("where uv")
    assert 'scripts\\launcher_dependency_state.ps1" check' in content
    assert 'scripts\\launcher_dependency_state.ps1" write' in content


class _HealthyPowerQueryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/api/health":
            self.send_error(404)
            return
        body = b'{"success":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_healthy_existing_service_is_reused_before_uv_or_database_checks(tmp_path: Path) -> None:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), _HealthyPowerQueryHandler)
    except OSError:
        pytest.skip("port 8765 is already occupied")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project = tmp_path / "existing service 測試"
        elsewhere = tmp_path / "other cwd"
        fake_bin = tmp_path / "empty tools"
        project.mkdir()
        elsewhere.mkdir()
        fake_bin.mkdir()
        launcher = project / "啟動.bat"
        shutil.copyfile(PROJECT_ROOT / "啟動.bat", launcher)
        (project / "scripts").mkdir()
        shutil.copyfile(
            PROJECT_ROOT / "scripts" / "launcher_health.ps1",
            project / "scripts" / "launcher_health.ps1",
        )

        response = _run_launcher(
            launcher,
            fake_bin=fake_bin,
            log=tmp_path / "unused uv.log",
            cwd=elsewhere,
            skip_existing_check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.returncode == 0, response.stdout + response.stderr
    assert "already running" in response.stdout
    assert not (project / ".venv").exists()
    assert not (project / "data").exists()
