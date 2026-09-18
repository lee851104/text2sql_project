"""Cleaning and alignment rules for 資料集 17141／17140.

Every case here comes from a real row in the official files, so a rule change
that breaks real data fails the suite instead of passing on invented input.
"""

from __future__ import annotations

import pytest

from align.renewable import (
    align_stations,
    build_station_records,
    chinese_part,
    is_subtotal,
    normalize_station,
    parse_county,
    parse_generation,
    repair_generation,
    summarize_alignment,
)


class TestChinesePart:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("發電站編號/Number of The Power Station", "發電站編號"),
            ("陸域風力/Onshore Wind", "陸域風力"),
            ("石門風力發電站/Shimen Wind Power Station", "石門風力發電站"),
            ("裝置容量(瓩)/Installed Capacity(kW)", "裝置容量(瓩)"),
        ],
    )
    def test_takes_the_chinese_side(self, value: str, expected: str) -> None:
        assert chinese_part(value) == expected

    def test_recovers_the_official_row_missing_its_separator(self) -> None:
        """17140 的彰工風力少了 `/`，中英文黏在一起。"""
        assert chinese_part("彰工風力Changgong Wind Power") == "彰工風力"

    def test_leaves_a_plain_chinese_value_alone(self) -> None:
        assert chinese_part("中屯風力") == "中屯風力"


class TestSubtotalRows:
    @pytest.mark.parametrize(
        "station_id", ["陸域風力小計", "離岸風力小計", "太陽光電小計", "地熱小計"]
    )
    def test_detects_every_official_subtotal(self, station_id: str) -> None:
        assert is_subtotal(station_id) is True

    def test_keeps_real_sites(self) -> None:
        assert is_subtotal("1") is False
        assert is_subtotal("26") is False


class TestNormalizeStation:
    def test_strips_the_suffix_the_two_files_disagree_on(self) -> None:
        """17141 帶「發電站」後綴，17140 沒有；正規化後必須相同。"""
        assert normalize_station("石門風力發電站") == normalize_station("石門風力")

    def test_only_strips_a_trailing_suffix(self) -> None:
        assert normalize_station("發電站管理處") == "發電站管理處"

    def test_applies_bilingual_split_first(self) -> None:
        assert normalize_station("彰工風力Changgong Wind Power") == "彰工風力"


class TestParseCounty:
    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("新北市石門區尖鹿里", "新北市"),
            ("桃園市龜山區忠義路2段366號", "桃園市"),
            ("澎湖縣湖西鄉菓葉村(#1)、南寮村(#2~#3)", "澎湖縣"),
            ("連江縣南竿鄉", "連江縣"),
        ],
    )
    def test_reads_the_leading_county(self, address: str, expected: str) -> None:
        assert parse_county(address) == expected

    def test_returns_none_for_a_subtotal_note(self) -> None:
        """小計列的地址欄塞的是說明文字，不是地址。"""
        assert parse_county("含試運轉中機組") is None
        assert parse_county("") is None


class TestParseGeneration:
    def test_reads_a_clean_integer(self) -> None:
        value = parse_generation("543699")
        assert (value.kwh, value.status) == (543699, "ok")

    @pytest.mark.parametrize("marker", ["-", "", "  "])
    def test_missing_stays_null_not_zero(self, marker: str) -> None:
        value = parse_generation(marker)
        assert value.kwh is None
        assert value.status == "missing"

    def test_a_real_zero_is_not_missing(self) -> None:
        value = parse_generation("0")
        assert (value.kwh, value.status) == (0, "ok")

    def test_damaged_value_is_suspect_and_excluded(self) -> None:
        """澎湖湖西風力 2024-07 的原始值尾端多一個字元，不得自行採用前綴數字。"""
        value = parse_generation("357,156.00 2")
        assert value.kwh is None
        assert value.status == "suspect"

    def test_unparseable_value_is_invalid(self) -> None:
        value = parse_generation("無資料")
        assert value.kwh is None
        assert value.status == "invalid"


class TestRepairGeneration:
    def test_applies_a_confirmed_repair(self) -> None:
        repaired = repair_generation(parse_generation("357,156.00 2"), {"357,156.00 2": 357156})
        assert (repaired.kwh, repaired.status) == (357156, "repaired")
        assert repaired.raw == "357,156.00 2"

    def test_leaves_a_suspect_value_alone_without_a_repair(self) -> None:
        untouched = repair_generation(parse_generation("357,156.00 2"), {})
        assert untouched.kwh is None
        assert untouched.status == "suspect"

    def test_never_rewrites_a_clean_value(self) -> None:
        clean = repair_generation(parse_generation("123"), {"123": 999})
        assert (clean.kwh, clean.status) == (123, "ok")


