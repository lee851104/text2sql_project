from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from text2sql.llm import (
    DisabledLLM,
    LLMIncompleteError,
    LLMRefusedError,
    LLMUnavailableError,
    OpenAILLM,
    classify_error,
    is_unavailable,
    parse_generated_query,
)


def test_openai_adapter_uses_bounded_client_without_nested_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_options: dict[str, object] = {}
    request_options: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs: object) -> SimpleNamespace:
            request_options.update(kwargs)
            return SimpleNamespace(output_text='{"sql":"SELECT 1 LIMIT 1","params":[]}')

    class FakeOpenAI:
        def __init__(self, **kwargs: object):
            client_options.update(kwargs)
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    adapter = OpenAILLM(
        api_key="sk-test-timeout-only",
        model="test-model",
        timeout_seconds=7.5,
    )
    output = adapter.generate("test prompt")

    assert client_options == {
        "api_key": "sk-test-timeout-only",
        "timeout": 7.5,
        "max_retries": 0,
    }
    assert request_options["model"] == "test-model"
    assert request_options["store"] is False
    param_types = request_options["text"]["format"]["schema"]["properties"]["params"]["items"][
        "anyOf"
    ]
    assert param_types == [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
    assert output.startswith("{")


def test_openai_adapter_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        OpenAILLM(api_key="sk-test", timeout_seconds=0)


def test_disabled_llm_reports_unavailability_rather_than_a_generic_failure() -> None:
    with pytest.raises(LLMUnavailableError):
        DisabledLLM().generate("任何 prompt")


@pytest.mark.parametrize(
    ("name", "unavailable"),
    (
        ("APIConnectionError", True),
        ("APITimeoutError", True),
        ("RateLimitError", True),
        ("InternalServerError", True),
        ("AuthenticationError", False),
        ("ValueError", False),
    ),
)
def test_service_outages_are_told_apart_from_a_bad_answer(name: str, unavailable: bool) -> None:
    """「連不上」與「答得不好」要分得開：前者不該重試，後者該。"""

    assert is_unavailable(type(name, (Exception,), {})("simulated")) is unavailable
    assert is_unavailable(LLMUnavailableError("未啟用")) is True


def _fake_sdk(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """裝一個假的 openai 套件，回傳 (建 client 的參數, 送出的請求參數)。"""

    client_options: dict[str, object] = {}
    request_options: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> SimpleNamespace:
            request_options.update(kwargs)
            message = SimpleNamespace(content='{"sql":"SELECT 1 LIMIT 1","params":[]}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeResponses:
        def create(self, **kwargs: object) -> SimpleNamespace:
            request_options.update(kwargs)
            return SimpleNamespace(output_text='{"sql":"SELECT 1 LIMIT 1","params":[]}')

    class FakeOpenAI:
        def __init__(self, **kwargs: object):
            client_options.update(kwargs)
            self.responses = FakeResponses()
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    return client_options, request_options


def test_chat_completions_provider_sends_base_url_and_the_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第三方講「OpenAI 相容」指的是 chat/completions，不是 OpenAI 自家的 responses。

    只換 base_url 而不換端點會打到一個對方沒有的路徑，所以兩者必須一起切換。
    """

    client_options, request_options = _fake_sdk(monkeypatch)

    adapter = OpenAILLM(
        api_key="gmi-test-key",
        model="meta-llama/Llama-3.3-70B-Instruct",
        base_url="https://api.gmi-serving.com/v1",
        api="chat_completions",
        temperature=0,
    )
    output = adapter.generate("test prompt")

    assert client_options["base_url"] == "https://api.gmi-serving.com/v1"
    assert request_options["messages"] == [{"role": "user", "content": "test prompt"}]
    assert "input" not in request_options, "chat completions 不該送 responses 的欄位"
    assert request_options["response_format"]["json_schema"]["strict"] is True
    assert request_options["temperature"] == 0
    assert output.startswith("{")


def test_default_provider_still_omits_base_url_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沒指定 base_url 就不能送這個鍵 —— 傳 None 進去會蓋掉 SDK 自己的預設。"""

    client_options, _request_options = _fake_sdk(monkeypatch)

    OpenAILLM(api_key="sk-test", model="test-model")

    assert "base_url" not in client_options


def test_structured_output_can_be_turned_off_for_endpoints_that_lack_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """關掉之後不送 schema，格式改由 prompt 要求與寬容解析負責；安全仍由 SqlGuard 顧。"""

    _client_options, request_options = _fake_sdk(monkeypatch)

    adapter = OpenAILLM(api_key="k", model="m", api="chat_completions", structured_output=False)
    adapter.generate("test prompt")

    assert "response_format" not in request_options


def test_temperature_is_omitted_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI 的 reasoning 模型不吃 temperature，所以 null 要是「不送」而不是送 0。"""

    _client_options, request_options = _fake_sdk(monkeypatch)

    OpenAILLM(api_key="k", model="m").generate("test prompt")

    assert "temperature" not in request_options


def test_unknown_api_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sdk(monkeypatch)
    with pytest.raises(ValueError, match="不支援的 api"):
        OpenAILLM(api_key="k", api="grpc")


def test_api_key_env_is_configurable_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """每家一個環境變數；認錯名字的話，設了 GMI key 的人會被當成沒有 key。"""

    _fake_sdk(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GMI_API_KEY", "gmi-from-environment")

    adapter = OpenAILLM(api_key_env="GMI_API_KEY", model_env="GMI_MODEL", default_model="llama")

    assert adapter.model == "llama"
    with pytest.raises(RuntimeError, match="GMI_API_KEY"):
        monkeypatch.delenv("GMI_API_KEY")
        OpenAILLM(api_key_env="GMI_API_KEY")


# ── 錯誤分類：SDK 丟的是子類別，名稱完全比對一個都接不到 ──────────────────


class _StatusError(Exception):
    """替身：只帶 `status_code`，模擬 SDK 的 APIStatusError 子類別。

    分類刻意靠這個屬性而不靠類別名稱，所以這裡不需要 import openai —— CI 沒裝
    online extra 也跑得動，而且第三方相容端點自己包的例外只要帶狀態碼就一樣分得出來。
    """

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status", "category"),
    (
        (400, "configuration"),
        (401, "authentication"),
        (403, "authentication"),
        (404, "configuration"),
        (409, "configuration"),
        (422, "configuration"),
        (429, "rate_limit"),
        (500, "service"),
        (503, "service"),
    ),
)
def test_status_errors_are_classified_by_code_not_by_class_name(status: int, category: str) -> None:
    """原本的名單用 `type(error).__name__` 完全比對。

    SDK 丟出來的是 BadRequestError、NotFoundError 這些子類別，名單裡的 `APIStatusError`
    只在狀態碼對不到特定子類別時才派上用場 —— 於是 400／404／409／422 全部漏掉，
    被當成「模型答得不好」重送三次。
    """

    assert classify_error(_StatusError(status)) == category


def test_connection_and_timeout_failures_are_service_problems() -> None:
    class APIConnectionError(Exception):
        pass

    class APITimeoutError(Exception):
        pass

    assert classify_error(APIConnectionError()) == "service"
    assert classify_error(APITimeoutError()) == "service"
    assert classify_error(LLMUnavailableError("設定未啟用")) == "service"


def test_bad_model_output_is_the_only_thing_worth_regenerating() -> None:
    """只有「模型答了但答得不能用」才該走 SQL 修復迴圈。"""

    assert classify_error(ValueError("LLM params 必須是陣列。")) == "output"


def test_the_real_sdk_exceptions_carry_the_status_code_we_classify_on() -> None:
    """分類靠 `status_code` 這個屬性，所以要確認 SDK 真的有給。

    沒裝 online extra 的 CI 會 skip；裝了的話，SDK 換版把屬性搬走就會在這裡紅。
    對照版本：openai 2.54.0。
    """

    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    for cls, status in (
        (openai.BadRequestError, 400),
        (openai.AuthenticationError, 401),
        (openai.NotFoundError, 404),
        (openai.RateLimitError, 429),
    ):
        error = cls("boom", response=httpx.Response(status, request=request), body=None)
        assert getattr(error, "status_code", None) == status


# ── 回應完成狀態：文字剛好能解析，不代表模型講完了 ──────────────────────


def _responses_client(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    class FakeResponses:
        def create(self, **kwargs: object) -> object:
            del kwargs
            return payload

    class FakeOpenAI:
        def __init__(self, **kwargs: object):
            del kwargs
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))


def _chat_client(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            del kwargs
            return payload

    class FakeOpenAI:
        def __init__(self, **kwargs: object):
            del kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))


VALID_OUTPUT = '{"sql":"SELECT 1 LIMIT 1","params":[]}'


def test_an_incomplete_response_is_refused_even_when_its_text_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """截斷處剛好落在合法 JSON 之後，文字就看不出問題 —— 但它不是模型想講的全部。"""

    _responses_client(
        monkeypatch,
        SimpleNamespace(
            output_text=VALID_OUTPUT,
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        ),
    )

    with pytest.raises(LLMIncompleteError, match="max_output_tokens"):
        OpenAILLM(api_key="sk-test", model="m").generate("prompt")


def test_a_refusal_is_not_mistaken_for_an_unparseable_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拒答拿去跑 SQL 修復迴圈，等於為一個不會改變的答案付三次錢。"""

    _responses_client(
        monkeypatch,
        SimpleNamespace(
            output_text="",
            status="completed",
            output=[
                SimpleNamespace(
                    content=[SimpleNamespace(type="refusal", refusal="我不能協助這個請求")]
                )
            ],
        ),
    )

    with pytest.raises(LLMRefusedError, match="我不能協助這個請求"):
        OpenAILLM(api_key="sk-test", model="m").generate("prompt")


def test_a_completed_response_still_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """新增的檢查不能把正常回應也擋掉。"""

    _responses_client(
        monkeypatch,
        SimpleNamespace(output_text=VALID_OUTPUT, status="completed", incomplete_details=None),
    )

    assert OpenAILLM(api_key="sk-test", model="m").generate("prompt") == VALID_OUTPUT


def test_a_compatible_endpoint_without_status_metadata_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相容端點可能整個 status 欄位都沒有；沒有資訊不等於有問題。"""

    _responses_client(monkeypatch, SimpleNamespace(output_text=VALID_OUTPUT))

    assert OpenAILLM(api_key="sk-test", model="m").generate("prompt") == VALID_OUTPUT


def test_chat_completions_length_finish_reason_is_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat_client(
        monkeypatch,
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=VALID_OUTPUT, refusal=None),
                )
            ]
        ),
    )

    with pytest.raises(LLMIncompleteError, match="length"):
        OpenAILLM(api_key="sk-test", model="m", api="chat_completions").generate("prompt")


def test_chat_completions_refusal_field_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    _chat_client(
        monkeypatch,
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=None, refusal="我不能協助這個請求"),
                )
            ]
        ),
    )

    with pytest.raises(LLMRefusedError):
        OpenAILLM(api_key="sk-test", model="m", api="chat_completions").generate("prompt")


def test_chat_completions_without_choices_says_so_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`choices[0]` 對空清單會 IndexError，而那個訊息對維運毫無指向性。"""

    _chat_client(monkeypatch, SimpleNamespace(choices=[]))

    with pytest.raises(LLMIncompleteError, match="choices"):
        OpenAILLM(api_key="sk-test", model="m", api="chat_completions").generate("prompt")


def test_chat_completions_empty_content_is_reported_not_regenerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat_client(
        monkeypatch,
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="   ", refusal=None)
                )
            ]
        ),
    )

    with pytest.raises(LLMIncompleteError):
        OpenAILLM(api_key="sk-test", model="m", api="chat_completions").generate("prompt")


