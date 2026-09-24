"""Raw payload archive and fetch-attempt log: the source of truth for rebuilds (§5.1).

封存檔解壓後與台電回傳的內容逐位元組相同，檔名帶 SHA-256 前 12 碼，所以內容可以自我驗證。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ingest.realtime.timeutil import to_taipei

UNPARSED_DIR = "_unparsed"


@dataclass(frozen=True)
class ArchivedFile:
    path: Path
    sha_prefix: str
    data_time: str | None  # 'YYYY-MM-DD HH:MM'；_unparsed 底下的檔案為 None


def payload_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def archive_payload(
    archive_dir: Path, raw: bytes, *, source_time: datetime | None, fetched_at: datetime
) -> ArchivedFile:
    """原樣 gzip 封存；同一時段內容相同只存一次。DateTime 讀不出來時放進 _unparsed/。"""
    digest = payload_sha256(raw)
    if source_time is None:
        stamp = fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = archive_dir / UNPARSED_DIR / f"{stamp}_{digest[:12]}.json.gz"
        data_time = None
    else:
        local = to_taipei(source_time)
        folder = archive_dir / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}"
        path = folder / f"{local:%H%M}_{digest[:12]}.json.gz"
        data_time = f"{local:%Y-%m-%d %H:%M}"
    if not path.exists():
        _atomic_write(path, gzip.compress(raw, compresslevel=9, mtime=0))
    return ArchivedFile(path, digest[:12], data_time)


def read_payload(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def iter_archive(archive_dir: Path, *, since: str | None = None) -> list[ArchivedFile]:
    """依（資料時段, 寫入時間）排序的封存檔；since 為 'YYYY-MM-DD' 時只列該日（含）之後。"""
    if not archive_dir.is_dir():
        return []
    found: list[tuple[str, int, str, ArchivedFile]] = []
    for path in archive_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.json.gz"):
        year, month, day = path.parts[-4:-1]
        hhmm, _, prefix = path.name.removesuffix(".json.gz").partition("_")
        if len(hhmm) != 4 or not hhmm.isdigit() or len(prefix) != 12:
            continue
        data_time = f"{year}-{month}-{day} {hhmm[:2]}:{hhmm[2:]}"
        if since is not None and data_time[:10] < since:
            continue
        archived = ArchivedFile(path, prefix, data_time)
        found.append((data_time, path.stat().st_mtime_ns, path.name, archived))
    found.sort(key=lambda item: item[:3])
    return [item[3] for item in found]


def _ensure_trailing_newline(path: Path) -> None:
    """當機時可能寫到一半；下一筆不能接在殘行後面，否則兩筆會一起壞掉。"""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
    if last != b"\n":
        with path.open("ab") as handle:
            handle.write(b"\n")


def append_attempt(attempts_dir: Path, record: Mapping[str, object]) -> None:
    """每次嘗試一行 JSON，依 attempted_at 的 UTC 月份分檔；沒有值的欄位省略。"""
    path = attempts_dir / f"{str(record['attempted_at'])[:7]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_trailing_newline(path)
    line = json.dumps(
        {key: value for key, value in record.items() if value is not None},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def iter_attempts(attempts_dir: Path) -> Iterator[dict[str, object]]:
    if not attempts_dir.is_dir():
        return
    for path in sorted(attempts_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue  # 當機時寫到一半的殘行
                if isinstance(record, dict):
                    yield record
