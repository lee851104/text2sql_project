"""Persistent, bounded diagnostics for failed query requests."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


class QueryErrorLog:
    def __init__(self, path: Path, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 3):
        self.path = Path(path).resolve()
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = RLock()

    def _rotate(self, incoming_bytes: int) -> None:
        if not self.path.is_file() or self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        self.path.with_name(f"{self.path.name}.{self.backups}").unlink(missing_ok=True)
        for number in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{number}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{number + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def record_failure(
        self,
        *,
        question: str,
        requested_mode: str | None,
        runtime: Mapping[str, Any],
        response: Mapping[str, Any],
        requested_scope: str = "trusted",
    ) -> str:
        diagnostic_id = uuid4().hex
        data = response.get("data")
        trace = data.get("trace", []) if isinstance(data, Mapping) else []
        event = {
            "schema_version": "powerquery-query-error-v1",
            "timestamp": datetime.now(UTC).isoformat(),
            "diagnostic_id": diagnostic_id,
            "question": question,
            "requested_mode": requested_mode,
            "requested_scope": requested_scope,
            "runtime": dict(runtime),
            "error_code": response.get("error_code"),
            "error": response.get("error"),
            "severity": response.get("severity"),
            "evidence": response.get("evidence", {}),
            "trace": trace,
        }
        encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate(len(encoded))
            with self.path.open("ab") as handle:
                handle.write(encoded)
        return diagnostic_id
