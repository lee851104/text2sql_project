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


def test_a_near_miss_asks_back_instead_of_failing_with_nothing() -> None:
    """規則接不住、LLM 也產不出來時，反問最接近的問法。

    使用者原本只會看到「SQL 在重試上限內未能通過驗證與執行」，那對他沒有任何幫助。
    """

    llm = FakeLLM(["DROP TABLE v_peak"] * 3)
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("燃料別有哪幾種")

    assert not response.success
    assert response.error_code == "DATA_SCOPE_NEAR_MATCH"
    assert response.severity == "clarify"
    assert "燃料別有哪些" in response.suggestions


def test_a_wrong_topic_guess_is_not_offered() -> None:
    """「電廠總共有幾間」最接近的是「總共有幾台機組」—— 主題是錯的。

    低於門檻就閉嘴。猜錯的代價是使用者以為那就是他問的，而畫面上不會有任何異狀。
    """

    llm = FakeLLM(["DROP TABLE v_peak"] * 3)
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("電廠總共有幾間")

    assert response.error_code == "GENERATION_FAILED"
    assert not response.suggestions


def test_a_missing_parameter_is_asked_about_instead_of_guessed() -> None:
    """「某天…排行榜」沒有說是哪一天。猜一天會回一張看起來完全正常、但不是他要的表。"""

    llm = FakeLLM(["DROP TABLE v_peak"] * 3)
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("某天機組尖峰功率排行榜")
    assert not response.success
    assert response.error_code == "MISSING_PARAMETER"
    assert response.severity == "clarify"
    assert response.evidence["missing"] == "date"
    assert response.suggestions and "排行" in response.suggestions[0]


def test_the_clarification_never_takes_a_question_the_model_answered() -> None:
    """反問排在重試迴圈之後，線上模型生得出能過守門的 SQL 就走不到它。"""

    valid = json.dumps(
        {"sql": 'SELECT "日期" FROM v_peak LIMIT 20', "params": []}, ensure_ascii=False
    )
    llm = FakeLLM([valid])
    response = make_pipeline(llm, lambda _sql, _params: (["日期"], [("2026-07-31",)])).query(
        "某天機組尖峰功率排行榜"
    )
    assert response.success
    assert response.data["source"] == "llm"


class _BoomLLM:
    """每次呼叫都以指定的例外名稱失敗，用來模擬線上服務的各種掛法。"""

    def __init__(self, name: str) -> None:
        self.error = type(name, (Exception,), {})
        self.calls = 0

    def generate(self, prompt: str) -> str:
        del prompt
        self.calls += 1
        raise self.error("simulated")


def test_an_unreachable_service_is_not_retried_and_says_so() -> None:
    """連不上的時候重問同一句只會讓使用者多等兩輪逾時，而且錯誤碼要講實話。"""

    llm = _BoomLLM("APIConnectionError")
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("查各類別歷史最大出力")
    assert llm.calls == 1
    assert response.error_code == "LLM_UNAVAILABLE"
    assert response.evidence["attempts"] == 1


def test_a_bad_answer_is_still_retried_to_the_limit() -> None:
    """模型有回答、只是答不好 —— 這種要重試，不能誤判成服務不通。"""

    llm = _BoomLLM("ValueError")
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("查各類別歷史最大出力")
    assert llm.calls == 3
    assert response.error_code == "GENERATION_FAILED"


def test_the_offline_answers_still_work_while_the_service_is_down() -> None:
    """降級的重點不是切換模型，是線上掛掉時離線做得到的事一件都不少。"""

    llm = _BoomLLM("APIConnectionError")
    pipeline = make_pipeline(llm, lambda _sql, _params: (["日期"], [("2026-07-31", 1.0)]))

    answered = pipeline.query("2026年7月備轉容量率最低是哪一天")
    assert answered.success
    assert llm.calls == 0, "規則題根本不該碰到 LLM"

    asked = pipeline.query("某天機組尖峰功率排行榜")
    assert asked.error_code == "MISSING_PARAMETER"
    assert asked.severity == "clarify"
