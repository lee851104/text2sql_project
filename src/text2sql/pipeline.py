"""Guarded Text2SQL orchestration with deterministic routing and bounded retries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Any, Protocol

from text2sql.corpus import load_corpus
from text2sql.entities import Entities, extract_entities
from text2sql.llm import GeneratedQuery, LLMProtocol, parse_generated_query
from text2sql.prompt import build_prompt
from text2sql.retriever import TfidfRetriever
from text2sql.router import route
from text2sql.scope_guard import ScopeError, ScopeGuard
from text2sql.semantic_guard import SemanticDecision
from text2sql.sql_guard import SqlGuard, SqlGuardResult

RunSql = Callable[[str, tuple[object, ...]], tuple[list[str], list[tuple[object, ...]]]]


class SemanticGuardProtocol(Protocol):
    def check_question(self, question: str, entities: Entities) -> SemanticDecision: ...

    def check_sql(
        self, question: str, query: GeneratedQuery, entities: Entities
    ) -> SemanticDecision: ...


class AllowAllSemanticGuard:
    def check_question(self, question: str, entities: Entities) -> SemanticDecision:
        return SemanticDecision()

    def check_sql(
        self, question: str, query: GeneratedQuery, entities: Entities
    ) -> SemanticDecision:
        return SemanticDecision()


@dataclass(frozen=True)
class PipelineResponse:
    success: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    severity: str | None = None
    suggestions: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Text2SQLPipeline:
    def __init__(
        self,
        *,
        llm: LLMProtocol,
        sql_guard: SqlGuard,
        run_sql: RunSql,
        corpus_path: Any,
        data_range: tuple[str, str],
        peak_columns: set[str],
        plants: set[str] | None = None,
        semantic_guard: SemanticGuardProtocol | None = None,
        scope_guard: ScopeGuard | None = None,
        max_attempts: int = 3,
        top_k: int = 5,
        ngram_min: int = 2,
        ngram_max: int = 4,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts 必須至少是 1")
        self.llm = llm
        self.sql_guard = sql_guard
        self.run_sql = run_sql
        self.corpus_path = Path(corpus_path)
        self._ngram_min = ngram_min
        self._ngram_max = ngram_max
        self._corpus_lock = RLock()
        self.corpus = load_corpus(self.corpus_path)
        self.retriever = TfidfRetriever(
            self.corpus["examples"], minimum=ngram_min, maximum=ngram_max
        )
        self.data_range = data_range
        self.peak_columns = peak_columns
        self.plants = plants or set()
        self.semantic_guard = semantic_guard or AllowAllSemanticGuard()
        self.scope_guard = scope_guard
        self.max_attempts = max_attempts
        self.top_k = top_k

    def reload_corpus(self, corpus_path: Path | None = None) -> str:
        """Atomically replace the retrieval snapshot used by subsequent queries."""
        next_path = Path(corpus_path) if corpus_path is not None else self.corpus_path
        corpus = load_corpus(next_path)
        retriever = TfidfRetriever(
            corpus["examples"],
            minimum=self._ngram_min,
            maximum=self._ngram_max,
        )
        with self._corpus_lock:
            self.corpus_path = next_path
            self.corpus = corpus
            self.retriever = retriever
        return str(corpus["version"])

    @staticmethod
    def _trace(trace: list[dict[str, Any]], stage: str, started: float, **details: Any) -> None:
        trace.append(
            {
                "stage": stage,
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                **details,
            }
        )

    def _apply_scope(
        self, generated: GeneratedQuery, plant: str | None
    ) -> tuple[str, tuple[object, ...]]:
        """Restrict validated SQL to one plant's rows, or pass it through unchanged."""
        if plant is None:
            return generated.sql, generated.params
        if self.scope_guard is None:
            raise ScopeError("此服務未載入授權對照，無法提供電廠帳號查詢。")
        return self.scope_guard.apply(generated.sql, generated.params, plant=plant)

    @staticmethod
    def _semantic_error(
        decision: SemanticDecision, trace: list[dict[str, Any]]
    ) -> PipelineResponse:
        return PipelineResponse(
            False,
            data={"trace": trace},
            error_code=decision.code,
            error=decision.reason,
            severity=decision.severity,
            suggestions=decision.suggestions,
            evidence=decision.evidence,
        )

    def query(self, question: str, *, plant: str | None = None) -> PipelineResponse:
        """Answer ``question``; ``plant`` restricts the result to that plant's own rows."""
        trace: list[dict[str, Any]] = []
        with self._corpus_lock:
            corpus = self.corpus
            retriever = self.retriever
        started = perf_counter()
        entities = extract_entities(question)
        self._trace(trace, "entities", started)

        started = perf_counter()
        question_decision = self.semantic_guard.check_question(question, entities)
        self._trace(trace, "semantic_question_guard", started, code=question_decision.code)
        if question_decision.severity in {"refuse", "clarify"}:
            return self._semantic_error(question_decision, trace)

        started = perf_counter()
        routed = route(
            question,
            entities,
            peak_columns=self.peak_columns,
            plants=self.plants,
            data_range=self.data_range,
        )
        self._trace(trace, "route", started, intent=routed.intent, matched=bool(routed.sql))
        retrieved = []
        if not routed.sql:
            started = perf_counter()
            retrieved = retriever.retrieve(question, top_k=self.top_k)
            self._trace(
                trace,
                "retrieve",
                started,
                example_ids=[item.example["id"] for item in retrieved],
            )

        prior_error: str | None = None
        attempts = 1 if routed.sql else self.max_attempts
        for attempt in range(1, attempts + 1):
            source = "router" if routed.sql else "llm"
            if routed.sql:
                generated = GeneratedQuery(routed.sql, routed.params)
            else:
                started = perf_counter()
                prompt = build_prompt(
                    question,
                    corpus=corpus,
                    examples=retrieved,
                    data_range=self.data_range,
                    prior_error=prior_error,
                )
                try:
                    generated = parse_generated_query(self.llm.generate(prompt))
                except Exception as error:  # Adapter errors become bounded pipeline errors.
                    prior_error = f"LLM_OUTPUT_ERROR: {type(error).__name__}"
                    self._trace(trace, "generate", started, attempt=attempt, error=prior_error)
                    if type(error).__name__ in {"AuthenticationError", "PermissionDeniedError"}:
                        return PipelineResponse(
                            False,
                            data={"trace": trace},
                            error_code="LLM_AUTH_FAILED",
                            error="OpenAI API key 驗證失敗，請到 API 設定更新金鑰。",
                            severity="error",
                            evidence={"attempts": attempt, "last_error": prior_error},
                        )
                    continue
                self._trace(trace, "generate", started, attempt=attempt)

            started = perf_counter()
            guard_result: SqlGuardResult = self.sql_guard.validate(generated.sql, generated.params)
            self._trace(
                trace,
                "sql_guard",
                started,
                attempt=attempt,
                source=source,
                code=guard_result.code,
            )
            if not guard_result.allowed:
                prior_error = f"{guard_result.code}: {guard_result.reason}"
                continue

            started = perf_counter()
            semantic = self.semantic_guard.check_sql(question, generated, entities)
            self._trace(trace, "semantic_sql_guard", started, attempt=attempt, code=semantic.code)
            if semantic.severity in {"refuse", "clarify"}:
                return self._semantic_error(semantic, trace)

            started = perf_counter()
            try:
                execute_sql, execute_params = self._apply_scope(generated, plant)
            except ScopeError as error:
                self._trace(trace, "scope_guard", started, attempt=attempt, error=str(error))
                return PipelineResponse(
                    False,
                    data={"trace": trace},
                    error_code="SCOPE_DENIED",
                    error=str(error),
                    severity="refuse",
                    evidence={"plant": plant},
                )
            self._trace(trace, "scope_guard", started, attempt=attempt, plant=plant)

            started = perf_counter()
            try:
                columns, rows = self.run_sql(execute_sql, execute_params)
            except Exception as error:
                prior_error = f"SQL_EXECUTION_ERROR: {type(error).__name__}: {error}"
                self._trace(trace, "execute", started, attempt=attempt, error=prior_error)
                continue
            self._trace(trace, "execute", started, attempt=attempt, record_count=len(rows))
            disclosures = []
            disclosed_codes: set[str] = set()
            for decision in (question_decision, semantic):
                if decision.severity == "disclose" and decision.code not in disclosed_codes:
                    disclosures.append(
                        {
                            "code": decision.code,
                            "reason": decision.reason,
                            "evidence": decision.evidence,
                        }
                    )
                    disclosed_codes.add(decision.code)
            return PipelineResponse(
                True,
                data={
                    "question": question,
                    "intent": routed.intent,
                    "source": source,
                    "sql": generated.sql,
                    "params": list(generated.params),
                    "tables": list(guard_result.tables),
                    "columns": columns,
                    "rows": [list(row) for row in rows],
                    "record_count": len(rows),
                    "scope": {"plant": plant} if plant is not None else None,
                    "disclosures": disclosures,
                    "trace": trace,
                },
            )

        return PipelineResponse(
            False,
            data={"trace": trace},
            error_code="GENERATION_FAILED",
            error="SQL 在重試上限內未能通過驗證與執行。",
            severity="error",
            evidence={"attempts": attempts, "last_error": prior_error},
        )
