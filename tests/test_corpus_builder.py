from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import text2sql.corpus_builder as corpus_builder
from text2sql.corpus import (
    DEFAULT_NGRAM_RANGE,
    build_index,
    column_values,
    load_corpus,
    ngram_range,
    normalize_question,
)
from text2sql.corpus_builder import (
    CorpusCandidate,
    deidentify,
    promote_batch,
    rollback_corpus,
)


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
                        "question": "2026年7月最高尖峰負載日",
                        "sql": "SELECT 1 LIMIT 1",
                        "intent": "system_metric",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _candidate(**overrides: str) -> CorpusCandidate:
    values = {
        "id": "candidate-1",
        "question": "林口1號機在2026年最高出力",
        "sql": 'SELECT MAX("尖峰出力_萬瓩") FROM v_peak LIMIT 1',
        "source": "corrected",
        "created_at": "2026-09-13T00:00:00Z",
        "approved_by": "reviewer",
        "schema_version": "1",
        "data_manifest_version": "fixture",
        "outcome": "corrected",
    }
    values.update(overrides)
    return CorpusCandidate(**values)


def _pass(_question: str, _sql: str) -> tuple[bool, str]:
    return True, ""


def _regression(_corpus: dict[str, object]) -> tuple[bool, dict[str, float]]:
    return True, {"execution_accuracy": 1.0}


def test_deidentify_removes_email_token_and_long_account_number() -> None:
    value = "a@example.com sk-secret123 123456789012"
    assert deidentify(value) == "[EMAIL] [TOKEN] [ACCOUNT]"


def test_deidentify_removes_taiwan_identity_phone_and_labeled_name() -> None:
    value = "請替王小明（0912-345-678，身分證 A123456789）查詢；姓名：陳美玲"

    assert deidentify(value) == ("請替[NAME]（[PHONE]，身分證 [TW_ID]）查詢；姓名：[NAME]")


def test_sensitive_parameter_is_rejected_before_publication(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)

    result = promote_batch(
        [_candidate(params=("a@example.com",))],
        corpus_path=corpus_path,
        index_path=tmp_path / "index.json",
        versions_dir=tmp_path / "versions",
        benchmark_question_set=set(),
        sql_validator=_pass,
        semantic_validator=_pass,
        result_validator=_pass,
        regression_gate=_regression,
    )

    assert result.rejected == ({"id": "candidate-1", "reason": "sensitive_params"},)
    assert "a@example.com" not in corpus_path.read_text(encoding="utf-8")


