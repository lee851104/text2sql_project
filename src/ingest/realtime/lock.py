"""Single-instance lock for the collector (§4.3).

原語與 serving/data_management.py 的 _FileLock 相同（Windows 用 msvcrt，其他平台用 fcntl），
但只試一次、不重試。只鎖第 0 個位元組；PID 與啟動時間寫在第 1 個位元組之後，
所以在 Windows 上其他行程仍讀得到持有者資訊。行程死掉時作業系統會自動釋放鎖。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class AlreadyRunning(RuntimeError):
    def __init__(self, path: Path, holder: dict[str, object] | None):
        pid = holder.get("pid", "未知") if holder else "未知"
        super().__init__(f"收集器已在執行（PID {pid}，鎖檔 {path}）。")
        self.holder = holder


def _open(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_holder(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as handle:
            handle.seek(1)
            text = handle.read().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        holder = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    return holder if isinstance(holder, dict) else None


def is_locked(path: Path) -> bool:
    """試鎖一次再立刻放掉；收集器在跑時回 True。"""
    if not path.exists():
        return False
    handle = _open(path)
    try:
        if _try_lock(handle):
            _unlock(handle)
            return False
        return True
    finally:
        handle.close()


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        handle = _open(self.path)
        if not _try_lock(handle):
            handle.close()
            raise AlreadyRunning(self.path, read_holder(self.path))
        holder = {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
        handle.truncate(1)
        handle.seek(1)
        handle.write(json.dumps(holder).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.release()
