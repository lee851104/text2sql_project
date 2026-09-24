"""Conditional GET for the d006001 endpoint (§4.4).

程式內部不自己重試：每一次重試都經過排程，所以每一次嘗試都會留下紀錄。
"""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import certifi

USER_AGENT = "PowerQuery-TW/0.3 (realtime collector)"

Opener = Callable[[urllib.request.Request, float], Any]


@dataclass(frozen=True)
class FetchResult:
    status: int | None  # 連線層失敗時為 None
    body: bytes | None
    etag: str | None
    last_modified: str | None
    retry_after: float | None
    error_type: str | None
    elapsed_ms: int


def default_opener(request: urllib.request.Request, timeout: float) -> Any:
    # Python 3.13 起系統憑證庫拒絕台電端點的憑證鏈；沿用 fetch.py 改用 certifi，不關閉驗證。
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(request, timeout=timeout, context=context)  # noqa: S310


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


def fetch(
    url: str,
    *,
    etag: str | None,
    last_modified: str | None,
    timeout: float,
    max_bytes: int,
    opener: Opener = default_opener,
    monotonic: Callable[[], float] = time.monotonic,
) -> FetchResult:
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    started = monotonic()

    def elapsed() -> int:
        return int((monotonic() - started) * 1000)

    try:
        with opener(request, timeout) as response:
            body = response.read(max_bytes + 1)
            status = int(getattr(response, "status", 200))
            response_headers = response.headers
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return FetchResult(304, None, etag, last_modified, None, None, elapsed())
        retry_after = parse_retry_after(error.headers.get("Retry-After") if error.headers else None)
        return FetchResult(
            error.code, None, None, None, retry_after, f"HTTP{error.code}", elapsed()
        )
    except urllib.error.URLError as error:
        reason = error.reason
        name = type(reason).__name__ if isinstance(reason, BaseException) else "URLError"
        return FetchResult(None, None, None, None, None, name, elapsed())
    except (OSError, http.client.HTTPException) as error:
        return FetchResult(None, None, None, None, None, type(error).__name__, elapsed())
    if len(body) > max_bytes:
        return FetchResult(status, None, None, None, None, "ResponseTooLarge", elapsed())
    if status != 200 or not body:
        error_type = "EmptyBody" if status == 200 else f"HTTP{status}"
        return FetchResult(status, None, None, None, None, error_type, elapsed())
    return FetchResult(
        200,
        body,
        response_headers.get("ETag"),
        response_headers.get("Last-Modified"),
        parse_retry_after(response_headers.get("Retry-After")),
        None,
        elapsed(),
    )
