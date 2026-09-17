import json
from pathlib import Path

from text2sql.llm import FakeLLM
from text2sql.pipeline import Text2SQLPipeline
from text2sql.sql_guard import SqlGuard

ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "corpus" / "training_corpus.json"


def make_pipeline(llm: FakeLLM, run_sql):
    return Text2SQLPipeline(
        llm=llm,
        sql_guard=SqlGuard(),
        run_sql=run_sql,
        corpus_path=CORPUS,
        data_range=("2025-01-01", "2026-07-31"),
        peak_columns={"台中#2"},
    )


def test_rule_route_runs_without_llm_and_still_uses_guard() -> None:
    llm = FakeLLM([])
    calls = []

    def run_sql(sql, params):
        calls.append((sql, params))
        return ["日期", "備轉容量率_pct"], [("2026-07-09", 4.1)]

    response = make_pipeline(llm, run_sql).query("2026年7月備轉容量率最低是哪一天")
    assert response.success
    assert not llm.calls
    assert calls[0][1] == ("2026-07-01", "2026-07-31")
    stages = [item["stage"] for item in response.data["trace"]]
    assert "sql_guard" in stages
    assert response.data["record_count"] == 1


def test_invalid_llm_sql_is_regenerated_with_guard_feedback() -> None:
    valid = json.dumps(
        {
            "sql": 'SELECT "日期" FROM v_system ORDER BY "日期" DESC LIMIT 1',
            "params": [],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(["DROP TABLE v_peak", valid])
    response = make_pipeline(llm, lambda _sql, _params: (["日期"], [("2026-07-31",)])).query(
        "列出所有資料可用日期"
    )
    assert response.success
    assert len(llm.calls) == 2
    assert "SQL_NOT_READ_ONLY" in llm.calls[1]
    guard_steps = [item for item in response.data["trace"] if item["stage"] == "sql_guard"]
    assert [item["code"] for item in guard_steps] == ["SQL_NOT_READ_ONLY", "OK"]


def test_pipeline_fails_honestly_after_three_attempts() -> None:
    llm = FakeLLM(["DROP TABLE v_peak"] * 3)
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("容量缺口欄位有哪些")
    assert not response.success
    assert response.error_code == "GENERATION_FAILED"
    assert response.evidence["attempts"] == 3
    assert len(llm.calls) == 3


def test_authentication_error_is_not_retried_and_is_actionable() -> None:
    class AuthenticationError(Exception):
        pass

    class InvalidCredentialLLM:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, _prompt: str) -> str:
            self.calls += 1
            raise AuthenticationError("sk-secret-must-not-leak")

    llm = InvalidCredentialLLM()
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("列出所有資料可用日期")

    assert not response.success
    assert response.error_code == "LLM_AUTH_FAILED"
    assert response.error == "OpenAI API key 驗證失敗，請到 API 設定更新金鑰。"
    assert response.evidence == {
        "attempts": 1,
        "last_error": "LLM_OUTPUT_ERROR: AuthenticationError",
    }
    assert llm.calls == 1
    assert "sk-secret" not in str(response.to_dict())
