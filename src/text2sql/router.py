"""Priority-ordered intent routing and zero-cost parameterized handlers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from align.naming import chinese_number
from text2sql.aliases import resolve_peak_column, resolve_peak_columns, resolve_plant
from text2sql.entities import Entities
from text2sql.retriever import TfidfRetriever


@dataclass(frozen=True)
class RoutedQuery:
    intent: str
    sql: str | None = None
    params: tuple[object, ...] = ()


# 入門型「資料裡有什麼」問句。使用者第一次打開時最先想知道的就是邊界，但這類問句
# 沒有日期、電廠或燃料實體，會掉進 other 然後產不出 SQL。
#
# 這裡用**完全比對**而不是包含比對。實測包含比對會偷走既有題目：「大觀發電廠有哪些
# 設備」命中「電廠有哪些」、「碧海在資料期間的峰值日期」命中「資料期間」、「資料涵蓋
# 的最早與最晚日期」命中「資料涵蓋」，離線執行率因此從 100% 掉到 95%。
#
# 代價是換個講法就不會命中，會掉回 other —— 但那與現況相同，不是退步；長尾講法本來
# 就該由線上模式的 LLM 接手（語料已收錄這四句）。
DATA_SCOPE_QUESTIONS: dict[str, frozenset[str]] = {
    "plants": frozenset(
        {
            "有哪些電廠",
            "有哪些發電廠",
            "電廠有哪些",
            "發電廠有哪些",
            "電廠清單",
            "列出所有電廠",
        }
    ),
    "fuels": frozenset(
        {
            "有哪些燃料",
            "有哪些燃料別",
            "有哪些燃料種類",
            "燃料有哪些",
            "燃料別有哪些",
            "燃料種類有哪些",
        }
    ),
    "period": frozenset(
        {
            "資料涵蓋到什麼時候",
            "資料到什麼時候",
            "資料期間",
            "資料範圍",
            "資料涵蓋期間",
        }
    ),
    "units": frozenset({"總共有幾台機組", "共有幾台機組", "有幾台機組", "機組總數"}),
}

_SCOPE_TRAILING = "？?。.！!、，,"


def data_scope_topic(question: str) -> str | None:
    """Return which scope question this is, or None when it is not one.

    只認完整的問句。任何多餘的修飾（電廠名、日期、其他欄位）都表示使用者要問的是別的
    東西，應該交給既有意圖處理。
    """

    compact = re.sub(r"\s+", "", question).strip(_SCOPE_TRAILING)
    for topic, accepted in DATA_SCOPE_QUESTIONS.items():
        if compact in accepted:
            return topic
    return None


def _is_subsequence(needle: str, haystack: str) -> bool:
    """``needle`` 的每個字依序出現在 ``haystack`` 裡，不必相鄰。"""

    iterator = iter(haystack)
    return all(char in iterator for char in needle)


def nearest_scope_topic(question: str) -> str | None:
    """Return the scope topic whose accepted form is a subsequence of ``question``.

    接住「目前有哪些電廠」這種只多了贅字的問法，不必維護一份贅字清單。

    **這個比對本身不安全**：「大觀發電廠有哪些設備」同樣包含「發電廠有哪些」這個子
    序列。安全性來自呼叫端 —— 它是 ``classify_intent`` 的**最後一條**規則，那些題目
    在上游就被更具體的規則接走了。把它往前搬會讓離線執行率再掉一次，
    ``tests/test_data_discovery.py`` 有成對的測試釘住這件事。

    同一句話命中多個主題時回 ``None``：分不出要問什麼就不要猜。
    """

    compact = re.sub(r"\s+", "", question).strip(_SCOPE_TRAILING)
    topics = {
        topic
        for topic, accepted in DATA_SCOPE_QUESTIONS.items()
        if any(_is_subsequence(form, compact) for form in accepted)
    }
    return topics.pop() if len(topics) == 1 else None


# 近似建議的相似度門檻。實測 benchmarks/ 185 題對這 21 句已接受問法的最高分：0.938
# （大觀發電廠有哪些設備）與 0.846（碧海在資料期間的峰值日期）都由更具體的規則接走，
# 走不到建議這一步；走得到的題目最高 0.692。門檻取 0.75 把它們全部留在線下，同時接住
# 「燃料別有哪幾種」(0.879)、「電廠有哪幾座」(0.810) 這種真的只是換個講法的問句。
#
# 低於門檻一律不建議：「電廠總共有幾間」最接近的是「總共有幾台機組」(0.626)，主題是
# 錯的。猜錯的代價是使用者以為那就是他問的，而畫面上不會有任何異狀。
SCOPE_SUGGESTION_THRESHOLD = 0.75


@lru_cache(maxsize=1)
def _scope_retriever() -> TfidfRetriever:
    return TfidfRetriever(
        [
            {"id": f"{topic}|{form}", "question": form}
            for topic, accepted in sorted(DATA_SCOPE_QUESTIONS.items())
            for form in sorted(accepted)
        ]
    )


def suggest_scope_question(question: str) -> str | None:
    """Return the accepted scope question closest to ``question``, or ``None``.

    只在管線已經答不出來時才用 —— 這時候的替代選項是一句「未能通過驗證與執行」，
    所以給得出一個夠接近的問法就是淨賺。給不出來就維持原樣，不硬湊。
    """

    compact = re.sub(r"\s+", "", question).strip(_SCOPE_TRAILING)
    if not compact:
        return None
    best = _scope_retriever().retrieve(compact, top_k=1)
    if not best or best[0].score < SCOPE_SUGGESTION_THRESHOLD:
        return None
    return str(best[0].example["question"])


def classify_intent(question: str) -> str:
    question = re.sub(r"\s+", "", question)
    if "成本" in question:
        return "generation_cost"
    if data_scope_topic(question) is not None:
        return "data_scope"
    # 再生能源只有 v_re_generation 有電量資料；每日尖峰資料的風光欄位是瞬時出力，
    # 因此只在問句明講「發電量／度數」或「自建」時才走這條路，不搶尖峰出力的題目。
    if _renewable_words(question):
        if any(word in question for word in ("裝置容量", "場站", "發電站", "幾座", "哪些站")):
            return "renewable_site"
        if any(word in question for word in ("發電量", "度數", "發了多少", "總發電")):
            return "renewable_generation"
    system_words = ("負載", "備轉", "供電能力", "工業用電", "民生用電", "系統指標")

    if (
        any(
            word in question
            for word in (
                "散點",
                "容量缺口",
                "資料可用日期",
                "系統摘要",
                "各類別",
                "各種類別",
                "每種類別",
                "前七月每月平均",
                "每月工業用電平均",
            )
        )
        or ("各欄位" in question and "容量" in question)
        or ("負載" in question and "備轉" in question)
        or ("每月平均" in question and "備轉" in question)
        or ("工業" in question and "民生" in question)
    ):
        return "other"

    plant_words = ("機組", "設備", "各機", "所屬", "機組名稱", "機組主檔")
    if ("電廠" in question or "廠" in question) and any(word in question for word in plant_words):
        return "plant_units"

    ranking_shape = any(
        word in question
        for word in (
            "前三",
            "前四",
            "前五",
            "前六",
            "前十",
            "排行",
            "排序",
            "名次",
            "由高到低",
            "由小到大",
        )
    ) or bool(
        re.search(
            r"(?:最大|最小|最高|最低)(?:的)?[0-9一二三四五六七八九十]+(?:名|欄|個|台)",
            question,
        )
    )
    if ranking_shape and any(word in question for word in ("出力", "功率", "欄位")):
        return "daily_ranking"

    fuel_words = ("燃料", "煤機", "燃煤", "水力", "天然氣", "燃氣", "重油", "輕柴油")
    statistic_words = ("容量", "幾台", "台數", "數量", "最多", "統計", "排行", "平均", "合計")
    if any(word in question for word in fuel_words) and any(
        word in question for word in statistic_words
    ):
        return "fuel_stats"

    if (
        any(word in question for word in ("歲修", "維修", "修復", "維修中"))
        or "停機事件" in question
    ):
        return "outage"

    comparison_shape = any(
        word in question for word in ("比較", "誰高", "兩台機組", "兩個彙總", "指定兩台")
    ) or (
        any(word in question for word in ("和", "與", "及"))
        and any(
            word in question
            for word in ("曲線", "峰值", "平均", "趨勢", "最大值", "輸出", "每日出力", "日數")
        )
    )
    if comparison_shape:
        return "comparison"
    if ranking_shape:
        return "daily_ranking"

    day_count_words = ("幾天", "日數", "天數", "日期數", "的天", "的日期")
    zero_state = (
        "零出力" in question
        and "非零出力" not in question
        or "零輸出" in question
        and "非零輸出" not in question
    ) or any(word in question for word in ("零值", "沒有出力", "沒發電", "等於零"))
    if zero_state or (
        any(word in question for word in day_count_words)
        and any(word in question for word in ("有出力", "有發電", "有值", "非零", "停機"))
    ):
        return "zero_days"

    if any(word in question for word in system_words):
        return "system_metric"

    explicit_day = bool(
        re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", question)
        or re.search(
            r"(?:20\d{2}年|今年|去年)?[0-9一二三四五六七八九十]+月[0-9一二三四五六七八九十]+日",
            question,
        )
        or any(word in question for word in ("某天", "指定日期", "指定日", "同一天"))
    )
    unit_shape = any(
        word in question
        for word in (
            "機組",
            "號機",
            "德基",
            "青山",
            "大林",
            "林口",
            "台中",
            "大觀",
            "興達",
            "出力",
            "輸出",
            "功率",
            "尖峰值",
        )
    )
    if explicit_day and unit_shape:
        return "unit_day"
    if any(word in question for word in ("最高", "最低", "最大", "最小", "峰值", "極值")):
        return "unit_extreme"
    # 最後一條。順序就是優先權：到這裡表示沒有任何具體規則認領這句話。
    if nearest_scope_topic(question) is not None:
        return "data_scope"
    return "other"


def _bounded_range(
    entities: Entities, data_range: tuple[str, str] | None
) -> tuple[str, str] | None:
    if not entities.date_range:
        return data_range
    if not data_range:
        return entities.date_range.start, entities.date_range.end
    return max(entities.date_range.start, data_range[0]), min(
        entities.date_range.end, data_range[1]
    )


def _range_clause(date_range: tuple[str, str] | None) -> tuple[str, tuple[object, ...]]:
    return ("", ()) if not date_range else (' AND "日期" BETWEEN ? AND ?', date_range)


def _system_metric(question: str) -> str | None:
    return next(
        (
            name
            for word, name in (
                ("備轉容量率", "備轉容量率_pct"),
                ("備轉率", "備轉容量率_pct"),
                ("備轉容量", "備轉容量_萬瓩"),
                ("淨尖峰供電能力", "淨尖峰供電能力_萬瓩"),
                ("尖峰負載", "尖峰負載_萬瓩"),
                ("負載", "尖峰負載_萬瓩"),
                ("民生用電", "民生用電_百萬度"),
                ("工業用電", "工業用電_百萬度"),
            )
            if word in question
        ),
        None,
    )


def _renewable_words(question: str) -> bool:
    return "自建" in question or any(
        word in question
        for word in ("陸域風力", "離岸風力", "太陽能", "太陽光電", "地熱", "再生能源")
    )


def _renewable_energy_type(question: str) -> str | None:
    """Narrow to one 能源別, or None to cover every renewable type."""
    compact = re.sub(r"\s+", "", question)
    return next(
        (
            name
            for phrase, name in (
                ("離岸風力", "離岸風力"),
                ("陸域風力", "陸域風力"),
                ("太陽光電", "太陽能"),
                ("太陽能", "太陽能"),
                ("地熱", "地熱"),
            )
            if phrase in compact
        ),
        None,
    )


def _renewable_filters(question: str, entities: Entities) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    params: list[object] = []
    energy = _renewable_energy_type(question)
    if energy:
        clauses.append('"能源別" = ?')
        params.append(energy)
    elif "風力" in question:
        # 只說「風力」時涵蓋陸域與離岸，不替使用者挑一種。
        clauses.append('"能源別" LIKE ?')
        params.append("%風力%")
    if entities.date_range:
        clauses.append('"年度" = ?')
        params.append(int(entities.date_range.start[:4]))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


def _generation_cost_type(question: str) -> str | None:
    compact = re.sub(r"\s+", "", question)
    return next(
        (
            name
            for phrase, name in (
                ("平均發購電", "平均發購電成本"),
                ("自發電力小計", "自發電力小計"),
                ("購入電力小計", "購入電力小計"),
                ("太陽光電", "太陽光電"),
                ("慣常水力", "慣常水力"),
                ("其他再生能源", "其他再生能源"),
                ("汽電共生", "汽電共生"),
                ("民營電廠", "民營電廠"),
                ("火力", "火力發電"),
                ("核能", "核能發電"),
                ("抽蓄", "抽蓄發電"),
                ("風力", "風力發電"),
                ("地熱", "地熱"),
                ("燃油", "燃油"),
                ("燃煤", "燃煤"),
                ("燃氣", "燃氣"),
                ("再生能源", "再生能源發電"),
            )
            if phrase in compact
        ),
        None,
    )


def route(
    question: str,
    entities: Entities,
    *,
    peak_columns: set[str],
    plants: set[str] | None = None,
    data_range: tuple[str, str] | None = None,
) -> RoutedQuery:
    intent = classify_intent(question)
    bounded_range = _bounded_range(entities, data_range)

    if intent == "generation_cost":
        generation_type = _generation_cost_type(question)
        year = int(entities.date_range.start[:4]) if entities.date_range else None
        if generation_type and year:
            return RoutedQuery(
                intent,
                'SELECT "年度", "電力來源", "發電方式", "成本_元每度", "決算類型" '
                'FROM v_generation_cost WHERE "年度" = ? AND "發電方式" = ? LIMIT 20',
                (year, generation_type),
            )
        if generation_type:
            return RoutedQuery(
                intent,
                'SELECT "年度", "電力來源", "發電方式", "成本_元每度", "決算類型" '
                'FROM v_generation_cost WHERE "發電方式" = ? ORDER BY "年度" DESC LIMIT 20',
                (generation_type,),
            )

    if intent == "renewable_generation":
        where, params = _renewable_filters(question, entities)
        return RoutedQuery(
            intent,
            'SELECT "能源別", SUM("發電量_度") AS "發電量_度" '
            f"FROM v_re_generation{where} "
            'GROUP BY "能源別" ORDER BY "發電量_度" DESC LIMIT 20',
            params,
        )

    if intent == "renewable_site":
        where, params = _renewable_filters(question, entities)
        return RoutedQuery(
            intent,
            'SELECT DISTINCT "發電站", "縣市", "能源別", "裝置容量_瓩", "主檔來源" '
            f"FROM v_re_generation{where} "
            'ORDER BY "裝置容量_瓩" DESC LIMIT 20',
            params,
        )

    if intent == "system_metric":
        metric = _system_metric(question)
        if metric and any(word in question for word in ("最新", "最近一天")):
            return RoutedQuery(
                intent,
                f'SELECT "日期", "{metric}" FROM v_system ORDER BY "日期" DESC LIMIT 1',
            )
        if metric:
            clause, params = _range_clause(bounded_range)
            if "每月" in question or "各月" in question:
                return RoutedQuery(
                    intent,
                    f'SELECT SUBSTR("日期", 1, 7) AS "月份", AVG("{metric}") AS "平均值" '
                    f'FROM v_system WHERE 1 = 1{clause} GROUP BY SUBSTR("日期", 1, 7) '
                    'ORDER BY "月份" LIMIT 24',
                    params,
                )
            if any(word in question for word in ("最低", "最小", "最高", "最大")):
                direction = "ASC" if any(word in question for word in ("最低", "最小")) else "DESC"
                return RoutedQuery(
                    intent,
                    f'SELECT "日期", "{metric}" FROM v_system WHERE 1 = 1{clause} '
                    f'ORDER BY "{metric}" {direction} LIMIT 1',
                    params,
                )
            return RoutedQuery(
                intent,
                f'SELECT "日期", "{metric}" FROM v_system WHERE 1 = 1{clause} '
                'ORDER BY "日期" LIMIT 200',
                params,
            )

    if intent == "unit_day" and entities.explicit_date:
        resolution = resolve_peak_column(question, peak_columns)
        if resolution.value:
            return RoutedQuery(
                intent,
                'SELECT "日期", "尖峰出力_萬瓩" FROM v_peak '
                'WHERE "機組欄位" = ? AND "日期" = ? LIMIT 1',
                (resolution.value, entities.explicit_date),
            )

    if intent == "unit_extreme":
        resolution = resolve_peak_column(question, peak_columns)
        if resolution.value:
            find_minimum = any(word in question for word in ("最低", "最小"))
            function, alias = ("MIN", "最小值") if find_minimum else ("MAX", "最大值")
            direction = "ASC" if find_minimum else "DESC"
            clause, range_params = _range_clause(bounded_range)
            nonzero = ' AND "尖峰出力_萬瓩" > 0' if "非零" in question else ""
            params = (resolution.value, *range_params)
            if any(word in question for word in ("日期", "哪天", "發生日", "全期")):
                return RoutedQuery(
                    intent,
                    'SELECT "日期", "尖峰出力_萬瓩" FROM v_peak '
                    f'WHERE "機組欄位" = ?{clause}{nonzero} '
                    f'ORDER BY "尖峰出力_萬瓩" {direction} LIMIT 1',
                    params,
                )
            return RoutedQuery(
                intent,
                f'SELECT {function}("尖峰出力_萬瓩") AS "{alias}" FROM v_peak '
                f'WHERE "機組欄位" = ?{clause}{nonzero} LIMIT 1',
                params,
            )

    if intent == "daily_ranking" and entities.explicit_date:
        direction = (
            "ASC" if any(word in question for word in ("最低", "最小", "由小到大")) else "DESC"
        )
        limit = entities.top_n or (64 if "所有" in question else 20)
        filters = ['"日期" = ?']
        params: list[object] = [entities.explicit_date]
        if "非零" in question:
            filters.append('"尖峰出力_萬瓩" > 0')
        if entities.fuel:
            filters.append('"類別" = ?')
            params.append(
                {"水": "水力", "煤": "燃煤", "天然氣": "燃氣"}.get(entities.fuel, entities.fuel)
            )
        return RoutedQuery(
            intent,
            'SELECT "機組欄位", "尖峰出力_萬瓩" FROM v_peak WHERE '
            + " AND ".join(filters)
            + f' ORDER BY "尖峰出力_萬瓩" {direction} LIMIT {min(limit, 200)}',
            tuple(params),
        )

    if intent == "plant_units" and plants:
        plant = resolve_plant(question, plants)
        if plant.value:
            columns = ['"機組名"']
            fuel_filter = entities.fuel is not None
            if "燃料" in question or (
                not fuel_filter and not any(word in question for word in ("容量", "商轉", "主檔"))
            ):
                columns.append('"燃料"')
            if "容量" in question or fuel_filter or "主檔" in question:
                columns.append('"裝置容量_萬瓩"')
            if "商轉" in question:
                columns.extend(['"商轉日期"', '"商轉日期精度"'])
            elif "設備" in question:
                columns.append('"商轉日期"')
            sql = f'SELECT {", ".join(columns)} FROM v_unit WHERE "電廠" = ?'
            params: list[object] = [plant.value]
            if fuel_filter:
                sql += ' AND "燃料" = ?'
                params.append(entities.fuel)
            return RoutedQuery(intent, sql + ' ORDER BY "機組名" LIMIT 50', tuple(params))

    if intent == "fuel_stats":
        if entities.fuel:
            params = (entities.fuel,)
            if any(word in question for word in ("幾台", "台數", "數量", "共有")):
                return RoutedQuery(
                    intent,
                    'SELECT COUNT(*) AS "台數" FROM v_unit WHERE "燃料" = ? LIMIT 1',
                    params,
                )
            if any(word in question for word in ("最高", "前", "排行")):
                return RoutedQuery(
                    intent,
                    'SELECT "電廠", "機組名", "裝置容量_萬瓩" FROM v_unit '
                    f'WHERE "燃料" = ? ORDER BY "裝置容量_萬瓩" DESC LIMIT {entities.top_n or 20}',
                    params,
                )
            function = "AVG" if "平均" in question else "SUM"
            alias = "平均容量" if function == "AVG" else "總容量"
            group = ' GROUP BY "電廠" ORDER BY "總容量" DESC' if "各電廠" in question else ""
            prefix = '"電廠", ' if group else ""
            return RoutedQuery(
                intent,
                f'SELECT {prefix}{function}("裝置容量_萬瓩") AS "{alias}" '
                f'FROM v_unit WHERE "燃料" = ?{group} LIMIT 20',
                params,
            )
        if "燃料" in question:
            return RoutedQuery(
                intent,
                'SELECT "燃料", COUNT(*) AS "台數" FROM v_unit GROUP BY "燃料" '
                'ORDER BY "台數" DESC LIMIT 20',
            )

    if intent == "outage":
        if "未對齊" in question:
            return RoutedQuery(
                intent,
                'SELECT "機組名", "對齊狀態" FROM v_outage WHERE "對齊狀態" IN (?, ?) LIMIT 50',
                ("unmatched", "ambiguous"),
            )
        if "異常" in question:
            return RoutedQuery(
                intent,
                'SELECT "機組名", "開始日期", "結束日期" FROM v_outage '
                'WHERE "日期狀態" = ? LIMIT 20',
                ("invalid_range",),
            )
        if entities.date_range:
            if "啟動" in question:
                return RoutedQuery(
                    intent,
                    'SELECT "機組名", "開始日期", "結束日期" FROM v_outage '
                    'WHERE "日期狀態" = ? AND "開始日期" BETWEEN ? AND ? '
                    'ORDER BY "開始日期" LIMIT 100',
                    ("valid", entities.date_range.start, entities.date_range.end),
                )
            return RoutedQuery(
                intent,
                'SELECT "機組名", "開始日期", "結束日期" FROM v_outage '
                'WHERE "日期狀態" = ? AND "開始日期" <= ? AND "結束日期" >= ? '
                "LIMIT 100",
                ("valid", entities.date_range.end, entities.date_range.start),
            )
        unit_match = re.search(r"(台中|林口|明潭)#?(\d{1,2})", question)
        if unit_match:
            plant, number = unit_match.groups()
            readable_number = chinese_number(int(number))
            outage_name = {
                "台中": f"中{readable_number}機",
                "林口": f"林{readable_number}機",
                "明潭": f"明潭#{number}機",
            }[plant]
            reason_column = ', "原因"' if "何時結束" not in question else ""
            order = '"結束日期" DESC' if "何時結束" in question else '"開始日期"'
            return RoutedQuery(
                intent,
                f'SELECT "機組名", "開始日期", "結束日期"{reason_column} '
                f'FROM v_outage WHERE "機組名" = ? ORDER BY {order} LIMIT 20',
                (outage_name,),
            )

    if intent == "zero_days":
        resolution = resolve_peak_column(question, peak_columns)
        if resolution.value:
            clause, range_params = _range_clause(bounded_range)
            positive = "沒有出力" not in question and any(
                word in question for word in ("有出力", "有值", "非零")
            )
            operator = ">" if positive else "="
            params = (resolution.value, *range_params)
            if "清單" in question or ("日期" in question and "日期數" not in question):
                return RoutedQuery(
                    intent,
                    'SELECT "日期" FROM v_peak WHERE "機組欄位" = ?'
                    f'{clause} AND "尖峰出力_萬瓩" {operator} 0 ORDER BY "日期" LIMIT 200',
                    params,
                )
            return RoutedQuery(
                intent,
                'SELECT COUNT(*) AS "天數" FROM v_peak WHERE "機組欄位" = ?'
                f'{clause} AND "尖峰出力_萬瓩" {operator} 0 LIMIT 1',
                params,
            )

    if intent == "comparison":
        columns = resolve_peak_columns(question, peak_columns)
        if len(columns) == 2:
            clause, range_params = _range_clause(bounded_range)
            params = (*columns, *range_params)
            prefix = 'WHERE "機組欄位" IN (?, ?)'
            if "平均" in question:
                return RoutedQuery(
                    intent,
                    'SELECT "機組欄位", AVG("尖峰出力_萬瓩") AS "平均值" FROM v_peak '
                    f'{prefix}{clause} GROUP BY "機組欄位" LIMIT 2',
                    params,
                )
            if any(word in question for word in ("最大", "最高", "峰值")):
                return RoutedQuery(
                    intent,
                    'SELECT "機組欄位", MAX("尖峰出力_萬瓩") AS "峰值" FROM v_peak '
                    f'{prefix}{clause} GROUP BY "機組欄位" LIMIT 2',
                    params,
                )
            if any(word in question for word in ("有值", "非零")):
                return RoutedQuery(
                    intent,
                    'SELECT "機組欄位", '
                    'SUM(CASE WHEN "尖峰出力_萬瓩" > 0 THEN 1 ELSE 0 END) AS "天數" '
                    f'FROM v_peak {prefix}{clause} GROUP BY "機組欄位" LIMIT 2',
                    params,
                )
            return RoutedQuery(
                intent,
                'SELECT "日期", "機組欄位", "尖峰出力_萬瓩" FROM v_peak '
                f'{prefix}{clause} ORDER BY "日期", "機組欄位" LIMIT 200',
                params,
            )

    if intent == "data_scope":
        topic = data_scope_topic(question) or nearest_scope_topic(question)
        if topic == "plants":
            return RoutedQuery(
                intent,
                'SELECT DISTINCT "電廠" FROM v_unit ORDER BY "電廠" LIMIT 200',
            )
        if topic == "fuels":
            return RoutedQuery(
                intent,
                'SELECT DISTINCT "燃料" FROM v_unit ORDER BY "燃料" LIMIT 200',
            )
        if topic == "period":
            return RoutedQuery(
                intent,
                'SELECT MIN("日期") AS "最早", MAX("日期") AS "最晚" FROM v_system LIMIT 1',
            )
        if topic == "units":
            return RoutedQuery(
                intent,
                'SELECT COUNT(*) AS "機組數" FROM v_unit LIMIT 1',
            )

    if intent == "other":
        clause, params = _range_clause(bounded_range)
        if "負載" in question and "備轉" in question:
            reserve = "備轉容量率_pct" if "率" in question else "備轉容量_萬瓩"
            return RoutedQuery(
                intent,
                f'SELECT "日期", "尖峰負載_萬瓩", "{reserve}" FROM v_system '
                f'WHERE 1 = 1{clause} ORDER BY "日期" LIMIT 200',
                params,
            )
        if "容量" in question and any(word in question for word in ("歷史峰值", "實測最大值")):
            return RoutedQuery(
                intent,
                'SELECT "機組欄位", "對應裝置容量_萬瓩", '
                'MAX("尖峰出力_萬瓩") AS "峰值" FROM v_peak '
                'WHERE "有機組主檔" = 1 GROUP BY "機組欄位", "對應裝置容量_萬瓩" LIMIT 100',
            )
        if "最早" in question and "最晚" in question:
            return RoutedQuery(
                intent,
                'SELECT MIN("日期") AS "最早日期", MAX("日期") AS "最晚日期" FROM v_system LIMIT 1',
            )
        if ("各類別" in question or "每種類別" in question) and "最高" in question:
            return RoutedQuery(
                intent,
                'SELECT "類別", MAX("尖峰出力_萬瓩") AS "最高值" FROM v_peak '
                'GROUP BY "類別" ORDER BY "最高值" DESC LIMIT 20',
            )
        monthly_metric = _system_metric(question)
        if monthly_metric and ("每月" in question or "各月" in question):
            return RoutedQuery(
                intent,
                f'SELECT SUBSTR("日期", 1, 7) AS "月份", AVG("{monthly_metric}") AS "平均值" '
                f'FROM v_system WHERE 1 = 1{clause} GROUP BY SUBSTR("日期", 1, 7) '
                'ORDER BY "月份" LIMIT 24',
                params,
            )

    return RoutedQuery(intent)
