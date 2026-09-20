"""Online and deterministic offline LLM adapters."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


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


class OpenAILLM:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("缺少 OPENAI_API_KEY；線上查詢不會自動改用 FakeLLM。")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必須大於 0。")
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("請先執行 `uv sync --extra online`。") from error
        self.client = OpenAI(api_key=key, timeout=timeout_seconds, max_retries=0)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

    def generate(self, prompt: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "text2sql_query",
                    "strict": True,
                    "schema": {
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
                    },
                }
            },
            store=False,
        )
        return response.output_text
