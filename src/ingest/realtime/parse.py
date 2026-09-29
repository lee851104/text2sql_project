"""Parse one d006001 payload into validated rows (pure: no files, no database).

結構有問題就整份拒收（PayloadRejected）；數值有問題就逐列標記後照收（ParseWarning）。
拒收不等於遺失：原始回應在解析前就已經封存，修好規則後重建即可補回。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from ingest.realtime.config import ValidationConfig
from ingest.realtime.timeutil import TAIPEI, format_slot

REQUIRED_FIELDS = (
    "機組類型",
    "機組名稱",
    "裝置容量(MW)",
    "淨發電量(MW)",
    "淨發電量/裝置容量比(%)",
    "備註",
)
STORAGE_LOAD_TYPE = "儲能負載"
MISSING_TOKENS = frozenset({"", "-", "N/A"})

_TAG = re.compile(r"<[^>]+>")
_TRAILING_ENGLISH = re.compile(r"\s*\([A-Za-z0-9 .,&/'-]+\)\s*$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_NUMBER_WITH_SHARE = re.compile(r"(-?\d+(?:\.\d+)?)\((-?\d+(?:\.\d+)?)%\)")
_PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)%")


class PayloadRejected(ValueError):
    """整份不收。`data_time` 在讀得出 DateTime 時才有值。"""

    def __init__(self, code: str, detail: str, *, data_time: str | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.data_time = data_time


@dataclass(frozen=True)
class ParseWarning:
    code: str
    detail: str


@dataclass(frozen=True)
class DetailRow:
    unit_type: str
    unit_type_raw: str
    unit_name: str
    flow: str
    net_mw: float | None
    capacity_mw: float | None
    load_ratio: float | None
    note: str
    value_status: str


@dataclass(frozen=True)
class SubtotalRow:
    unit_type: str
    subtotal_name: str
    net_mw: float | None
    net_share_pct: float | None
    capacity_mw: float | None
    capacity_share_pct: float | None
    detail_net_mw: float


@dataclass(frozen=True)
class QuarantinedRow:
    row_index: int
    reason: str
    raw_row: str


@dataclass(frozen=True)
class ParsedSnapshot:
    data_time: str
    details: tuple[DetailRow, ...]
    subtotals: tuple[SubtotalRow, ...]
    quarantined: tuple[QuarantinedRow, ...]
    warnings: tuple[ParseWarning, ...]

    @property
    def quality(self) -> str:
        return "warn" if self.warnings else "ok"


def clean_type(raw: str) -> str:
    """`儲能負載(Energy Storage System Load)</b>` → `儲能負載`。"""
    return _TRAILING_ENGLISH.sub("", _TAG.sub("", raw).strip()).strip()


def parse_number(text: str) -> tuple[float | None, str]:
    """回傳（數值, 'ok'｜'missing'｜'invalid'）；`-`、`N/A`、空字串是缺值，不是 0。"""
    value = text.strip().replace(",", "")
    if value in MISSING_TOKENS:
        return None, "missing"
    if _NUMBER.fullmatch(value):
        return float(value), "ok"
    return None, "invalid"


def parse_ratio(text: str) -> tuple[float | None, str]:
    """`79.521%` → 0.79521。"""
    value = text.strip().replace(",", "")
    if value in MISSING_TOKENS:
        return None, "missing"
    match = _PERCENT.fullmatch(value)
    if match:
        return round(float(match.group(1)) / 100, 8), "ok"
    return None, "invalid"


def parse_with_share(text: str) -> tuple[float | None, float | None]:
    """小計的值：`16521.4(50.877%)` → (16521.4, 50.877)。"""
    value = text.strip().replace(",", "")
    match = _NUMBER_WITH_SHARE.fullmatch(value)
    if match:
        return float(match.group(1)), float(match.group(2))
    number, _status = parse_number(value)
    return number, None


def _looks_aggregated(text: object) -> bool:
    return bool(_NUMBER_WITH_SHARE.fullmatch(str(text).strip().replace(",", "")))


def _source_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("DateTime 不是字串")
    moment = datetime.fromisoformat(value.strip())
    return moment.replace(tzinfo=TAIPEI) if moment.tzinfo is None else moment.astimezone(TAIPEI)


def read_datetime(raw: bytes) -> datetime | None:
    """只讀出 DateTime（UTC+8），不驗證其他內容；讀不到就回 None。決定封存路徑用。"""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        return _source_datetime(payload["DateTime"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def parse_payload(
    raw: bytes, *, now: datetime, validation: ValidationConfig, slot_minutes: int
) -> ParsedSnapshot:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadRejected("PAYLOAD_INVALID", f"不是可解析的 JSON：{error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("aaData"), list):
        raise PayloadRejected("PAYLOAD_INVALID", "頂層必須是含 aaData 陣列的物件。")
    try:
        moment = _source_datetime(payload.get("DateTime"))
    except (TypeError, ValueError) as error:
        raise PayloadRejected(
            "PAYLOAD_INVALID", f"DateTime 無法解析：{payload.get('DateTime')!r}"
        ) from error
    data_time = format_slot(moment)
    if moment.minute % slot_minutes or moment.second or moment.microsecond:
        raise PayloadRejected(
            "DATETIME_OFF_SLOT",
            f"{moment.isoformat()} 不在 {slot_minutes} 分鐘整點。",
            data_time=data_time,
        )
    if moment > now + timedelta(seconds=validation.future_tolerance_seconds):
        raise PayloadRejected(
            "DATETIME_IN_FUTURE", f"{moment.isoformat()} 比現在晚太多。", data_time=data_time
        )

    details: list[DetailRow] = []
    subtotal_rows: list[tuple[str, dict[str, object]]] = []
    quarantined: list[QuarantinedRow] = []
    extra_fields: set[str] = set()
    unparseable: dict[str, list[str]] = defaultdict(list)
    for index, row in enumerate(payload["aaData"]):
        if not isinstance(row, dict):
            raise PayloadRejected(
                "PAYLOAD_INVALID", f"aaData 第 {index} 列不是物件。", data_time=data_time
            )
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            raise PayloadRejected(
                "FIELD_MISSING", f"第 {index} 列缺少欄位：{'、'.join(missing)}", data_time=data_time
            )
        extra_fields.update(str(key) for key in row if key not in REQUIRED_FIELDS)
        type_raw = str(row["機組類型"]).strip()
        unit_type = clean_type(type_raw)
        name = str(row["機組名稱"]).strip()
        if name.startswith("小計"):
            subtotal_rows.append((unit_type, row))
            continue
        if _looks_aggregated(row["淨發電量(MW)"]) or _looks_aggregated(row["裝置容量(MW)"]):
            raw_row = json.dumps(row, ensure_ascii=False)
            quarantined.append(QuarantinedRow(index, "UNRECOGNIZED_AGGREGATE", raw_row))
            continue
        net, net_status = parse_number(str(row["淨發電量(MW)"]))
        capacity, capacity_status = parse_number(str(row["裝置容量(MW)"]))
        ratio, ratio_status = parse_ratio(str(row["淨發電量/裝置容量比(%)"]))
        for field, status in (
            ("淨發電量(MW)", net_status),
            ("裝置容量(MW)", capacity_status),
            ("淨發電量/裝置容量比(%)", ratio_status),
        ):
            if status == "invalid":
                unparseable[field].append(name)
        note = str(row["備註"]).strip()
        value_status = net_status
        if net_status == "ok" and note in validation.unreliable_notes:
            value_status = "comm_error"
        flow = "storage_load" if unit_type == STORAGE_LOAD_TYPE else "generation"
        details.append(
            DetailRow(unit_type, type_raw, name, flow, net, capacity, ratio, note, value_status)
        )

    if not details:
        raise PayloadRejected("NO_DETAIL_ROWS", "沒有任何明細列。", data_time=data_time)
    keys = [(detail.unit_type, detail.unit_name) for detail in details]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    subtotal_types = [unit_type for unit_type, _row in subtotal_rows]
    duplicates += sorted({(t, "小計") for t in subtotal_types if subtotal_types.count(t) > 1})
    if duplicates:
        listed = "、".join(f"{unit_type}|{name}" for unit_type, name in duplicates)
        raise PayloadRejected(
            "DUPLICATE_KEY", f"重複的（類型, 名稱）：{listed}", data_time=data_time
        )

    warnings: list[ParseWarning] = []
    detail_sums: dict[str, float] = defaultdict(float)
    for detail in details:
        if detail.net_mw is not None:
            detail_sums[detail.unit_type] += detail.net_mw
    subtotals: list[SubtotalRow] = []
    for unit_type, row in subtotal_rows:
        net, net_share = parse_with_share(str(row["淨發電量(MW)"]))
        capacity, capacity_share = parse_with_share(str(row["裝置容量(MW)"]))
        detail_net = round(detail_sums.get(unit_type, 0.0), 1)
        name = str(row["機組名稱"]).strip()
        subtotals.append(
            SubtotalRow(unit_type, name, net, net_share, capacity, capacity_share, detail_net)
        )
        if net is not None and round(abs(detail_net - net), 6) > validation.subtotal_tolerance_mw:
            warnings.append(
                ParseWarning(
                    "SUBTOTAL_MISMATCH", f"{unit_type}：明細 {detail_net:.1f} MW，小計 {net:.1f} MW"
                )
            )
    for row in quarantined:
        warnings.append(ParseWarning("UNRECOGNIZED_AGGREGATE", f"第 {row.row_index} 列已隔離"))
    for unit_type in sorted({detail.unit_type for detail in details} - validation.known_types):
        warnings.append(ParseWarning("UNKNOWN_TYPE", unit_type))
    low, high = validation.detail_rows_expected
    if not low <= len(details) <= high:
        warnings.append(
            ParseWarning("ROW_COUNT_UNUSUAL", f"明細 {len(details)} 列，預期 {low}–{high} 列")
        )
    if extra_fields:
        warnings.append(ParseWarning("EXTRA_FIELD", "、".join(sorted(extra_fields))))
    for field, names in unparseable.items():
        listed = "、".join(names[:5])
        warnings.append(ParseWarning("VALUE_UNPARSEABLE", f"{field} {len(names)} 列：{listed}"))
    return ParsedSnapshot(
        data_time, tuple(details), tuple(subtotals), tuple(quarantined), tuple(warnings)
    )
