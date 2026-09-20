from __future__ import annotations

import json
from collections import Counter

from ingest.validate import PROJECT_ROOT
from text2sql.corpus import load_corpus, normalize_question

BENCHMARK_DIR = PROJECT_ROOT / "benchmarks"


def _load(name: str) -> list[dict[str, object]]:
    return json.loads((BENCHMARK_DIR / name).read_text(encoding="utf-8"))


def test_benchmark_questions_do_not_appear_verbatim_in_corpus() -> None:
    corpus = load_corpus(PROJECT_ROOT / "corpus/training_corpus.json")
    corpus_questions = {normalize_question(example["question"]) for example in corpus["examples"]}
    benchmark_questions = {
        normalize_question(item["question"])
        for path in BENCHMARK_DIR.glob("*_questions.json")
        for item in json.loads(path.read_text(encoding="utf-8"))
        if "question" in item
    }

    assert corpus_questions.isdisjoint(benchmark_questions)


def test_corpus_and_benchmarks_meet_minimum_sample_sizes() -> None:
    corpus = load_corpus(PROJECT_ROOT / "corpus/training_corpus.json")
    golden = _load("golden_questions.json")
    evaluation = _load("eval_questions.json")
    traps = _load("trap_questions.json")
    attacks = _load("attack_questions.json")

    assert 40 <= len(corpus["examples"]) <= 60
    assert min(Counter(item["intent"] for item in golden).values()) >= 8
    assert len(evaluation) >= 60
    assert Counter(item["in_corpus"] for item in evaluation)[True] >= 20
    assert Counter(item["in_corpus"] for item in evaluation)[False] >= 20
    assert min(Counter(item["expect"]["code"] for item in traps).values()) >= 5
    assert len(attacks) == 16


def test_all_corpus_sql_targets_semantic_views_with_a_limit() -> None:
    corpus = load_corpus(PROJECT_ROOT / "corpus/training_corpus.json")
    for example in corpus["examples"]:
        normalized_sql = " ".join(example["sql"].upper().split())
        assert normalized_sql.startswith("SELECT ")
        assert " FROM V_" in normalized_sql
        assert " LIMIT " in normalized_sql


def test_benchmark_directory_contains_only_expected_versioned_json() -> None:
    assert {path.name for path in BENCHMARK_DIR.iterdir()} == {
        "golden_questions.json",
        "eval_questions.json",
        "trap_questions.json",
        "attack_questions.json",
    }
