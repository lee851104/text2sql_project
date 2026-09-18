"""Capacity fill for the 21 B columns with no unit master.

Fixtures mirror real rows: 8931 writes subtotals as `15918.1(26.052%)`, leaves
retired units out entirely, and the nuclear master reports MWe as a plain integer.
"""

from __future__ import annotations

import pytest

from align.column_capacity import (
    CapacityConfigError,
    build_column_capacities,
    megawatts_to_kilowatts,
    parse_megawatts,
    summarize_column_capacities,
)

REALTIME = [
    {"機組類型": "民營電廠-燃煤", "機組名稱": "和平#1", "裝置容量(MW)": "648.6"},
    {"機組類型": "民營電廠-燃煤", "機組名稱": "和平#2", "裝置容量(MW)": "660.0"},
    {"機組類型": "民營電廠-燃煤", "機組名稱": "小計", "裝置容量(MW)": "1308.6(2.142%)"},
    {"機組類型": "民營電廠-燃氣", "機組名稱": "嘉惠#1", "裝置容量(MW)": "700.0"},
    {"機組類型": "民營電廠-燃氣", "機組名稱": "嘉惠#2", "裝置容量(MW)": "510.0"},
    {"機組類型": "風力", "機組名稱": "小計(註5)", "裝置容量(MW)": "4187.1(6.853%)"},
    {"機組類型": "燃料油", "機組名稱": "核二Gas1", "裝置容量(MW)": "-"},
]

NUCLEAR = [
    {"電廠名稱": "核一廠", "機組名稱": "一號機", "裝置容量MWe": "636"},
    {"電廠名稱": "核三廠", "機組名稱": "二號機", "裝置容量MWe": "951"},
    {"電廠名稱": "龍門廠", "機組名稱": "一號機", "裝置容量MWe": "1350"},
]


class TestParseMegawatts:
    def test_reads_a_plain_figure(self) -> None:
        assert parse_megawatts("648.6") == 648.6

    def test_ignores_the_percentage_a_subtotal_carries(self) -> None:
        assert parse_megawatts("1308.6(2.142%)") == 1308.6

    def test_returns_none_for_a_dash(self) -> None:
        assert parse_megawatts("-") is None

    def test_converts_to_the_unit_the_master_uses(self) -> None:
        assert megawatts_to_kilowatts(648.6) == 648_600


class TestBuildColumnCapacities:
    def test_single_unit_column(self) -> None:
        [result] = build_column_capacities(
            {"和平#1": {"source": "realtime", "units": ["和平#1"]}},
            realtime_rows=REALTIME,
            nuclear_rows=NUCLEAR,
        )
        assert result.capacity_kw == 648_600
        assert result.source == "realtime"

    def test_aggregate_column_sums_its_units(self) -> None:
        [result] = build_column_capacities(
            {"嘉惠(#1~#2)": {"source": "realtime", "units": ["嘉惠#1", "嘉惠#2"]}},
            realtime_rows=REALTIME,
            nuclear_rows=NUCLEAR,
        )
        assert result.capacity_kw == 1_210_000
        assert result.source_detail == "嘉惠#1+嘉惠#2"

    def test_subtotal_column_reads_the_annotated_row(self) -> None:
        """風力的小計列寫成「小計(註5)」，不是「小計」。"""
        [result] = build_column_capacities(
            {"風力發電": {"source": "realtime_subtotal", "fuel": "風力"}},
            realtime_rows=REALTIME,
            nuclear_rows=NUCLEAR,
        )
        assert result.capacity_kw == 4_187_100

    def test_nuclear_column_reads_the_master(self) -> None:
        [result] = build_column_capacities(
            {"核一#1": {"source": "nuclear", "plant": "核一廠", "unit": "一號機"}},
            realtime_rows=REALTIME,
            nuclear_rows=NUCLEAR,
        )
        assert result.capacity_kw == 636_000
        assert result.source == "nuclear"

    def test_unavailable_column_keeps_its_reason(self) -> None:
        """麥寮已從即時清單消失，容量留 NULL 而不是抓一個近似值。"""
        [result] = build_column_capacities(
            {"麥寮#1": {"source": "unavailable", "note": "8931 即時清單已無此機組"}},
            realtime_rows=REALTIME,
            nuclear_rows=NUCLEAR,
        )
        assert result.capacity_kw is None
        assert result.note == "8931 即時清單已無此機組"

    def test_a_subtotal_row_never_leaks_into_unit_lookup(self) -> None:
        with pytest.raises(CapacityConfigError, match="小計"):
            build_column_capacities(
                {"某欄": {"source": "realtime", "units": ["小計"]}},
                realtime_rows=REALTIME,
                nuclear_rows=NUCLEAR,
            )

    def test_missing_source_row_raises_instead_of_filling_null(self) -> None:
        """設定指名的機組不在快照裡時要出錯，不能靜靜地留空。"""
        with pytest.raises(CapacityConfigError, match="麥寮#1"):
            build_column_capacities(
                {"麥寮#1": {"source": "realtime", "units": ["麥寮#1"]}},
                realtime_rows=REALTIME,
                nuclear_rows=NUCLEAR,
            )

    def test_unknown_nuclear_unit_raises(self) -> None:
        with pytest.raises(CapacityConfigError, match="核二廠"):
            build_column_capacities(
                {"核二#1": {"source": "nuclear", "plant": "核二廠", "unit": "一號機"}},
                realtime_rows=REALTIME,
                nuclear_rows=NUCLEAR,
            )

    def test_unsupported_source_raises(self) -> None:
        with pytest.raises(CapacityConfigError, match="不支援"):
            build_column_capacities(
                {"某欄": {"source": "guesswork"}},
                realtime_rows=REALTIME,
                nuclear_rows=NUCLEAR,
            )


class TestSummary:
    def test_counts_by_source_and_lists_the_gaps(self) -> None:
        summary = summarize_column_capacities(
            build_column_capacities(
                {
                    "和平#1": {"source": "realtime", "units": ["和平#1"]},
                    "核一#1": {"source": "nuclear", "plant": "核一廠", "unit": "一號機"},
                    "麥寮#1": {"source": "unavailable", "note": "無來源"},
                },
                realtime_rows=REALTIME,
                nuclear_rows=NUCLEAR,
            )
        )
        assert summary["configured"] == 3
        assert summary["filled"] == 2
        assert summary["capacity_kw"] == 648_600 + 636_000
        assert summary["unavailable"] == ["麥寮#1"]
