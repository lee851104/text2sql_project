"""The seed corpus is what the model imitates, so every example must be one we would run."""

from __future__ import annotations

import pytest
import yaml

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT
from text2sql.corpus import load_corpus
from text2sql.db import ReadOnlySQLite
from text2sql.sql_guard import SqlGuard

CORPUS = load_corpus(PROJECT_ROOT / "corpus/training_corpus.json")
EXAMPLES = CORPUS["examples"]
GUARD_CONFIG = yaml.safe_load((PROJECT_ROOT / "configs/guard.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def executor(tmp_path_factory: pytest.TempPathFactory) -> ReadOnlySQLite:
    database = tmp_path_factory.mktemp("corpus-examples") / "power.db"
    build_database(database, root=PROJECT_ROOT)
    return ReadOnlySQLite(database, timeout_seconds=float(GUARD_CONFIG["sql"]["timeout_seconds"]))


@pytest.mark.parametrize("example", EXAMPLES, ids=[example["id"] for example in EXAMPLES])
def test_corpus_example_passes_the_sql_guard(example: dict[str, object]) -> None:
    # 範例若寫死字面值，模型照抄後會被 SQL_LITERAL_NOT_PARAMETERIZED 擋下，重試也只會再抄一次。
    guard = SqlGuard(max_rows=int(GUARD_CONFIG["sql"]["max_rows"]))

    result = guard.validate(str(example["sql"]), tuple(example.get("params", ())))

    assert result.allowed, f"{result.code}: {result.reason}"


@pytest.mark.integration
@pytest.mark.parametrize("example", EXAMPLES, ids=[example["id"] for example in EXAMPLES])
def test_corpus_example_executes_against_the_database(
    example: dict[str, object], executor: ReadOnlySQLite
) -> None:
    executor.execute(str(example["sql"]), tuple(example.get("params", ())))


def test_corpus_example_params_match_their_placeholders() -> None:
    for example in EXAMPLES:
        params = example.get("params", [])
        assert isinstance(params, list), example["id"]
        assert str(example["sql"]).count("?") == len(params), example["id"]
