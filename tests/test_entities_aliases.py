from datetime import date

from text2sql.aliases import resolve_peak_column, resolve_peak_columns, resolve_plant
from text2sql.entities import DateRange, extract_entities


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
