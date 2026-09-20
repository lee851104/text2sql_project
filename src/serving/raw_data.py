"""Catalog and bounded lazy readers for the complete raw open-data snapshot."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO
from uuid import uuid4
from xml.etree import ElementTree

SUPPORTED_FORMATS = {"CSV", "JSON", "XML", "ZIP"}
MAX_ROWS = 200
MAX_JSON_BYTES = 128 * 1024 * 1024
PREVIEW_ROWS = 5
FIELD_ALIASES = {
    "地址": ("地址", "在哪裡", "在哪", "位置"),
    "電話": ("電話", "聯絡電話", "連絡電話"),
}
QUESTION_NOISE = ("請問", "請查詢", "查詢", "告訴我", "的", "是什麼", "為何")


class RawDataError(ValueError):
    """A safe error that may be returned to an API caller."""


class RawDataNotFoundError(RawDataError):
    """Raised when a catalog resource or ZIP member does not exist."""


class RawDataService:
    def __init__(self, *, root: Path, database: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.manifest = self._find_manifest()
        self.snapshot_root = self.manifest.parent
        self.database = Path(
            database or self.root / "data" / "processed" / "raw_open_data.db"
        ).resolve()
        self.inbox = (self.root / "data" / "raw" / "inbox").resolve()
        self._lock = RLock()

    def _find_manifest(self) -> Path:
        candidates = (
            self.root / "data" / "raw" / "manifest.json",
            self.root / "manifest.json",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return candidates[0].resolve()

    def _ensure_catalog(self) -> None:
        if not self.database.is_file():
            self.rebuild()

    def rebuild(self) -> dict[str, object]:
        with self._lock:
            if not self.manifest.is_file():
                raise RawDataError("找不到原始資料 manifest.json。")
            try:
                manifest_data = json.loads(self.manifest.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RawDataError("原始資料 manifest.json 無法讀取。") from error
            resources = manifest_data.get("resources")
            if not isinstance(resources, list):
                raise RawDataError("原始資料 manifest.json 缺少 resources 清單。")

            entries = [self._manifest_entry(item) for item in resources if isinstance(item, dict)]
            entries.extend(self._inbox_entries(entries))
            self.database.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.database.with_name(f".{self.database.name}.{uuid4().hex}.tmp")
            try:
                connection = sqlite3.connect(temporary)
                try:
                    self._create_schema(connection)
                    for entry in entries:
                        self._insert_entry(connection, entry)
                    connection.execute(
                        "INSERT INTO raw_meta(key, value) VALUES (?, ?)",
                        ("fetched_at", str(manifest_data.get("fetched_at") or "")),
                    )
                    connection.commit()
                finally:
                    connection.close()
                os.replace(temporary, self.database)
            finally:
                temporary.unlink(missing_ok=True)
            return self.status()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE raw_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE raw_resource (
                resource_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                resource_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                format TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                source_url TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                license TEXT NOT NULL,
                file_present INTEGER NOT NULL,
                queryable INTEGER NOT NULL,
                columns_json TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                parse_error TEXT NOT NULL
            );
            CREATE INDEX idx_raw_resource_title ON raw_resource(title);
            CREATE INDEX idx_raw_resource_format ON raw_resource(format);
            """
        )

    def _manifest_entry(self, item: Mapping[str, Any]) -> dict[str, Any]:
        dataset_id = str(item.get("DatasetId") or "unknown")
        try:
            resource_index = int(item.get("ResourceIndex") or 1)
        except (TypeError, ValueError):
            resource_index = 1
        relative_path = str(item.get("RelativePath") or "")
        path = self._resolve_source(relative_path)
        format_name = str(item.get("Format") or path.suffix.lstrip(".")).upper()
        return {
            "resource_id": f"{dataset_id}-{resource_index:02d}",
            "dataset_id": dataset_id,
            "resource_index": resource_index,
            "title": str(item.get("Title") or path.stem),
            "format": format_name,
            "relative_path": self._relative_display(path),
            "path": path,
            "source_url": str(item.get("Url") or ""),
            "bytes": int(item.get("Bytes") or (path.stat().st_size if path.is_file() else 0)),
            "status": str(item.get("Status") or "unknown"),
            "updated_at": str(item.get("UpdatedAt") or ""),
            "license": str(item.get("License") or ""),
        }

    def _inbox_entries(self, existing: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not self.inbox.is_dir():
            return []
        known = {Path(str(item["path"])).resolve() for item in existing}
        additions: list[dict[str, Any]] = []
        for path in sorted(item for item in self.inbox.rglob("*") if item.is_file()):
            resolved = path.resolve()
            if resolved in known or resolved.suffix.lstrip(".").upper() not in SUPPORTED_FORMATS:
                continue
            digest = hashlib.sha256(str(resolved.relative_to(self.inbox)).encode()).hexdigest()[:12]
            additions.append(
                {
                    "resource_id": f"local-{digest}-01",
                    "dataset_id": f"local-{digest}",
                    "resource_index": 1,
                    "title": resolved.stem,
                    "format": resolved.suffix.lstrip(".").upper(),
                    "relative_path": self._relative_display(resolved),
                    "path": resolved,
                    "source_url": "",
                    "bytes": resolved.stat().st_size,
                    "status": "local_inbox",
                    "updated_at": "",
                    "license": "",
                }
            )
        return additions

    def _resolve_source(self, relative_path: str) -> Path:
        normalized = Path(relative_path.replace("\\", "/"))
        candidates = (self.snapshot_root / normalized, self.root / normalized)
        approved = (
            self.snapshot_root.resolve(),
            (self.root / "files").resolve(),
            (self.root / "data" / "raw" / "files").resolve(),
            self.inbox,
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if (
                any(resolved == base or resolved.is_relative_to(base) for base in approved)
                and resolved.exists()
            ):
                return resolved
        fallback = candidates[0].resolve()
        if any(fallback == base or fallback.is_relative_to(base) for base in approved):
            return fallback
        raise RawDataError("manifest.json 含有不安全的資料路徑。")

    def _relative_display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.name

    def _insert_entry(self, connection: sqlite3.Connection, entry: Mapping[str, Any]) -> None:
        path = Path(entry["path"])
        present = path.is_file()
        queryable = present and entry["format"] in SUPPORTED_FORMATS
        columns: list[str] = []
        preview: list[list[Any]] = []
        parse_error = ""
        if queryable:
            try:
                result = self._read_path(path, str(entry["format"]), limit=PREVIEW_ROWS)
                columns = result["columns"]
                preview = result["rows"]
            except (
                OSError,
                UnicodeError,
                csv.Error,
                json.JSONDecodeError,
                ElementTree.ParseError,
                zipfile.BadZipFile,
                RawDataError,
            ):
                parse_error = "內容預覽無法解析；仍可查詢資源中繼資料。"
        connection.execute(
            """
            INSERT INTO raw_resource (
                resource_id, dataset_id, resource_index, title, format, relative_path,
                source_url, bytes, status, updated_at, license, file_present, queryable,
                columns_json, preview_json, parse_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["resource_id"],
                entry["dataset_id"],
                entry["resource_index"],
                entry["title"],
                entry["format"],
                entry["relative_path"],
                entry["source_url"],
                entry["bytes"],
                entry["status"],
                entry["updated_at"],
                entry["license"],
                int(present),
                int(queryable),
                json.dumps(columns, ensure_ascii=False),
                json.dumps(preview, ensure_ascii=False),
                parse_error,
            ),
        )

    def status(self) -> dict[str, object]:
        self._ensure_catalog()
        with sqlite3.connect(self.database) as connection:
            total, queryable, present, total_bytes = connection.execute(
                "SELECT COUNT(*), SUM(queryable), SUM(file_present), SUM(bytes) FROM raw_resource"
            ).fetchone()
            formats = dict(
                connection.execute(
                    "SELECT format, COUNT(*) FROM raw_resource GROUP BY format ORDER BY format"
                ).fetchall()
            )
            fetched = connection.execute(
                "SELECT value FROM raw_meta WHERE key = 'fetched_at'"
            ).fetchone()
        return {
            "total_resources": int(total or 0),
            "queryable_resources": int(queryable or 0),
            "present_resources": int(present or 0),
            "total_bytes": int(total_bytes or 0),
            "formats": formats,
            "fetched_at": fetched[0] if fetched else "",
            "catalog": self.database.name,
        }

    def list_resources(
        self,
        *,
        search: str | None = None,
        format_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        self._ensure_catalog()
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM raw_resource"
        params: list[object] = []
        if format_name:
            sql += " WHERE format = ?"
            params.append(format_name.upper())
        sql += " ORDER BY dataset_id, resource_index"
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(sql, params)]
        if search and search.strip():
            rows.sort(key=lambda row: self._match_score(search, str(row["title"])), reverse=True)
            matched = [row for row in rows if self._match_score(search, str(row["title"])) > 0]
            rows = matched or rows
        return [self._public_resource(row) for row in rows[:limit]]

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())

    @classmethod
    def _match_score(cls, question: str, title: str) -> float:
        left = cls._normalize(question).replace("台灣電力公司", "")
        right = cls._normalize(title).replace("台灣電力公司", "")
        if not left or not right:
            return 0.0
        if left in right or right in left:
            return 2.0
        left_pairs = {left[index : index + 2] for index in range(max(1, len(left) - 1))}
        right_pairs = {right[index : index + 2] for index in range(max(1, len(right) - 1))}
        return len(left_pairs & right_pairs) / max(1, len(left_pairs))

    @staticmethod
    def _public_resource(row: Mapping[str, Any]) -> dict[str, object]:
        return {
            "resource_id": row["resource_id"],
            "dataset_id": row["dataset_id"],
            "resource_index": row["resource_index"],
            "title": row["title"],
            "format": row["format"],
            "relative_path": row["relative_path"],
            "source_url": row["source_url"],
            "bytes": row["bytes"],
            "status": row["status"],
            "updated_at": row["updated_at"],
            "license": row["license"],
            "file_present": bool(row["file_present"]),
            "queryable": bool(row["queryable"]),
            "columns": json.loads(row["columns_json"]),
            "preview": json.loads(row["preview_json"]),
            "parse_error": row["parse_error"],
        }

    def _resource(self, resource_id: str) -> dict[str, Any]:
        self._ensure_catalog()
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM raw_resource WHERE resource_id = ?", (resource_id,)
            ).fetchone()
        if row is None:
            raise RawDataNotFoundError("找不到指定的原始資料資源。")
        return dict(row)

    def read_rows(
        self,
        resource_id: str,
        *,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
        member: str | None = None,
    ) -> dict[str, object]:
        resource = self._resource(resource_id)
        path = self._resolve_source(str(resource["relative_path"]))
        if not path.is_file():
            raise RawDataNotFoundError("原始資料檔案不存在。")
        result = self._read_path(
            path,
            str(resource["format"]),
            search=(search or "").strip(),
            limit=max(1, min(int(limit), MAX_ROWS)),
            offset=max(0, int(offset)),
            member=member,
        )
        return {
            "resource_id": resource_id,
            "title": resource["title"],
            "format": resource["format"],
            "member": member,
            **result,
        }

    def _read_path(
        self,
        path: Path,
        format_name: str,
        *,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
        member: str | None = None,
    ) -> dict[str, Any]:
        if format_name == "CSV":
            with path.open("rb") as handle:
                return self._read_csv(handle, search=search, limit=limit, offset=offset)
        if format_name == "JSON":
            with path.open("rb") as handle:
                return self._read_json(handle, search=search, limit=limit, offset=offset)
        if format_name == "XML":
            with path.open("rb") as handle:
                return self._read_xml(handle, search=search, limit=limit, offset=offset)
        if format_name == "ZIP":
            return self._read_zip(path, member=member, search=search, limit=limit, offset=offset)
        raise RawDataError("此資源格式尚不支援內容查詢。")

    @staticmethod
    def _text_stream(handle: BinaryIO) -> io.TextIOWrapper:
        sample = handle.read(65536)
        handle.seek(0)
        encoding = "utf-8-sig"
        for candidate in ("utf-8-sig", "utf-8", "cp950", "big5"):
            try:
                sample.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        return io.TextIOWrapper(handle, encoding=encoding, errors="replace", newline="")

    @classmethod
    def _read_csv(cls, handle: BinaryIO, *, search: str, limit: int, offset: int) -> dict[str, Any]:
        text = cls._text_stream(handle)
        reader = csv.reader(text)
        try:
            columns = [
                str(value).strip() or f"column_{index + 1}"
                for index, value in enumerate(next(reader))
            ]
        except StopIteration:
            return {"columns": [], "rows": [], "has_more": False}
        rows, matched = [], 0
        needle = search.casefold()
        for values in reader:
            row = [value for value in values]
            if needle and needle not in " ".join(row).casefold():
                continue
            if matched < offset:
                matched += 1
                continue
            if len(rows) >= limit:
                return {"columns": columns, "rows": rows, "has_more": True}
            rows.append(row + [""] * max(0, len(columns) - len(row)))
            matched += 1
        return {"columns": columns, "rows": rows, "has_more": False}

    @staticmethod
    def _json_records(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item if isinstance(item, dict) else {"value": item} for item in value]
        if isinstance(value, dict):
            for child in value.values():
                if isinstance(child, list):
                    return [item if isinstance(item, dict) else {"value": item} for item in child]
            return [value]
        return [{"value": value}]

    @classmethod
    def _read_json(
        cls, handle: BinaryIO, *, search: str, limit: int, offset: int
    ) -> dict[str, Any]:
        payload = handle.read(MAX_JSON_BYTES + 1)
        if len(payload) > MAX_JSON_BYTES:
            handle.seek(0)
            return cls._read_json_array_stream(
                handle,
                search=search,
                limit=limit,
                offset=offset,
            )
        decoded = None
        for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
            try:
                decoded = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            decoded = payload.decode("utf-8", errors="replace")
        records = cls._json_records(json.loads(decoded))
        columns: list[str] = []
        for record in records:
            for key in record:
                if str(key) not in columns:
                    columns.append(str(key))
        rows, matched = [], 0
        needle = search.casefold()
        for record in records:
            values = [cls._cell(record.get(column)) for column in columns]
            if needle and needle not in " ".join(map(str, values)).casefold():
                continue
            if matched < offset:
                matched += 1
                continue
            if len(rows) >= limit:
                return {"columns": columns, "rows": rows, "has_more": True}
            rows.append(values)
            matched += 1
        return {"columns": columns, "rows": rows, "has_more": False}

    @classmethod
    def _read_json_array_stream(
        cls,
        handle: BinaryIO,
        *,
        search: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        text = cls._text_stream(handle)
        decoder = json.JSONDecoder()
        buffer = ""
        array_started = False
        records: list[dict[str, Any]] = []
        matched = 0
        has_more = False
        needle = search.casefold()

        while True:
            if not array_started:
                chunk = text.read(65536)
                if not chunk:
                    raise RawDataError("大型 JSON 找不到可串流查詢的陣列。")
                buffer += chunk
                marker = buffer.find("[")
                if marker < 0:
                    buffer = buffer[-1024:]
                    continue
                buffer = buffer[marker + 1 :]
                array_started = True

            buffer = buffer.lstrip()
            if buffer.startswith(","):
                buffer = buffer[1:].lstrip()
            if buffer.startswith("]"):
                break
            try:
                value, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError as error:
                chunk = text.read(65536)
                if not chunk:
                    raise RawDataError("大型 JSON 陣列內容不完整。") from error
                buffer += chunk
                continue
            buffer = buffer[end:]
            record = value if isinstance(value, dict) else {"value": value}
            if needle and needle not in " ".join(map(str, record.values())).casefold():
                continue
            if matched < offset:
                matched += 1
                continue
            if len(records) >= limit:
                has_more = True
                break
            records.append(record)
            matched += 1

        result = cls._records_result(records, search="", limit=limit, offset=0)
        result["has_more"] = has_more
        return result

    @staticmethod
    def _cell(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return value

    @classmethod
    def _read_xml(cls, handle: BinaryIO, *, search: str, limit: int, offset: int) -> dict[str, Any]:
        """Stream XML rows, filtering by `search` before the read limit applies.

        先前的順序是「先讀滿 offset + limit + 1 筆就停，再交給 `_records_result` 過濾」，
        於是符合搜尋詞的記錄只要排在那個位置之後就永遠讀不到：三筆資料、第三筆才符合、
        limit=1 時回的是空結果。而且它不報錯 —— 使用者看到的是「查無資料」，
        和真的沒有這筆資料分不出來。

        `_read_csv` 與 `_read_json` 的順序本來就是對的：先過濾、再跳 offset、
        最後才看夠不夠 limit。這裡照同一個順序，也保留 iterparse 的串流讀法。
        """

        needle = search.casefold()
        records: list[dict[str, Any]] = []
        matched = 0
        has_more = False
        for _event, element in ElementTree.iterparse(handle, events=("end",)):
            children = list(element)
            if not (children and all(not list(child) for child in children)):
                continue
            record = {str(key): value for key, value in element.attrib.items()}
            for child in children:
                record[str(child.tag).split("}")[-1]] = (child.text or "").strip()
            element.clear()
            if not any(value not in (None, "") for value in record.values()):
                continue
            if needle and needle not in " ".join(map(str, record.values())).casefold():
                continue
            if matched < offset:
                matched += 1
                continue
            if len(records) >= limit:
                has_more = True
                break
            records.append(record)
            matched += 1
        # 過濾與分頁都已經在上面做完，所以這裡不再傳 search／offset，
        # 與 `_read_json_array_stream` 的收尾方式一致。
        result = cls._records_result(records, search="", limit=limit, offset=0)
        result["has_more"] = has_more
        return result

    @classmethod
    def _records_result(
        cls, records: list[dict[str, Any]], *, search: str, limit: int, offset: int
    ) -> dict[str, Any]:
        columns: list[str] = []
        for record in records:
            for key in record:
                if key not in columns:
                    columns.append(key)
        filtered = [
            record
            for record in records
            if not search or search.casefold() in " ".join(map(str, record.values())).casefold()
        ]
        page = filtered[offset : offset + limit]
        return {
            "columns": columns,
            "rows": [[cls._cell(record.get(column)) for column in columns] for record in page],
            "has_more": len(filtered) > offset + limit,
        }

    def _read_zip(
        self,
        path: Path,
        *,
        member: str | None,
        search: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        with zipfile.ZipFile(path) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if member is None:
                rows = [
                    [
                        item.filename,
                        item.file_size,
                        item.compress_size,
                        Path(item.filename).suffix.lstrip(".").upper(),
                    ]
                    for item in files
                    if not search or search.casefold() in item.filename.casefold()
                ]
                return {
                    "columns": ["member", "bytes", "compressed_bytes", "format"],
                    "rows": rows[offset : offset + limit],
                    "has_more": len(rows) > offset + limit,
                }
            info = next((item for item in files if item.filename == member), None)
            if info is None:
                raise RawDataNotFoundError("ZIP 內找不到指定檔案。")
            member_format = Path(info.filename).suffix.lstrip(".").upper()
            if member_format not in {"CSV", "JSON", "XML"}:
                raise RawDataError("ZIP 內此檔案格式不支援內容查詢。")
            with archive.open(info) as handle:
                if member_format == "CSV":
                    return self._read_csv(handle, search=search, limit=limit, offset=offset)
                if member_format == "JSON":
                    return self._read_json(handle, search=search, limit=limit, offset=offset)
                return self._read_xml(handle, search=search, limit=limit, offset=offset)

    def query(self, question: str) -> dict[str, object]:
        resource_match = re.search(r"(?:\d+-\d{2}|local-[a-f0-9]{12}-\d{2})", question)
        if resource_match:
            result = self.read_rows(resource_match.group(0), limit=50)
            result.update(
                {
                    "question": question,
                    "query_scope": "raw",
                    "intent": "raw_resource_rows",
                    "record_count": len(result["rows"]),
                    "disclosures": ["原始資料尚未經欄位、單位與日期語意標準化。"],
                    "trace": [{"stage": "raw_resource_reader", "status": "ok"}],
                }
            )
            return {"success": True, "data": result}

        direct_result = self._direct_row_lookup(question)
        if direct_result is not None:
            return {"success": True, "data": direct_result}

        resources = self.list_resources(search=question, limit=20)
        columns = ["resource_id", "title", "format", "bytes", "updated_at", "queryable"]
        rows = [[resource[column] for column in columns] for resource in resources]
        return {
            "success": True,
            "data": {
                "question": question,
                "query_scope": "raw",
                "intent": "raw_catalog_search",
                "columns": columns,
                "rows": rows,
                "record_count": len(rows),
                "total_resources": self.status()["total_resources"],
                "disclosures": [
                    "這是原始資源目錄搜尋；輸入結果中的 resource_id 可讀取該檔內容。",
                    "原始資料尚未經欄位、單位與日期語意標準化。",
                ],
                "trace": [{"stage": "raw_catalog", "status": "ok"}],
            },
        }

    def _direct_row_lookup(self, question: str) -> dict[str, object] | None:
        requested_fields = [
            field
            for field, aliases in FIELD_ALIASES.items()
            if any(alias in question for alias in aliases)
        ]
        if not requested_fields:
            return None

        entity = question
        for field in requested_fields:
            for alias in FIELD_ALIASES[field]:
                entity = entity.replace(alias, "")
        for token in QUESTION_NOISE:
            entity = entity.replace(token, "")
        entity = re.sub(r"[\s，。？！、：:]+", "", entity).strip()
        if len(entity) < 2:
            return None

        resources = self.list_resources(limit=500)
        candidates = [
            resource
            for resource in resources
            if any(
                field in str(column) for field in requested_fields for column in resource["columns"]
            )
        ]
        candidates.sort(
            key=lambda resource: self._match_score(question, str(resource["title"])),
            reverse=True,
        )
        for resource in candidates:
            try:
                result = self.read_rows(str(resource["resource_id"]), search=entity, limit=20)
            except RawDataError:
                continue
            if not result["rows"]:
                continue
            return {
                **result,
                "question": question,
                "query_scope": "raw",
                "intent": "raw_resource_lookup",
                "record_count": len(result["rows"]),
                "source_resource": {
                    "resource_id": resource["resource_id"],
                    "title": resource["title"],
                    "updated_at": resource["updated_at"],
                    "source_url": resource["source_url"],
                },
                "explanation": f"從原始資源「{resource['title']}」找到符合資料。",
                "disclosures": ["原始資料尚未經欄位、單位與日期語意標準化。"],
                "trace": [{"stage": "raw_resource_lookup", "status": "ok"}],
            }
        return None
