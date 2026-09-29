from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from realtime_support import make_config, payload_bytes, snapshot_payload, tiny_payload

from ingest.realtime.parse import (
    PayloadRejected,
    clean_type,
    parse_number,
    parse_payload,
    parse_ratio,
    parse_with_share,
    read_datetime,
)

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)  # 22:00 in Taipei, after the 21:40 snapshot


def _parse(raw: bytes, tmp_path: Path, **overrides: object):
    config = make_config(tmp_path, **overrides)
    return parse_payload(
        raw, now=NOW, validation=config.validation, slot_minutes=config.schedule.slot_minutes
    )


def _details(parsed) -> dict[tuple[str, str], object]:
    return {(row.unit_type, row.unit_name): row for row in parsed.details}


def test_real_snapshot_parses_cleanly(tmp_path: Path) -> None:
    parsed = _parse(payload_bytes(), tmp_path)

    assert parsed.data_time == "2026-09-18 21:40"
    assert len(parsed.details) == 204
    assert len(parsed.subtotals) == 11
    assert parsed.quarantined == ()
    assert parsed.quality == "ok", parsed.warnings


def test_names_repeat_across_types_and_stay_separate(tmp_path: Path) -> None:
    details = _details(_parse(payload_bytes(), tmp_path))

    discharge = details[("儲能", "明潭#1")]
    charge = details[("儲能負載", "明潭#1")]
    assert discharge.flow == "generation"
    assert charge.flow == "storage_load"
    assert charge.unit_type_raw == "儲能負載(Energy Storage System Load)</b>"
    assert details[("儲能負載", "電池(註16)")].net_mw == -19.4
    assert ("太陽能", "其它台電自有") in details
    assert ("風力", "其它台電自有") in details


def test_subtotal_with_a_footnote_is_still_a_subtotal(tmp_path: Path) -> None:
    parsed = _parse(payload_bytes(), tmp_path)
    subtotals = {row.unit_type: row for row in parsed.subtotals}

    assert subtotals["風力"].subtotal_name == "小計(註5)"
    assert subtotals["風力"].net_mw == 2561.4
    assert subtotals["風力"].detail_net_mw == 2561.4
    assert subtotals["燃氣"].net_share_pct == 50.877
    assert "儲能負載" not in subtotals
    assert not any(row.unit_name.startswith("小計") for row in parsed.details)


def test_values_become_null_not_zero(tmp_path: Path) -> None:
    details = _details(_parse(payload_bytes(), tmp_path))

    island = details[("燃料油", "離島其它(註4)")]
    assert island.net_mw is None
    assert island.value_status == "missing"
    trial = details[("燃氣", "台中CC#1(註10)")]
    assert trial.capacity_mw is None
    assert trial.load_ratio is None
    assert details[("燃氣", "大潭CC#1")].load_ratio == 0.79521
    assert details[("燃氣", "大潭CC#1")].note == ""


def test_communication_failure_marks_the_value_untrusted(tmp_path: Path) -> None:
    row = _details(_parse(payload_bytes(), tmp_path))[("風力", "龍三風(註10)")]

    assert row.net_mw == 0.0
    assert row.value_status == "comm_error"


# 參數用短 id：pytest 會把測試 id 放進環境變數，整份回應當 id 會超過 Windows 的 32,767 字元上限。
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param(b"not json", "PAYLOAD_INVALID", id="not-json"),
        pytest.param(json.dumps({"aaData": []}).encode(), "PAYLOAD_INVALID", id="no-datetime"),
        pytest.param(
            payload_bytes(data_time="2026-09-18T21:45:00"), "DATETIME_OFF_SLOT", id="off-slot"
        ),
        pytest.param(
            payload_bytes(data_time="2026-09-18T23:00:00"), "DATETIME_IN_FUTURE", id="future"
        ),
    ],
)
def test_structural_problems_reject_the_whole_payload(
    raw: bytes, code: str, tmp_path: Path
) -> None:
    with pytest.raises(PayloadRejected) as caught:
        _parse(raw, tmp_path)
    assert caught.value.code == code


def test_missing_field_rejects(tmp_path: Path) -> None:
    payload = snapshot_payload()
    del payload["aaData"][0]["淨發電量(MW)"]

    with pytest.raises(PayloadRejected) as caught:
        _parse(payload_bytes(payload), tmp_path)
    assert caught.value.code == "FIELD_MISSING"
    assert caught.value.data_time == "2026-09-18 21:40"


