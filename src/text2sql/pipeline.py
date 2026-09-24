"""Guarded Text2SQL orchestration with deterministic routing and bounded retries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Any, Protocol

from text2sql.corpus import load_corpus
from text2sql.entities import Entities, extract_entities
from text2sql.llm import (
    RETRYABLE_CATEGORY,
    GeneratedQuery,
    LLMProtocol,
    classify_error,
    parse_generated_query,
)
from text2sql.prompt import build_prompt
from text2sql.retriever import TfidfRetriever
from text2sql.router import missing_parameter_clarification, route, suggest_scope_question
from text2sql.scope_guard import ScopeError, ScopeGuard
from text2sql.semantic_guard import SemanticDecision
from text2sql.sql_guard import SqlGuard, SqlGuardResult

RunSql = Callable[[str, tuple[object, ...]], tuple[list[str], list[tuple[object, ...]]]]

LLM_FAILURE_CODES = {
    "authentication": "LLM_AUTH_FAILED",
    "configuration": "LLM_REQUEST_REJECTED",
    "rate_limit": "LLM_RATE_LIMITED",
    "service": "LLM_UNAVAILABLE",
    "refused": "LLM_REFUSED",
    "incomplete": "LLM_INCOMPLETE",
}

# 憑證與設定的問題，換個問法不會變好。交給迴圈後面的離線澄清的話，使用者會看到
# 「這句沒有指名是哪一座電廠」，然後照做，然後還是不能用 —— 而真正該修的人不知道。
IMMEDIATE_LLM_FAILURES = frozenset({"authentication", "configuration"})


class SemanticGuardProtocol(Protocol):
    def check_question(self, question: str, entities: Entities) -> SemanticDecision: ...

    def check_sql(
        self, question: str, query: GeneratedQuery, entities: Entities
    ) -> SemanticDecision: ...

    def describe_aggregate_scope(self, query: GeneratedQuery) -> SemanticDecision | None: ...

    def explain_unanswerable_date(
        self, entities: Entities, *, question: str = ""
    ) -> SemanticDecision | None: ...


class AllowAllSemanticGuard:
    def check_question(self, question: str, entities: Entities) -> SemanticDecision:
        return SemanticDecision()

    def check_sql(
        self, question: str, query: GeneratedQuery, entities: Entities
    ) -> SemanticDecision:
        return SemanticDecision()

    def describe_aggregate_scope(self, query: GeneratedQuery) -> SemanticDecision | None:
        del query
        return None

    def explain_unanswerable_date(
        self, entities: Entities, *, question: str = ""
    ) -> SemanticDecision | None:
        del entities, question
        return None


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
        column_values: Mapping[str, Sequence[str]] | None = None,
        column_value_conflicts: Mapping[str, str] | None = None,
        semantic_guard: SemanticGuardProtocol | None = None,
        scope_guard: ScopeGuard | None = None,
        max_attempts: int = 3,
        top_k: int = 5,
        ngram_min: int = 2,
        ngram_max: int = 4,
        min_score: float = 0.0,
        relative_score: float = 0.0,
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
        # 封閉集合欄位的值。空的時候 prompt 就少這一段，行為與先前相同。
        self.column_values = {key: list(values) for key, values in (column_values or {}).items()}
        self.column_value_conflicts = dict(column_value_conflicts or {})
        self.semantic_guard = semantic_guard or AllowAllSemanticGuard()
        self.scope_guard = scope_guard
        self.max_attempts = max_attempts
        self.top_k = top_k
        self.min_score = min_score
        self.relative_score = relative_score

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

    def _llm_failure_response(
        self,
        category: str,
        error: Exception,
        trace: list[dict[str, Any]],
        attempts: int,
    ) -> PipelineResponse:
        """把轉接層的分類變成一句使用者或維運能據以行動的話。

        只帶例外的類別名稱，不帶它的訊息 —— 上游的錯誤原文可能回顯我們送出去的東西。
        """

        environment = getattr(self.llm, "api_key_env", None)
        hint = f"（這個 provider 讀的是 {environment}）" if environment else ""
        messages = {
            "authentication": f"線上服務拒絕了這組憑證；請到 API 設定確認金鑰與權限。{hint}",
            "configuration": "線上服務不接受這個請求；請確認模型名稱、端點與輸出格式設定。",
            "rate_limit": "線上服務這次限流了，稍後再試。",
            "service": "線上生成這次用不了，而這一題需要它才答得出來。"
            "規則接得住的問題不受影響，可以先換一個具體一點的問法。",
            "refused": "模型拒絕回答這一題。換個問法或改問資料本身，重複送同一句不會有不同結果。",
            "incomplete": "模型的回應沒有講完；沒講完的 SQL 不會拿去執行。",
        }
        return PipelineResponse(
            False,
            data={"trace": trace},
            error_code=LLM_FAILURE_CODES[category],
            error=messages[category],
            severity="error",
            evidence={
                "attempts": attempts,
                "llm_error": category,
                "last_error": f"LLM_OUTPUT_ERROR: {type(error).__name__}",
            },
        )

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
            retrieved = retriever.retrieve(
                question,
                top_k=self.top_k,
                min_score=self.min_score,
                relative_score=self.relative_score,
            )
            self._trace(
                trace,
                "retrieve",
                started,
                example_ids=[item.example["id"] for item in retrieved],
                # 被門檻砍掉幾個。全被砍掉時 prompt 只剩 schema 與領域規則，那比塞五個
                # 0.000 分的範例好 —— 但要看得到它發生了。
                dropped=self.top_k - len(retrieved),
            )

        prior_error: str | None = None
        prior_sql: str | None = None
        llm_failure: tuple[str, Exception, int] | None = None
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
                    column_values=self.column_values,
                    column_value_conflicts=self.column_value_conflicts,
                    prior_error=prior_error,
                    prior_sql=prior_sql,
                )
                try:
                    generated = parse_generated_query(self.llm.generate(prompt))
                except Exception as error:  # Adapter errors become bounded pipeline errors.
                    category = classify_error(error)
                    prior_error = f"LLM_OUTPUT_ERROR: {type(error).__name__}"
                    # 連解析都沒過，手上沒有可以還給模型的 SQL。
                    prior_sql = None
                    self._trace(
                        trace,
                        "generate",
                        started,
                        attempt=attempt,
                        error=prior_error,
                        category=category,
                    )
                    if category == RETRYABLE_CATEGORY:
                        continue
                    if category in IMMEDIATE_LLM_FAILURES:
                        return self._llm_failure_response(category, error, trace, attempt)
                    # 其餘不重送，但離線做得到的事還是要做完 —— 缺參數反問與近似問法
                    # 建議都在迴圈後面，服務不通或限流的時候它們正好是最有用的那一段。
                    llm_failure = (category, error, attempt)
                    break
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
                prior_sql = generated.sql
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
                prior_sql = generated.sql
                self._trace(trace, "execute", started, attempt=attempt, error=prior_error)
                continue
            self._trace(trace, "execute", started, attempt=attempt, record_count=len(rows))
            disclosures = []
            disclosed_codes: set[str] = set()
            # 期間另外問，因為 check_sql 一次只回一個 decision：再生能源的查詢會先撞上
            # RENEWABLE_SELF_BUILT_ONLY，期間就永遠輪不到，而那兩件事都該說。
            scope_note = self.semantic_guard.describe_aggregate_scope(generated)
            for decision in (question_decision, semantic, scope_note):
                if decision is None:
                    continue
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

        # 最後一步：與其只回「未能通過驗證與執行」，不如問一句。這一層放在這裡而不是
        # 放在意圖分類，是因為相似度本身分不開 —— 會被它偷走的題目，到這裡早就被更具
        # 體的規則接走了。線上模式同理：LLM 答得出來就走不到這裡。
        # 日期先問。「2024年台中出力」答不出來的原因就是 2024 沒有出力資料，回一句
        # 「缺少參數」或「線上生成用不了」都是把使用者指向錯的方向。
        out_of_range = self.semantic_guard.explain_unanswerable_date(entities, question=question)
        if out_of_range is not None:
            return self._semantic_error(out_of_range, trace)

        # 離線澄清照樣優先給使用者 —— 它是他當下做得到的事。但線上為什麼沒接上要留在
        # evidence 裡，否則限流或服務中斷會完全躲在「這句沒有指名是哪一座電廠」後面。
        llm_note = {"llm_error": llm_failure[0]} if llm_failure else {}

        missing = missing_parameter_clarification(
            question,
            entities,
            peak_columns=self.peak_columns,
            plants=self.plants,
            data_range=self.data_range,
        )
        if missing is not None:
            return PipelineResponse(
                False,
                data={"trace": trace},
                error_code="MISSING_PARAMETER",
                error=missing.reason,
                severity="clarify",
                suggestions=(missing.suggestion,),
                evidence={"missing": missing.missing, "attempts": attempts, **llm_note},
            )

        suggestion = suggest_scope_question(question)
        if suggestion is not None:
            return PipelineResponse(
                False,
                data={"trace": trace},
                error_code="DATA_SCOPE_NEAR_MATCH",
                error="這句我沒有把握。你是不是想問下面這個？",
                severity="clarify",
                suggestions=(suggestion,),
                evidence={"attempts": attempts, "last_error": prior_error, **llm_note},
            )

        if llm_failure is not None:
            category, error, made = llm_failure
            return self._llm_failure_response(category, error, trace, made)

        return PipelineResponse(
            False,
            data={"trace": trace},
            error_code="GENERATION_FAILED",
            error="SQL 在重試上限內未能通過驗證與執行。",
            severity="error",
            evidence={"attempts": attempts, "last_error": prior_error},
        )
