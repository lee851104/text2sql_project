from __future__ import annotations

from pathlib import Path

from realtime_support import make_config, snapshot_payload

from ingest.realtime.candidates import plant_aliases, propose


def _proposals(tmp_path: Path) -> dict[tuple[str, str], dict[str, str]]:
    aliases = plant_aliases(make_config(tmp_path).plants_csv)
    return {
        (row["unit_type"], row["unit_name"]): row for row in propose(snapshot_payload(), aliases)
    }


def test_every_detail_row_gets_exactly_one_proposal(tmp_path: Path) -> None:
    proposals = _proposals(tmp_path)

    assert len(proposals) == 204
    assert not any(name.startswith("小計") for _type, name in proposals)


def test_prefix_match_proposes_the_plant(tmp_path: Path) -> None:
    proposals = _proposals(tmp_path)

    assert proposals[("燃氣", "大潭CC#1")]["plant_id"] == "8"
    assert proposals[("儲能負載", "明潭#1")]["access_scope"] == "plant"
    assert proposals[("儲能負載", "明潭#1")]["plant_id"] == "12"


def test_renewables_and_buckets_are_shared(tmp_path: Path) -> None:
    proposals = _proposals(tmp_path)

    assert proposals[("太陽能", "其它購電太陽能")]["grain"] == "bucket"
    assert proposals[("太陽能", "其它購電太陽能")]["access_scope"] == "shared"
    assert proposals[("風力", "沃一風")]["access_scope"] == "shared"
    assert proposals[("汽電共生", "汽電共生")]["grain"] == "bucket"
    assert proposals[("儲能", "電池(註16)")]["grain"] == "bucket"


def test_no_unique_match_is_left_for_a_human(tmp_path: Path) -> None:
    row = _proposals(tmp_path)[("燃煤", "林口#1")]  # 測試名冊只有大潭與明潭

    assert row["access_scope"] == ""
    assert row["note"].startswith("待人工確認")
