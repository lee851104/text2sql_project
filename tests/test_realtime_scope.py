"""Which realtime units each identity may read (RT-3a spec §5)."""

from __future__ import annotations

import pytest

from text2sql.realtime_scope import (
    ALL_PLANTS,
    RealtimeScope,
    UnitRow,
    own_unit_keys,
    visible_unit_keys,
)

DATAN_GAS = UnitRow("燃氣|大潭#1", "plant", 8)
UNDECIDED_GAS = UnitRow("燃氣|興達#1", "undecided", None)
SHARED_SOLAR = UnitRow("太陽能|太陽能購電", "shared", None)
MINGTAN_HYDRO = UnitRow("水力|明潭#1", "plant", 12)
UNITS = (DATAN_GAS, UNDECIDED_GAS, SHARED_SOLAR, MINGTAN_HYDRO)
DATAN = RealtimeScope("plant", 8, "大潭發電廠")
MINGTAN = RealtimeScope("plant", 12, "明潭發電廠")


def test_the_all_scope_sees_everything_including_undecided_units() -> None:
    assert visible_unit_keys(ALL_PLANTS, UNITS) is None
    assert own_unit_keys(ALL_PLANTS, UNITS) == frozenset()


def test_a_plant_sees_its_own_units_and_every_shared_row() -> None:
    assert visible_unit_keys(DATAN, UNITS) == {DATAN_GAS.key, SHARED_SOLAR.key}
    assert visible_unit_keys(MINGTAN, UNITS) == {MINGTAN_HYDRO.key, SHARED_SOLAR.key}


@pytest.mark.parametrize("plant_id", [8, 12, 999])
def test_undecided_units_are_never_shown_to_a_plant(plant_id: int) -> None:
    scope = RealtimeScope("plant", plant_id, "某電廠")

    assert UNDECIDED_GAS.key not in visible_unit_keys(scope, UNITS)
    assert UNDECIDED_GAS.key not in own_unit_keys(scope, UNITS)


def test_own_units_exclude_shared_rows() -> None:
    assert own_unit_keys(DATAN, UNITS) == {DATAN_GAS.key}
    assert own_unit_keys(MINGTAN, UNITS) == {MINGTAN_HYDRO.key}


def test_a_plant_with_no_units_sees_only_shared_rows() -> None:
    scope = RealtimeScope("plant", 999, "沒有機組的電廠")

    assert visible_unit_keys(scope, UNITS) == {SHARED_SOLAR.key}
    assert own_unit_keys(scope, UNITS) == frozenset()


def test_units_can_be_any_iterable() -> None:
    assert visible_unit_keys(DATAN, iter(UNITS)) == {DATAN_GAS.key, SHARED_SOLAR.key}


@pytest.mark.parametrize(
    ("kind", "plant_id", "plant_name"),
    [
        ("plant", None, None),
        ("plant", 8, None),
        ("plant", None, "大潭發電廠"),
        ("all", 8, None),
        ("all", None, "大潭發電廠"),
        ("region", None, None),
    ],
)
def test_an_inconsistent_scope_is_refused(
    kind: str, plant_id: int | None, plant_name: str | None
) -> None:
    with pytest.raises(ValueError):
        RealtimeScope(kind, plant_id, plant_name)  # type: ignore[arg-type]


def test_the_scope_label_names_the_plant() -> None:
    assert ALL_PLANTS.label == "all"
    assert DATAN.label == "plant:大潭發電廠"
