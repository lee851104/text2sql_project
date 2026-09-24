from __future__ import annotations

import gzip
import os
from datetime import UTC, datetime
from pathlib import Path

from realtime_support import payload_bytes

from ingest.realtime.archive import (
    append_attempt,
    archive_payload,
    iter_archive,
    iter_attempts,
    payload_sha256,
    read_payload,
)
from ingest.realtime.parse import read_datetime

FETCHED = datetime(2026, 9, 18, 13, 45, 20, tzinfo=UTC)


def test_payload_is_archived_verbatim_under_its_slot(tmp_path: Path) -> None:
    raw = payload_bytes()
    archived = archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)

    digest = payload_sha256(raw)
    assert archived.path == tmp_path / "2026/09/18" / f"2140_{digest[:12]}.json.gz"
    assert archived.data_time == "2026-09-18 21:40"
    assert archived.sha_prefix == digest[:12]
    assert read_payload(archived.path) == raw
    assert gzip.decompress(archived.path.read_bytes()) == raw


def test_same_content_is_stored_once(tmp_path: Path) -> None:
    raw = payload_bytes()
    first = archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)
    stamp = first.path.stat().st_mtime_ns
    second = archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)

    assert second.path == first.path
    assert second.path.stat().st_mtime_ns == stamp
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1


def test_unreadable_datetime_goes_to_unparsed(tmp_path: Path) -> None:
    archived = archive_payload(tmp_path, b"garbage", source_time=None, fetched_at=FETCHED)

    assert archived.path.parent == tmp_path / "_unparsed"
    assert archived.path.name.startswith("20260918T134520Z_")
    assert archived.data_time is None
    assert iter_archive(tmp_path) == []


def test_iter_archive_orders_by_slot_then_write_time(tmp_path: Path) -> None:
    later = payload_bytes(data_time="2026-09-18T21:50:00")
    first = payload_bytes(data_time="2026-09-18T21:40:00")
    revised = first.replace(b"590.6", b"591.0")
    for raw in (later, first, revised):
        archive_payload(tmp_path, raw, source_time=read_datetime(raw), fetched_at=FETCHED)
    older_revision = next(p for p in tmp_path.rglob("2140_*") if read_payload(p) == first)
    os.utime(older_revision, ns=(1, 1))

    found = iter_archive(tmp_path)
    assert [f.data_time for f in found] == [
        "2026-09-18 21:40",
        "2026-09-18 21:40",
        "2026-09-18 21:50",
    ]
    assert read_payload(found[0].path) == first
    assert iter_archive(tmp_path, since="2026-09-19") == []


def test_attempt_log_round_trips_and_survives_a_torn_line(tmp_path: Path) -> None:
    append_attempt(
        tmp_path, {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "fetch", "etag": None}
    )
    with (tmp_path / "2026-09.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"attempted_at": "2026-09-18T13:5')  # 當機時寫到一半
    append_attempt(tmp_path, {"attempted_at": "2026-09-18T13:55:20+00:00", "kind": "resume"})

    records = list(iter_attempts(tmp_path))
    assert records == [
        {"attempted_at": "2026-09-18T13:45:20+00:00", "kind": "fetch"},
        {"attempted_at": "2026-09-18T13:55:20+00:00", "kind": "resume"},
    ]
