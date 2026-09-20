"""Online and deterministic offline LLM adapters."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol


class LLMUnavailableError(RuntimeError):
    """線上生成這次用不了：沒設定、連不上、逾時或額度用盡。

    與「模型回答了但生不出能過守門的 SQL」是兩件事，必須分得開 —— 前者叫使用者稍後
    再試，後者叫他換個問法。兩者共用一句「SQL 在重試上限內未能通過驗證與執行」的話，
    他只會兩件事都做不了，維運也分不出是服務掛了還是覆蓋不足。
    """


# 這些 OpenAI 例外代表「服務這次不通」，重試同一個 prompt 不會有不同結果，只會讓使用
# 者多等兩倍的 timeout。按名稱比對是因為 openai 套件是選用相依，離線環境 import 不到。
UNAVAILABLE_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "APIStatusError",
        "InternalServerError",
        "RateLimitError",
        "ConnectionError",
        "Timeout",
    }
)


def is_unavailable(error: BaseException) -> bool:
    """這個例外是「服務不通」還是「模型答得不好」？"""

    return isinstance(error, LLMUnavailableError) or type(error).__name__ in (
        UNAVAILABLE_ERROR_NAMES
    )


class LLMProtocol(Protocol):
    def generate(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class GeneratedQuery:
    sql: str
    params: tuple[object, ...]


def parse_generated_query(output: str) -> GeneratedQuery:
    value = output.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.lower().startswith("sql\n"):
            value = value[4:]
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return GeneratedQuery(value, ())
    if not isinstance(payload, dict) or not isinstance(payload.get("sql"), str):
        raise ValueError("LLM 輸出必須含字串 sql 欄位。")
    params = payload.get("params", [])
    if not isinstance(params, list):
        raise ValueError("LLM params 必須是陣列。")
    return GeneratedQuery(payload["sql"].strip(), tuple(params))


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
    """Explicitly fail long-tail generation when online access is not configured."""

    def generate(self, prompt: str) -> str:
        del prompt
        raise LLMUnavailableError("線上 LLM 未啟用；請設定 OPENAI_API_KEY 後重試。")


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

# `responses` 是 OpenAI 自家的 /v1/responses；`chat_completions` 是業界相容的那個。
# 「OpenAI 相容」的第三方服務指的幾乎都是後者 —— GMI 的文件示範就是 chat.completions，
# 所以只換 base_url 而不換端點會直接 404。
SUPPORTED_APIS = frozenset({"responses", "chat_completions"})


class OpenAILLM:
    """Adapter for any OpenAI-compatible endpoint, not just OpenAI's own.

    一個類別涵蓋兩家的原因是它們共用同一個 SDK 與同一份 JSON Schema，差別只在三處：
    打哪個端點（`api`）、打去哪裡（`base_url`）、以及對方吃不吃 structured outputs。
    把這三件事變成參數，比複製一份轉接層再各自長歪要好維護。

    `structured_output=False` 是給不支援 `strict` json_schema 的服務用的退路 ——
    這時輸出格式只剩 prompt 裡的要求，靠 `parse_generated_query` 的寬容解析接住，
    而生出來的東西一樣要過 `SqlGuard`，所以放寬的是格式保證，不是安全。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        base_url: str | None = None,
        api: str = "responses",
        structured_output: bool = True,
        temperature: float | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        model_env: str = "OPENAI_MODEL",
        default_model: str = "gpt-5.4-mini",
    ):
        key = api_key or os.getenv(api_key_env)
        if not key:
            raise RuntimeError(f"缺少 {api_key_env}；線上查詢不會自動改用 FakeLLM。")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必須大於 0。")
        if api not in SUPPORTED_APIS:
            raise ValueError(f"不支援的 api：{api}；可用的是 {sorted(SUPPORTED_APIS)}。")
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("請先執行 `uv sync --extra online`。") from error
        options: dict[str, Any] = {
            "api_key": key,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        # 沒指定就不傳，讓 SDK 用它自己的預設；傳一個 None 進去會蓋掉預設。
        if base_url:
            options["base_url"] = base_url
        self.client = OpenAI(**options)
        self.model = model or os.getenv(model_env, default_model)
        self.api = api
        self.structured_output = structured_output
        self.temperature = temperature

    def generate(self, prompt: str) -> str:
        if self.api == "chat_completions":
            return self._generate_chat_completions(prompt)
        return self._generate_responses(prompt)

    def _generate_responses(self, prompt: str) -> str:
        options: dict[str, Any] = {"model": self.model, "input": prompt, "store": False}
        if self.structured_output:
            options["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": QUERY_SCHEMA_NAME,
                    "strict": True,
                    "schema": QUERY_SCHEMA,
                }
            }
        if self.temperature is not None:
            options["temperature"] = self.temperature
        return self.client.responses.create(**options).output_text

    def _generate_chat_completions(self, prompt: str) -> str:
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
        response = self.client.chat.completions.create(**options)
        return response.choices[0].message.content or ""
