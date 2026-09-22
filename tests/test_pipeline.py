import json
from pathlib import Path

import pytest

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
    assert response.evidence == {
        "attempts": 1,
        "llm_error": "authentication",
        "last_error": "LLM_OUTPUT_ERROR: AuthenticationError",
    }
    assert llm.calls == 1
    assert "sk-secret" not in str(response.to_dict())


def test_the_credential_message_names_the_variable_this_provider_reads() -> None:
    """原本寫死「OpenAI API key」。用 GMI 的人照著去翻 OpenAI 的設定，翻不到東西。"""

    class AuthenticationError(Exception):
        status_code = 401

    class GmiLLM:
        api_key_env = "GMI_API_KEY"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, _prompt: str) -> str:
            self.calls += 1
            raise AuthenticationError("401")

    response = make_pipeline(GmiLLM(), lambda _sql, _params: ([], [])).query("列出所有資料可用日期")

    assert response.error_code == "LLM_AUTH_FAILED"
    assert "GMI_API_KEY" in str(response.error)
    assert "OpenAI" not in str(response.error)


@pytest.mark.parametrize(
    ("status", "code"),
    ((400, "LLM_REQUEST_REJECTED"), (404, "LLM_REQUEST_REJECTED"), (422, "LLM_REQUEST_REJECTED")),
)
def test_a_request_the_provider_rejects_is_sent_once_not_three_times(
    status: int, code: str
) -> None:
    """模型名稱、端點或輸出格式不被接受，重送同一份不會變成被接受。

    原本這幾個被當成「模型答得不好」跑完整個 SQL 修復迴圈：三倍的錢與等待，換來一句
    指向錯方向的錯誤 —— 而且在會觸發缺參數反問的題目上，使用者看到的是「這句沒有指名
    是哪一座電廠」，跟真正的原因完全無關。
    """

    class Rejected(Exception):
        def __init__(self) -> None:
            super().__init__(f"HTTP {status}")
            self.status_code = status

    class RejectingLLM:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, _prompt: str) -> str:
            self.calls += 1
            raise Rejected()

    llm = RejectingLLM()
    response = make_pipeline(llm, lambda _sql, _params: ([], [])).query("列出所有資料可用日期")

    assert llm.calls == 1
    assert response.error_code == code
    assert response.evidence["llm_error"] == "configuration"


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


def test_the_trace_says_how_many_examples_the_floor_dropped() -> None:
    """全被砍掉時 prompt 只剩 schema 與領域規則，那比塞五個 0.000 分的範例好。

    但要看得到它發生了 —— 否則長尾答不好時，沒人知道模型手上根本沒有範例。
    """

    valid = json.dumps(
        {"sql": 'SELECT "日期" FROM v_system LIMIT 1', "params": []}, ensure_ascii=False
    )
    pipeline = Text2SQLPipeline(
        llm=FakeLLM([valid]),
        sql_guard=SqlGuard(),
        run_sql=lambda _sql, _params: (["日期"], [("2026-07-31",)]),
        corpus_path=CORPUS,
        data_range=("2025-01-01", "2026-07-31"),
        peak_columns={"台中#2"},
        min_score=0.9,
    )
    response = pipeline.query("asdfghjkl 完全無關的字")
    step = next(item for item in response.data["trace"] if item["stage"] == "retrieve")
    assert step["example_ids"] == []
    assert step["dropped"] == pipeline.top_k


def test_a_pipeline_without_floors_keeps_every_example() -> None:
    """預設不篩 —— 門檻是 serving 層從設定檔給的，不是管線自己長出來的。"""

    pipeline = Text2SQLPipeline(
        llm=FakeLLM(["SELECT 1"] * 3),
        sql_guard=SqlGuard(),
        run_sql=lambda _sql, _params: ([], []),
        corpus_path=CORPUS,
        data_range=("2025-01-01", "2026-07-31"),
        peak_columns={"台中#2"},
    )
    assert pipeline.min_score == 0.0
    assert pipeline.relative_score == 0.0
    response = pipeline.query("asdfghjkl 完全無關的字")
    step = next(item for item in response.data["trace"] if item["stage"] == "retrieve")
    assert step["dropped"] == 0
    assert len(step["example_ids"]) == pipeline.top_k


def test_a_retry_shows_the_model_what_it_wrote_last_time() -> None:
    """守門的錯誤碼是我們自己定義的分類，不像資料庫錯誤那樣指名道姓。"""

    rejected = json.dumps({"sql": "SELECT * FROM sqlite_master", "params": []}, ensure_ascii=False)
    accepted = json.dumps(
        {"sql": 'SELECT "日期" FROM v_system LIMIT 1', "params": []}, ensure_ascii=False
    )
    llm = FakeLLM([rejected, accepted])
    response = make_pipeline(llm, lambda _sql, _params: (["日期"], [("2026-07-31",)])).query(
        "列出所有資料可用日期"
    )
    assert response.success
    assert len(llm.calls) == 2
    retry = json.loads(llm.calls[1])
    assert retry["previous_attempt_sql"] == "SELECT * FROM sqlite_master"
    assert "sqlite_master" in retry["previous_attempt_error"]


def test_a_malformed_answer_leaves_no_sql_to_hand_back() -> None:
    """解析就失敗的時候手上沒有 SQL，不能把上一輪的舊 SQL 冒充成這一輪的。

    注意「非 JSON」不算解析失敗 —— `parse_generated_query` 刻意把它當成純 SQL 字串
    （模型很愛在 SQL 前後講話），那條路有 SqlGuard 擋，而且應該把原文還給模型看。
    真正解析不了的是 schema 對不起來，例如 sql 不是字串。
    """

    accepted = json.dumps(
        {"sql": 'SELECT "日期" FROM v_system LIMIT 1', "params": []}, ensure_ascii=False
    )
    llm = FakeLLM(['{"sql": 123, "params": []}', accepted])
    response = make_pipeline(llm, lambda _sql, _params: (["日期"], [("2026-07-31",)])).query(
        "列出所有資料可用日期"
    )
    assert response.success
    retry = json.loads(llm.calls[1])
    assert "previous_attempt_sql" not in retry
    assert retry["previous_attempt_error"] == "LLM_OUTPUT_ERROR: ValueError"


def test_a_chatty_answer_is_handed_back_verbatim() -> None:
    """模型在 SQL 外面講話時，被擋下的原文要還給它看，它才知道自己輸出了什麼。"""

    accepted = json.dumps(
        {"sql": 'SELECT "日期" FROM v_system LIMIT 1', "params": []}, ensure_ascii=False
    )
    llm = FakeLLM(["好的，以下是查詢：SELECT * FROM sqlite_master 希望有幫助！", accepted])
    response = make_pipeline(llm, lambda _sql, _params: (["日期"], [("2026-07-31",)])).query(
        "列出所有資料可用日期"
    )
    assert response.success
    retry = json.loads(llm.calls[1])
    assert "希望有幫助" in retry["previous_attempt_sql"]
