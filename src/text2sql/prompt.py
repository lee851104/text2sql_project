"""Prompt assembly with explicit schema, rules, data range, and retrieved examples."""

from __future__ import annotations

import json
from typing import Any

from text2sql.retriever import RetrievedExample

EXAMPLES_NOTE = "由不像到最像排序，最後一則最接近本題，請優先模仿它。"


def build_prompt(
    question: str,
    *,
    corpus: dict[str, Any],
    examples: list[RetrievedExample],
    data_range: tuple[str, str],
    prior_error: str | None = None,
    prior_sql: str | None = None,
) -> str:
    """Assemble the prompt: task → schema → rules → examples → question → last failure.

    ★ 範例由**不像到最像**排序
      最接近本題的那一則緊鄰 `question`。模型對長文中段的注意力最弱，而 examples 是這
      份 prompt 裡唯一會隨語料長大的區塊 —— 現在整份約 2.3 KB，差異還小，語料變多以後
      就不是了。排序本身沒有意義除非講出來，所以另附 `examples_note`。

    ★ 重試時把**上次的 SQL 一起還給模型**
      只給錯誤碼不夠。`SQL_MISSING_TABLE: 查詢必須從語意檢視讀取資料。` 是我們自己定義
      的分類，不像資料庫錯誤那樣指名道姓，模型收到它並不知道自己上次查了哪張表。
      整份脈絡（ddl／rules／examples）也一律保留 —— 模型第一次寫錯，不該連參考書一起
      被收走。
    """

    payload: dict[str, Any] = {
        "task": "將問題轉成 SQLite 單一 SELECT，只查 v_* view，使用 ? placeholder，並加上 LIMIT。",
        "output": {"sql": "string", "params": ["JSON scalar"]},
        "data_range": {"start": data_range[0], "end": data_range[1]},
        "ddl": corpus["ddl"],
        "rules": corpus["documentation"],
        "examples_note": EXAMPLES_NOTE,
        "examples": [item.example for item in sorted(examples, key=lambda item: item.score)],
        "question": question,
    }
    if prior_sql:
        payload["previous_attempt_sql"] = prior_sql
    if prior_error:
        payload["previous_attempt_error"] = prior_error
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
