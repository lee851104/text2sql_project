"""Construct and safely switch query runtimes: offline rules or a local LLM."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

import yaml

from ingest.realtime.config import load_config as load_realtime_config
from ingest.validate import PROJECT_ROOT
from text2sql.corpus import column_value_conflicts, column_values
from text2sql.db import ReadOnlySQLite
from text2sql.llm import DisabledLLM, LocalLLM
from text2sql.pipeline import Text2SQLPipeline
from text2sql.realtime_query import RealtimeQueryEngine
from text2sql.scope_guard import ScopeGuard
from text2sql.semantic_guard import SemanticGuard
from text2sql.sql_guard import SqlGuard

RuntimeMode = Literal["offline", "local", "auto"]
ActiveRuntimeMode = Literal["offline", "local"]
Probe = Callable[[str], bool]

_UNSET = object()
LOGGER = logging.getLogger(__name__)
PROVIDER_ENV = "POWERQUERY_LLM_PROVIDER"
# auto 模式多久重新確認一次 Ollama 在不在。每個查詢都問一次，Ollama 沒開時
# 連規則接得住的查詢也要多等一個連線逾時。
PROBE_TTL_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class ServiceRuntime:
    pipeline: Text2SQLPipeline = field(repr=False)
    executor: ReadOnlySQLite = field(repr=False)
    database: Path
    data_range: tuple[str, str]
    peak_columns: set[str]
    plants: set[str]
    sites: set[str]
    local_llm: bool
    root: Path = PROJECT_ROOT
    mode: ActiveRuntimeMode = "offline"
    model: str = "qwen3:8b"
    provider: str = "ollama"
    base_url: str | None = None
    requested_mode: RuntimeMode = "auto"
    # 呼叫端自己帶進來的 runtime（例如測試的 FakeLLM）。它的 LLM 不能從設定重建，
    # 換資料庫時不能默默換成真的 Ollama。
    injected: bool = False


def _yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必須是 YAML mapping。")
    return payload


def _optional_scope_guard(database: Path) -> ScopeGuard | None:
    """Load the authorisation roster, tolerating a snapshot built before schema 3.

    A database without the scope tables still serves the all-plants scope; plant accounts
    then fail closed in the pipeline rather than silently reading every plant's rows.
    """
    try:
        return ScopeGuard.from_database(database)
    except sqlite3.OperationalError:
        return None


def _single_column(executor: ReadOnlySQLite, sql: str) -> set[str]:
    _columns, rows = executor.execute(sql, ())
    return {str(row[0]) for row in rows}


def _normalise_mode(mode: str) -> RuntimeMode:
    value = mode.strip().lower()
    if value not in {"offline", "local", "auto"}:
        raise ValueError("mode 必須是 offline、local 或 auto。")
    return cast(RuntimeMode, value)


def _clean_model(model: str | None, settings: dict[str, Any]) -> str:
    if model is not None:
        configured = model
    else:
        environment_model = os.getenv(str(settings.get("model_env", "OLLAMA_MODEL")))
        configured = (
            environment_model
            if environment_model and environment_model.strip()
            else str(settings.get("default_model", "qwen3:8b"))
        )
    configured = configured.strip()
    if not configured:
        raise ValueError("model 不可為空。")
    return configured


def _provider_settings(llm_config: dict[str, Any], provider: str) -> dict[str, Any]:
    """Merge the chosen provider's block over the top-level defaults.

    指定的 provider 不在 `providers` 裡就是設定打錯了，直接失敗 ——
    默默換成另一種模式會讓人以為自己開了推理，評測結果也對不上。
    """

    providers = llm_config.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("configs/llm.yaml 缺少 providers。")
    block = providers.get(provider)
    if not isinstance(block, dict):
        raise ValueError(f"不支援的本機 LLM provider：{provider}；可用的是 {sorted(providers)}。")
    return {**llm_config, **block}


def _selected_provider(llm_config: dict[str, Any]) -> str:
    """`POWERQUERY_LLM_PROVIDER` wins over llm.yaml, so switching modes needs no file edit."""

    override = os.getenv(PROVIDER_ENV, "").strip()
    return override or str(llm_config.get("provider", "ollama"))


def probe_local_llm(base_url: str, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Is an Ollama server answering at ``base_url``? Only asks for the model list."""

    try:
        # base_url 來自 llm.yaml，不是使用者輸入。
        with urllib.request.urlopen(  # noqa: S310
            base_url.rstrip("/") + "/models", timeout=timeout_seconds
        ) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _realtime_engine(
    root: Path, guard_config: dict[str, Any], clock: Callable[[], datetime] | None
) -> RealtimeQueryEngine | None:
    """The realtime branch; a broken realtime config only disables realtime questions."""

    try:
        config = load_realtime_config(root)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        LOGGER.exception("即時收集設定讀取失敗，即時問句停用")
        return None
    return RealtimeQueryEngine(
        config,
        max_rows=int(guard_config["sql"]["max_rows"]),
        timeout_seconds=float(guard_config["sql"]["timeout_seconds"]),
        clock=clock,
    )


