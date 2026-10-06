from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingest.realtime.lock import AlreadyRunning, SingleInstanceLock, is_locked, read_holder

SRC = Path(__file__).resolve().parents[1] / "src"


def test_second_instance_is_refused_with_the_holder_pid(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    with SingleInstanceLock(path):
        assert is_locked(path)
        assert read_holder(path)["pid"] == os.getpid()
        with pytest.raises(AlreadyRunning, match=str(os.getpid())):
            SingleInstanceLock(path).acquire()
    assert not is_locked(path)


def test_probe_without_a_lock_file(tmp_path: Path) -> None:
    assert not is_locked(tmp_path / "missing.lock")


def test_lock_is_released_when_the_process_dies(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from ingest.realtime.lock import SingleInstanceLock\n"
        "SingleInstanceLock(Path(sys.argv[1])).acquire()\n"
        "os._exit(0)\n"  # 不釋放就結束，模擬當機
    )
    environment = {**os.environ, "PYTHONPATH": str(SRC)}
    subprocess.run(
        [sys.executable, "-c", script, str(path)], check=True, env=environment, timeout=30
    )

    with SingleInstanceLock(path):
        assert is_locked(path)
