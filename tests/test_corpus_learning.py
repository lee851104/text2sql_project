from __future__ import annotations

import gc
import json
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from serving.corpus_learning import CorpusLearningService, CorpusSelfApprovalError
from text2sql.corpus import corpus_checksum, load_corpus
from text2sql.llm import FakeLLM
from text2sql.pipeline import PipelineResponse, Text2SQLPipeline
from text2sql.sql_guard import SqlGuard

SQL = 'SELECT "日期" FROM v_system WHERE "日期" = ? LIMIT 1'
PARAMS = ("2026-07-09",)
COLUMNS = ("日期",)
ROWS = (("2026-07-09",),)


def _write_corpus(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": "corpus-v1",
                "ddl": [],
                "documentation": [],
                "examples": [
                    {
                        "id": "base",
                        "question": "alpha beta gamma delta",
                        "sql": 'SELECT "日期" FROM v_system LIMIT 1',
                        "intent": "right",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _pipeline(corpus: Path, runner=None) -> Text2SQLPipeline:
    return Text2SQLPipeline(
        llm=FakeLLM([]),
        sql_guard=SqlGuard(),
        run_sql=runner or (lambda _sql, _params: (list(COLUMNS), list(ROWS))),
        corpus_path=corpus,
        data_range=("2025-01-01", "2026-07-31"),
        peak_columns=set(),
    )


def _service(
    tmp_path: Path,
    *,
    benchmark_items: list[dict[str, str]] | None = None,
    runner=None,
    allow_self_approval: bool = False,
) -> tuple[CorpusLearningService, Text2SQLPipeline, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    canonical = tmp_path / "canonical.json"
    database = tmp_path / "power.db"
    database.touch()
    _write_corpus(canonical)
    pipeline = _pipeline(canonical, runner)
    benchmark_paths = []
    if benchmark_items is not None:
        benchmark = tmp_path / "benchmarks.json"
        benchmark.write_text(json.dumps(benchmark_items), encoding="utf-8")
        benchmark_paths.append(benchmark)
    service = CorpusLearningService(
        database=database,
        canonical_corpus_path=canonical,
        benchmark_paths=benchmark_paths,
        pipeline=pipeline,
        allow_self_approval=allow_self_approval,
    )
    return service, pipeline, canonical


def _submit(
    service: CorpusLearningService,
    *,
    question: str = "查詢 2026 年 7 月 9 日資料",
    source: str = "router",
    candidate_id: str | None = None,
    intent: str = "system_metric",
    sql: str = SQL,
    rows=ROWS,
    tables=("v_system",),
    data_provenance=None,
    proposed_by: str | None = None,
) -> dict[str, object]:
    return service.submit(
        question=question,
        sql=sql,
        params=PARAMS,
        intent=intent,
        source=source,
        columns=COLUMNS,
        rows=rows,
        tables=tables,
        data_provenance=data_provenance,
        candidate_id=candidate_id,
        proposed_by=proposed_by,
    )


def test_router_candidate_waits_for_review_then_promotes_and_hot_reloads(
    tmp_path: Path,
) -> None:
    service, pipeline, canonical = _service(tmp_path)

    pending = _submit(service, question="請替 a@example.com 查詢 2026 年資料")

    assert pending["status"] == "pending_review"
    assert pending["approved_by"] == ""
    assert len(load_corpus(canonical)["examples"]) == 1
    result = service.review(pending["id"], approve=True, reviewer="operator")
    assert result["status"] == "promoted"
    learned = load_corpus(service.corpus_path)["examples"][-1]
    assert learned["params"] == list(PARAMS)
    assert learned["question"] == "請替 [EMAIL] 查詢 2026 年資料"
    assert len(pipeline.corpus["examples"]) == 2
    disk = service.candidates_path.read_text(encoding="utf-8")
    assert "a@example.com" not in disk
    assert '"rows"' not in disk
    status = service.status()
    assert status["candidate_counts"]["promoted"] == 1
    assert status["index_synchronized"] is True
    assert status["policy"]["auto_promote_source"] is None
    assert status["policy"]["manual_review_required"] is True


def test_candidate_keeps_immutable_table_and_source_provenance(tmp_path: Path) -> None:
    service, _pipeline_instance, _canonical = _service(tmp_path)
    provenance = {
        "database_version": "db-demo-v1",
        "data_sources": [
            {
                "dataset": "daily_csv",
                "display_name": "daily.csv",
                "sha256": "a" * 64,
                "views": ["v_system"],
            }
        ],
    }

    pending = _submit(service, data_provenance=provenance)
    assert pending["tables"] == ["v_system"]
    assert pending["data_provenance"] == provenance
    promoted = service.review(pending["id"], approve=True, reviewer="operator")
    assert promoted["status"] == "promoted"
    learned = load_corpus(service.corpus_path)["examples"][-1]
    assert learned["metadata"]["tables"] == ["v_system"]
    assert learned["metadata"]["data_provenance"] == provenance


def test_llm_candidate_is_validated_but_waits_for_review(tmp_path: Path) -> None:
    service, pipeline, _canonical = _service(tmp_path)

    pending = _submit(service, source="llm")

    assert pending["status"] == "pending_review"
    assert all(item["passed"] for item in pending["validation"].values())
    assert len(pipeline.corpus["examples"]) == 1
    promoted = service.review(pending["id"], approve=True, reviewer="operator")
    assert promoted["status"] == "promoted"
    assert len(pipeline.corpus["examples"]) == 2


def test_benchmark_sql_replay_and_retrieval_gates_reject_candidates(tmp_path: Path) -> None:
    service, _pipeline_instance, _canonical = _service(
        tmp_path / "leak",
        benchmark_items=[{"question": "保留的評測問題", "intent": "right"}],
    )
    leaked = _submit(service, question="保留的評測問題")
    assert leaked["reason"] == "benchmark_leakage"

    service, _pipeline_instance, _canonical = _service(tmp_path / "sql")
    unsafe = _submit(service, question="不同問題", sql="DROP TABLE v_system")
    assert str(unsafe["reason"]).startswith("sql_guard:")

    service, _pipeline_instance, _canonical = _service(tmp_path / "replay")
    mismatch = _submit(service, question="另一個問題", rows=(("2025-01-01",),))
    assert mismatch["reason"] == "result_replay:mismatch"

    service, _pipeline_instance, _canonical = _service(
        tmp_path / "regression",
        benchmark_items=[{"question": "alpha beta gamma", "intent": "right"}],
    )
    regressed = _submit(
        service,
        question="alpha beta gamma x",
        intent="wrong",
    )
    assert regressed["reason"] == "retrieval_regression"


def test_duplicate_submissions_are_idempotent_under_threads(tmp_path: Path) -> None:
    service, _pipeline_instance, _canonical = _service(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: _submit(service), range(16)))

    assert {result["id"] for result in results} == {results[0]["id"]}
    assert len(service.list_entries()) == 1
    assert sum(bool(result.get("duplicate")) for result in results) == 15

    conflict = _submit(service, question="識別碼衝突甲", candidate_id="fixed-id")
    assert conflict["id"] == "fixed-id"
    response = PipelineResponse(
        True,
        data={
            "question": "識別碼衝突乙",
            "sql": SQL,
            "params": list(PARAMS),
            "intent": "system_metric",
            "source": "router",
            "columns": list(COLUMNS),
            "rows": [list(row) for row in ROWS],
        },
    )
    original_id = service._candidate_id  # Force the observe-only conflict path.
    service._candidate_id = lambda *_args: "fixed-id"  # type: ignore[method-assign]
    try:
        observed = service.observe(response)
    finally:
        service._candidate_id = original_id  # type: ignore[method-assign]
    assert observed == {
        "accepted": False,
        "status": "error",
        "reason": "learning_internal_error",
        "error_type": "ValueError",
    }


def test_question_already_in_published_corpus_is_ignored(tmp_path: Path) -> None:
    service, _pipeline_instance, _canonical = _service(tmp_path)

    result = _submit(service, question="alpha beta gamma delta")

    assert result["status"] == "ignored"
    assert result["reason"] == "duplicate_question"
    assert service.status()["candidate_counts"]["ignored"] == 1


def test_new_runtime_pipeline_attaches_to_latest_promoted_corpus(tmp_path: Path) -> None:
    service, _pipeline_instance, canonical = _service(tmp_path)
    pending = _submit(service)
    service.review(pending["id"], approve=True, reviewer="operator")
    replacement = _pipeline(canonical)

    attached = service.attach_pipeline(replacement)

    assert attached["corpus_version"] == service.status()["corpus_version"]
    assert len(replacement.corpus["examples"]) == 2
    assert service.list_events(limit=1)[0]["event"] == "candidate_promoted"


def test_learning_service_does_not_strongly_retain_detached_runtime_pipeline(
    tmp_path: Path,
) -> None:
    service, pipeline, _canonical = _service(tmp_path)
    pipeline_reference = weakref.ref(pipeline)

    del pipeline
    gc.collect()

    assert pipeline_reference() is None
    with pytest.raises(RuntimeError, match="尚未附加"):
        service._active_pipeline(None)


def test_two_service_instances_do_not_lose_concurrent_updates(tmp_path: Path) -> None:
    first, _first_pipeline, canonical = _service(tmp_path)
    second_pipeline = _pipeline(canonical)
    second = CorpusLearningService(
        database=first.database,
        canonical_corpus_path=canonical,
        pipeline=second_pipeline,
        workspace=first.workspace,
    )

    def submit(index: int) -> dict[str, object]:
        service = first if index % 2 else second
        return _submit(
            service,
            question=f"查詢 2026 年 7 月 9 日資料，版本 {index}",
            source="llm",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(24)))

    assert {result["status"] for result in results} == {"pending_review"}
    assert len(first.list_entries()) == 24
    assert len(second.list_entries()) == 24
    persisted = json.loads(first.candidates_path.read_text(encoding="utf-8"))
    assert len(persisted["entries"]) == 24


def test_baseline_change_rebases_workspace_and_requeues_promoted_candidates(
    tmp_path: Path,
) -> None:
    service, _pipeline_instance, canonical = _service(tmp_path)
    staged = _submit(service)
    promoted = service.review(staged["id"], approve=True, reviewer="operator")
    pending = _submit(service, question="canonical 升版前的待審候選", source="llm")
    previous_manifest = json.loads(service.workspace_manifest_path.read_text(encoding="utf-8"))

    new_canonical = load_corpus(canonical)
    new_canonical["version"] = "corpus-v2"
    new_canonical["examples"].append(
        {
            "id": "base-v2",
            "question": "全新 canonical 基線範例",
            "sql": 'SELECT "日期" FROM v_system LIMIT 1',
            "intent": "right",
        }
    )
    canonical.write_text(
        json.dumps(new_canonical, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    replacement = _pipeline(canonical)
    rebased = CorpusLearningService(
        database=service.database,
        canonical_corpus_path=canonical,
        pipeline=replacement,
        workspace=service.workspace,
        data_manifest_version="rebuilt-db-v2",
    )

    active = load_corpus(rebased.corpus_path)
    candidate = next(item for item in rebased.list_entries() if item["id"] == promoted["id"])
    preserved_pending = next(item for item in rebased.list_entries() if item["id"] == pending["id"])
    manifest = json.loads(rebased.workspace_manifest_path.read_text(encoding="utf-8"))
    event = rebased.list_events(limit=1)[0]
    assert active == new_canonical
    assert candidate["status"] == "pending_review"
    assert candidate["reason"] == "baseline_changed_revalidation_required"
    assert candidate["promoted_version"] is None
    assert preserved_pending["status"] == "pending_review"
    assert preserved_pending["reason"] == "manual_review_required"
    assert manifest["canonical_corpus_checksum"] == corpus_checksum(new_canonical)
    assert manifest["database_manifest_version"] == "rebuilt-db-v2"
    assert manifest["initialized_at"] == previous_manifest["initialized_at"]
    assert event["event"] == "workspace_rebased"
    assert event["candidates_pending_review"] == 1
    assert (
        event["previous_canonical_corpus_checksum"]
        == previous_manifest["canonical_corpus_checksum"]
    )
    assert any(rebased.versions_dir.glob(f"{promoted['promoted_version']}-*.json"))
    assert json.loads(rebased.index_path.read_text(encoding="utf-8"))["document_count"] == 2

    reviewed = rebased.review(promoted["id"], approve=True, reviewer="operator")
    assert reviewed["status"] == "promoted"
    assert len(replacement.corpus["examples"]) == 3


def test_database_manifest_change_alone_triggers_safe_rebase(tmp_path: Path) -> None:
    service, _pipeline_instance, canonical = _service(tmp_path)
    staged = _submit(service)
    promoted = service.review(staged["id"], approve=True, reviewer="operator")
    replacement = _pipeline(canonical)

    reopened = CorpusLearningService(
        database=service.database,
        canonical_corpus_path=canonical,
        pipeline=replacement,
        workspace=service.workspace,
        data_manifest_version="different-db-build",
    )

    assert load_corpus(reopened.corpus_path) == load_corpus(canonical)
    candidate = next(item for item in reopened.list_entries() if item["id"] == promoted["id"])
    assert candidate["status"] == "pending_review"
    assert reopened.status()["workspace_identity"]["database_manifest_version"] == (
        "different-db-build"
    )


def test_non_promoted_candidate_can_be_corrected_as_a_revision(tmp_path: Path) -> None:
    service, _pipeline_instance, _canonical = _service(tmp_path)
    question = "查詢 2026 年 7 月 9 日修正版資料"
    pending = _submit(service, question=question, source="llm")

    corrected = _submit(service, question=question, source="router")

    assert pending["status"] == "pending_review"
    assert corrected["status"] == "pending_review"
    assert corrected["id"] != pending["id"]
    assert corrected["revision_of"] == pending["id"]
    assert len(service.list_entries()) == 2
    corrected = service.review(corrected["id"], approve=True, reviewer="operator")
    assert corrected["status"] == "promoted"

    idempotent = _submit(
        service,
        question=question,
        source="llm",
        sql='SELECT "日期" FROM v_system ORDER BY "日期" LIMIT 1',
    )
    assert idempotent["id"] == corrected["id"]
    assert idempotent["status"] == "promoted"
    assert idempotent["duplicate"] is True


def test_rejected_and_ignored_candidates_allow_later_revisions(tmp_path: Path) -> None:
    service, _pipeline_instance, canonical = _service(tmp_path)
    rejected = _submit(service, question="待修正的不安全查詢", sql="DROP TABLE v_system")
    corrected = _submit(service, question="待修正的不安全查詢")
    assert rejected["status"] == "rejected"
    assert corrected["status"] == "pending_review"
    assert corrected["revision_of"] == rejected["id"]
    corrected = service.review(corrected["id"], approve=True, reviewer="operator")
    assert corrected["status"] == "promoted"

    ignored = _submit(service, question="alpha beta gamma delta")
    assert ignored["status"] == "ignored"
    changed = load_corpus(canonical)
    changed["version"] = "corpus-without-old-question"
    changed["examples"] = []
    canonical.write_text(json.dumps(changed), encoding="utf-8")
    replacement = _pipeline(canonical)
    reopened = CorpusLearningService(
        database=service.database,
        canonical_corpus_path=canonical,
        pipeline=replacement,
        workspace=service.workspace,
        data_manifest_version="rebuilt-without-old-question",
    )
    revised = _submit(
        reopened,
        question="alpha beta gamma delta",
        sql='SELECT "日期" FROM v_system WHERE "日期" = ? ORDER BY "日期" LIMIT 1',
    )
    assert revised["status"] == "pending_review"
    revised = reopened.review(revised["id"], approve=True, reviewer="operator")
    assert revised["status"] == "promoted"
    assert revised["revision_of"] == ignored["id"]


def test_corrupted_event_tail_is_skipped_and_future_events_remain_readable(
    tmp_path: Path,
) -> None:
    service, _pipeline_instance, _canonical = _service(tmp_path)
    result = _submit(service, source="llm")
    latest_valid = service.list_events(limit=1)[0]
    with service.events_path.open("ab") as handle:
        handle.write(b'{"schema_version":"corpus-events-v1","event":')

    assert service.status()["latest_event"] == latest_valid
    duplicate = _submit(service, source="llm")

    assert duplicate["id"] == result["id"]
    assert duplicate["duplicate"] is True
    events = service.list_events()
    assert events[0]["event"] == "duplicate_candidate"
    assert all(isinstance(event, dict) for event in events)


def test_end_to_end_learning_redacts_taiwan_personal_data(tmp_path: Path) -> None:
    service, _pipeline_instance, _canonical = _service(tmp_path)
    original_values = ("王小明", "0912-345-678", "A123456789")

    pending = _submit(
        service,
        question="請替王小明（0912-345-678，身分證 A123456789）查詢 2026 年資料",
    )

    result = service.review(pending["id"], approve=True, reviewer="operator")
    assert result["status"] == "promoted"
    surfaces = (
        service.candidates_path.read_text(encoding="utf-8"),
        json.dumps(service.list_entries(), ensure_ascii=False),
        service.corpus_path.read_text(encoding="utf-8"),
        service.events_path.read_text(encoding="utf-8"),
    )
    for surface in surfaces:
        assert all(value not in surface for value in original_values)
    assert "[NAME]" in surfaces[0]
    assert "[PHONE]" in surfaces[0]
    assert "[TW_ID]" in surfaces[0]


def test_a_candidate_records_the_account_whose_query_produced_it(tmp_path: Path) -> None:
    service, _pipeline, _canonical = _service(tmp_path)

    pending = _submit(service, proposed_by="analyst")
    anonymous = _submit(service, question="另一個問句 2026", candidate_id="second")

    assert pending["proposed_by"] == "analyst"
    assert anonymous["proposed_by"] is None


def test_the_account_that_produced_a_candidate_cannot_promote_it(tmp_path: Path) -> None:
    """自己問出來、自己核准進正式語料，實質上就是自審。"""

    service, _pipeline, _canonical = _service(tmp_path)
    pending = _submit(service, proposed_by="analyst")

    with pytest.raises(CorpusSelfApprovalError, match="請由另一個帳號審核"):
        service.review(pending["id"], approve=True, reviewer="analyst")

    assert service.list_entries(state="pending_review", limit=10)[0]["id"] == pending["id"]
    refused = [
        event
        for event in service.list_events(limit=50)
        if event["event"] == "candidate_self_approval_refused"
    ]
    assert len(refused) == 1


def test_another_account_may_promote_the_candidate(tmp_path: Path) -> None:
    service, _pipeline, _canonical = _service(tmp_path)
    pending = _submit(service, proposed_by="analyst")

    promoted = service.review(pending["id"], approve=True, reviewer="reviewer")

    assert promoted["status"] == "promoted"
    assert promoted["approved_by"] == "reviewer"


def test_the_proposer_may_still_reject_their_own_candidate(tmp_path: Path) -> None:
    service, _pipeline, _canonical = _service(tmp_path)
    pending = _submit(service, proposed_by="analyst")

    rejected = service.review(pending["id"], approve=False, reviewer="analyst")

    assert rejected["status"] == "rejected"


def test_an_anonymous_candidate_is_outside_the_rule(tmp_path: Path) -> None:
    """匿名不是身分：兩個不同訪客都會記成 None，拿來比對只會擋到不相干的人。

    這是這條規則已知的邊界，不是遺漏 —— 匿名查詢產生的候選仍可由任何帳號審核。
    """

    service, _pipeline, _canonical = _service(tmp_path)
    pending = _submit(service)

    promoted = service.review(pending["id"], approve=True, reviewer="reviewer")

    assert promoted["status"] == "promoted"


def test_the_single_operator_override_also_covers_corpus_promotion(tmp_path: Path) -> None:
    service, _pipeline, _canonical = _service(tmp_path, allow_self_approval=True)
    pending = _submit(service, proposed_by="solo")

    promoted = service.review(pending["id"], approve=True, reviewer="solo")

    assert promoted["status"] == "promoted"
