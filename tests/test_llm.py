from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from text2sql.llm import DisabledLLM, LLMUnavailableError, OpenAILLM, is_unavailable


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
