from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from text2sql.llm import (
    LOCAL_API_KEY,
    DisabledLLM,
    LLMIncompleteError,
    LLMRefusedError,
    LLMUnavailableError,
    LocalLLM,
    classify_error,
    is_unavailable,
    parse_generated_query,
)

OLLAMA = "http://127.0.0.1:11434/v1"


def _fake_sdk(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """裝一個假的 openai 套件，回傳 (建 client 的參數, 送出的請求參數)。"""

    client_options: dict[str, object] = {}
    request_options: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> SimpleNamespace:
            request_options.update(kwargs)
            message = SimpleNamespace(content='{"sql":"SELECT 1 LIMIT 1","params":[]}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs: object):
            client_options.update(kwargs)
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    return client_options, request_options


def test_local_adapter_talks_only_to_the_configured_ollama_without_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只連 llm.yaml 指定的本機端點；SDK 自己的重試關掉，重試次數由管線決定。"""

    client_options, request_options = _fake_sdk(monkeypatch)

    adapter = LocalLLM(base_url=OLLAMA, model="qwen3:8b", timeout_seconds=7.5, temperature=0)
    output = adapter.generate("test prompt")

    assert client_options == {
        "api_key": LOCAL_API_KEY,
        "base_url": OLLAMA,
        "timeout": 7.5,
        "max_retries": 0,
    }
    assert request_options["model"] == "qwen3:8b"
    assert request_options["messages"] == [{"role": "user", "content": "test prompt"}]
    assert request_options["response_format"]["json_schema"]["strict"] is True
    param_types = request_options["response_format"]["json_schema"]["schema"]["properties"][
        "params"
    ]["items"]["anyOf"]
    assert param_types == [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
    assert request_options["temperature"] == 0
    assert output.startswith("{")


def test_local_adapter_needs_no_api_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """地端模式不讀任何 *_API_KEY；環境裡有沒有雲端金鑰都不影響它。"""

    client_options, _request_options = _fake_sdk(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    LocalLLM(base_url=OLLAMA, model="m")

    assert client_options["api_key"] == LOCAL_API_KEY


def test_local_adapter_rejects_non_positive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sdk(monkeypatch)
    with pytest.raises(ValueError, match="timeout_seconds"):
        LocalLLM(base_url=OLLAMA, timeout_seconds=0)


def test_local_adapter_requires_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """沒有 base_url 時 SDK 會退回 OpenAI 官方雲端 —— 地端版不能有這條路。"""

    _fake_sdk(monkeypatch)
    with pytest.raises(ValueError, match="base_url"):
        LocalLLM(base_url="")


def test_model_falls_back_to_the_environment_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sdk(monkeypatch)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert LocalLLM(base_url=OLLAMA).model == "qwen3:8b"
    monkeypatch.setenv("OLLAMA_MODEL", "gemma3:4b")
    assert LocalLLM(base_url=OLLAMA).model == "gemma3:4b"


def test_disabled_llm_reports_unavailability_rather_than_a_generic_failure() -> None:
    with pytest.raises(LLMUnavailableError, match="Ollama"):
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


def test_structured_output_can_be_turned_off_for_models_that_lack_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """關掉之後不送 schema，格式改由 prompt 要求與寬容解析負責；安全仍由 SqlGuard 顧。"""

    _client_options, request_options = _fake_sdk(monkeypatch)

    LocalLLM(base_url=OLLAMA, model="m", structured_output=False).generate("test prompt")

    assert "response_format" not in request_options


def test_temperature_is_omitted_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """null 要是「不送」而不是送 0：交給模型自己的預設。"""

    _client_options, request_options = _fake_sdk(monkeypatch)

    LocalLLM(base_url=OLLAMA, model="m").generate("test prompt")

    assert "temperature" not in request_options


def test_extra_body_carries_ollama_specific_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """qwen3 的推理開關不在 SDK 的型別裡，要從 extra_body 送。"""

    _client_options, request_options = _fake_sdk(monkeypatch)

    LocalLLM(base_url=OLLAMA, model="m", extra_body={"reasoning_effort": "none"}).generate("p")

    assert request_options["extra_body"] == {"reasoning_effort": "none"}


# ── 錯誤分類：SDK 丟的是子類別，名稱完全比對一個都接不到 ──────────────────


class _StatusError(Exception):
    """替身：只帶 `status_code`，模擬 SDK 的 APIStatusError 子類別。

    分類刻意靠這個屬性而不靠類別名稱，所以這裡不需要 import openai —— CI 沒裝
    local extra 也跑得動，而且第三方相容端點自己包的例外只要帶狀態碼就一樣分得出來。
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

    沒裝 local extra 的 CI 會 skip；裝了的話，SDK 換版把屬性搬走就會在這裡紅。
    對照版本：openai 2.54.0。
    """

    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    for cls, status in (
        (openai.BadRequestError, 400),
        (openai.AuthenticationError, 401),
        (openai.NotFoundError, 404),
        (openai.RateLimitError, 429),
    ):
        error = cls("boom", response=httpx.Response(status, request=request), body=None)
        assert getattr(error, "status_code", None) == status


# ── 回應完成狀態：文字剛好能解析，不代表模型講完了 ──────────────────────


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


def test_a_length_finish_reason_is_truncation_even_when_the_text_parses(
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
        LocalLLM(base_url=OLLAMA, model="m").generate("prompt")


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
        LocalLLM(base_url=OLLAMA, model="m").generate("prompt")


def test_chat_completions_without_choices_says_so_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`choices[0]` 對空清單會 IndexError，而那個訊息對維運毫無指向性。"""

    _chat_client(monkeypatch, SimpleNamespace(choices=[]))

    with pytest.raises(LLMIncompleteError, match="choices"):
        LocalLLM(base_url=OLLAMA, model="m").generate("prompt")


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
        LocalLLM(base_url=OLLAMA, model="m").generate("prompt")


def test_a_completed_answer_still_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """這些檢查不能把正常回應也擋掉。"""

    _chat_client(
        monkeypatch,
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=VALID_OUTPUT, refusal=None),
                )
            ]
        ),
    )

    assert LocalLLM(base_url=OLLAMA, model="m").generate("prompt") == VALID_OUTPUT


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