def build_runtime(
    *,
    database: Path | None = None,
    root: Path = PROJECT_ROOT,
    mode: RuntimeMode = "auto",
    model: str | None = None,
    realtime_clock: Callable[[], datetime] | None = None,
    request_timeout_seconds: float | None = None,
    extra_body: dict[str, Any] | None = None,
    probe: Probe | None = None,
    retrieval_top_k: int | None = None,
) -> ServiceRuntime:
    """Build one immutable runtime.

    ``offline`` always uses deterministic routing. ``local`` always hands the long tail
    to Ollama, even when it is down, so the user sees the real failure. ``auto`` uses
    Ollama only when it answers right now.
    """

    requested_mode = _normalise_mode(mode)
    root = root.resolve()
    config = _yaml(root / "configs/config.yaml")
    guard_config = _yaml(root / "configs/guard.yaml")
    llm_config = _yaml(root / "configs/llm.yaml")
    retriever_config = _yaml(root / "configs/retriever.yaml")

    database = (database or root / config["paths"]["database"]).resolve()
    timeout = float(guard_config["sql"]["timeout_seconds"])
    executor = ReadOnlySQLite(database, timeout_seconds=timeout)
    peak_columns = _single_column(executor, 'SELECT DISTINCT "機組欄位" FROM v_peak LIMIT 200')
    plants = _single_column(executor, 'SELECT DISTINCT "電廠" FROM v_unit LIMIT 200')
    sites = _single_column(executor, 'SELECT DISTINCT "發電站" FROM v_re_generation LIMIT 200')
    semantic_guard = SemanticGuard.from_database(database, peak_columns=peak_columns)
    scope_guard = _optional_scope_guard(database)

    provider = _selected_provider(llm_config)
    settings = _provider_settings(llm_config, provider)
    base_url = str(settings.get("base_url") or "").strip()
    if not base_url:
        raise ValueError(f"本機 LLM provider {provider} 缺少 base_url。")
    if requested_mode == "auto":
        available = (probe or probe_local_llm)(base_url)
        active_mode: ActiveRuntimeMode = "local" if available else "offline"
    else:
        active_mode = requested_mode
    selected_model = _clean_model(model, settings)

    # 本機模型在一般筆電上生成一句 SQL 要十幾秒到幾分鐘，逾時跟著模式走。
    request_timeout = (
        float(request_timeout_seconds)
        if request_timeout_seconds is not None
        else float(settings.get("request_timeout_seconds", 120))
    )
    temperature = settings.get("temperature")
    llm = (
        LocalLLM(
            base_url=base_url,
            model=selected_model,
            timeout_seconds=request_timeout,
            structured_output=bool(settings.get("structured_output", True)),
            temperature=(None if temperature is None else float(temperature)),
            # 評測可以覆寫 provider 的 extra_body（例如暫時把推理打開），不必改 llm.yaml。
            extra_body=(extra_body if extra_body is not None else settings.get("extra_body"))
            or None,
            model_env=str(settings.get("model_env", "OLLAMA_MODEL")),
            default_model=str(settings.get("default_model", "qwen3:8b")),
        )
        if active_mode == "local"
        else DisabledLLM()
    )
    ngram = retriever_config["character_ngram"]
    sql_guard = SqlGuard(max_rows=int(guard_config["sql"]["max_rows"]))
    view_values = column_values(executor.execute, sql_guard.allowed_columns)
    pipeline = Text2SQLPipeline(
        llm=llm,
        sql_guard=sql_guard,
        run_sql=executor.execute,
        # 封閉集合欄位的值跟著 allowlist 走：守門准查的欄位，才有必要讓模型知道值長什麼樣。
        column_values=view_values,
        column_value_conflicts=column_value_conflicts(view_values),
        corpus_path=root / "corpus/training_corpus.json",
        data_range=semantic_guard.data_range,
        peak_columns=peak_columns,
        plants=plants,
        sites=sites,
        semantic_guard=semantic_guard,
        scope_guard=scope_guard,
        realtime=_realtime_engine(root, guard_config, realtime_clock),
        max_attempts=int(llm_config["max_attempts"]),
        # 評測可以把檢索關掉（0）單獨量 RAG 的效果，不必改 retriever.yaml。
        top_k=int(retriever_config["top_k"]) if retrieval_top_k is None else retrieval_top_k,
        ngram_min=int(ngram["min"]),
        ngram_max=int(ngram["max"]),
        min_score=float(retriever_config.get("min_score", 0.0)),
        relative_score=float(retriever_config.get("relative_score", 0.0)),
    )
    return ServiceRuntime(
        pipeline=pipeline,
        executor=executor,
        database=database,
        data_range=semantic_guard.data_range,
        peak_columns=peak_columns,
        plants=plants,
        sites=sites,
        local_llm=active_mode == "local",
        root=root,
        mode=active_mode,
        model=selected_model,
        provider=provider,
        base_url=base_url,
        requested_mode=requested_mode,
    )