def test_duplicate_key_rejects(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["aaData"].append(dict(payload["aaData"][0]))

    with pytest.raises(PayloadRejected) as caught:
        _parse(payload_bytes(payload), tmp_path)
    assert caught.value.code == "DUPLICATE_KEY"


def test_only_subtotals_rejects(tmp_path: Path) -> None:
    raw = tiny_payload("2026-09-18T21:40:00", [("燃氣", "小計", "1(1%)", "1(1%)", "")])

    with pytest.raises(PayloadRejected) as caught:
        _parse(raw, tmp_path)
    assert caught.value.code == "NO_DETAIL_ROWS"


def test_subtotal_mismatch_is_flagged_not_rejected(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["aaData"][0]["淨發電量(MW)"] = "600.6"  # 大潭CC#1 was 590.6

    parsed = _parse(payload_bytes(payload), tmp_path)
    assert parsed.quality == "warn"
    assert [w.code for w in parsed.warnings] == ["SUBTOTAL_MISMATCH"]
    assert "燃氣" in parsed.warnings[0].detail


def test_renamed_aggregate_is_quarantined(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["aaData"].insert(
        1,
        {
            "機組類型": "燃氣",
            "機組名稱": "合計",
            "裝置容量(MW)": "15918.1(26.052%)",
            "淨發電量(MW)": "16521.4(50.877%)",
            "淨發電量/裝置容量比(%)": "",
            "備註": "",
        },
    )

    parsed = _parse(payload_bytes(payload), tmp_path)
    assert [row.reason for row in parsed.quarantined] == ["UNRECOGNIZED_AGGREGATE"]
    assert parsed.quarantined[0].row_index == 1
    assert "UNRECOGNIZED_AGGREGATE" in [w.code for w in parsed.warnings]
    assert ("燃氣", "合計") not in _details(parsed)


def test_value_and_shape_warnings(tmp_path: Path) -> None:
    raw = tiny_payload(
        "2026-09-18T21:40:00",
        [("核融合", "新機組", "100", "abc", ""), ("燃氣", "大潭CC#1", "742.7", "590.6", "")],
    )
    payload = json.loads(raw)
    payload["aaData"][1]["新欄位"] = "x"

    parsed = _parse(json.dumps(payload, ensure_ascii=False).encode(), tmp_path)
    codes = [w.code for w in parsed.warnings]
    assert codes == ["UNKNOWN_TYPE", "ROW_COUNT_UNUSUAL", "EXTRA_FIELD", "VALUE_UNPARSEABLE"]
    assert _details(parsed)[("核融合", "新機組")].value_status == "invalid"


def test_cleaning_helpers() -> None:
    assert clean_type("儲能負載(Energy Storage System Load)</b>") == "儲能負載"
    assert clean_type("民營電廠-燃氣") == "民營電廠-燃氣"
    assert parse_number("1,234.5") == (1234.5, "ok")
    assert parse_number(" ") == (None, "missing")
    assert parse_number("N/A") == (None, "missing")
    assert parse_ratio("38.70066%") == (0.3870066, "ok")
    assert parse_with_share("16521.4(50.877%)") == (16521.4, 50.877)


def test_read_datetime_is_lenient() -> None:
    assert read_datetime(payload_bytes()) == datetime.fromisoformat("2026-09-18T21:40:00+08:00")
    assert read_datetime(b"garbage") is None


def test_subtotal_tolerance_exactly_at_threshold(tmp_path: Path) -> None:
    """Difference of exactly 0.1 MW (at tolerance) must NOT warn.

    Uses values that trip float precision bug: 596.6 (details) vs 596.7 (subtotal).
    In floats, 596.7 - 596.6 = 0.10000000000002274, which would warn if not rounded.
    """
    raw = tiny_payload(
        "2026-09-18T21:40:00",
        [
            ("風力", "Plant A", "300", "298.3", ""),
            ("風力", "Plant B", "300", "298.3", ""),
            ("風力", "小計", "600", "596.7", ""),
        ],
    )
    parsed = _parse(raw, tmp_path)

    # Should NOT warn about SUBTOTAL_MISMATCH because 0.1 MW is exactly at the tolerance
    assert not any(w.code == "SUBTOTAL_MISMATCH" for w in parsed.warnings)

    subtotals = {row.unit_type: row for row in parsed.subtotals}
    assert subtotals["風力"].net_mw == 596.7
    assert subtotals["風力"].detail_net_mw == 596.6


def test_subtotal_tolerance_beyond_threshold(tmp_path: Path) -> None:
    """Difference of 0.2 MW (beyond tolerance) must warn."""
    raw = tiny_payload(
        "2026-09-18T21:40:00",
        [
            ("風力", "Plant A", "300", "298.3", ""),
            ("風力", "Plant B", "300", "298.3", ""),
            ("風力", "小計", "600", "596.8", ""),
        ],
    )
    parsed = _parse(raw, tmp_path)

    # Should warn because 0.2 MW exceeds the 0.1 MW tolerance
    assert any(w.code == "SUBTOTAL_MISMATCH" for w in parsed.warnings)

    subtotals = {row.unit_type: row for row in parsed.subtotals}
    assert subtotals["風力"].net_mw == 596.8
    assert subtotals["風力"].detail_net_mw == 596.6
