"""Prompt for the realtime branch: views, closed-set values, rules and resolved times.

與 `prompt.build_prompt` 同樣用 JSON 包起來，但不帶語料範例：即時 SQL 不進語料（RT-3b §2），
而檢視只有三張、欄位固定，把規則講清楚比塞範例有用。時間在這裡就解析好交給模型 ——
模型不知道「現在」是幾點，讓它自己寫 `date('now')` 會拿到 UTC，而且評測沒辦法固定。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

REALTIME_DDL = {
    "v_rt_10min": (
        "每 10 分鐘一列、每部機組（或彙總列）一列。"
        "欄位：資料時間 'YYYY-MM-DD HH:MM'、日期、時刻、機組鍵（機組類型|機組名稱）、"
        "機組類型、機組名稱、粒度（個別／彙總／未定）、電廠（可能是 NULL）、"
        "裝置容量_MW、淨發電量_MW（瞬間值）、出力比（0–1）、備註、"
        "數值狀態（正常／無值／無法解析／通訊異常）。明細只保留最近 14 天。"
    ),
    "v_rt_now": "與 v_rt_10min 相同欄位，只有最新一個時段。",
    "v_rt_daily": (
        "每天每部機組一列（已過完的日子）。欄位：日期、機組鍵、機組類型、機組名稱、粒度、電廠、"
        "估算發電量_MWh、最高出力_MW、平均出力_MW、最低出力_MW、取樣點數、應有點數（144）、"
        "資料完整度（0–1）、當日備註、資料來源（即時收集／台電歷史資料）。永久保留；台電歷史資料（d006010）只有台電自有機組。"
    ),
}
REALTIME_RULES = (
    "只能查 v_rt_10min、v_rt_now、v_rt_daily；不可以查或 JOIN 其他表。",
    '所有字串值都用 ? 參數，例如數值狀態要寫 "數值狀態" = ? 並在 params 放 "正常"。',
    '加總出力只算數值狀態為「正常」的值：SUM(CASE WHEN "數值狀態" = ? THEN "淨發電量_MW" END)。',
    "今天到目前的發電量（MWh）＝正常值的加總 ÷ 6.0；已過完的日子用 v_rt_daily 的估算發電量_MWh。",
    "「現在」用 \"資料時間\" = 給定的 latest，不要用 date('now')。",
    "儲能（放電）與儲能負載（充電）是不同的機組類型，不要合併。",
    "一定要有 LIMIT，最多 200。",
)


def build_realtime_prompt(
    question: str,
    *,
    latest: str,
    today: str,
    day: str | None,
    unit_types: Sequence[str],
    prior_error: str | None = None,
    prior_sql: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "task": (
            "將問題轉成 SQLite 單一 SELECT，只查 v_rt_* view，使用 ? placeholder，並加上 LIMIT。"
        ),
        "output": {"sql": "string", "params": ["JSON scalar"]},
        "time": {
            "timezone": "Asia/Taipei (UTC+8)",
            "latest": latest,
            "today": today,
            "today_start": f"{today} 00:00",
            **({"day": day} if day else {}),
        },
        "ddl": REALTIME_DDL,
        "column_values": {
            "機組類型": list(unit_types),
            "數值狀態": ["正常", "無值", "無法解析", "通訊異常"],
            "粒度": ["個別", "彙總", "未定"],
        },
        "rules": list(REALTIME_RULES),
        "question": question,
    }
    if prior_sql:
        payload["previous_attempt_sql"] = prior_sql
    if prior_error:
        payload["previous_attempt_error"] = prior_error
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