class RuntimeManager:
    """Thread-safe owner of the runtime mode: offline rules or a local LLM."""

    __slots__ = (
        "_active_runtime",
        "_base_url",
        "_database",
        "_default_mode",
        "_local_runtime",
        "_lock",
        "_model",
        "_offline_runtime",
        "_probe",
        "_probe_checked_at",
        "_probe_result",
        "_provider",
        "_root",
    )

    def __init__(
        self,
        runtime: ServiceRuntime | None = None,
        *,
        database: Path | None = None,
        root: Path = PROJECT_ROOT,
        default_mode: RuntimeMode | None = None,
        model: str | None = None,
        probe: Probe | None = None,
    ) -> None:
        self._lock = RLock()
        self._probe = probe or probe_local_llm
        self._probe_checked_at: float | None = None
        self._probe_result = False

        if runtime is not None:
            if database is not None:
                raise ValueError("injected runtime 不可與 database 同時指定。")
            if model is not None:
                raise ValueError("injected runtime 不可同時覆寫 model。")
            # 呼叫端帶進來的 LLM（例如 FakeLLM）不能從設定重建：標成 injected，
            # 之後一律沿用它，而不是換成真的 Ollama。
            if runtime.mode == "local" and not runtime.injected:
                runtime = replace(runtime, injected=True)
            self._active_runtime = runtime
            self._database = runtime.database
            self._root = runtime.root.resolve()
            self._model = runtime.model.strip()
            self._provider = runtime.provider
            self._base_url = runtime.base_url
            self._default_mode = _normalise_mode(default_mode or runtime.requested_mode)
            self._offline_runtime = runtime if runtime.mode == "offline" else None
            self._local_runtime = runtime if runtime.mode == "local" else None
            if runtime.mode == "local":
                # 注入的本機 runtime 不是真的 Ollama，探測它沒有意義，一律當作可用。
                self._probe_checked_at = float("inf")
                self._probe_result = True
            return

        selected_default = _normalise_mode(default_mode or "auto")
        initial = build_runtime(
            database=database,
            root=root,
            mode=selected_default,
            model=model,
            probe=self._remember_probe,
        )
        self._active_runtime = initial
        self._database = initial.database
        self._root = initial.root
        self._model = initial.model
        self._provider = initial.provider
        self._base_url = initial.base_url
        self._default_mode = selected_default
        self._offline_runtime = initial if initial.mode == "offline" else None
        self._local_runtime = initial if initial.mode == "local" else None

    def __repr__(self) -> str:
        with self._lock:
            return (
                "RuntimeManager("
                f"default_mode={self._default_mode!r}, "
                f"active_mode={self._active_runtime.mode!r}, "
                f"model={self._model!r})"
            )

    def _remember_probe(self, base_url: str) -> bool:
        self._probe_result = bool(self._probe(base_url))
        self._probe_checked_at = time.monotonic()
        return self._probe_result

    def _local_available(self) -> bool:
        checked = self._probe_checked_at
        if checked is not None and time.monotonic() - checked < PROBE_TTL_SECONDS:
            return self._probe_result
        if not self._base_url:
            return False
        return self._remember_probe(self._base_url)

    @property
    def active_runtime(self) -> ServiceRuntime:
        with self._lock:
            return self._active_runtime

    @property
    def default_mode(self) -> RuntimeMode:
        with self._lock:
            return self._default_mode

    def status(self) -> dict[str, object]:
        """Return UI-safe mode metadata."""

        with self._lock:
            return {
                "default_mode": self._default_mode,
                "active_mode": self._active_runtime.mode,
                "local_available": self._local_available(),
                "provider": self._provider,
                "model": self._model,
                "base_url": self._base_url,
                "offline_capability": True,
            }

    def _build(self, mode: ActiveRuntimeMode) -> ServiceRuntime:
        return build_runtime(
            database=self._database,
            root=self._root,
            mode=mode,
            model=self._model,
        )

    def _effective(self, requested: RuntimeMode) -> ActiveRuntimeMode:
        if requested == "local" or (requested == "auto" and self._local_available()):
            return "local"
        return "offline"

    def get_runtime(self, mode: RuntimeMode | None = None) -> ServiceRuntime:
        """Select a runtime for one query without changing the configured default."""

        with self._lock:
            effective = self._effective(_normalise_mode(mode or self._default_mode))
            if effective == "offline":
                candidate = self._offline_runtime
                if candidate is None or candidate.model != self._model:
                    candidate = self._build("offline")
                    self._offline_runtime = candidate
            else:
                candidate = self._local_runtime
                if candidate is None or (not candidate.injected and candidate.model != self._model):
                    candidate = self._build("local")
                    self._local_runtime = candidate
            self._active_runtime = candidate
            return candidate

    def get_runtime_for_database(
        self,
        database: Path,
        mode: RuntimeMode | None = None,
    ) -> ServiceRuntime:
        """Pin one request to ``database`` and select its mode atomically.

        Data-version publication and per-request mode selection are separate
        operations.  Holding the manager lock across both prevents a concurrent
        publisher from changing the process-wide runtime between those two steps.
        The returned :class:`ServiceRuntime` remains an immutable request snapshot
        even when a later request switches the manager to another database.
        """

        with self._lock:
            self.switch_database(database)
            return self.get_runtime(mode)

    def runtime_and_status(
        self,
        mode: RuntimeMode | None = None,
    ) -> tuple[ServiceRuntime, dict[str, object]]:
        """Select a runtime and report the same locked state snapshot."""
        with self._lock:
            runtime = self.get_runtime(mode)
            return runtime, self.status()

    def configure(
        self,
        *,
        mode: RuntimeMode | None = None,
        model: str | None | object = _UNSET,
    ) -> ServiceRuntime:
        """Atomically change defaults; a failed build leaves all prior state intact."""

        with self._lock:
            candidate_mode = _normalise_mode(mode or self._default_mode)
            candidate_model = self._model if model is _UNSET else str(model or "").strip()
            if not candidate_model:
                raise ValueError("model 不可為空。")
            if candidate_mode == "auto" and self._probe_checked_at != float("inf"):
                # 使用者剛按下套用，就該拿當下的狀態，而不是三十秒前的快取。
                self._probe_checked_at = None
            effective = self._effective(candidate_mode)
            reusable_injected = (
                self._local_runtime
                if effective == "local"
                and self._local_runtime is not None
                and self._local_runtime.injected
                and candidate_model == self._model
                else None
            )
            # Build before mutating manager state. This is the rollback boundary.
            candidate = reusable_injected or build_runtime(
                database=self._database,
                root=self._root,
                mode=effective,
                model=candidate_model,
            )

            if candidate_model != self._model:
                self._offline_runtime = None
                self._local_runtime = None
            self._default_mode = candidate_mode
            self._model = candidate_model
            self._provider = candidate.provider
            self._base_url = candidate.base_url
            if candidate.mode == "offline":
                self._offline_runtime = candidate
            else:
                self._local_runtime = candidate
            self._active_runtime = candidate
            return candidate

    def configure_and_status(
        self,
        *,
        mode: RuntimeMode | None = None,
        model: str | None | object = _UNSET,
    ) -> dict[str, object]:
        """Configure the manager and return the resulting state under one lock."""
        with self._lock:
            self.configure(mode=mode, model=model)
            return self.status()

    def switch_database(self, database: Path) -> ServiceRuntime:
        """Atomically move future queries to a newly built database snapshot.

        A complete runtime is constructed before any manager state changes. Existing
        requests retain their immutable ``ServiceRuntime`` and can therefore finish
        against the previous SQLite file while later requests use the new version.
        """

        target = Path(database).resolve()
        with self._lock:
            if target == self._database:
                return self._active_runtime
            if self._active_runtime.injected:
                raise RuntimeError("注入式本機 LLM 執行環境無法從設定重建，不能切換資料庫。")

            # This is the rollback boundary: build_runtime opens and validates the
            # new semantic layer before the active pointer or caches are mutated.
            candidate = build_runtime(
                database=target,
                root=self._root,
                mode=self._active_runtime.mode,
                model=self._model,
            )

            self._database = target
            self._offline_runtime = candidate if candidate.mode == "offline" else None
            self._local_runtime = candidate if candidate.mode == "local" else None
            self._provider = candidate.provider
            self._base_url = candidate.base_url
            self._active_runtime = candidate
            return candidate

    def switch_database_and_status(self, database: Path) -> dict[str, object]:
        """Switch the active snapshot and return a UI-safe state summary."""

        with self._lock:
            runtime = self.switch_database(database)
            return {**self.status(), "database": runtime.database.name}
