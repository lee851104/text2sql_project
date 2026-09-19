"""Durable, isolated corpus learning with guarded automatic promotion."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import Any, BinaryIO
from weakref import ReferenceType, WeakSet, ref

from eval.promotion_gate import CorpusRegressionGate
from text2sql.corpus import (
    benchmark_questions,
    build_index,
    corpus_checksum,
    load_corpus,
    normalize_question,
)
from text2sql.corpus_builder import CorpusCandidate, deidentify, promote_batch
from text2sql.entities import extract_entities
from text2sql.llm import GeneratedQuery
from text2sql.pipeline import PipelineResponse, Text2SQLPipeline

CANDIDATE_SCHEMA = "corpus-candidates-v1"


class CorpusSelfApprovalError(ValueError):
    """The account whose query produced a candidate may not promote it itself."""


EVENT_SCHEMA = "corpus-events-v1"
WORKSPACE_SCHEMA = "corpus-workspace-v1"
DEFAULT_WORKSPACE_NAME = ".powerquery-learning"

_WORKSPACE_LOCKS_GUARD = Lock()
_WORKSPACE_LOCKS: dict[str, RLock] = {}


def _workspace_thread_lock(path: Path) -> RLock:
    """Share a reentrant lock between service instances in this process."""
    key = os.path.normcase(str(path.resolve()))
    with _WORKSPACE_LOCKS_GUARD:
        return _WORKSPACE_LOCKS.setdefault(key, RLock())


class _WorkspaceFileLock:
    """Advisory single-byte lock that works on both Windows and POSIX."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> _WorkspaceFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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
    """Return a JSON-safe, deidentified value without retaining result rows."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return deidentify(value)
    if isinstance(value, Mapping):
        return {deidentify(str(key)): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return deidentify(str(value))


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _canonical_result_checksum(columns: Sequence[Any], rows: Sequence[Sequence[Any]]) -> str:
    """Hash a result independent of row or column ordering."""
    names = [str(column) for column in columns]
    order = sorted(range(len(names)), key=lambda index: (names[index], index))
    ordered_columns = [names[index] for index in order]
    ordered_rows = [[_json_value(row[index]) for index in order] for row in rows]
    ordered_rows.sort(
        key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    )
    payload = json.dumps(
        {"columns": ordered_columns, "rows": ordered_rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_benchmark_items(paths: Iterable[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path.name} 必須是 JSON array。")
        items.extend(
            item
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("question"), str)
            and isinstance(item.get("intent"), str)
        )
    return items


def _json_roundtrip(value: Any) -> Any:
    """Normalize tuples and other JSON container details for disk comparisons."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class CorpusLearningService:
    """Stage, inspect, review, and publish learned examples beside the runtime DB.

    The version-controlled corpus is copied once as a seed. Every subsequent write
    targets ``workspace`` only. ``observe`` deliberately converts internal failures
    to JSON-safe outcomes so learning can never turn a successful query into an API
    failure.
    """

    def __init__(
        self,
        *,
        database: Path,
        canonical_corpus_path: Path,
        benchmark_paths: Iterable[Path] = (),
        benchmark_dir: Path | None = None,
        pipeline: Text2SQLPipeline | None = None,
        workspace: Path | None = None,
        schema_version: str = "semantic-v1",
        data_manifest_version: str | None = None,
        maximum_retrieval_drop: float = 0.0,
        allow_self_approval: bool = False,
    ):
        if not 0 <= maximum_retrieval_drop <= 1:
            raise ValueError("maximum_retrieval_drop 必須介於 0 與 1。")
        self.allow_self_approval = bool(allow_self_approval)
        self.database = Path(database).resolve()
        self.canonical_corpus_path = Path(canonical_corpus_path).resolve()
        self.workspace = (workspace or self.database.parent / DEFAULT_WORKSPACE_NAME).resolve()
        self.corpus_path = self.workspace / "corpus" / "training_corpus.json"
        self.index_path = self.workspace / "corpus" / "index.json"
        self.versions_dir = self.workspace / "corpus" / "versions"
        self.candidates_path = self.workspace / "candidates.json"
        self.events_path = self.workspace / "events.jsonl"
        self.workspace_manifest_path = self.workspace / "workspace.json"
        self._lock_path = self.workspace / ".workspace.lock"
        self.schema_version = deidentify(schema_version)
        self.data_manifest_version = deidentify(data_manifest_version or self._database_version())
        self.maximum_retrieval_drop = maximum_retrieval_drop
        self._lock = RLock()
        self._workspace_lock = _workspace_thread_lock(self.workspace)
        self._transaction_depth = 0
        self._pipelines: WeakSet[Text2SQLPipeline] = WeakSet()
        self._pipeline_ref: ReferenceType[Text2SQLPipeline] | None = None

        paths = [Path(path).resolve() for path in benchmark_paths]
        if benchmark_dir is not None:
            paths.extend(sorted(Path(benchmark_dir).resolve().glob("*_questions.json")))
        self.benchmark_paths = tuple(dict.fromkeys(paths))
        self._benchmark_questions = benchmark_questions(self.benchmark_paths)
        self._evaluation_items = _load_benchmark_items(self.benchmark_paths)

        self.workspace.mkdir(parents=True, exist_ok=True)
        with self._workspace_transaction():
            self._initialize_workspace()
            self._reconcile_candidates()
        if pipeline is not None:
            self.attach_pipeline(pipeline)

    def _database_version(self) -> str:
        try:
            uri = f"{self.database.as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                row = connection.execute(
                    "SELECT data_version, data_checksum FROM meta_manifest WHERE id = 1"
                ).fetchone()
            if row and row[0] and row[1]:
                return deidentify(f"db-{row[0]}-{str(row[1])[:12]}")
        except (OSError, sqlite3.Error, ValueError):
            # Tests and legacy databases may not yet contain meta_manifest. The
            # stat-based fallback still detects a changed snapshot safely.
            pass
        try:
            stat = self.database.stat()
        except FileNotFoundError:
            return "database-missing"
        payload = f"{stat.st_size}:{stat.st_mtime_ns}".encode()
        return f"db-{hashlib.sha256(payload).hexdigest()[:12]}"

    @contextmanager
    def _workspace_transaction(self) -> Iterator[None]:
        """Serialize all read-modify-write cycles across threads and processes."""
        with self._lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            with self._workspace_lock, _WorkspaceFileLock(self._lock_path):
                self._transaction_depth = 1
                try:
                    yield
                finally:
                    self._transaction_depth = 0

    def _initialize_workspace(self) -> None:
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        if not self.candidates_path.exists():
            _atomic_json_write(
                self.candidates_path,
                {"schema_version": CANDIDATE_SCHEMA, "entries": []},
            )

        canonical = load_corpus(self.canonical_corpus_path)
        canonical_checksum = corpus_checksum(canonical)
        if not self.corpus_path.exists():
            _atomic_json_write(self.corpus_path, canonical)
            self._repair_index(canonical)
            self._save_workspace_manifest(canonical, canonical)
            self._append_event(
                "workspace_initialized",
                canonical_corpus_checksum=canonical_checksum,
                database_manifest_version=self.data_manifest_version,
            )
            return

        corpus = load_corpus(self.corpus_path)
        manifest_exists = self.workspace_manifest_path.exists()
        manifest = self._load_workspace_manifest()
        if manifest is None and manifest_exists:
            self._rebase_workspace(
                canonical,
                corpus,
                previous_manifest=None,
                reason="workspace_manifest_invalid",
            )
            return
        if manifest is None:
            # Upgrade legacy workspaces without discarding already promoted examples.
            self._repair_index(corpus)
            self._save_workspace_manifest(canonical, corpus)
            self._append_event(
                "workspace_manifest_migrated",
                canonical_corpus_checksum=canonical_checksum,
                database_manifest_version=self.data_manifest_version,
            )
            return

        recorded_database_version = manifest.get(
            "database_manifest_version",
            manifest.get("data_manifest_version"),
        )
        baseline_changed = (
            manifest.get("schema_version") != WORKSPACE_SCHEMA
            or manifest.get("canonical_corpus_checksum") != canonical_checksum
            or recorded_database_version != self.data_manifest_version
        )
        if baseline_changed:
            self._rebase_workspace(
                canonical,
                corpus,
                previous_manifest=manifest,
                reason="baseline_changed",
            )
            return
        self._repair_index(corpus)
        self._save_workspace_manifest(
            canonical,
            corpus,
            initialized_at=str(manifest.get("initialized_at") or _now()),
        )

    def _repair_index(self, corpus: Mapping[str, Any]) -> None:
        expected_index = _json_roundtrip(build_index(corpus))
        current_index: dict[str, Any] | None = None
        if self.index_path.exists():
            try:
                current_index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                current_index = None
        if current_index != expected_index:
            _atomic_json_write(self.index_path, expected_index)

    def _load_workspace_manifest(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.workspace_manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    def _save_workspace_manifest(
        self,
        canonical: Mapping[str, Any],
        active: Mapping[str, Any],
        *,
        initialized_at: str | None = None,
    ) -> None:
        timestamp = _now()
        _atomic_json_write(
            self.workspace_manifest_path,
            {
                "schema_version": WORKSPACE_SCHEMA,
                "initialized_at": initialized_at or timestamp,
                "updated_at": timestamp,
                "canonical_corpus_version": canonical["version"],
                "canonical_corpus_checksum": corpus_checksum(dict(canonical)),
                "database_manifest_version": self.data_manifest_version,
                "active_corpus_version": active["version"],
                "active_corpus_checksum": corpus_checksum(dict(active)),
            },
        )

    def _rebase_workspace(
        self,
        canonical: dict[str, Any],
        previous_corpus: dict[str, Any],
        *,
        previous_manifest: Mapping[str, Any] | None,
        reason: str,
    ) -> None:
        previous_checksum = corpus_checksum(previous_corpus)
        backup_path = self.versions_dir / (
            f"{previous_corpus['version']}-{previous_checksum[:12]}.json"
        )
        if not backup_path.exists():
            _atomic_json_write(backup_path, previous_corpus)

        document = self._candidate_document()
        entries: list[dict[str, Any]] = document["entries"]
        pending_count = 0
        timestamp = _now()
        for entry in entries:
            if entry.get("status") != "promoted":
                continue
            entry["status"] = "pending_review"
            entry["reason"] = "baseline_changed_revalidation_required"
            entry["approved_by"] = ""
            entry["promoted_version"] = None
            entry["updated_at"] = timestamp
            validation = entry.setdefault("validation", {})
            validation["baseline_rebase"] = {
                "passed": False,
                "code": "REVALIDATION_REQUIRED",
            }
            pending_count += 1

        _atomic_json_write(self.corpus_path, canonical)
        self._repair_index(canonical)
        self._save_candidates(entries)
        self._save_workspace_manifest(
            canonical,
            canonical,
            initialized_at=(
                str(previous_manifest.get("initialized_at"))
                if previous_manifest and previous_manifest.get("initialized_at")
                else timestamp
            ),
        )
        self._append_event(
            "workspace_rebased",
            reason=reason,
            previous_active_checksum=previous_checksum,
            previous_canonical_corpus_checksum=(
                previous_manifest.get("canonical_corpus_checksum") if previous_manifest else None
            ),
            canonical_corpus_checksum=corpus_checksum(canonical),
            previous_database_manifest_version=(
                previous_manifest.get(
                    "database_manifest_version",
                    previous_manifest.get("data_manifest_version"),
                )
                if previous_manifest
                else None
            ),
            database_manifest_version=self.data_manifest_version,
            candidates_pending_review=pending_count,
            backup_path=backup_path.name,
        )

    def _candidate_document(self) -> dict[str, Any]:
        payload = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != CANDIDATE_SCHEMA:
            raise ValueError("不支援的學習候選資料版本。")
        if not isinstance(payload.get("entries"), list):
            raise ValueError("學習候選 entries 必須是陣列。")
        return payload

    def _save_candidates(self, entries: list[dict[str, Any]]) -> None:
        _atomic_json_write(
            self.candidates_path,
            {"schema_version": CANDIDATE_SCHEMA, "entries": entries},
        )

    def _append_event(self, event: str, **details: Any) -> None:
        payload = {
            "schema_version": EVENT_SCHEMA,
            "at": _now(),
            "event": event,
            **_json_value(details),
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        with self.events_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.seek(0, os.SEEK_END)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _valid_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def _reconcile_candidates(self) -> None:
        document = self._candidate_document()
        entries = document["entries"]
        corpus = load_corpus(self.corpus_path)
        published = {str(example["id"]): str(corpus["version"]) for example in corpus["examples"]}
        changed = False
        for entry in entries:
            # Only recover an interrupted promotion. Reviewed/rebased entries must
            # remain reviewable even if a new canonical corpus happens to share an ID.
            if entry["id"] in published and entry.get("status") == "validating":
                entry["status"] = "promoted"
                entry["promoted_version"] = published[entry["id"]]
                entry["updated_at"] = _now()
                changed = True
        if changed:
            self._save_candidates(entries)

    def attach_pipeline(self, pipeline: Text2SQLPipeline) -> dict[str, Any]:
        """Attach a current runtime and immediately point it at the workspace corpus."""
        with self._workspace_transaction():
            version = pipeline.reload_corpus(self.corpus_path)
            self._pipelines.add(pipeline)
            self._pipeline_ref = ref(pipeline)
            return {"attached": True, "corpus_version": version}

    def _active_pipeline(
        self,
        pipeline: Text2SQLPipeline | None,
    ) -> Text2SQLPipeline:
        selected = pipeline or (self._pipeline_ref() if self._pipeline_ref is not None else None)
        if selected is None:
            raise RuntimeError("尚未附加 Text2SQL pipeline。")
        if selected not in self._pipelines:
            self.attach_pipeline(selected)
        return selected

    @staticmethod
    def _response_payload(response: PipelineResponse | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(response, PipelineResponse):
            return response.to_dict()
        return dict(response)

    @staticmethod
    def _response_source(data: Mapping[str, Any]) -> str:
        source = data.get("source")
        if source in {"router", "llm"}:
            return str(source)
        for step in reversed(data.get("trace", [])):
            if isinstance(step, Mapping) and step.get("source") in {"router", "llm"}:
                return str(step["source"])
        return "unknown"

    def observe(
        self,
        response: PipelineResponse | Mapping[str, Any],
        *,
        pipeline: Text2SQLPipeline | None = None,
        data_provenance: Mapping[str, Any] | None = None,
        proposed_by: str | None = None,
    ) -> dict[str, Any]:
        """Record a query outcome without propagating learning failures to callers."""
        try:
            payload = self._response_payload(response)
            if not payload.get("success") or not isinstance(payload.get("data"), Mapping):
                return {
                    "accepted": False,
                    "status": "ignored",
                    "reason": "query_unsuccessful",
                }
            data = payload["data"]
            if data.get("scope"):
                # 電廠帳號看到的是受限子集，不能當成這個問句的標準答案。
                return {
                    "accepted": False,
                    "status": "ignored",
                    "reason": "plant_scoped_query",
                }
            return self.submit(
                question=str(data["question"]),
                sql=str(data["sql"]),
                params=tuple(data.get("params", ())),
                intent=str(data.get("intent", "other")),
                source=self._response_source(data),
                columns=tuple(data.get("columns", ())),
                rows=tuple(tuple(row) for row in data.get("rows", ())),
                tables=tuple(str(table) for table in data.get("tables", ())),
                data_provenance=data_provenance,
                pipeline=pipeline,
                proposed_by=proposed_by,
            )
        except Exception as error:  # Learning is explicitly fail-open for query delivery.
            try:
                with self._workspace_transaction():
                    self._append_event(
                        "learning_error",
                        error_type=type(error).__name__,
                    )
            except Exception:
                pass
            return {
                "accepted": False,
                "status": "error",
                "reason": "learning_internal_error",
                "error_type": type(error).__name__,
            }

    def submit(
        self,
        *,
        question: str,
        sql: str,
        params: Sequence[object],
        intent: str,
        source: str,
        columns: Sequence[Any],
        rows: Sequence[Sequence[Any]],
        tables: Sequence[str] = (),
        data_provenance: Mapping[str, Any] | None = None,
        pipeline: Text2SQLPipeline | None = None,
        candidate_id: str | None = None,
        proposed_by: str | None = None,
    ) -> dict[str, Any]:
        """Submit a successful result for validation and mandatory human review.

        ``proposed_by`` 是讓這筆候選產生的登入帳號（匿名查詢為 ``None``）。候選本身是
        系統從成功查詢抓下來的，沒有「提案人」欄位可比對，所以要管制「自己讓它進來、
        自己核准」就得先把這件事記下來。
        """
        with self._workspace_transaction():
            active_pipeline = self._active_pipeline(pipeline)
            clean_question = deidentify(question).strip()
            clean_sql = deidentify(sql).strip()
            clean_params = tuple(_json_value(value) for value in params)
            clean_intent = deidentify(intent)
            clean_source = source if source in {"router", "llm"} else "unknown"
            clean_tables = sorted({deidentify(str(table)) for table in tables})
            clean_provenance = _json_value(
                data_provenance
                or {
                    "database_version": self.data_manifest_version,
                    "data_sources": [],
                }
            )
            normalized = normalize_question(clean_question)
            if not normalized:
                raise ValueError("question 不可為空。")
            base_identifier = deidentify(
                candidate_id
                or self._candidate_id(
                    clean_question,
                    clean_sql,
                    clean_params,
                    clean_source,
                    clean_intent,
                )
            )
            document = self._candidate_document()
            entries: list[dict[str, Any]] = document["entries"]
            by_id = next((entry for entry in entries if entry["id"] == base_identifier), None)
            if by_id is not None and by_id["normalized_question"] != normalized:
                raise ValueError("duplicate_candidate_id")
            question_entries = [
                entry for entry in entries if entry["normalized_question"] == normalized
            ]
            promoted = next(
                (entry for entry in reversed(question_entries) if entry["status"] == "promoted"),
                None,
            )
            if promoted is not None:
                self._append_event(
                    "duplicate_candidate",
                    candidate_id=promoted["id"],
                    kind="promoted_question",
                )
                return {**_json_value(promoted), "duplicate": True}

            exact = next(
                (
                    entry
                    for entry in reversed(question_entries)
                    if entry["sql"] == clean_sql
                    and _json_roundtrip(entry.get("params", [])) == list(clean_params)
                    and entry.get("source") == clean_source
                    and entry.get("intent") == clean_intent
                ),
                None,
            )
            if exact is not None:
                self._append_event(
                    "duplicate_candidate",
                    candidate_id=exact["id"],
                    kind="same_revision",
                )
                return {**_json_value(exact), "duplicate": True}

            revision_of = question_entries[-1]["id"] if question_entries else None
            identifier = self._unique_candidate_id(base_identifier, entries)

            timestamp = _now()
            entry: dict[str, Any] = {
                "id": identifier,
                "question": clean_question,
                "normalized_question": normalized,
                "sql": clean_sql,
                "params": list(clean_params),
                "intent": clean_intent,
                "source": clean_source,
                "proposed_by": deidentify(proposed_by).strip() if proposed_by else None,
                "revision_of": revision_of,
                "created_at": timestamp,
                "updated_at": timestamp,
                "schema_version": self.schema_version,
                "data_manifest_version": self.data_manifest_version,
                "tables": clean_tables,
                "data_provenance": clean_provenance,
                "outcome": "success",
                "status": "validating",
                "approved_by": "",
                "review_note": "",
                "result_checksum": _canonical_result_checksum(columns, rows),
                "validation": {},
                "reason": None,
                "promoted_version": None,
            }
            entries.append(entry)
            self._save_candidates(entries)
            self._append_event(
                "candidate_staged",
                candidate_id=identifier,
                source=entry["source"],
                revision_of=revision_of,
            )

            validation, reason = self._validate_entry(entry, active_pipeline)
            entry["validation"] = validation
            entry["updated_at"] = _now()
            if reason:
                duplicate = reason in {"duplicate_id", "duplicate_question"}
                entry["status"] = "ignored" if duplicate else "rejected"
                entry["reason"] = reason
                self._save_candidates(entries)
                self._append_event(
                    "candidate_ignored" if duplicate else "candidate_rejected",
                    candidate_id=identifier,
                    reason=reason,
                )
                return _json_value(entry)

            entry["status"] = "pending_review"
            entry["reason"] = "manual_review_required"
            self._save_candidates(entries)
            self._append_event(
                "candidate_pending_review",
                candidate_id=identifier,
                source=entry["source"],
            )
            return _json_value(entry)

    @staticmethod
    def _candidate_id(
        question: str,
        sql: str,
        params: tuple[Any, ...],
        source: str = "unknown",
        intent: str = "other",
    ) -> str:
        payload = json.dumps(
            [normalize_question(question), sql, list(params), source, intent],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"learned-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _unique_candidate_id(
        base_identifier: str,
        entries: Sequence[Mapping[str, Any]],
    ) -> str:
        identifiers = {str(entry["id"]) for entry in entries}
        if base_identifier not in identifiers:
            return base_identifier
        revision = 2
        while f"{base_identifier}-r{revision}" in identifiers:
            revision += 1
        return f"{base_identifier}-r{revision}"

    def _validate_entry(
        self,
        entry: dict[str, Any],
        pipeline: Text2SQLPipeline,
    ) -> tuple[dict[str, Any], str | None]:
        question = str(entry["question"])
        sql = str(entry["sql"])
        params = tuple(entry["params"])
        validation: dict[str, Any] = {}

        leaked = entry["normalized_question"] in self._benchmark_questions
        validation["benchmark_leakage"] = {
            "passed": not leaked,
            "code": "OK" if not leaked else "BENCHMARK_LEAKAGE",
        }
        if leaked:
            return validation, "benchmark_leakage"

        corpus = load_corpus(self.corpus_path)
        duplicate_id = any(example["id"] == entry["id"] for example in corpus["examples"])
        duplicate_question = any(
            normalize_question(example["question"]) == entry["normalized_question"]
            for example in corpus["examples"]
        )
        validation["duplicates"] = {
            "passed": not (duplicate_id or duplicate_question),
            "duplicate_id": duplicate_id,
            "duplicate_question": duplicate_question,
        }
        if duplicate_id:
            return validation, "duplicate_id"
        if duplicate_question:
            return validation, "duplicate_question"

        sql_decision = pipeline.sql_guard.validate(sql, params)
        validation["sql_guard"] = {
            "passed": sql_decision.allowed,
            "code": sql_decision.code,
            "reason": deidentify(sql_decision.reason),
        }
        if not sql_decision.allowed:
            return validation, f"sql_guard:{sql_decision.code}"

        entities = extract_entities(question)
        question_decision = pipeline.semantic_guard.check_question(question, entities)
        question_passed = question_decision.severity not in {"refuse", "clarify"}
        validation["semantic_question"] = {
            "passed": question_passed,
            "code": question_decision.code,
            "severity": question_decision.severity,
        }
        if not question_passed:
            return validation, f"semantic_question:{question_decision.code}"

        generated = GeneratedQuery(sql, params)
        semantic_decision = pipeline.semantic_guard.check_sql(question, generated, entities)
        semantic_passed = semantic_decision.severity not in {"refuse", "clarify"}
        validation["semantic_sql"] = {
            "passed": semantic_passed,
            "code": semantic_decision.code,
            "severity": semantic_decision.severity,
        }
        if not semantic_passed:
            return validation, f"semantic_sql:{semantic_decision.code}"

        try:
            columns, rows = pipeline.run_sql(sql, params)
            actual_checksum = _canonical_result_checksum(columns, rows)
        except Exception as error:
            validation["result_replay"] = {
                "passed": False,
                "code": f"EXECUTION_ERROR:{type(error).__name__}",
            }
            return validation, "result_replay:execution_error"
        replay_passed = actual_checksum == entry["result_checksum"]
        validation["result_replay"] = {
            "passed": replay_passed,
            "code": "OK" if replay_passed else "RESULT_MISMATCH",
            "expected_checksum": entry["result_checksum"],
            "actual_checksum": actual_checksum,
        }
        if not replay_passed:
            return validation, "result_replay:mismatch"

        proposed = {
            **corpus,
            "examples": [*corpus["examples"], self._example_from_entry(entry)],
        }
        gate = CorpusRegressionGate(
            corpus,
            self._evaluation_items,
            maximum_drop=self.maximum_retrieval_drop,
        )
        regression_passed, metrics = gate(proposed)
        validation["retrieval_regression"] = {
            "passed": regression_passed,
            "metrics": metrics,
        }
        if not regression_passed:
            return validation, "retrieval_regression"
        return validation, None

    @staticmethod
    def _example_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "question": entry["question"],
            "sql": entry["sql"],
            "params": list(entry["params"]),
            "intent": entry["intent"],
            "metadata": {
                "source": entry["source"],
                "created_at": entry["created_at"],
                "approved_by": entry["approved_by"],
                "schema_version": entry["schema_version"],
                "data_manifest_version": entry["data_manifest_version"],
                "outcome": entry["outcome"],
                "result_checksum": entry["result_checksum"],
                "tables": entry.get("tables", []),
                "data_provenance": entry.get("data_provenance", {}),
            },
        }

    def _promote_entry(
        self,
        entry: dict[str, Any],
        entries: list[dict[str, Any]],
        pipeline: Text2SQLPipeline,
    ) -> None:
        candidate = CorpusCandidate(
            id=entry["id"],
            question=entry["question"],
            sql=entry["sql"],
            params=tuple(entry["params"]),
            source=entry["source"],
            created_at=entry["created_at"],
            approved_by=entry["approved_by"],
            schema_version=entry["schema_version"],
            data_manifest_version=entry["data_manifest_version"],
            outcome=entry["outcome"],
            result_checksum=entry["result_checksum"],
            tables=tuple(entry.get("tables", ())),
            data_provenance=dict(entry.get("data_provenance", {})),
            validation={"intent": entry["intent"], **entry["validation"]},
        )
        corpus = load_corpus(self.corpus_path)
        regression_gate = CorpusRegressionGate(
            corpus,
            self._evaluation_items,
            maximum_drop=self.maximum_retrieval_drop,
        )

        def passed(*_args: Any) -> tuple[bool, str]:
            return True, ""

        result = promote_batch(
            [candidate],
            corpus_path=self.corpus_path,
            index_path=self.index_path,
            versions_dir=self.versions_dir,
            benchmark_question_set=self._benchmark_questions,
            sql_validator=passed,
            semantic_validator=passed,
            result_validator=passed,
            regression_gate=regression_gate,
        )
        entry["updated_at"] = _now()
        if not result.promoted:
            entry["status"] = "rejected"
            entry["reason"] = (
                result.rejected[0]["reason"] if result.rejected else "promotion_failed"
            )
            self._save_candidates(entries)
            self._append_event(
                "candidate_rejected",
                candidate_id=entry["id"],
                reason=entry["reason"],
            )
            return

        entry["status"] = "promoted"
        entry["reason"] = None
        entry["promoted_version"] = result.version
        entry["validation"]["promotion"] = {
            "passed": True,
            "metrics": result.metrics,
        }
        self._save_candidates(entries)
        manifest = self._load_workspace_manifest() or {}
        self._save_workspace_manifest(
            load_corpus(self.canonical_corpus_path),
            load_corpus(self.corpus_path),
            initialized_at=str(manifest.get("initialized_at") or _now()),
        )
        reload_errors: list[str] = []
        for attached in tuple(self._pipelines | {pipeline}):
            try:
                attached.reload_corpus(self.corpus_path)
            except Exception as error:
                reload_errors.append(type(error).__name__)
        self._append_event(
            "candidate_promoted",
            candidate_id=entry["id"],
            corpus_version=result.version,
            reviewer=entry.get("approved_by", ""),
            note=entry.get("review_note", ""),
            reload_errors=reload_errors,
        )

    def review(
        self,
        candidate_id: str,
        *,
        approve: bool,
        reviewer: str,
        note: str = "",
        pipeline: Text2SQLPipeline | None = None,
    ) -> dict[str, Any]:
        """Approve or reject any validated candidate after human review."""
        reviewer = deidentify(reviewer).strip()
        if not reviewer:
            raise ValueError("reviewer 不可為空。")
        note = deidentify(note).strip()
        with self._workspace_transaction():
            active_pipeline = self._active_pipeline(pipeline)
            document = self._candidate_document()
            entries: list[dict[str, Any]] = document["entries"]
            entry = next((item for item in entries if item["id"] == candidate_id), None)
            if entry is None:
                raise KeyError(candidate_id)
            if entry["status"] != "pending_review":
                raise ValueError("candidate_not_pending_review")
            proposed_by = entry.get("proposed_by")
            if approve and proposed_by and proposed_by == reviewer and not self.allow_self_approval:
                # 匿名查詢記為 None：那不是一個身分，兩個不同的訪客都會長一樣，拿來
                # 比對只會擋到不相干的人，也擋不住真的想繞的人（登出、問、再登入）。
                # 因此這條規則只在「提出的人有帳號」時成立，這是它已知的邊界。
                self._append_event(
                    "candidate_self_approval_refused",
                    candidate_id=candidate_id,
                    reviewer=reviewer,
                )
                raise CorpusSelfApprovalError("這筆候選由你自己的查詢產生，請由另一個帳號審核。")
            entry["approved_by"] = reviewer
            entry["review_note"] = note
            entry["updated_at"] = _now()
            if not approve:
                entry["status"] = "rejected"
                entry["reason"] = "manual_rejection"
                self._save_candidates(entries)
                self._append_event(
                    "candidate_rejected",
                    candidate_id=candidate_id,
                    reason="manual_rejection",
                    reviewer=reviewer,
                    note=note,
                )
                return _json_value(entry)

            validation, reason = self._validate_entry(entry, active_pipeline)
            entry["validation"] = validation
            if reason:
                entry["status"] = "rejected"
                entry["reason"] = reason
                self._save_candidates(entries)
                self._append_event("candidate_rejected", candidate_id=candidate_id, reason=reason)
                return _json_value(entry)
            self._promote_entry(entry, entries, active_pipeline)
            return _json_value(entry)

    def status(self) -> dict[str, Any]:
        """Return real, non-secret learning and publication state."""
        with self._workspace_transaction():
            entries = self._candidate_document()["entries"]
            states = Counter(str(entry["status"]) for entry in entries)
            corpus = load_corpus(self.corpus_path)
            expected_index = _json_roundtrip(build_index(corpus))
            try:
                index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                index = None
            manifest = self._load_workspace_manifest()
            events = self._valid_events()
            latest_event = events[-1] if events else None
            return {
                "schema_version": "corpus-learning-status-v1",
                "workspace_ready": self.workspace.is_dir(),
                "corpus_version": corpus["version"],
                "corpus_checksum": corpus_checksum(corpus),
                "published_examples": len(corpus["examples"]),
                "index_synchronized": index == expected_index,
                "candidate_counts": {
                    "total": len(entries),
                    "validating": states["validating"],
                    "pending_review": states["pending_review"],
                    "promoted": states["promoted"],
                    "rejected": states["rejected"],
                    "ignored": states["ignored"],
                },
                "policy": {
                    "auto_promote_source": None,
                    "manual_review_required": True,
                    "router_requires_review": True,
                    "llm_requires_review": True,
                    "maximum_retrieval_drop": self.maximum_retrieval_drop,
                },
                "attached_pipelines": len(self._pipelines),
                "workspace_identity": _json_value(manifest),
                "latest_event": _json_value(latest_event),
            }

    def list_entries(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List sanitized candidates; query result rows are never persisted or returned."""
        if not 1 <= limit <= 500:
            raise ValueError("limit 必須介於 1 與 500。")
        with self._workspace_transaction():
            entries = self._candidate_document()["entries"]
            selected = [entry for entry in entries if state is None or entry["status"] == state]
            return [_json_value(entry) for entry in reversed(selected[-limit:])]

    def list_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 必須介於 1 與 500。")
        with self._workspace_transaction():
            events = self._valid_events()
            return [_json_value(event) for event in reversed(events[-limit:])]
