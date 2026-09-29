from datetime import date

import pytest

from text2sql.aliases import (
    resolve_peak_column,
    resolve_peak_columns,
    resolve_plant,
    resolve_re_site,
)
from text2sql.entities import DateRange, extract_entities, unparsed_date


def test_extracts_chinese_roc_and_relative_dates() -> None:
    reference = date(2026, 9, 13)
    cases = {
        "2026年五月二日台中2號機": (DateRange("2026-05-02", "2026-05-02"), "2026-05-02"),
        "114年5月1日尖峰負載": (DateRange("2025-05-01", "2025-05-01"), "2025-05-01"),
        "去年六月一日台中5號機": (DateRange("2025-06-01", "2025-06-01"), "2025-06-01"),
        "2026年2月備轉容量": (DateRange("2026-02-01", "2026-02-28"), None),
        "今年上半年": (DateRange("2026-01-01", "2026-06-30"), None),
    }
    for question, expected in cases.items():
        entities = extract_entities(question, reference_date=reference)
        assert (entities.date_range, entities.explicit_date) == expected


def test_extracts_top_n_and_longest_fuel_alias() -> None:
    entities = extract_entities("天然氣出力最大的五名")
    assert entities.top_n == 5
    assert entities.fuel == "天然氣"


def test_aliases_resolve_exact_shorthand_and_ambiguity() -> None:
    columns = {"台中#2", "興達#3", "興達 (#1-#5)", "德基"}
    assert resolve_peak_column("台中2號機", columns).value == "台中#2"
    assert resolve_peak_column("查德基", columns).value == "德基"
    ambiguous = resolve_peak_column("興達3機", columns)
    assert ambiguous.ambiguous
    assert set(ambiguous.candidates) == {"興達#3", "興達 (#1-#5)"}

    plant = resolve_plant("中火設備", {"台中發電廠", "林口發電廠"})
    assert plant.value == "台中發電廠"


def test_resolve_re_site_matches_a_named_station_ignoring_unit_suffix() -> None:
    """「蘆竹(#1~#8)」問的是場站「蘆竹風力」，括號裡的機組編號只是裝飾。"""

    sites = {"蘆竹風力", "林口風力", "台南鹽田太陽光電"}
    assert resolve_re_site("再生能源的蘆竹(#1~#8)裝置容量多少?", sites).value == "蘆竹風力"
    assert resolve_re_site("林口(#4~#6)的地址?", sites).value == "林口風力"


def test_resolve_re_site_is_ambiguous_when_two_sites_share_a_root() -> None:
    """兩個場站拿掉能源別後綴會撞名時要反問，不能猜一個。"""

    sites = {"台中電廠太陽光電", "台中電廠風力", "台南鹽田太陽光電"}
    resolution = resolve_re_site("台中電廠再生能源發電量", sites)
    assert resolution.value is None
    assert resolution.ambiguous
    assert set(resolution.candidates) == {"台中電廠太陽光電", "台中電廠風力"}


def test_resolve_re_site_returns_no_match_for_an_aggregate_question() -> None:
    """沒有點名場站的問句不該被誤判成「比對不到」的錯誤，就是單純沒有場站篩選。"""

    sites = {"蘆竹風力", "林口風力"}
    resolution = resolve_re_site("2026年2月各種再生能源發電量？", sites)
    assert resolution.value is None
    assert not resolution.ambiguous
    assert resolution.candidates == ()


def test_chinese_unit_numbers_resolve_both_comparison_targets() -> None:
    """「林口一號二號」「台中三號四號」要解出兩台，否則 comparison 規則接不到。"""

    columns = {"林口#1", "林口#2", "林口#3", "台中#3", "台中#4"}
    assert resolve_peak_columns("比較林口一號二號七月平均出力", columns) == ("林口#1", "林口#2")
    assert resolve_peak_columns("林口二號與三號年度平均", columns) == ("林口#2", "林口#3")
    assert resolve_peak_columns("台中三號四號去年最大值誰高", columns) == ("台中#3", "台中#4")


def test_a_chinese_numeral_without_a_unit_suffix_is_not_a_unit_number() -> None:
    """「找單一機組一段時間的極值」裡的「一」不是機組編號。"""

    assert resolve_peak_columns("找單一機組一段時間的極值", {"台中#1", "林口#1"}) == ()


def test_two_distinct_units_in_one_question_are_not_an_ambiguity() -> None:
    """兩台機組各出現一次是比較，不是一個名字有兩種讀法。"""

    columns = {"台中#1", "台中#2", "興達#3", "興達 (#1-#5)"}
    resolution = resolve_peak_column("比較台中#1和台中#2的平均出力", columns)
    assert not resolution.ambiguous
    assert resolution.value == "台中#1"
    assert resolve_peak_column("興達3機", columns).ambiguous, "真正的歧義仍要反問"


# 同一天的各種寫法必須落在同一個值 —— 教材 db.py 開頭列的就是這件事（11405／114年05月
# ／114/05／202505 都是同一個月）。實測本專案原本只認 12/19 種，其餘掉到「只認得年份」
# 那條變成靜默查整年。
SAME_DAY_FORMS = (
    "2026-07-20",
    "2026/07/20",
    "2026/7/20",
    "2026.7.20",
    "2026 07 20",
    "20260720",
    "2026年7月20日",
    "2026年07月20日",
    "2026年七月二十日",
    "115年7月20日",
    "115/7/20",
    "115-07-20",
    "115.7.20",
    "115 7 20",
    "1150720",
    "民國115年7月20日",
    "２０２６年７月２０日",
)


@pytest.mark.parametrize("written", SAME_DAY_FORMS)
def test_every_way_of_writing_one_day_lands_on_the_same_value(written: str) -> None:
    entities = extract_entities(f"{written}台中#1的尖峰出力")
    assert entities.explicit_date == "2026-07-20", written
    assert entities.date_range == DateRange("2026-07-20", "2026-07-20"), written


SAME_MONTH_FORMS = (
    "2026年7月",
    "2026-07",
    "2026/07",
    "2026.07",
    "202607",
    "115年7月",
    "115/07",
    "11507",
)


@pytest.mark.parametrize("written", SAME_MONTH_FORMS)
def test_every_way_of_writing_one_month_lands_on_the_same_range(written: str) -> None:
    entities = extract_entities(f"{written}台中#1平均出力")
    assert entities.date_range == DateRange("2026-07-01", "2026-07-31"), written
    assert entities.explicit_date is None, written


def test_a_two_digit_year_is_not_guessed() -> None:
    """「26/7/20」的 26 可能是年也可能是日。猜錯的代價是答案看起來完全正常。"""

    entities = extract_entities("26/7/20台中#1的尖峰出力")
    assert entities.explicit_date is None
    assert entities.date_range is None
    assert unparsed_date("26/7/20台中#1的尖峰出力", entities) == "26/7/20"


def test_a_question_without_any_date_has_nothing_unparsed() -> None:
    """沒提日期時退回完整期間是對的 —— 那條路不能被誤判成「看不懂」。"""

    for question in ("台中#1最高出力", "大觀發電廠有哪些機組", "有哪些電廠"):
        entities = extract_entities(question)
        assert unparsed_date(question, entities) is None, question


def test_the_unreadable_fragment_is_reported_whole() -> None:
    """「2026年13月40日」回報成「看不懂 2026年」只會讓人去改對的那一半。"""

    question = "2026年13月40日台中#1最高出力"
    entities = extract_entities(question)
    assert entities.date_range is None
    assert unparsed_date(question, entities) == "2026年13月40日"
