"""Local (Ollama) and deterministic offline LLM adapters."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol


class LLMUnavailableError(RuntimeError):
    """本機生成這次用不了：沒啟用、Ollama 沒開、連不上或逾時。

    與「模型回答了但生不出能過守門的 SQL」是兩件事，必須分得開 —— 前者叫使用者稍後
    再試，後者叫他換個問法。兩者共用一句「SQL 在重試上限內未能通過驗證與執行」的話，
    他只會兩件事都做不了，維運也分不出是服務掛了還是覆蓋不足。
    """


class LLMRefusedError(RuntimeError):
    """模型拒絕回答。拿去跑 SQL 修復迴圈，是為一個不會改變的答案多等三輪。"""


class LLMIncompleteError(RuntimeError):
    """回應沒講完：截斷、空內容，或連 choices 都沒有。

    截斷處剛好落在合法 JSON 之後時，文字看不出任何問題 —— 但那不是模型想講的全部，
    拿去執行等於執行一句被剪掉一半的 SQL。
    """


# 依 HTTP 狀態碼分類，不看類別名稱。SDK 丟出來的是 APIStatusError 的**子類別**，而
# `type(error).__name__` 是完全比對 —— 原本名單裡的 "APIStatusError" 只在狀態碼對不到
# 特定子類別時才派上用場，於是 400／404／409／422 全部漏掉，被當成「模型答得不好」
# 重送三次。名稱比對留作後備：相容端點自己包的例外可能沒有 status_code。
AUTHENTICATION_ERROR_NAMES = frozenset(
    {"AuthenticationError", "PermissionDeniedError", "OAuthError"}
)
RATE_LIMIT_ERROR_NAMES = frozenset({"RateLimitError"})
CONFIGURATION_ERROR_NAMES = frozenset(
    {"BadRequestError", "NotFoundError", "ConflictError", "UnprocessableEntityError"}
)
SERVICE_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "APIStatusError",
        "InternalServerError",
        "ConnectionError",
        "TimeoutError",
    }
)

# 只有這一類值得重送。其餘重送同一個 prompt 只會得到同一個結果，還多等兩輪。
RETRYABLE_CATEGORY = "output"


def classify_error(error: BaseException) -> str:
    """把轉接層的例外分成 pipeline 能據以決策的類別。

    configuration（請求本身不被接受：模型名稱、端點、輸出格式）、authentication、
    rate_limit、service、refused、incomplete，以及 output（模型答了但答得不能用）。
    """

    if isinstance(error, LLMRefusedError):
        return "refused"
    if isinstance(error, LLMIncompleteError):
        return "incomplete"
    if isinstance(error, LLMUnavailableError):
        return "service"
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in {401, 403}:
            return "authentication"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "service"
        if status >= 400:
            return "configuration"
    name = type(error).__name__
    if name in AUTHENTICATION_ERROR_NAMES:
        return "authentication"
    if name in RATE_LIMIT_ERROR_NAMES:
        return "rate_limit"
    if name in CONFIGURATION_ERROR_NAMES:
        return "configuration"
    if name in SERVICE_ERROR_NAMES:
        return "service"
    return RETRYABLE_CATEGORY


def is_unavailable(error: BaseException) -> bool:
    """這個例外是「服務不通」還是「模型答得不好」？（窄問法，分類以 classify_error 為準。）"""

    return classify_error(error) in {"service", "rate_limit"}


class LLMProtocol(Protocol):
    def generate(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class GeneratedQuery:
    sql: str
    params: tuple[object, ...]


# strict json_schema 只在支援它的端點上成立。關掉 structured output 的退路、以及
# 相容端點自己的實作，都只剩這裡把關，所以純量與長度要在本地再驗一次。
MAXIMUM_SQL_LENGTH = 4000
MAXIMUM_PARAM_COUNT = 50
MAXIMUM_PARAM_LENGTH = 500


def _reject_constant(name: str) -> float:
    # json 預設把 NaN／Infinity 解成浮點數。它們進了 SQL 比較會全部為假而且不報錯，
    # 看起來就像「真的沒有符合的資料」。
    raise ValueError(f"LLM params 不接受 {name}。")


def _scalar_param(value: object, index: int) -> object:
    """參數必須是能綁進 SQLite 的純量。dict／list 會讓驅動丟 InterfaceError。"""

    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"LLM params[{index}] 不是有限的數值。")
        return value
    if isinstance(value, str):
        if len(value) > MAXIMUM_PARAM_LENGTH:
            raise ValueError(f"LLM params[{index}] 超過 {MAXIMUM_PARAM_LENGTH} 字元。")
        return value
    raise ValueError(f"LLM params[{index}] 必須是純量，收到 {type(value).__name__}。")


# 推理模型（例如 Ollama 上的 qwen3）會先吐一段 <think>…</think> 才給答案；答案本身也常被包在
# ```json 圍欄裡、前面還有一句說明。這兩種都先剝掉，剩下的才是要解析的輸出。
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def parse_generated_query(output: str) -> GeneratedQuery:
    value = _THINK_BLOCK.sub("", output).strip()
    fenced = _JSON_FENCE.search(value)
    if fenced is not None and not value.startswith("{"):
        value = fenced.group(1).strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.lower().startswith("sql\n"):
            value = value[4:]
    try:
        payload = json.loads(value, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        return GeneratedQuery(value, ())
    if not isinstance(payload, dict) or not isinstance(payload.get("sql"), str):
        raise ValueError("LLM 輸出必須含字串 sql 欄位。")
    sql = payload["sql"].strip()
    if len(sql) > MAXIMUM_SQL_LENGTH:
        raise ValueError(f"LLM sql 超過 {MAXIMUM_SQL_LENGTH} 字元。")
    params = payload.get("params", [])
    if not isinstance(params, list):
        raise ValueError("LLM params 必須是陣列。")
    if len(params) > MAXIMUM_PARAM_COUNT:
        raise ValueError(f"LLM params 超過 {MAXIMUM_PARAM_COUNT} 個。")
    return GeneratedQuery(sql, tuple(_scalar_param(item, i) for i, item in enumerate(params)))


class FakeLLM:
    def __init__(self, outputs: Iterable[str]):
        self._outputs = iter(outputs)
        self.calls: list[str] = []

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        try:
            return next(self._outputs)
        except StopIteration as error:
            raise RuntimeError("FakeLLM 沒有更多預設輸出。") from error


class DisabledLLM:
    """Explicitly fail long-tail generation when the local LLM is not in use."""

    def generate(self, prompt: str) -> str:
        del prompt
        raise LLMUnavailableError(
            "本機 LLM 未啟用；請啟動 Ollama，並把執行模式切到本機 LLM 後重試。"
        )


QUERY_SCHEMA_NAME = "text2sql_query"
QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "params": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
            },
        },
    },
    "required": ["sql", "params"],
    "additionalProperties": False,
}

# Ollama 不驗證 key，但 OpenAI SDK 沒有 api_key 就不肯建 client。這不是祕密。
LOCAL_API_KEY = "ollama"


class LocalLLM:
    """Adapter for a local model served by Ollama's OpenAI-compatible chat endpoint.

    只打本機（或同一個內網）的 Ollama，問句與資料不會離開這台機器。用 OpenAI SDK
    是因為 Ollama 的 `/v1/chat/completions` 跟它相容，連 strict json_schema 輸出都接，
    省得自己再寫一套 HTTP、逾時與錯誤分類。

    `structured_output=False` 是給不支援 json_schema 的模型用的退路 —— 這時輸出格式只
    剩 prompt 裡的要求，靠 `parse_generated_query` 的寬容解析接住，而生出來的東西一樣
    要過 `SqlGuard`，所以放寬的是格式保證，不是安全。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None = None,
        timeout_seconds: float = 120.0,
        structured_output: bool = True,
        temperature: float | None = None,
        model_env: str = "OLLAMA_MODEL",
        default_model: str = "qwen3:8b",
        extra_body: Mapping[str, Any] | None = None,
    ):
        if not base_url:
            raise ValueError("base_url 不可為空；本機 LLM 要指名 Ollama 的位址。")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必須大於 0。")
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("請先執行 `uv sync --extra local`。") from error
        self.client = OpenAI(
            api_key=LOCAL_API_KEY,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self.base_url = base_url
        self.model = model or os.getenv(model_env, default_model)
        self.structured_output = structured_output
        # SDK 沒有型別的參數，例如用 reasoning_effort: none 關掉 qwen3 的推理。
        self.extra_body = dict(extra_body or {})
        self.temperature = temperature

    def generate(self, prompt: str) -> str:
        options: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.structured_output:
            options["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": QUERY_SCHEMA_NAME,
                    "strict": True,
                    "schema": QUERY_SCHEMA,
                },
            }
        if self.temperature is not None:
            options["temperature"] = self.temperature
        if self.extra_body:
            options["extra_body"] = self.extra_body
        response = self.client.chat.completions.create(**options)
        choices = getattr(response, "choices", None) or ()
        if not choices:
            raise LLMIncompleteError("模型回應沒有任何 choices；端點或模型設定可能不相容。")
        choice = choices[0]
        message = getattr(choice, "message", None)
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise LLMRefusedError(f"模型拒絕回答：{refusal}")
        finish = getattr(choice, "finish_reason", None)
        if finish == "length":
            raise LLMIncompleteError("模型回應在 length 上限被截斷；沒講完的 SQL 不會拿去執行。")
        if finish == "content_filter":
            raise LLMRefusedError("模型回應被內容過濾擋下。")
        content = getattr(message, "content", None) or ""
        if not content.strip():
            raise LLMIncompleteError("模型回了空內容。")
        return content