def test_failed_batch_does_not_publish_partial_examples(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.json"
    index_path = tmp_path / "index.json"
    _write_corpus(corpus_path)
    before = corpus_path.read_bytes()

    result = promote_batch(
        [_candidate(), _candidate(id="bad", outcome="failed", question="另一題")],
        corpus_path=corpus_path,
        index_path=index_path,
        versions_dir=tmp_path / "versions",
        benchmark_question_set=set(),
        sql_validator=_pass,
        semantic_validator=_pass,
        result_validator=_pass,
        regression_gate=_regression,
    )

    assert result.promoted is False
    assert corpus_path.read_bytes() == before
    assert not index_path.exists()


def test_promotion_versions_and_rollback_are_atomic(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.json"
    index_path = tmp_path / "index.json"
    versions = tmp_path / "versions"
    _write_corpus(corpus_path)

    result = promote_batch(
        [_candidate()],
        corpus_path=corpus_path,
        index_path=index_path,
        versions_dir=versions,
        benchmark_question_set=set(),
        sql_validator=_pass,
        semantic_validator=_pass,
        result_validator=_pass,
        regression_gate=_regression,
    )

    assert result.promoted is True
    assert len(load_corpus(corpus_path)["examples"]) == 2
    backup = next(versions.glob("*.json"))
    rollback_corpus(corpus_path=corpus_path, index_path=index_path, backup_path=backup)
    assert len(load_corpus(corpus_path)["examples"]) == 1


def test_failed_rollback_restores_both_published_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_path = tmp_path / "corpus.json"
    index_path = tmp_path / "index.json"
    versions = tmp_path / "versions"
    _write_corpus(corpus_path)
    promote_batch(
        [_candidate()],
        corpus_path=corpus_path,
        index_path=index_path,
        versions_dir=versions,
        benchmark_question_set=set(),
        sql_validator=_pass,
        semantic_validator=_pass,
        result_validator=_pass,
        regression_gate=_regression,
    )
    before_corpus = corpus_path.read_bytes()
    before_index = index_path.read_bytes()
    backup = next(versions.glob("*.json"))
    original_write = corpus_builder._atomic_json_write

    def fail_index_write(path: Path, payload: object) -> None:
        if path == index_path:
            raise OSError("injected index write failure")
        original_write(path, payload)

    monkeypatch.setattr(corpus_builder, "_atomic_json_write", fail_index_write)
    with pytest.raises(OSError, match="injected index write failure"):
        rollback_corpus(corpus_path=corpus_path, index_path=index_path, backup_path=backup)

    assert corpus_path.read_bytes() == before_corpus
    assert index_path.read_bytes() == before_index


def test_failed_promotion_restores_both_published_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_path = tmp_path / "corpus.json"
    index_path = tmp_path / "index.json"
    versions = tmp_path / "versions"
    _write_corpus(corpus_path)
    promote_batch(
        [_candidate()],
        corpus_path=corpus_path,
        index_path=index_path,
        versions_dir=versions,
        benchmark_question_set=set(),
        sql_validator=_pass,
        semantic_validator=_pass,
        result_validator=_pass,
        regression_gate=_regression,
    )
    before_corpus = corpus_path.read_bytes()
    before_index = index_path.read_bytes()
    original_write = corpus_builder._atomic_json_write

    def fail_index_write(path: Path, payload: object) -> None:
        if path == index_path:
            raise OSError("injected index write failure")
        original_write(path, payload)

    monkeypatch.setattr(corpus_builder, "_atomic_json_write", fail_index_write)
    with pytest.raises(OSError, match="injected index write failure"):
        promote_batch(
            [_candidate(id="candidate-2", question="第二個候選")],
            corpus_path=corpus_path,
            index_path=index_path,
            versions_dir=versions,
            benchmark_question_set=set(),
            sql_validator=_pass,
            semantic_validator=_pass,
            result_validator=_pass,
            regression_gate=_regression,
        )

    assert corpus_path.read_bytes() == before_corpus
    assert index_path.read_bytes() == before_index


def test_benchmark_question_is_rejected(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)
    candidate = _candidate()

    result = promote_batch(
        [candidate],
        corpus_path=corpus_path,
        index_path=tmp_path / "index.json",
        versions_dir=tmp_path / "versions",
        benchmark_question_set={normalize_question(candidate.question)},
        sql_validator=_pass,
        semantic_validator=_pass,
        result_validator=_pass,
        regression_gate=_regression,
    )

    assert result.rejected == ({"id": "candidate-1", "reason": "benchmark_leakage"},)


def _retriever_config(root: Path, body: str) -> Path:
    (root / "configs").mkdir(parents=True, exist_ok=True)
    path = root / "configs/retriever.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_index_uses_the_same_ngram_setting_as_retrieval(tmp_path: Path) -> None:
    """索引與檢索必須讀同一份設定，否則設定一改就永遠判定「索引不同步」。"""

    _retriever_config(tmp_path, "top_k: 5\ncharacter_ngram:\n  min: 3\n  max: 3\n")
    assert ngram_range(tmp_path) == (3, 3)


@pytest.mark.parametrize(
    "body",
    (
        "top_k: 5\ncharacter_ngram:\n  min: 4\n  max: 2\n",
        "top_k: 5\n",
        "character_ngram:\n  min: 0\n  max: 3\n",
        "這不是: [yaml\n",
    ),
)
def test_a_broken_retriever_setting_falls_back_instead_of_failing(
    tmp_path: Path, body: str
) -> None:
    """設定壞掉時索引寧可用已知的預設值，也不要整個建不起來。"""

    _retriever_config(tmp_path, body)
    assert ngram_range(tmp_path) == DEFAULT_NGRAM_RANGE


def test_a_missing_retriever_setting_falls_back(tmp_path: Path) -> None:
    assert ngram_range(tmp_path / "nowhere") == DEFAULT_NGRAM_RANGE


def test_the_index_records_how_it_was_cut(tmp_path: Path) -> None:
    """少了這兩個數字，事後看著一份索引也說不出它是用幾個字切的。"""

    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)
    corpus = load_corpus(corpus_path)

    three = build_index(corpus, ngram=(3, 3))
    wide = build_index(corpus, ngram=(2, 4))

    assert three["character_ngram"] == {"min": 3, "max": 3}
    assert wide["character_ngram"] == {"min": 2, "max": 4}
    assert {len(gram) for gram in three["documents"][0]["ngrams"]} == {3}
    assert {len(gram) for gram in wide["documents"][0]["ngrams"]} == {2, 3, 4}
    assert three["documents"] != wide["documents"], "設定不同就該切出不同的索引"


# ── 封閉集合欄位的偵測：哪些欄位的值該寫進 prompt ──────────────────────


def _fake_sql(tables: dict[str, list[object]]):
    """假的 run_sql：照 view.column 給值，並尊重 LIMIT。"""

    pattern = re.compile(r'SELECT DISTINCT "(.+)" FROM (\w+) LIMIT (\d+)')

    def run(sql: str, params: tuple[object, ...]):
        del params
        match = pattern.match(sql)
        assert match, sql
        column, view, limit = match.group(1), match.group(2), int(match.group(3))
        values = tables.get(f"{view}.{column}", [])
        return [column], [(value,) for value in values[:limit]]

    return run


def test_closed_text_columns_are_collected() -> None:
    run = _fake_sql({"v_unit.燃料": ["水", "重油", "天然氣", "輕柴油", "煤"]})

    assert column_values(run, {"v_unit": ["燃料"]}) == {
        "v_unit.燃料": ["水", "重油", "天然氣", "輕柴油", "煤"]
    }


def test_a_column_with_too_many_values_is_not_an_enum() -> None:
    """多取一筆就是為了分辨「剛好 25 個」與「至少 26 個」—— 後者是資料，不是列舉。"""

    run = _fake_sql({"v_peak.日期": [f"2026-01-{day:02d}" for day in range(1, 40)]})

    assert column_values(run, {"v_peak": ["日期"]}, max_values=25) == {}


def test_numeric_columns_are_skipped() -> None:
    """列舉數字對模型沒有幫助，只會佔掉注意力。"""

    run = _fake_sql({"v_re_generation.月份": [str(month) for month in range(1, 13)]})

    assert column_values(run, {"v_re_generation": ["月份"]}) == {}


def test_long_identifier_columns_are_skipped() -> None:
    values = [f"realtime:和平#{n}@0762068123{n:02d}" for n in range(12)]

    run = _fake_sql({"v_peak.容量來源": values})

    assert column_values(run, {"v_peak": ["容量來源"]}, max_chars=100) == {}


def test_one_unreadable_column_does_not_sink_the_whole_prompt() -> None:
    """快照與 allowlist 不同步時少列一欄，不要讓整個 prompt 組不出來。"""

    def run(sql: str, params: tuple[object, ...]):
        del params
        if "壞掉的欄位" in sql:
            raise RuntimeError("no such column")
        return ["燃料"], [("煤",), ("天然氣",)]

    assert column_values(run, {"v_unit": ["燃料", "壞掉的欄位"]}) == {
        "v_unit.燃料": ["煤", "天然氣"]
    }


def test_blank_values_do_not_become_an_enum() -> None:
    run = _fake_sql({"v_outage.原因": ["", "   ", None]})

    assert column_values(run, {"v_outage": ["原因"]}) == {}
