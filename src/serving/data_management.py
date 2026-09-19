"""Versioned, review-gated management of the fixed PowerQuery data inputs.

The service deliberately never overwrites a published SQLite database.  Every
candidate is built at a content-addressed path, reviewed, and then activated by
switching an ``active.json`` pointer and an optional in-process runtime callback.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import EACCES, EBUSY, EPERM
from functools import partial
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import Any, BinaryIO, Literal, Protocol
from uuid import uuid4

from ingest.validate import (
    CROSSWALK_REQUIRED,
    DAILY_LONG_REQUIRED,
    DAILY_SYSTEM_COLUMNS,
    GENERATION_COST_REQUIRED,
    OUTAGE_REQUIRED,
    UNITS_REQUIRED,
)

ACTIVE_SCHEMA = "powerquery-data-active-v1"
AUDIT_SCHEMA = "powerquery-data-audit-v1"
AUDIT_HEAD_SCHEMA = "powerquery-data-audit-head-v1"
AUDIT_HEAD_ESTABLISHED_SCHEMA = "powerquery-data-audit-head-established-v1"
BUILD_JOURNAL_SCHEMA = "powerquery-data-build-journal-v1"
CHANGE_SCHEMA = "powerquery-data-change-v1"
DATABASE_SCHEMA = "powerquery-data-database-v1"
MUTATION_SCHEMA = "powerquery-data-mutation-v1"
PUBLISH_SCHEMA = "powerquery-data-publish-v1"
SOURCE_SCHEMA = "powerquery-data-sources-v1"
VERSION_SCHEMA = "powerquery-data-version-v1"
ZERO_HASH = "0" * 64
_AUDIT_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")
_SENSITIVE_AUDIT_KEYS = ("api_key", "apikey", "credential", "password", "secret", "token")
_IO_RETRY_ATTEMPTS = 6
_IO_RETRY_BASE_SECONDS = 0.02
_FILE_LOCK_ATTEMPTS = 100
_FILE_LOCK_RETRY_SECONDS = 0.05
_WINDOWS_RETRYABLE_ERRORS = {5, 32, 33}

ChangeState = Literal["pending_review", "approved", "rejected", "failed"]
ChangeAction = Literal["upload", "remove", "rollback"]


@dataclass(frozen=True)
class DataSlot:
    """One explicitly supported input of the deterministic database builder."""

    name: str
    filename: str
    required_columns: frozenset[str]
    removable: bool = False


DATA_SLOTS: dict[str, DataSlot] = {
    "units_csv": DataSlot("units_csv", "units.csv", frozenset(UNITS_REQUIRED)),
    "daily_csv": DataSlot(
        "daily_csv",
        "daily.csv",
        frozenset({"日期", *DAILY_SYSTEM_COLUMNS}),
    ),
    "crosswalk_csv": DataSlot(
        "crosswalk_csv",
        "crosswalk.csv",
        frozenset(CROSSWALK_REQUIRED),
    ),
    "daily_long_csv": DataSlot(
        "daily_long_csv",
        "daily_long.csv",
        frozenset(DAILY_LONG_REQUIRED),
    ),
    "outage_csv": DataSlot(
        "outage_csv",
        "outage.csv",
        frozenset(OUTAGE_REQUIRED),
        removable=True,
    ),
    "generation_cost_csv": DataSlot(
        "generation_cost_csv",
        "generation_cost.csv",
        frozenset(GENERATION_COST_REQUIRED),
    ),
}


class BuildDatabase(Protocol):
    def __call__(
        self,
        target: Path,
        source_paths: Mapping[str, Path],
    ) -> Mapping[str, Any] | None: ...


class SwitchRuntime(Protocol):
    def __call__(self, database: Path) -> object: ...


class DataManagementError(RuntimeError):
    """Base class for errors safe to map to management API responses."""


class DataManagementValidationError(DataManagementError):
    """An upload, source snapshot, or built database failed validation."""


class DataManagementConflictError(DataManagementError):
    """A pending change was based on a data version that is no longer active."""


class DataManagementNotFoundError(DataManagementError):
    """A requested change, version, or source does not exist."""


class DataManagementStateError(DataManagementError):
    """An operation is invalid for the current review state."""


class SeparationOfDutiesError(DataManagementError):
    """The account that proposed a change may not be the one that publishes it."""


class AuditIntegrityError(DataManagementError):
    """The append-only audit chain is missing, malformed, or has been altered."""


_THREAD_LOCKS_GUARD = Lock()
_THREAD_LOCKS: dict[str, RLock] = {}


def _thread_lock(path: Path) -> RLock:
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, RLock())


def _retryable_io_error(error: OSError) -> bool:
    """Return whether a short-lived filesystem lock may succeed on retry."""

    if isinstance(error, PermissionError):
        return True
    if error.errno in {EACCES, EBUSY, EPERM}:
        return True
    return getattr(error, "winerror", None) in _WINDOWS_RETRYABLE_ERRORS


def _bounded_io(operation):
    """Retry transient Windows/OneDrive sharing violations with a short bound."""

    for attempt in range(_IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except OSError as error:
            if not _retryable_io_error(error) or attempt + 1 == _IO_RETRY_ATTEMPTS:
                raise
            time.sleep(_IO_RETRY_BASE_SECONDS * (2**attempt))
    raise AssertionError("unreachable")


def _replace(source: Path, target: Path) -> None:
    _bounded_io(lambda: os.replace(source, target))


def _unlink(path: Path, *, missing_ok: bool = True) -> None:
    _bounded_io(lambda: path.unlink(missing_ok=missing_ok))


class _FileLock:
    """A one-byte advisory lock shared by Windows and POSIX processes."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> _FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = _bounded_io(lambda: self.path.open("a+b"))
        except OSError as error:
            raise DataManagementStateError("資料管理工作區暫時無法鎖定。") from error
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                lock = partial(msvcrt.locking, handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                lock = partial(fcntl.flock, handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for attempt in range(_FILE_LOCK_ATTEMPTS):
                try:
                    lock()
                    break
                except OSError as error:
                    if attempt + 1 == _FILE_LOCK_ATTEMPTS:
                        raise DataManagementStateError(
                            "資料管理工作區正由另一個程序使用。"
                        ) from error
                    time.sleep(_FILE_LOCK_RETRY_SECONDS)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(_json_value(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temporary_path, path)
    finally:
        with suppress(OSError):
            _unlink(temporary_path)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    temporary_path = Path(temporary_name)
    try:
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        _replace(temporary_path, target)
    finally:
        with suppress(OSError):
            _unlink(temporary_path)


def _atomic_bytes_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temporary_path, target)
    finally:
        with suppress(OSError):
            _unlink(temporary_path)


def _clean_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DataManagementValidationError(f"{field} 必須是字串。")
    cleaned = value.strip()
    if not cleaned:
        raise DataManagementValidationError(f"{field} 不可為空。")
    if len(cleaned) > maximum or any(ord(character) < 32 for character in cleaned):
        raise DataManagementValidationError(f"{field} 格式無效。")
    return cleaned


def _clean_optional_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DataManagementValidationError(f"{field} 必須是字串。")
    cleaned = value.strip()
    if len(cleaned) > maximum or any(ord(character) < 32 for character in cleaned):
        raise DataManagementValidationError(f"{field} 格式無效。")
    return cleaned


def _request_reason(reason: str, note: str | None) -> str:
    selected = _clean_optional_text(reason, field="reason", maximum=500)
    if note is None:
        return selected
    cleaned_note = _clean_optional_text(note, field="note", maximum=500)
    if selected and cleaned_note and selected != cleaned_note:
        raise DataManagementValidationError("reason 與 note 不可同時提供不同內容。")
    return selected or cleaned_note


def _audit_details(value: Any, *, key: str = "") -> Any:
    """Return bounded JSON data with obvious credential fields redacted."""

    normalized_key = key.casefold().replace("-", "_")
    if normalized_key and any(marker in normalized_key for marker in _SENSITIVE_AUDIT_KEYS):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2_000]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(child_key)[:120]: _audit_details(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_audit_details(child) for child in value[:500]]
    return str(value)[:2_000]


class DataManagementService:
    """Stage, review, publish, and inspect immutable PowerQuery data versions."""

    def __init__(
        self,
        *,
        workspace: Path,
        source_paths: Mapping[str, Path],
        initial_database: Path,
        build_database: BuildDatabase,
        switch_runtime: SwitchRuntime | None = None,
        max_upload_bytes: int = 64 * 1024 * 1024,
        allow_self_approval: bool = False,
    ) -> None:
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes 必須大於 0。")
        unknown = set(source_paths) - set(DATA_SLOTS)
        missing = set(DATA_SLOTS) - set(source_paths)
        if unknown or missing:
            raise ValueError(
                f"source_paths 必須精確包含固定資料槽；missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )

        self.workspace = Path(workspace).resolve()
        self._bootstrap_sources = {
            name: Path(path).resolve() for name, path in source_paths.items()
        }
        self._initial_database = Path(initial_database).resolve()
        self._build_database = build_database
        self._switch_runtime = switch_runtime
        self.max_upload_bytes = max_upload_bytes
        self.allow_self_approval = bool(allow_self_approval)
        self.sources_dir = self.workspace / "sources"
        self.databases_dir = self.workspace / "databases"
        self.changes_dir = self.workspace / "changes"
        self.versions_dir = self.workspace / "versions"
        self.active_path = self.workspace / "active.json"
        self.audit_path = self.workspace / "audit.jsonl"
        self.audit_head_path = self.workspace / "audit-head.json"
        self.audit_head_established_path = self.workspace / "audit-head-established.json"
        self.mutation_journal_path = self.workspace / "mutation-journal.json"
        self.publish_journal_path = self.workspace / "publish-journal.json"
        self._lock_path = self.workspace / ".workspace.lock"
        self._thread_lock = _thread_lock(self.workspace)
        self._transaction_depth = 0
        self._recovery_enabled = False

        self.workspace.mkdir(parents=True, exist_ok=True)
        with self._transaction():
            for directory in (
                self.sources_dir,
                self.databases_dir,
                self.changes_dir,
                self.versions_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            self._recover_pending_mutation()
            self._recover_pending_publish()
            if self.active_path.exists():
                validated = self._validate_active()
                self._validate_active_audit(validated["active"], self._read_audit())
            else:
                self._bootstrap()
            self._recovery_enabled = True

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._thread_lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            with _FileLock(self._lock_path):
                self._transaction_depth = 1
                try:
                    if self._recovery_enabled:
                        self._recover_pending_mutation()
                        self._recover_pending_publish()
                    yield
                finally:
                    self._transaction_depth = 0

    def _inside_workspace(self, relative: str) -> Path:
        candidate = (self.workspace / relative).resolve()
        if not candidate.is_relative_to(self.workspace):
            raise DataManagementValidationError("管理檔路徑超出 workspace。")
        return candidate

    def _load_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            raise DataManagementValidationError(f"管理檔無法讀取：{path.name}") from error
        if not isinstance(payload, dict):
            raise DataManagementValidationError(f"管理檔格式無效：{path.name}")
        return payload

    def _source_manifest_path(self, version: str) -> Path:
        return self.sources_dir / version / "manifest.json"

    def _database_path(self, version: str) -> Path:
        return self.databases_dir / f"power-{version}.db"

    def _database_manifest_path(self, version: str) -> Path:
        return self.databases_dir / f"power-{version}.json"

    def _database_build_journal_path(self, version: str) -> Path:
        self._version_path(version)
        return self.databases_dir / f".power-{version}.build.json"

    def _change_path(self, change_id: str) -> Path:
        if not change_id.startswith("change-") or not change_id[7:].isalnum():
            raise DataManagementValidationError("change_id 格式無效。")
        return self.changes_dir / f"{change_id}.json"

    def _version_path(self, version: str) -> Path:
        if not version.startswith("data-") or len(version) != 69:
            raise DataManagementValidationError("data version 格式無效。")
        try:
            int(version[5:], 16)
        except ValueError as error:
            raise DataManagementValidationError("data version 格式無效。") from error
        return self.versions_dir / f"{version}.json"

    def _active(self) -> dict[str, Any]:
        payload = self._load_json(self.active_path)
        if payload.get("schema_version") != ACTIVE_SCHEMA:
            raise DataManagementValidationError("active manifest schema 無效。")
        self._version_path(str(payload.get("version", "")))
        self._inside_workspace(str(payload.get("database", "")))
        return payload

    @staticmethod
    def _version_for_entries(entries: Mapping[str, Mapping[str, Any]]) -> str:
        identity = {
            name: {
                "present": bool(entry["present"]),
                "sha256": entry.get("sha256"),
                "bytes": entry.get("bytes", 0),
            }
            for name, entry in sorted(entries.items())
        }
        return f"data-{_sha256_bytes(_canonical_json(identity))}"

    def _entries_from_paths(self, paths: Mapping[str, Path]) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for name, slot in DATA_SLOTS.items():
            path = paths[name]
            present = path.is_file()
            if not present and not slot.removable:
                raise DataManagementValidationError(f"必填資料檔不存在：{slot.filename}")
            entries[name] = {
                "slot": name,
                "filename": slot.filename,
                "present": present,
                "sha256": _sha256_file(path) if present else None,
                "bytes": path.stat().st_size if present else 0,
                "path": f"files/{slot.filename}" if present else None,
            }
        return entries

    def _materialize_sources(
        self,
        paths: Mapping[str, Path],
        *,
        base_version: str | None,
        action: str,
    ) -> tuple[str, dict[str, Any]]:
        entries = self._entries_from_paths(paths)
        version = self._version_for_entries(entries)
        destination = self.sources_dir / version
        manifest_path = destination / "manifest.json"
        if manifest_path.exists():
            manifest = self._load_json(manifest_path)
            self._verify_sources(version, manifest)
            return version, manifest

        temporary = Path(tempfile.mkdtemp(prefix=".sources-", dir=self.sources_dir))
        try:
            files_dir = temporary / "files"
            files_dir.mkdir()
            for name, entry in entries.items():
                if entry["present"]:
                    _atomic_copy(paths[name], files_dir / DATA_SLOTS[name].filename)
            manifest = {
                "schema_version": SOURCE_SCHEMA,
                "version": version,
                "created_at": _now(),
                "base_version": base_version,
                "action": action,
                "files": entries,
            }
            _atomic_json_write(temporary / "manifest.json", manifest)
            with suppress(FileExistsError):
                _replace(temporary, destination)
                # Another completed immutable snapshot is safe to reuse.  The
                # process lock normally prevents this; this branch also covers
                # filesystems that surface a concurrent directory publish late.
        finally:
            if temporary.exists():
                with suppress(OSError):
                    _bounded_io(partial(shutil.rmtree, temporary))
        manifest = self._load_json(manifest_path)
        self._verify_sources(version, manifest)
        return version, manifest

    def _source_paths(self, version: str) -> dict[str, Path]:
        manifest = self._load_json(self._source_manifest_path(version))
        self._verify_sources(version, manifest)
        root = self.sources_dir / version / "files"
        return {name: root / slot.filename for name, slot in DATA_SLOTS.items()}

    def _verify_sources(self, version: str, manifest: Mapping[str, Any]) -> None:
        if manifest.get("schema_version") != SOURCE_SCHEMA or manifest.get("version") != version:
            raise DataManagementValidationError("來源 manifest 格式無效。")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(DATA_SLOTS):
            raise DataManagementValidationError("來源 manifest 資料槽不完整。")
        root = self.sources_dir / version
        for name, slot in DATA_SLOTS.items():
            entry = files[name]
            if not isinstance(entry, dict):
                raise DataManagementValidationError(f"{name} manifest 格式無效。")
            present = bool(entry.get("present"))
            if not present:
                if not slot.removable:
                    raise DataManagementValidationError(f"必填資料檔缺少：{slot.filename}")
                if entry.get("sha256") is not None or entry.get("path") is not None:
                    raise DataManagementValidationError(f"{name} 的移除 manifest 無效。")
                continue
            relative = entry.get("path")
            if relative != f"files/{slot.filename}":
                raise DataManagementValidationError(f"{name} 的來源路徑無效。")
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise DataManagementValidationError(f"來源檔不存在：{slot.filename}")
            if path.stat().st_size != entry.get("bytes") or _sha256_file(path) != entry.get(
                "sha256"
            ):
                raise DataManagementValidationError(f"來源檔已被篡改：{slot.filename}")
        if self._version_for_entries(files) != version:
            raise DataManagementValidationError("來源版本 checksum 不一致。")

    def _write_database_build_journal(
        self,
        version: str,
        metadata: Mapping[str, Any],
        source_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "schema_version": BUILD_JOURNAL_SCHEMA,
            "data_version": version,
            "created_at": _now(),
            "source_sha256": self._expected_database_source_hashes(source_manifest),
            "database_metadata": _json_value(metadata),
        }
        payload["journal_hash"] = _sha256_bytes(_canonical_json(payload))
        _atomic_json_write(self._database_build_journal_path(version), payload)
        return payload

    def _verified_orphan_database_metadata(
        self,
        version: str,
        target: Path,
        source_manifest: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Recover an orphan only when its pre-rename build proof is intact.

        A checksum freshly calculated from an unknown orphan proves only its
        present bytes.  The build journal was durably written before the atomic
        database rename and binds those bytes to the exact source snapshot.
        """

        journal_path = self._database_build_journal_path(version)
        if not journal_path.is_file():
            return None
        try:
            journal = self._load_json(journal_path)
            recorded_hash = journal.get("journal_hash")
            unsigned = {key: value for key, value in journal.items() if key != "journal_hash"}
            metadata = journal.get("database_metadata")
            if (
                journal.get("schema_version") != BUILD_JOURNAL_SCHEMA
                or journal.get("data_version") != version
                or recorded_hash != _sha256_bytes(_canonical_json(unsigned))
                or journal.get("source_sha256")
                != self._expected_database_source_hashes(source_manifest)
                or not isinstance(metadata, dict)
                or metadata.get("schema_version") != DATABASE_SCHEMA
                or metadata.get("data_version") != version
                or metadata.get("database") != target.relative_to(self.workspace).as_posix()
                or metadata.get("bytes") != target.stat().st_size
                or metadata.get("sha256") != _sha256_file(target)
            ):
                return None
        except (DataManagementError, OSError):
            return None
        recovered = dict(metadata)
        recovered["report"] = {"origin": "recovered_verified_build_journal"}
        return recovered

    def _build_or_reuse_database(self, version: str) -> tuple[Path, dict[str, Any]]:
        target = self._database_path(version)
        metadata_path = self._database_manifest_path(version)
        build_journal_path = self._database_build_journal_path(version)
        source_manifest = self._load_json(self._source_manifest_path(version))
        self._verify_sources(version, source_manifest)
        if target.is_file() and metadata_path.is_file():
            metadata = self._load_json(metadata_path)
            self._verify_database(version, metadata)
            with suppress(OSError):
                _unlink(build_journal_path)
            return target, metadata

        if target.is_file() and not metadata_path.exists():
            metadata = self._verified_orphan_database_metadata(version, target, source_manifest)
            if metadata is not None:
                _atomic_json_write(metadata_path, metadata)
                self._verify_database(version, metadata)
                with suppress(OSError):
                    _unlink(build_journal_path)
                return target, metadata

            # Unknown bytes are never blessed by calculating a fresh checksum.
            # Discard the uncommitted orphan and rebuild from verified sources.
            _unlink(target)
            with suppress(OSError):
                _unlink(build_journal_path)

        if metadata_path.exists() and not target.exists():
            # A sidecar without its immutable payload cannot be trusted.  It is
            # safe to discard and deterministically rebuild from the source set.
            _unlink(metadata_path)
            with suppress(OSError):
                _unlink(build_journal_path)

        temporary = self.databases_dir / f".build-{uuid4().hex}.db"
        try:
            report = self._build_database(temporary, self._source_paths(version)) or {}
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise DataManagementValidationError("建庫 callback 未產生有效 SQLite 檔。")
            digest = _sha256_file(temporary)
            size = temporary.stat().st_size
            metadata = {
                "schema_version": DATABASE_SCHEMA,
                "data_version": version,
                "database": target.relative_to(self.workspace).as_posix(),
                "bytes": size,
                "sha256": digest,
                "built_at": _now(),
                "report": _json_value(report),
            }
            self._write_database_build_journal(version, metadata, source_manifest)
            _replace(temporary, target)
            _atomic_json_write(metadata_path, metadata)
            with suppress(OSError):
                _unlink(build_journal_path)
            return target, metadata
        except DataManagementError:
            raise
        except Exception as error:
            raise DataManagementValidationError("候選資料庫建置失敗。") from error
        finally:
            with suppress(OSError):
                _unlink(temporary)

    def _verify_database(self, version: str, metadata: Mapping[str, Any]) -> Path:
        if metadata.get("schema_version") != DATABASE_SCHEMA:
            raise DataManagementValidationError("資料庫 manifest schema 無效。")
        if metadata.get("data_version") != version:
            raise DataManagementValidationError("資料庫與資料版本不一致。")
        path = self._inside_workspace(str(metadata.get("database", "")))
        if path != self._database_path(version) or not path.is_file():
            raise DataManagementValidationError("版本資料庫不存在。")
        if path.stat().st_size == 0:
            raise DataManagementValidationError("版本資料庫不可為空。")
        if path.stat().st_size != metadata.get("bytes") or _sha256_file(path) != metadata.get(
            "sha256"
        ):
            raise DataManagementValidationError("版本資料庫已被篡改。")
        return path

    @staticmethod
    def _expected_database_source_hashes(source_manifest: Mapping[str, Any]) -> dict[str, str]:
        files = source_manifest.get("files")
        if not isinstance(files, Mapping):
            return {}
        return {
            DATA_SLOTS[name].filename: str(entry["sha256"])
            for name, entry in files.items()
            if isinstance(entry, Mapping) and entry.get("present") and entry.get("sha256")
        }

    def _initial_database_matches_sources(self, source_manifest: Mapping[str, Any]) -> bool:
        """Verify provenance before copying a pre-existing database into the workspace."""

        if not self._initial_database.is_file():
            return False
        expected = self._expected_database_source_hashes(source_manifest)
        if not expected:
            return False
        try:
            uri = f"{self._initial_database.as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                row = connection.execute(
                    "SELECT source_sha256 FROM meta_manifest WHERE id = 1"
                ).fetchone()
            recorded = json.loads(row[0]) if row and isinstance(row[0], str) else None
        except (json.JSONDecodeError, OSError, sqlite3.Error):
            return False
        return isinstance(recorded, dict) and recorded == expected

    def _bootstrap(self) -> None:
        version, source_manifest = self._materialize_sources(
            self._bootstrap_sources,
            base_version=None,
            action="bootstrap",
        )
        database_target = self._database_path(version)
        database_manifest_path = self._database_manifest_path(version)
        if database_target.exists() or database_manifest_path.exists():
            database_target, database_metadata = self._build_or_reuse_database(version)
        elif self._initial_database_matches_sources(source_manifest):
            _atomic_copy(self._initial_database, database_target)
            database_metadata = {
                "schema_version": DATABASE_SCHEMA,
                "data_version": version,
                "database": database_target.relative_to(self.workspace).as_posix(),
                "bytes": database_target.stat().st_size,
                "sha256": _sha256_file(database_target),
                "built_at": _now(),
                "report": {"origin": "bootstrap_copy"},
            }
            _atomic_json_write(database_manifest_path, database_metadata)
        else:
            database_target, database_metadata = self._build_or_reuse_database(version)
        self._verify_database(version, database_metadata)
        version_path = self._version_path(version)
        if version_path.is_file():
            version_record = self._load_json(version_path)
            self._validate_version_record(version, version_record, database_metadata)
            timestamp = str(version_record.get("published_at") or _now())
        else:
            timestamp = _now()
            version_record = {
                "schema_version": VERSION_SCHEMA,
                "version": version,
                "database": database_target.relative_to(self.workspace).as_posix(),
                "database_sha256": database_metadata["sha256"],
                "source_manifest": self._source_manifest_path(version)
                .relative_to(self.workspace)
                .as_posix(),
                "published_at": timestamp,
                "published_by": "system:bootstrap",
                "previous_version": None,
                "change_id": None,
            }
            _atomic_json_write(version_path, version_record)

        events = self._read_audit()
        initialized = any(
            event.get("event") == "workspace_initialized"
            and event.get("details", {}).get("active_version") == version
            for event in events
        )
        if not initialized:
            self._append_audit(
                "workspace_initialized",
                actor="system:bootstrap",
                details={"active_version": version, "source_slots": sorted(DATA_SLOTS)},
            )

        # The pointer is the only commit record and is deliberately written
        # after every immutable artifact and its audit event are durable.
        _atomic_json_write(
            self.active_path,
            {
                "schema_version": ACTIVE_SCHEMA,
                "revision": 1,
                "version": version,
                "database": database_target.relative_to(self.workspace).as_posix(),
                "previous_version": None,
                "activated_at": timestamp,
                "activated_by": "system:bootstrap",
                "change_id": None,
            },
        )

    def _validate_version_record(
        self,
        version: str,
        record: Mapping[str, Any],
        database_metadata: Mapping[str, Any],
    ) -> None:
        expected_database = self._database_path(version).relative_to(self.workspace).as_posix()
        expected_sources = (
            self._source_manifest_path(version).relative_to(self.workspace).as_posix()
        )
        if (
            record.get("schema_version") != VERSION_SCHEMA
            or record.get("version") != version
            or record.get("database") != expected_database
            or record.get("database_sha256") != database_metadata.get("sha256")
            or record.get("source_manifest") != expected_sources
        ):
            raise DataManagementValidationError("data version manifest 與 active 資料不一致。")

    def _validate_active(self) -> dict[str, Any]:
        active = self._active()
        revision = active.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise DataManagementValidationError("active revision 無效。")
        version = str(active["version"])
        source_manifest = self._load_json(self._source_manifest_path(version))
        self._verify_sources(version, source_manifest)
        database_metadata = self._load_json(self._database_manifest_path(version))
        database = self._verify_database(version, database_metadata)
        if database != self._inside_workspace(str(active["database"])):
            raise DataManagementValidationError("active database 指標不一致。")
        version_record = self._load_json(self._version_path(version))
        self._validate_version_record(version, version_record, database_metadata)
        return {
            "active": active,
            "version": version,
            "revision": revision,
            "database": database,
            "database_metadata": database_metadata,
            "source_manifest": source_manifest,
            "version_record": version_record,
        }

    @staticmethod
    def _validate_active_audit(
        active: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        approvals = [event for event in events if event.get("event") == "change_approved"]
        if active.get("revision") != len(approvals) + 1:
            raise AuditIntegrityError("active revision 與稽核發布次數不一致。")
        if approvals:
            details = approvals[-1].get("details")
            if not isinstance(details, Mapping) or (
                details.get("active_version") != active.get("version")
                or details.get("previous_version") != active.get("previous_version")
                or details.get("change_id") != active.get("change_id")
            ):
                raise AuditIntegrityError("active pointer 與最新發布稽核事件不一致。")
            return
        initialized = next(
            (event for event in events if event.get("event") == "workspace_initialized"),
            None,
        )
        details = initialized.get("details") if isinstance(initialized, Mapping) else None
        if (
            not isinstance(details, Mapping)
            or details.get("active_version") != active.get("version")
            or active.get("change_id") is not None
            or active.get("previous_version") is not None
        ):
            raise AuditIntegrityError("bootstrap active pointer 與稽核事件不一致。")

    def _validated_active_and_audit(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fail closed unless the selected data and its audit history agree."""

        validated = self._validate_active()
        events = self._read_audit()
        self._validate_active_audit(validated["active"], events)
        return validated, events

    def _load_mutation_journal(self) -> dict[str, Any]:
        journal = self._load_json(self.mutation_journal_path)
        recorded_hash = journal.get("journal_hash")
        unsigned = {key: value for key, value in journal.items() if key != "journal_hash"}
        next_change = journal.get("next_change")
        audit_event = journal.get("audit_event")
        operation = journal.get("operation")
        expected_hash = journal.get("expected_change_sha256")
        if (
            journal.get("schema_version") != MUTATION_SCHEMA
            or recorded_hash != _sha256_bytes(_canonical_json(unsigned))
            or operation not in {"change_staged", "change_build_failed", "change_rejected"}
            or not isinstance(next_change, dict)
            or next_change.get("schema_version") != CHANGE_SCHEMA
            or next_change.get("id") != journal.get("change_id")
            or not isinstance(audit_event, dict)
            or audit_event.get("event") != operation
            or (expected_hash is not None and not isinstance(expected_hash, str))
            or (
                operation == "change_staged"
                and (next_change.get("status") != "pending_review" or expected_hash is not None)
            )
            or (
                operation == "change_build_failed"
                and (next_change.get("status") != "failed" or expected_hash is not None)
            )
            or (
                operation == "change_rejected"
                and (next_change.get("status") != "rejected" or expected_hash is None)
            )
        ):
            raise DataManagementValidationError("mutation journal 格式或 checksum 無效。")
        self._change_path(str(journal.get("change_id", "")))
        return journal

    def _write_mutation_journal(
        self,
        *,
        operation: str,
        previous_change: Mapping[str, Any] | None,
        next_change: Mapping[str, Any],
        audit_event: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.mutation_journal_path.exists():
            raise DataManagementStateError("尚有資料變更需要恢復。")
        payload = {
            "schema_version": MUTATION_SCHEMA,
            "created_at": _now(),
            "operation": operation,
            "change_id": next_change["id"],
            "expected_change_sha256": (
                _sha256_bytes(_canonical_json(previous_change))
                if previous_change is not None
                else None
            ),
            "next_change": _json_value(next_change),
            "audit_event": _json_value(audit_event),
        }
        payload["journal_hash"] = _sha256_bytes(_canonical_json(payload))
        _atomic_json_write(self.mutation_journal_path, payload)
        return payload

    def _complete_mutation_journal(self, journal: Mapping[str, Any]) -> dict[str, Any]:
        """Idempotently commit one change-record/audit pair."""

        self._validated_active_and_audit()
        change_id = str(journal["change_id"])
        change_path = self._change_path(change_id)
        desired = dict(journal["next_change"])
        expected_hash = journal.get("expected_change_sha256")
        if change_path.is_file():
            current = self._load_json(change_path)
            if _canonical_json(current) != _canonical_json(desired):
                if (
                    expected_hash is None
                    or _sha256_bytes(_canonical_json(current)) != expected_hash
                ):
                    raise DataManagementStateError("mutation journal 與既有變更狀態衝突。")
                _atomic_json_write(change_path, desired)
        else:
            if expected_hash is not None:
                raise DataManagementStateError("mutation journal 的既有變更遺失。")
            _atomic_json_write(change_path, desired)

        self._append_audit_payload(journal["audit_event"])
        # These mutations never advance the active pointer, so its invariant
        # must remain strictly valid after the associated event is durable.
        self._validated_active_and_audit()
        with suppress(OSError):
            _unlink(self.mutation_journal_path)
        return desired

    def _recover_pending_mutation(self) -> None:
        if not self.mutation_journal_path.is_file():
            return
        try:
            self._complete_mutation_journal(self._load_mutation_journal())
        except DataManagementError:
            raise
        except Exception as error:
            raise DataManagementStateError("未完成的資料變更尚無法恢復。") from error

    def _commit_change_mutation(
        self,
        *,
        operation: str,
        actor: str,
        previous_change: Mapping[str, Any] | None,
        next_change: Mapping[str, Any],
        audit_details: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._validated_active_and_audit()
        audit_event = self._new_audit_payload(
            operation,
            actor=actor,
            details=audit_details,
        )
        journal = self._write_mutation_journal(
            operation=operation,
            previous_change=previous_change,
            next_change=next_change,
            audit_event=audit_event,
        )
        return self._complete_mutation_journal(journal)

    def _load_publish_journal(self) -> dict[str, Any]:
        journal = self._load_json(self.publish_journal_path)
        recorded_hash = journal.get("journal_hash")
        unsigned = {key: value for key, value in journal.items() if key != "journal_hash"}
        if (
            journal.get("schema_version") != PUBLISH_SCHEMA
            or recorded_hash != _sha256_bytes(_canonical_json(unsigned))
            or not isinstance(journal.get("base_active"), dict)
            or not isinstance(journal.get("next_active"), dict)
            or not isinstance(journal.get("version_record"), dict)
            or not isinstance(journal.get("approved_change"), dict)
            or not isinstance(journal.get("audit_event"), dict)
            or not isinstance(journal.get("pending_change_sha256"), str)
        ):
            raise DataManagementValidationError("publish journal 格式或 checksum 無效。")
        self._change_path(str(journal.get("change_id", "")))
        return journal

    def _write_publish_journal(self, journal: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(_json_value(journal))
        payload["journal_hash"] = _sha256_bytes(_canonical_json(payload))
        _atomic_json_write(self.publish_journal_path, payload)
        return payload

    @staticmethod
    def _same_active(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return all(
            left.get(key) == right.get(key)
            for key in ("schema_version", "revision", "version", "database")
        )

    def _validate_publish_transition_audit(
        self,
        *,
        base_active: Mapping[str, Any],
        next_active: Mapping[str, Any],
        audit_event: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        """Validate the sole allowed pre-commit transitional audit state."""

        if not events or _canonical_json(events[-1]) != _canonical_json(audit_event):
            raise AuditIntegrityError("publish journal 的發布稽核事件不在鏈結尾端。")
        self._validate_active_audit(base_active, events[:-1])
        self._validate_active_audit(next_active, events)

    def _ensure_publish_records(self, journal: Mapping[str, Any]) -> None:
        next_active = journal["next_active"]
        version = str(next_active["version"])
        database_metadata = self._load_json(self._database_manifest_path(version))
        candidate_database = self._verify_database(version, database_metadata)
        self._verify_sources(version, self._load_json(self._source_manifest_path(version)))
        if candidate_database.relative_to(self.workspace).as_posix() != next_active.get(
            "database"
        ) or database_metadata.get("sha256") != journal.get("database_sha256"):
            raise DataManagementValidationError("publish journal 的候選資料庫不一致。")

        version_record = journal["version_record"]
        self._validate_version_record(version, version_record, database_metadata)
        version_path = self._version_path(version)
        if version_path.is_file():
            self._validate_version_record(
                version,
                self._load_json(version_path),
                database_metadata,
            )
        else:
            _atomic_json_write(version_path, version_record)

        approved_change = journal["approved_change"]
        change_id = str(journal["change_id"])
        if (
            approved_change.get("schema_version") != CHANGE_SCHEMA
            or approved_change.get("id") != change_id
            or approved_change.get("status") != "approved"
            or approved_change.get("candidate_version") != version
        ):
            raise DataManagementValidationError("publish journal 的審核結果無效。")
        change_path = self._change_path(change_id)
        if change_path.is_file():
            current_change = self._load_json(change_path)
            current_is_approved = _canonical_json(current_change) == _canonical_json(
                approved_change
            )
            current_is_expected_pending = current_change.get(
                "status"
            ) == "pending_review" and _sha256_bytes(_canonical_json(current_change)) == journal.get(
                "pending_change_sha256"
            )
            if not current_is_approved and not current_is_expected_pending:
                raise DataManagementStateError("publish journal 與既有審核狀態衝突。")
        _atomic_json_write(change_path, approved_change)
        self._append_audit_payload(journal["audit_event"])

    def _complete_publish_journal(
        self,
        journal: Mapping[str, Any],
        *,
        switch_runtime: bool,
    ) -> dict[str, Any]:
        base_active = journal["base_active"]
        next_active = journal["next_active"]
        validated = self._validate_active()
        active = validated["active"]
        base_selected = self._same_active(active, base_active)
        already_committed = self._same_active(active, next_active)
        if not base_selected and not already_committed:
            raise DataManagementConflictError("publish journal 的基準版本已不再啟用。")

        events = self._read_audit()
        audit_event = journal["audit_event"]
        event_is_durable = any(event.get("id") == audit_event.get("id") for event in events)
        if already_committed:
            self._validate_active_audit(active, events)
        elif event_is_durable:
            self._validate_publish_transition_audit(
                base_active=base_active,
                next_active=next_active,
                audit_event=audit_event,
                events=events,
            )
        else:
            self._validate_active_audit(active, events)

        version = str(next_active["version"])
        database_metadata = self._load_json(self._database_manifest_path(version))
        candidate_database = self._verify_database(version, database_metadata)
        previous_database = self._inside_workspace(str(base_active["database"]))
        switched = False
        try:
            if switch_runtime and self._switch_runtime is not None:
                self._switch_runtime(candidate_database)
                switched = True
            self._ensure_publish_records(journal)
            if base_selected:
                # Re-read and re-verify both sides after runtime switching and
                # record publication.  External tampering during a callback can
                # therefore never reach the active-pointer commit.
                current = self._validate_active()["active"]
                if not self._same_active(current, base_active):
                    raise AuditIntegrityError("active pointer 在發布 commit 前已變更。")
                self._validate_publish_transition_audit(
                    base_active=base_active,
                    next_active=next_active,
                    audit_event=audit_event,
                    events=self._read_audit(),
                )
                # The approval event was appended after the earlier candidate
                # check.  Bind the final commit to the same still-intact source
                # and database bytes, including fault-injected callback writes.
                final_metadata = self._load_json(self._database_manifest_path(version))
                final_database = self._verify_database(version, final_metadata)
                self._verify_sources(
                    version,
                    self._load_json(self._source_manifest_path(version)),
                )
                if final_database.relative_to(self.workspace).as_posix() != next_active.get(
                    "database"
                ) or final_metadata.get("sha256") != journal.get("database_sha256"):
                    raise DataManagementValidationError(
                        "publish journal 在 commit 前的候選資料庫不一致。"
                    )
                # All immutable records and the anchored approval event are
                # durable before this sole commit pointer is replaced.
                _atomic_json_write(self.active_path, next_active)
        except Exception:
            if switched and base_selected and self._switch_runtime is not None:
                with suppress(Exception):
                    self._switch_runtime(previous_database)
            raise

        with suppress(OSError):
            _unlink(self.publish_journal_path)
        return dict(journal["approved_change"])

    def _recover_pending_publish(self, *, switch_runtime: bool | None = None) -> None:
        if not self.publish_journal_path.is_file():
            return
        try:
            journal = self._load_publish_journal()
            should_switch = self._recovery_enabled if switch_runtime is None else switch_runtime
            self._complete_publish_journal(journal, switch_runtime=should_switch)
        except DataManagementError:
            raise
        except Exception as error:
            raise DataManagementStateError("未完成的資料發布尚無法恢復。") from error

    def _audit_head_was_established(self) -> bool:
        if not self.audit_head_established_path.is_file():
            return False
        try:
            marker = self._load_json(self.audit_head_established_path)
        except DataManagementValidationError as error:
            raise AuditIntegrityError("稽核錨點建立標記無法讀取。") from error
        if marker != {
            "schema_version": AUDIT_HEAD_ESTABLISHED_SCHEMA,
            "established": True,
        }:
            raise AuditIntegrityError("稽核錨點建立標記無效。")
        return True

    def _establish_audit_head(self) -> None:
        _atomic_json_write(
            self.audit_head_established_path,
            {
                "schema_version": AUDIT_HEAD_ESTABLISHED_SCHEMA,
                "established": True,
            },
        )

    def _read_audit(self) -> list[dict[str, Any]]:
        audit_exists = self.audit_path.is_file()
        head_exists = self.audit_head_path.is_file()
        head_was_established = self._audit_head_was_established()
        if not audit_exists:
            if head_exists or head_was_established or self.active_path.exists():
                raise AuditIntegrityError("稽核記錄遺失。")
            return []
        events: list[dict[str, Any]] = []
        previous_hash = ZERO_HASH
        try:
            encoded = self.audit_path.read_bytes()
            if encoded and not encoded.endswith(b"\n"):
                raise AuditIntegrityError("稽核記錄尾端已截斷。")
            lines = encoded.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise AuditIntegrityError("稽核記錄不是有效的 UTF-8。") from error
        except OSError as error:
            raise AuditIntegrityError("稽核記錄無法讀取。") from error
        for line_number, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AuditIntegrityError(f"稽核記錄第 {line_number} 列已損壞。") from error
            if not isinstance(event, dict) or event.get("schema_version") != AUDIT_SCHEMA:
                raise AuditIntegrityError(f"稽核記錄第 {line_number} 列 schema 無效。")
            recorded_hash = event.get("event_hash")
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            expected_hash = _sha256_bytes(_canonical_json(unsigned))
            if event.get("previous_hash") != previous_hash or recorded_hash != expected_hash:
                raise AuditIntegrityError(f"稽核記錄第 {line_number} 列 hash chain 無效。")
            previous_hash = str(recorded_hash)
            events.append(event)

        if self.active_path.exists() and not events:
            raise AuditIntegrityError("已啟用的資料工作區不可缺少稽核事件。")

        expected_head = {
            "schema_version": AUDIT_HEAD_SCHEMA,
            "event_count": len(events),
            "event_hash": events[-1]["event_hash"] if events else ZERO_HASH,
        }
        if not head_exists:
            if head_was_established:
                raise AuditIntegrityError("稽核錨點遺失。")
            # Smoothly migrate a valid v1 workspace.  The head is intentionally
            # separate from the log so subsequent loss or tail truncation is
            # detectable even though the retained chain remains self-consistent.
            _atomic_json_write(self.audit_head_path, expected_head)
            self._establish_audit_head()
            return events

        try:
            head = self._load_json(self.audit_head_path)
        except DataManagementValidationError as error:
            raise AuditIntegrityError("稽核錨點無法讀取。") from error
        count = head.get("event_count")
        anchored_hash = head.get("event_hash")
        if (
            head.get("schema_version") != AUDIT_HEAD_SCHEMA
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(anchored_hash, str)
        ):
            raise AuditIntegrityError("稽核錨點格式無效。")
        if len(events) < count:
            raise AuditIntegrityError("稽核記錄已被截短。")
        prefix_hash = events[count - 1]["event_hash"] if count else ZERO_HASH
        if prefix_hash != anchored_hash:
            raise AuditIntegrityError("稽核記錄與錨點不一致。")
        if len(events) > count:
            # A crash can occur after the atomically replaced log is durable but
            # before its head is advanced.  A valid extension of the anchored
            # prefix is safe to finish here.
            _atomic_json_write(self.audit_head_path, expected_head)
        if not head_was_established:
            # One-time v1 migration marker.  Once durable, a missing head is a
            # loss event rather than another migration opportunity.
            self._establish_audit_head()
        return events

    def _new_audit_payload(
        self,
        event: str,
        *,
        actor: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        events = self._read_audit()
        payload = {
            "schema_version": AUDIT_SCHEMA,
            "id": f"audit-{uuid4().hex}",
            "timestamp": _now(),
            "event": event,
            "actor": actor,
            "details": _audit_details(details),
            "previous_hash": events[-1]["event_hash"] if events else ZERO_HASH,
        }
        payload["event_hash"] = _sha256_bytes(_canonical_json(payload))
        return payload

    def _append_audit_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Idempotently append one precomputed event and advance its durable anchor."""

        normalized = dict(_json_value(payload))
        recorded_hash = normalized.get("event_hash")
        unsigned = {key: value for key, value in normalized.items() if key != "event_hash"}
        if (
            normalized.get("schema_version") != AUDIT_SCHEMA
            or not isinstance(normalized.get("id"), str)
            or not str(normalized["id"]).startswith("audit-")
            or recorded_hash != _sha256_bytes(_canonical_json(unsigned))
        ):
            raise AuditIntegrityError("待追加的稽核事件格式無效。")

        events = self._read_audit()
        for existing in events:
            if existing.get("id") == normalized["id"]:
                if _canonical_json(existing) != _canonical_json(normalized):
                    raise AuditIntegrityError("稽核事件識別碼重複且內容不一致。")
                return existing
        previous_hash = events[-1]["event_hash"] if events else ZERO_HASH
        if normalized.get("previous_hash") != previous_hash:
            raise AuditIntegrityError("待追加的稽核事件不是目前鏈結的下一筆。")

        existing_bytes = self.audit_path.read_bytes() if self.audit_path.is_file() else b""
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _atomic_bytes_write(self.audit_path, existing_bytes + encoded + b"\n")
        _atomic_json_write(
            self.audit_head_path,
            {
                "schema_version": AUDIT_HEAD_SCHEMA,
                "event_count": len(events) + 1,
                "event_hash": recorded_hash,
            },
        )
        if not self.audit_head_established_path.is_file():
            self._establish_audit_head()
        return normalized

    def _append_audit(
        self, event: str, *, actor: str, details: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._append_audit_payload(
            self._new_audit_payload(event, actor=actor, details=details)
        )

    def _decode_upload(self, content_base64: str) -> bytes:
        if not isinstance(content_base64, str):
            raise DataManagementValidationError("content_base64 必須是字串。")
        encoded = content_base64.strip()
        if encoded.startswith("data:"):
            marker = ";base64,"
            if marker not in encoded:
                raise DataManagementValidationError("data URI 必須使用 base64。")
            encoded = encoded.split(marker, 1)[1]
        maximum_encoded = ((self.max_upload_bytes + 2) // 3) * 4 + 8
        if len(encoded) > maximum_encoded:
            raise DataManagementValidationError("上傳檔案超過大小上限。")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise DataManagementValidationError("content_base64 格式無效。") from error
        if not payload:
            raise DataManagementValidationError("上傳檔案不可為空。")
        if len(payload) > self.max_upload_bytes:
            raise DataManagementValidationError("上傳檔案超過大小上限。")
        return payload

    @staticmethod
    def _validate_csv(slot: DataSlot, payload: bytes) -> int:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DataManagementValidationError("上傳資料檔必須是 UTF-8 CSV。") from error
        if "\0" in text:
            raise DataManagementValidationError("CSV 不可含 NUL 字元。")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None:
            raise DataManagementValidationError("CSV 缺少標頭。")
        fieldnames = [name.strip() for name in reader.fieldnames]
        if len(fieldnames) != len(set(fieldnames)):
            raise DataManagementValidationError("CSV 標頭不可重複。")
        missing = slot.required_columns - set(fieldnames)
        if missing:
            raise DataManagementValidationError(
                f"{slot.filename} 缺少欄位：{'、'.join(sorted(missing))}"
            )
        row_count = sum(1 for _row in reader)
        if row_count == 0:
            raise DataManagementValidationError("CSV 至少需要一筆資料。")
        return row_count

    def _create_source_candidate(
        self,
        *,
        slot_name: str,
        action: Literal["upload", "remove"],
        payload: bytes | None,
    ) -> tuple[str, dict[str, Any]]:
        active = self._validated_active_and_audit()[0]["active"]
        base_version = str(active["version"])
        current_paths = self._source_paths(base_version)
        temporary = Path(tempfile.mkdtemp(prefix=".candidate-", dir=self.workspace))
        try:
            candidate_paths: dict[str, Path] = {}
            for name, slot in DATA_SLOTS.items():
                candidate = temporary / slot.filename
                source = current_paths[name]
                if source.is_file():
                    _atomic_copy(source, candidate)
                candidate_paths[name] = candidate
            target = candidate_paths[slot_name]
            if action == "upload":
                if payload is None:
                    raise AssertionError("upload payload is required")
                _atomic_bytes_write(target, payload)
            else:
                target.unlink(missing_ok=True)
            version, manifest = self._materialize_sources(
                candidate_paths,
                base_version=base_version,
                action=action,
            )
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        if version == base_version:
            raise DataManagementStateError("資料內容與目前版本相同。")
        return version, manifest

    def _new_change(
        self,
        *,
        action: ChangeAction,
        slot: str | None,
        actor: str,
        candidate_version: str,
        database_metadata: Mapping[str, Any],
        original_filename: str | None = None,
        upload_sha256: str | None = None,
        upload_bytes: int | None = None,
        row_count: int | None = None,
        request_reason: str = "",
    ) -> dict[str, Any]:
        active = self._validated_active_and_audit()[0]["active"]
        identifier = f"change-{uuid4().hex}"
        timestamp = _now()
        change = {
            "schema_version": CHANGE_SCHEMA,
            "id": identifier,
            "action": action,
            "slot": slot,
            "status": "pending_review",
            "base_version": active["version"],
            "candidate_version": candidate_version,
            "candidate_database": database_metadata["database"],
            "database_sha256": database_metadata["sha256"],
            "build_report": database_metadata.get("report", {}),
            "original_filename": original_filename,
            "upload_sha256": upload_sha256,
            "upload_bytes": upload_bytes,
            "row_count": row_count,
            "request_reason": request_reason or None,
            "created_at": timestamp,
            "created_by": actor,
            "reviewed_at": None,
            "reviewed_by": None,
            "review_reason": None,
        }
        return self._commit_change_mutation(
            operation="change_staged",
            actor=actor,
            previous_change=None,
            next_change=change,
            audit_details={
                "change_id": identifier,
                "action": action,
                "slot": slot,
                "base_version": active["version"],
                "candidate_version": candidate_version,
                "upload_sha256": upload_sha256,
                "upload_bytes": upload_bytes,
                "reason": request_reason or None,
            },
        )

    def _new_failed_change(
        self,
        *,
        action: Literal["upload", "remove"],
        slot: str,
        actor: str,
        candidate_version: str,
        request_reason: str,
        error_type: str,
        original_filename: str | None = None,
        upload_sha256: str | None = None,
        upload_bytes: int | None = None,
        row_count: int | None = None,
    ) -> dict[str, Any]:
        active = self._validated_active_and_audit()[0]["active"]
        identifier = f"change-{uuid4().hex}"
        timestamp = _now()
        change = {
            "schema_version": CHANGE_SCHEMA,
            "id": identifier,
            "action": action,
            "slot": slot,
            "status": "failed",
            "base_version": active["version"],
            "candidate_version": candidate_version,
            "candidate_database": None,
            "database_sha256": None,
            "build_report": {},
            "original_filename": original_filename,
            "upload_sha256": upload_sha256,
            "upload_bytes": upload_bytes,
            "row_count": row_count,
            "request_reason": request_reason or None,
            "created_at": timestamp,
            "created_by": actor,
            "reviewed_at": timestamp,
            "reviewed_by": "system:validation",
            "review_reason": "candidate_build_failed",
            "error_type": error_type,
        }
        return self._commit_change_mutation(
            operation="change_build_failed",
            actor=actor,
            previous_change=None,
            next_change=change,
            audit_details={
                "change_id": identifier,
                "action": action,
                "slot": slot,
                "base_version": active["version"],
                "candidate_version": candidate_version,
                "error_type": error_type,
                "reason": request_reason or None,
                "upload_sha256": upload_sha256,
                "upload_bytes": upload_bytes,
            },
        )

    @property
    def active_version(self) -> str:
        with self._transaction():
            return str(self._validated_active_and_audit()[0]["version"])

    def active_snapshot(self) -> dict[str, Any]:
        """Return one fully verified active-data snapshot under the workspace lock."""

        with self._transaction():
            validated = self._validate_active()
            self._validate_active_audit(validated["active"], self._read_audit())
            return {
                "version": validated["version"],
                "revision": validated["revision"],
                "database": validated["database"],
                "database_sha256": validated["database_metadata"]["sha256"],
                "sources": _json_value(validated["source_manifest"]["files"]),
            }

    @property
    def active_database(self) -> Path:
        with self._transaction():
            return self._validated_active_and_audit()[0]["database"]

    def stage_upload(
        self,
        slot: str,
        content_base64: str,
        *,
        actor: str,
        filename: str | None = None,
        reason: str = "",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Validate and build an uploaded CSV without changing the active version."""

        actor = _clean_text(actor, field="actor", maximum=80)
        request_reason = _request_reason(reason, note)
        try:
            definition = DATA_SLOTS[slot]
        except KeyError as error:
            raise DataManagementValidationError("不支援的資料槽。") from error
        original_filename = filename or definition.filename
        original_filename = _clean_text(original_filename, field="filename", maximum=200)
        if Path(original_filename).suffix.casefold() != ".csv":
            raise DataManagementValidationError("只允許上傳 CSV 檔案。")
        payload = self._decode_upload(content_base64)
        row_count = self._validate_csv(definition, payload)
        with self._transaction():
            self._validated_active_and_audit()
            candidate_version, _manifest = self._create_source_candidate(
                slot_name=slot,
                action="upload",
                payload=payload,
            )
            try:
                _database, database_metadata = self._build_or_reuse_database(candidate_version)
            except DataManagementError as error:
                self._new_failed_change(
                    action="upload",
                    slot=slot,
                    actor=actor,
                    candidate_version=candidate_version,
                    request_reason=request_reason,
                    error_type=type(error).__name__,
                    original_filename=Path(original_filename).name,
                    upload_sha256=_sha256_bytes(payload),
                    upload_bytes=len(payload),
                    row_count=row_count,
                )
                raise
            return self._new_change(
                action="upload",
                slot=slot,
                actor=actor,
                candidate_version=candidate_version,
                database_metadata=database_metadata,
                original_filename=Path(original_filename).name,
                upload_sha256=_sha256_bytes(payload),
                upload_bytes=len(payload),
                row_count=row_count,
                request_reason=request_reason,
            )

    def stage_remove(
        self,
        slot: str,
        *,
        actor: str,
        reason: str = "",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Stage removal of an optional source.  Required sources can never be removed."""

        actor = _clean_text(actor, field="actor", maximum=80)
        request_reason = _request_reason(reason, note)
        try:
            definition = DATA_SLOTS[slot]
        except KeyError as error:
            raise DataManagementValidationError("不支援的資料槽。") from error
        if not definition.removable:
            raise DataManagementValidationError(f"{definition.filename} 是必填資料，不可移除。")
        with self._transaction():
            self._validated_active_and_audit()
            if not self._source_paths(self.active_version)[slot].is_file():
                raise DataManagementStateError(f"{definition.filename} 目前已不存在。")
            candidate_version, _manifest = self._create_source_candidate(
                slot_name=slot,
                action="remove",
                payload=None,
            )
            try:
                _database, database_metadata = self._build_or_reuse_database(candidate_version)
            except DataManagementError as error:
                self._new_failed_change(
                    action="remove",
                    slot=slot,
                    actor=actor,
                    candidate_version=candidate_version,
                    request_reason=request_reason,
                    error_type=type(error).__name__,
                )
                raise
            return self._new_change(
                action="remove",
                slot=slot,
                actor=actor,
                candidate_version=candidate_version,
                database_metadata=database_metadata,
                request_reason=request_reason,
            )

    def stage_rollback(
        self,
        version: str,
        *,
        actor: str,
        reason: str = "",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Create a reviewable change that points to a previously published version."""

        actor = _clean_text(actor, field="actor", maximum=80)
        request_reason = _request_reason(reason, note)
        with self._transaction():
            self._validated_active_and_audit()
            if version == self.active_version:
                raise DataManagementStateError("目標版本已在使用中。")
            version_path = self._version_path(version)
            if not version_path.is_file():
                raise DataManagementNotFoundError("找不到指定的已發布版本。")
            self._verify_sources(version, self._load_json(self._source_manifest_path(version)))
            database_metadata = self._load_json(self._database_manifest_path(version))
            self._verify_database(version, database_metadata)
            return self._new_change(
                action="rollback",
                slot=None,
                actor=actor,
                candidate_version=version,
                database_metadata=database_metadata,
                request_reason=request_reason,
            )

    def get_change(self, change_id: str) -> dict[str, Any]:
        with self._transaction():
            self._validated_active_and_audit()
            path = self._change_path(change_id)
            if not path.is_file():
                raise DataManagementNotFoundError("找不到指定的變更。")
            change = self._load_json(path)
            if change.get("schema_version") != CHANGE_SCHEMA:
                raise DataManagementValidationError("變更 manifest schema 無效。")
            return _json_value(change)

    def review(
        self,
        change_id: str,
        *,
        approve: bool,
        reviewer: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Approve or reject one pending change; approval is the publication boundary.

        核准是發布邊界，所以提案人不能核准自己的提案：一個人就能跨過這條邊界時，
        `created_by` 與 `reviewed_by` 兩個欄位記的是同一件事，稽核鏈證明不了任何分工。

        駁回不受這條規則限制 —— 撤回自己的提案不會讓任何東西上線。

        單人部署可以用 `allow_self_approval` 明確放行，但每一筆自審都會在變更紀錄與
        稽核日誌標上 `self_approved`，事後仍查得出哪些發布沒有經過第二個人。
        """

        reviewer = _clean_text(reviewer, field="reviewer", maximum=80)
        reason = _clean_optional_text(reason, field="reason", maximum=500)
        with self._transaction():
            self._validated_active_and_audit()
            change = self.get_change(change_id)
            if change["status"] != "pending_review":
                raise DataManagementStateError("變更已審核，不可重複處理。")
            self_approved = approve and change.get("created_by") == reviewer
            if self_approved and not self.allow_self_approval:
                self._append_audit(
                    "self_approval_refused",
                    actor=reviewer,
                    details={
                        "change_id": change_id,
                        "created_by": change.get("created_by"),
                    },
                )
                raise SeparationOfDutiesError("提案人不可核准自己的變更；請由另一個帳號審核。")
            timestamp = _now()
            if not approve:
                pending_change = dict(change)
                change.update(
                    status="rejected",
                    reviewed_at=timestamp,
                    reviewed_by=reviewer,
                    review_reason=reason or "manual_rejection",
                )
                return self._commit_change_mutation(
                    operation="change_rejected",
                    actor=reviewer,
                    previous_change=pending_change,
                    next_change=change,
                    audit_details={
                        "change_id": change_id,
                        "base_version": change["base_version"],
                        "candidate_version": change["candidate_version"],
                        "reason": change["review_reason"],
                    },
                )

            active = self._validated_active_and_audit()[0]["active"]
            if change["base_version"] != active["version"]:
                self._append_audit(
                    "change_conflict",
                    actor=reviewer,
                    details={
                        "change_id": change_id,
                        "base_version": change["base_version"],
                        "active_version": active["version"],
                    },
                )
                self._validated_active_and_audit()
                raise DataManagementConflictError(
                    "候選變更基於舊版本；請重新上傳或建立 rollback 候選。"
                )

            candidate_version = str(change["candidate_version"])
            self._verify_sources(
                candidate_version,
                self._load_json(self._source_manifest_path(candidate_version)),
            )
            database_metadata = self._load_json(self._database_manifest_path(candidate_version))
            candidate_database = self._verify_database(candidate_version, database_metadata)
            if database_metadata["sha256"] != change["database_sha256"]:
                raise DataManagementValidationError("候選變更的資料庫 checksum 不一致。")

            previous_database = self._inside_workspace(str(active["database"]))
            next_active = {
                "schema_version": ACTIVE_SCHEMA,
                "revision": int(active["revision"]) + 1,
                "version": candidate_version,
                "database": candidate_database.relative_to(self.workspace).as_posix(),
                "previous_version": active["version"],
                "activated_at": timestamp,
                "activated_by": reviewer,
                "change_id": change_id,
            }
            version_record = {
                "schema_version": VERSION_SCHEMA,
                "version": candidate_version,
                "database": candidate_database.relative_to(self.workspace).as_posix(),
                "database_sha256": database_metadata["sha256"],
                "source_manifest": self._source_manifest_path(candidate_version)
                .relative_to(self.workspace)
                .as_posix(),
                "published_at": timestamp,
                "published_by": reviewer,
                "previous_version": active["version"],
                "change_id": change_id,
            }
            approved_change = {
                **change,
                "status": "approved",
                "reviewed_at": timestamp,
                "reviewed_by": reviewer,
                "review_reason": reason or None,
                "self_approved": self_approved,
            }
            audit_event = self._new_audit_payload(
                "change_approved",
                actor=reviewer,
                details={
                    "change_id": change_id,
                    "action": change["action"],
                    "previous_version": active["version"],
                    "active_version": candidate_version,
                    "active_revision": next_active["revision"],
                    "database_sha256": database_metadata["sha256"],
                    "reason": reason or None,
                    # 覆寫過的自審必須留在稽核鏈上，否則等於沒有規則。
                    "self_approved": self_approved,
                },
            )
            if self._switch_runtime is not None:
                try:
                    self._switch_runtime(candidate_database)
                except Exception as error:
                    with suppress(Exception):
                        self._switch_runtime(previous_database)
                    self._validated_active_and_audit()
                    self._append_audit(
                        "change_publish_failed",
                        actor=reviewer,
                        details={
                            "change_id": change_id,
                            "candidate_version": candidate_version,
                            "error_type": type(error).__name__,
                        },
                    )
                    self._validated_active_and_audit()
                    raise DataManagementStateError("執行中的查詢環境無法切換資料庫。") from error
                try:
                    self._validated_active_and_audit()
                except Exception:
                    with suppress(Exception):
                        self._switch_runtime(previous_database)
                    raise
            try:
                self._validated_active_and_audit()
                journal = self._write_publish_journal(
                    {
                        "schema_version": PUBLISH_SCHEMA,
                        "created_at": timestamp,
                        "change_id": change_id,
                        "database_sha256": database_metadata["sha256"],
                        "pending_change_sha256": _sha256_bytes(_canonical_json(change)),
                        "base_active": active,
                        "next_active": next_active,
                        "version_record": version_record,
                        "approved_change": approved_change,
                        "audit_event": audit_event,
                    }
                )
                return self._complete_publish_journal(journal, switch_runtime=False)
            except Exception as error:
                if self._switch_runtime is not None:
                    with suppress(Exception):
                        self._switch_runtime(previous_database)
                raise DataManagementStateError(
                    "資料發布尚未完成；系統會依 publish journal 自動恢復。"
                ) from error

    def list_sources(self, *, version: str | None = None) -> dict[str, Any]:
        with self._transaction():
            active = self._validated_active_and_audit()[0]
            selected = version or str(active["version"])
            manifest_path = self._source_manifest_path(selected)
            if not manifest_path.is_file():
                raise DataManagementNotFoundError("找不到指定的資料版本。")
            manifest = self._load_json(manifest_path)
            self._verify_sources(selected, manifest)
            return _json_value(manifest)

    def source_file(self, slot: str, *, version: str | None = None) -> Path:
        with self._transaction():
            active = self._validated_active_and_audit()[0]
            if slot not in DATA_SLOTS:
                raise DataManagementValidationError("不支援的資料槽。")
            selected = version or str(active["version"])
            paths = self._source_paths(selected)
            path = paths[slot]
            if not path.is_file():
                raise DataManagementNotFoundError("該版本不含指定資料檔。")
            return path

    def list_versions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 必須介於 1 與 500。")
        with self._transaction():
            active = str(self._validated_active_and_audit()[0]["version"])
            records = [
                self._load_json(path)
                for path in self.versions_dir.glob("data-*.json")
                if path.is_file()
            ]
            records.sort(key=lambda item: (str(item.get("published_at", "")), item["version"]))
            return [
                {**_json_value(record), "active": record["version"] == active}
                for record in reversed(records[-limit:])
            ]

    def list_changes(
        self,
        *,
        state: ChangeState | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 必須介於 1 與 500。")
        with self._transaction():
            self._validated_active_and_audit()
            records = [
                self._load_json(path)
                for path in self.changes_dir.glob("change-*.json")
                if path.is_file()
            ]
            if state is not None:
                records = [record for record in records if record.get("status") == state]
            records.sort(key=lambda item: (str(item.get("created_at", "")), item["id"]))
            return [_json_value(record) for record in reversed(records[-limit:])]

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 必須介於 1 與 500。")
        with self._transaction():
            _active, events = self._validated_active_and_audit()
            return [_json_value(event) for event in reversed(events[-limit:])]

    def record_audit(
        self,
        event: str,
        *,
        actor: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an authenticated external management event to the same hash chain.

        This hook is intended for corpus, authentication, and runtime mutations.
        Obvious credential-like mapping keys are redacted before persistence.
        """

        event = event.strip().casefold()
        if not _AUDIT_EVENT_PATTERN.fullmatch(event):
            raise DataManagementValidationError("event 必須是小寫字母、數字或 _:-。")
        actor = _clean_text(actor, field="actor", maximum=80)
        safe_details = _audit_details(details or {})
        if len(_canonical_json(safe_details)) > 64 * 1024:
            raise DataManagementValidationError("audit details 超過 64 KiB 上限。")
        with self._transaction():
            self._validated_active_and_audit()
            result = self._append_audit(event, actor=actor, details=safe_details)
            self._validated_active_and_audit()
            return _json_value(result)

    def status(self) -> dict[str, Any]:
        with self._transaction():
            validated = self._validate_active()
            active = validated["active"]
            sources = self.list_sources(version=str(active["version"]))
            changes = self.list_changes(limit=500)
            audits = self._read_audit()
            self._validate_active_audit(active, audits)
            return {
                "schema_version": "powerquery-data-status-v1",
                "workspace_ready": True,
                "active_version": active["version"],
                "active_revision": active["revision"],
                "active_database": active["database"],
                "sources": sources["files"],
                "supported_slots": {
                    name: {
                        "filename": slot.filename,
                        "removable": slot.removable,
                        "required_columns": sorted(slot.required_columns),
                    }
                    for name, slot in DATA_SLOTS.items()
                },
                "changes": {
                    "total": len(changes),
                    "pending_review": sum(
                        change["status"] == "pending_review" for change in changes
                    ),
                    "approved": sum(change["status"] == "approved" for change in changes),
                    "rejected": sum(change["status"] == "rejected" for change in changes),
                    "failed": sum(change["status"] == "failed" for change in changes),
                },
                "published_versions": len(self.list_versions(limit=500)),
                "runtime_switch_configured": self._switch_runtime is not None,
                "audit_chain_valid": True,
                "audit_events": len(audits),
                "latest_audit_hash": audits[-1]["event_hash"] if audits else ZERO_HASH,
            }


__all__ = [
    "AUDIT_SCHEMA",
    "DATA_SLOTS",
    "ACTIVE_SCHEMA",
    "AuditIntegrityError",
    "BuildDatabase",
    "DataManagementConflictError",
    "DataManagementError",
    "DataManagementNotFoundError",
    "DataManagementService",
    "DataManagementStateError",
    "DataManagementValidationError",
    "DataSlot",
    "SwitchRuntime",
]
