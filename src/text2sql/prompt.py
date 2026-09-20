"""Prompt assembly with explicit schema, rules, data range, and retrieved examples."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from text2sql.retriever import RetrievedExample

EXAMPLES_NOTE = "由不像到最像排序，最後一則最接近本題，請優先模仿它。"
COLUMN_VALUES_NOTE = "以下欄位的值是封閉集合。參數必須原樣使用這裡列出的值，不要改寫問句裡的說法。"


def build_prompt(
    question: str,
    *,
    corpus: dict[str, Any],
    examples: list[RetrievedExample],
    data_range: tuple[str, str],
    column_values: Mapping[str, Sequence[str]] | None = None,
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

    ★ 封閉集合欄位**連值一起給**，緊跟在 `ddl` 後面
      ddl 只有欄位名時，模型不知道「燃料」裡裝的是「煤」還是「燃煤」，只能抄問句的詞。
      實測它會寫出結構完全正確的 SQL 卻填錯參數，回 0 筆而且不報錯 —— 看起來就像真的
      沒有這種資料。值屬於 schema 的一部分，所以位置緊鄰 ddl 而不是丟進 rules。
    """

    payload: dict[str, Any] = {
        "task": "將問題轉成 SQLite 單一 SELECT，只查 v_* view，使用 ? placeholder，並加上 LIMIT。",
        "output": {"sql": "string", "params": ["JSON scalar"]},
        "data_range": {"start": data_range[0], "end": data_range[1]},
        "ddl": corpus["ddl"],
    }
    if column_values:
        payload["column_values_note"] = COLUMN_VALUES_NOTE
        payload["column_values"] = {key: list(values) for key, values in column_values.items()}
    payload.update(
        {
            "rules": corpus["documentation"],
            "examples_note": EXAMPLES_NOTE,
            "examples": [item.example for item in sorted(examples, key=lambda item: item.score)],
            "question": question,
        }
    )
    if prior_sql:
        payload["previous_attempt_sql"] = prior_sql
    if prior_error:
        payload["previous_attempt_error"] = prior_error
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