class TestAlignStations:
    def test_matches_across_the_suffix_difference(self) -> None:
        links = align_stations(["石門風力發電站"], ["石門風力"])
        assert [(link.key, link.status) for link in links] == [("石門風力", "matched")]

    def test_generation_without_a_master_stays_unmatched(self) -> None:
        """中屯風力有發電量但場址主檔沒有它，不得憑空建立對應。"""
        links = align_stations(["石門風力發電站"], ["石門風力", "中屯風力"])
        statuses = {link.key: link.status for link in links}
        assert statuses["中屯風力"] == "generation_only"

    def test_preserves_the_original_name_on_both_sides(self) -> None:
        links = align_stations(["石門風力發電站"], ["石門風力"])
        assert links[0].site_name == "石門風力發電站"
        assert links[0].generation_name == "石門風力"

    def test_confirmed_alias_joins_the_two_variants(self) -> None:
        links = align_stations(
            ["龜山第三加壓站太陽光電發電站"],
            ["龜山加壓站太陽光電"],
            aliases={"龜山加壓站太陽光電": "龜山第三加壓站太陽光電"},
        )
        assert [link.status for link in links] == ["matched"]

    def test_summary_counts_and_lists_the_unmatched(self) -> None:
        summary = summarize_alignment(align_stations(["石門風力發電站"], ["石門風力", "中屯風力"]))
        assert summary["matched"] == 1
        assert summary["generation_only"] == 1
        assert summary["unmatched"] == ["中屯風力"]


class TestSupplementedStations:
    """場址主檔漏收的站，靠補充檔補上時必須和官方對齊結果分得開。"""

    def test_supplement_matches_as_a_distinct_status(self) -> None:
        links = align_stations(
            ["石門風力發電站"],
            ["石門風力", "中屯風力"],
            supplement_names=["中屯風力發電站"],
        )
        statuses = {link.key: link.status for link in links}
        assert statuses == {"石門風力": "matched", "中屯風力": "supplemented"}

    def test_official_master_wins_over_a_supplement(self) -> None:
        links = align_stations(
            ["中屯風力發電站"], ["中屯風力"], supplement_names=["中屯風力發電站"]
        )
        assert [link.status for link in links] == ["matched"]

    def test_summary_separates_alignment_from_coverage(self) -> None:
        """對齊率只算兩個官方檔真的對上的，補充檔算進覆蓋率。"""
        summary = summarize_alignment(
            align_stations(
                ["石門風力發電站"],
                ["石門風力", "中屯風力"],
                supplement_names=["中屯風力發電站"],
            )
        )
        assert summary["match_rate"] == 0.5
        assert summary["coverage_rate"] == 1.0
        assert summary["unmatched"] == []
        assert summary["supplemented_names"] == ["中屯風力"]


class TestBuildStationRecords:
    KEY_MAP = {
        "發電站編號": "發電站編號",
        "發電站名稱": "發電站名稱",
        "能源別": "能源別",
        "地址": "地址",
        "裝置容量(瓩)": "裝置容量(瓩)",
        "風機數量": "風機數量",
        "型號": "型號",
        "申設狀態": "申設狀態",
    }

    @staticmethod
    def _site(station_id: str, name: str, capacity: str, **overrides: str) -> dict[str, str]:
        row = {
            "發電站編號": station_id,
            "發電站名稱": name,
            "能源別": "陸域風力/Onshore Wind",
            "地址": "彰化縣伸港鄉",
            "裝置容量(瓩)": capacity,
            "風機數量": "4",
            "型號": "Vestas V47",
            "申設狀態": "取得執照",
        }
        row.update(overrides)
        return row

    def test_sums_the_sites_of_one_station(self) -> None:
        """彰工風力在 17141 是 4 個場址，入庫粒度是發電站。"""
        records = build_station_records(
            [
                self._site("1", "彰工風力發電站", "46000"),
                self._site("2", "彰工風力發電站", "16000"),
            ],
            self.KEY_MAP,
        )
        assert len(records) == 1
        assert records[0].capacity_kw == 62000
        assert records[0].site_count == 2
        assert records[0].turbine_count == 8
        assert records[0].county == "彰化縣"
        assert records[0].source == "official"

    def test_drops_subtotal_rows_so_capacity_cannot_double(self) -> None:
        records = build_station_records(
            [
                self._site("1", "彰工風力發電站", "46000"),
                self._site("陸域風力小計", "18站", "332940", 地址="含試運轉中機組"),
            ],
            self.KEY_MAP,
        )
        assert [record.station_name for record in records] == ["彰工風力"]
        assert sum(record.capacity_kw for record in records) == 46000

    def test_appends_a_supplement_marked_as_such(self) -> None:
        records = build_station_records(
            [self._site("1", "彰工風力發電站", "46000")],
            self.KEY_MAP,
            supplement_rows=[
                {
                    "station_name": "中屯風力發電站",
                    "energy_type": "陸域風力",
                    "capacity_kw": "4800",
                    "unit_count": "8",
                    "county": "澎湖縣",
                    "status_note": "112.10.19起安全性停機",
                }
            ],
        )
        supplemented = [record for record in records if record.source == "supplement"]
        assert len(supplemented) == 1
        assert supplemented[0].station_name == "中屯風力"
        assert supplemented[0].capacity_kw == 4800
        assert supplemented[0].note == "112.10.19起安全性停機"

    def test_never_lets_a_supplement_shadow_the_official_master(self) -> None:
        records = build_station_records(
            [self._site("1", "彰工風力發電站", "46000")],
            self.KEY_MAP,
            supplement_rows=[
                {
                    "station_name": "彰工風力發電站",
                    "energy_type": "陸域風力",
                    "capacity_kw": "1",
                    "unit_count": "1",
                    "county": "彰化縣",
                    "status_note": "",
                }
            ],
        )
        assert len(records) == 1
        assert records[0].source == "official"
        assert records[0].capacity_kw == 46000
