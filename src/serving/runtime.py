"""Construct and safely switch query runtimes for offline and online use."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

import yaml

from ingest.validate import PROJECT_ROOT
from text2sql.corpus import column_value_conflicts, column_values
from text2sql.db import ReadOnlySQLite
from text2sql.llm import DisabledLLM, OpenAILLM
from text2sql.pipeline import Text2SQLPipeline
from text2sql.scope_guard import ScopeGuard
from text2sql.semantic_guard import SemanticGuard
from text2sql.sql_guard import SqlGuard

RuntimeMode = Literal["offline", "online", "auto"]
ActiveRuntimeMode = Literal["offline", "online"]
RuntimeSource = Literal["offline", "memory", "environment", "injected"]

_UNSET = object()


@dataclass(frozen=True)
class ServiceRuntime:
    pipeline: Text2SQLPipeline = field(repr=False)
    executor: ReadOnlySQLite = field(repr=False)
    database: Path
    data_range: tuple[str, str]
    peak_columns: set[str]
    plants: set[str]
    sites: set[str]
    online_llm: bool
    root: Path = PROJECT_ROOT
    mode: ActiveRuntimeMode = "offline"
    model: str = "gpt-5.4-mini"
    source: RuntimeSource = "offline"
    provider: str = "openai"
    # 這個 runtime 的憑證來自哪個環境變數。跟著 runtime 走而不是每次重讀設定，
    # 是因為 RuntimeManager 要靠它偵測 key 被換掉或移除 —— 認錯變數名的話，
    # 設了 GMI_API_KEY 的人會因為 OPENAI_API_KEY 是空的而被清掉線上 runtime。
    api_key_env: str = "OPENAI_API_KEY"
    requested_mode: RuntimeMode = "auto"
    credential_fingerprint: bytes | None = field(default=None, repr=False)


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
    if value not in {"offline", "online", "auto"}:
        raise ValueError("mode 必須是 offline、online 或 auto。")
    return cast(RuntimeMode, value)


def _clean_model(
    model: str | None,
    llm_config: dict[str, Any],
    *,
    use_environment: bool,
) -> str:
    if model is not None:
        configured = model
    elif use_environment:
        environment_model = os.getenv(str(llm_config.get("model_env", "OPENAI_MODEL")))
        configured = (
            environment_model
            if environment_model and environment_model.strip()
            else str(llm_config.get("default_model", "gpt-5.4-mini"))
        )
    else:
        configured = str(llm_config.get("default_model", "gpt-5.4-mini"))
    configured = configured.strip()
    if not configured:
        raise ValueError("model 不可為空。")
    return configured


def _clean_api_key(api_key: str | None) -> str | None:
    if api_key is None:
        return None
    if not isinstance(api_key, str):
        raise TypeError("api_key 必須是字串或 None。")
    return api_key.strip() or None


def _credential(api_key: str | None, api_key_env: str) -> tuple[str | None, RuntimeSource]:
    explicit = _clean_api_key(api_key)
    if explicit:
        return explicit, "memory"
    environment = os.getenv(api_key_env, "").strip()
    if environment:
        return environment, "environment"
    return None, "offline"


def _provider_settings(llm_config: dict[str, Any], provider: str) -> dict[str, Any]:
    """Merge the chosen provider's block over the top-level defaults.

    `providers` 整段缺席時回到頂層設定，讓舊的 llm.yaml 照樣跑得起來；
    但 `providers` 存在而指定的 provider 不在裡面，就是設定打錯了，直接失敗 ——
    默默退回 openai 會讓人以為自己在打 GMI，帳單和結果都對不上。
    """

    providers = llm_config.get("providers")
    if not isinstance(providers, dict):
        return dict(llm_config)
    block = providers.get(provider)
    if not isinstance(block, dict):
        raise ValueError(f"不支援的線上 provider：{provider}；可用的是 {sorted(providers)}。")
    return {**llm_config, **block}


def build_runtime(
    *,
    database: Path | None = None,
    root: Path = PROJECT_ROOT,
    mode: RuntimeMode = "auto",
    model: str | None = None,
    api_key: str | None = None,
) -> ServiceRuntime:
    """Build one immutable runtime without retaining a supplied API key.

    ``auto`` uses the online adapter only when a key is available. ``offline``
    always uses deterministic routing, even if the process has an API key.
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

    # provider 決定去哪裡拿 key、拿哪個環境變數，所以要先解析它才問得出憑證。
    provider = str(llm_config.get("provider", "openai"))
    settings = _provider_settings(llm_config, provider)
    api_key_env = str(settings.get("api_key_env", "OPENAI_API_KEY"))

    key, key_source = _credential(api_key, api_key_env)
    active_mode: ActiveRuntimeMode = (
        "online" if requested_mode == "online" or (requested_mode == "auto" and key) else "offline"
    )
    if active_mode == "online" and key is None:
        raise RuntimeError(f"線上模式需要 API key；請在記憶體設定或使用 {api_key_env}。")
    selected_model = _clean_model(
        model,
        settings,
        use_environment=requested_mode != "offline",
    )

    request_timeout = float(llm_config.get("request_timeout_seconds", 30))
    temperature = settings.get("temperature")
    llm = (
        OpenAILLM(
            model=selected_model,
            api_key=key,
            timeout_seconds=request_timeout,
            base_url=(str(settings["base_url"]) if settings.get("base_url") else None),
            api=str(settings.get("api", "responses")),
            structured_output=bool(settings.get("structured_output", True)),
            temperature=(None if temperature is None else float(temperature)),
            api_key_env=api_key_env,
            model_env=str(settings.get("model_env", "OPENAI_MODEL")),
            default_model=str(settings.get("default_model", "gpt-5.4-mini")),
        )
        if active_mode == "online"
        # 離線時也要指名這個 provider 讀的是哪個變數，不然使用者會被指去設 OPENAI_API_KEY。
        else DisabledLLM(api_key_env=api_key_env)
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
        max_attempts=int(llm_config["max_attempts"]),
        top_k=int(retriever_config["top_k"]),
        ngram_min=int(ngram["min"]),
        ngram_max=int(ngram["max"]),
        min_score=float(retriever_config.get("min_score", 0.0)),
        relative_score=float(retriever_config.get("relative_score", 0.0)),
    )
    source: RuntimeSource = key_source if active_mode == "online" else "offline"
    return ServiceRuntime(
        pipeline=pipeline,
        executor=executor,
        database=database,
        data_range=semantic_guard.data_range,
        peak_columns=peak_columns,
        plants=plants,
        sites=sites,
        online_llm=active_mode == "online",
        root=root,
        mode=active_mode,
        model=selected_model,
        source=source,
        provider=provider,
        api_key_env=api_key_env,
        requested_mode=requested_mode,
        credential_fingerprint=(
            hashlib.sha256(key.encode("utf-8")).digest()
            if active_mode == "online" and key is not None
            else None
        ),
    )