# ── 本地輸出驗證：strict schema 不是每條路徑都有 ────────────────────────


@pytest.mark.parametrize(
    ("label", "raw"),
    (
        ("巢狀 dict", '{"sql":"SELECT 1","params":[{"bad":"type"}]}'),
        ("巢狀 list", '{"sql":"SELECT 1","params":[[1,2]]}'),
        ("NaN", '{"sql":"SELECT 1","params":[NaN]}'),
        ("Infinity", '{"sql":"SELECT 1","params":[Infinity]}'),
        ("溢位成 inf 的字面量", '{"sql":"SELECT 1","params":[1e400]}'),
    ),
)
def test_params_that_are_not_finite_scalars_are_rejected(label: str, raw: str) -> None:
    """關掉 structured output 的路徑只剩這裡把關，而它原本只檢查 params 是不是 list。"""

    del label
    with pytest.raises(ValueError):
        parse_generated_query(raw)


def test_an_over_long_param_is_rejected() -> None:
    raw = json.dumps({"sql": "SELECT 1", "params": ["x" * 5000]})

    with pytest.raises(ValueError, match="字元"):
        parse_generated_query(raw)


def test_ordinary_params_still_pass() -> None:
    raw = json.dumps({"sql": "SELECT 1", "params": ["煤", 2024, 1.5, True, None]})

    assert parse_generated_query(raw).params == ("煤", 2024, 1.5, True, None)


def test_disabled_llm_names_the_variable_the_active_provider_actually_reads() -> None:
    """用 GMI 的人被指去設 OPENAI_API_KEY，會照做，然後繼續不能用。"""

    with pytest.raises(LLMUnavailableError, match="GMI_API_KEY"):
        DisabledLLM(api_key_env="GMI_API_KEY").generate("任何 prompt")