class RuntimeManager:
    """Thread-safe owner of runtime mode and memory-only online credentials."""

    __slots__ = (
        "_active_runtime",
        "_api_key",
        "_database",
        "_default_mode",
        "_lock",
        "_model",
        "_offline_runtime",
        "_online_runtime",
        "_online_signature",
        "_provider",
        "_root",
        "_api_key_env",
    )

    def __init__(
        self,
        runtime: ServiceRuntime | None = None,
        *,
        database: Path | None = None,
        root: Path = PROJECT_ROOT,
        default_mode: RuntimeMode | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._lock = RLock()
        self._api_key = _clean_api_key(api_key)

        if runtime is not None:
            if database is not None:
                raise ValueError("injected runtime 不可與 database 同時指定。")
            if model is not None or api_key is not None:
                raise ValueError("injected runtime 不可同時覆寫 model 或 api_key。")
            # A caller-supplied online runtime already owns its configured client,
            # while its credential is intentionally absent from ServiceRuntime.
            # Mark it as injected so the manager does not mistake that absence for
            # a removed environment credential and discard the usable adapter.
            if runtime.mode == "online" and runtime.source == "memory" and self._api_key is None:
                runtime = replace(runtime, source="injected")
            self._active_runtime = runtime
            self._database = runtime.database
            self._root = runtime.root.resolve()
            self._model = runtime.model.strip()
            self._provider = runtime.provider
            self._api_key_env = runtime.api_key_env
            self._default_mode = _normalise_mode(default_mode or runtime.requested_mode)
            self._offline_runtime = runtime if runtime.mode == "offline" else None
            self._online_runtime = runtime if runtime.mode == "online" else None
            self._online_signature = (
                (runtime.model, runtime.credential_fingerprint)
                if runtime.mode == "online"
                and runtime.source != "injected"
                and runtime.credential_fingerprint is not None
                else None
            )
            return

        selected_default = _normalise_mode(default_mode or "auto")
        initial = build_runtime(
            database=database,
            root=root,
            mode=selected_default,
            model=model,
            api_key=self._api_key,
        )
        self._active_runtime = initial
        self._database = initial.database
        self._root = initial.root
        self._model = initial.model
        self._provider = initial.provider
        self._api_key_env = initial.api_key_env
        self._default_mode = selected_default
        self._offline_runtime = initial if initial.mode == "offline" else None
        self._online_runtime = initial if initial.mode == "online" else None
        self._online_signature = (
            (initial.model, initial.credential_fingerprint)
            if initial.mode == "online" and initial.credential_fingerprint is not None
            else None
        )

    def __repr__(self) -> str:
        with self._lock:
            return (
                "RuntimeManager("
                f"default_mode={self._default_mode!r}, "
                f"active_mode={self._active_runtime.mode!r}, "
                f"model={self._model!r}, online_configured={self._online_configured()!r})"
            )

    @staticmethod
    def _signature(model: str, api_key: str) -> tuple[str, bytes]:
        return model, hashlib.sha256(api_key.encode("utf-8")).digest()

    def _current_credential(self) -> tuple[str | None, RuntimeSource]:
        if self._api_key:
            return self._api_key, "memory"
        return _credential(None, self._api_key_env)

    def _drop_stale_online_runtime(self) -> None:
        """Release cached clients as soon as their credential is no longer current."""
        cached = self._online_runtime
        if cached is None or cached.source == "injected":
            return
        key, _source = self._current_credential()
        signature = self._signature(self._model, key) if key else None
        if key is not None and signature == self._online_signature:
            return

        self._online_runtime = None
        self._online_signature = None
        if self._active_runtime is cached:
            if self._offline_runtime is None or self._offline_runtime.model != self._model:
                self._offline_runtime = self._build(
                    "offline",
                    model=self._model,
                    api_key=None,
                )
            self._active_runtime = self._offline_runtime

    def _online_configured(self) -> bool:
        self._drop_stale_online_runtime()
        key, _source = self._current_credential()
        injected = self._online_runtime is not None and self._online_runtime.source == "injected"
        return bool(key or injected)

    @property
    def active_runtime(self) -> ServiceRuntime:
        with self._lock:
            return self._active_runtime

    @property
    def default_mode(self) -> RuntimeMode:
        with self._lock:
            return self._default_mode

    def status(self) -> dict[str, object]:
        """Return UI-safe mode metadata; credentials are deliberately omitted."""

        with self._lock:
            self._drop_stale_online_runtime()
            return {
                "default_mode": self._default_mode,
                "active_mode": self._active_runtime.mode,
                "online_configured": self._online_configured(),
                "provider": self._provider,
                "model": self._model,
                "source": self._active_runtime.source,
                "offline_capability": True,
            }

    def _build(
        self,
        mode: RuntimeMode,
        *,
        model: str,
        api_key: str | None,
        source: RuntimeSource = "offline",
    ) -> ServiceRuntime:
        return build_runtime(
            database=self._database,
            root=self._root,
            mode=mode,
            model=model,
            api_key=api_key if source == "memory" else None,
        )

    def get_runtime(self, mode: RuntimeMode | None = None) -> ServiceRuntime:
        """Select a runtime for one query without changing the configured default."""

        with self._lock:
            self._drop_stale_online_runtime()
            requested = _normalise_mode(mode or self._default_mode)
            key, source = self._current_credential()
            effective: ActiveRuntimeMode = (
                "online"
                if requested == "online" or (requested == "auto" and self._online_configured())
                else "offline"
            )

            if effective == "offline":
                candidate = self._offline_runtime
                if candidate is None or candidate.model != self._model:
                    candidate = self._build("offline", model=self._model, api_key=None)
                    self._offline_runtime = candidate
            else:
                if key is None:
                    if self._online_runtime is None or self._online_runtime.source != "injected":
                        raise RuntimeError("線上模式尚未設定 API key。")
                    candidate = self._online_runtime
                else:
                    signature = self._signature(self._model, key)
                    candidate = self._online_runtime
                    if candidate is None or self._online_signature != signature:
                        candidate = self._build(
                            "online", model=self._model, api_key=key, source=source
                        )
                        self._online_runtime = candidate
                        self._online_signature = (
                            candidate.model,
                            candidate.credential_fingerprint,
                        )

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
        api_key: str | None | object = _UNSET,
    ) -> ServiceRuntime:
        """Atomically change defaults; a failed build leaves all prior state intact."""

        with self._lock:
            self._drop_stale_online_runtime()
            candidate_mode = _normalise_mode(mode or self._default_mode)
            candidate_model = self._model if model is _UNSET else str(model or "").strip()
            if not candidate_model:
                raise ValueError("model 不可為空。")
            candidate_key = (
                self._api_key if api_key is _UNSET else _clean_api_key(cast(str | None, api_key))
            )

            reusable_injected = (
                self._online_runtime
                if api_key is _UNSET
                and candidate_key is None
                and candidate_mode in {"online", "auto"}
                and self._online_runtime is not None
                and self._online_runtime.source == "injected"
                and candidate_model == self._model
                else None
            )
            # Build before mutating manager state. This is the rollback boundary.
            # A caller-injected adapter has no recoverable credential by design, so
            # unchanged online/auto settings reuse that already-built client.
            candidate = reusable_injected or build_runtime(
                database=self._database,
                root=self._root,
                mode=candidate_mode,
                model=candidate_model,
                api_key=candidate_key,
            )

            settings_changed = (
                candidate_model != self._model
                or candidate_key != self._api_key
                or api_key is not _UNSET
            )
            self._default_mode = candidate_mode
            self._model = candidate_model
            self._api_key = candidate_key
            self._provider = candidate.provider
            if settings_changed:
                self._offline_runtime = None
                self._online_runtime = None
                self._online_signature = None
            if candidate.mode == "offline":
                self._offline_runtime = candidate
            else:
                self._online_runtime = candidate
                self._online_signature = (
                    (candidate.model, candidate.credential_fingerprint)
                    if candidate.credential_fingerprint is not None
                    else None
                )
            self._active_runtime = candidate
            return candidate

    def configure_and_status(
        self,
        *,
        mode: RuntimeMode | None = None,
        model: str | None | object = _UNSET,
        api_key: str | None | object = _UNSET,
    ) -> dict[str, object]:
        """Configure the manager and return the resulting state under one lock."""
        with self._lock:
            self.configure(mode=mode, model=model, api_key=api_key)
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

            self._drop_stale_online_runtime()
            key, _source = self._current_credential()
            if (
                key is None
                and self._active_runtime.mode == "online"
                and self._active_runtime.source == "injected"
            ):
                raise RuntimeError("注入式線上執行環境沒有可重建的憑證，無法切換資料庫。")

            # This is the rollback boundary: build_runtime opens and validates the
            # new semantic layer before the active pointer or caches are mutated.
            candidate = build_runtime(
                database=target,
                root=self._root,
                mode=self._default_mode,
                model=self._model,
                api_key=self._api_key,
            )

            self._database = target
            self._offline_runtime = candidate if candidate.mode == "offline" else None
            self._online_runtime = candidate if candidate.mode == "online" else None
            self._online_signature = (
                (candidate.model, candidate.credential_fingerprint)
                if candidate.mode == "online" and candidate.credential_fingerprint is not None
                else None
            )
            self._provider = candidate.provider
            self._active_runtime = candidate
            return candidate

    def switch_database_and_status(self, database: Path) -> dict[str, object]:
        """Switch the active snapshot and return a UI-safe state summary."""

        with self._lock:
            runtime = self.switch_database(database)
            return {**self.status(), "database": runtime.database.name}
