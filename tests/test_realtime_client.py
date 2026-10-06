from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message

from ingest.realtime.client import USER_AGENT, fetch, parse_retry_after


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200):
        super().__init__(body)
        self.status = status
        self.headers = headers


def _headers(values: dict[str, str]) -> Message:
    message = Message()
    for key, value in values.items():
        message[key] = value
    return message


def _fetch(opener, **overrides):
    options = {"etag": None, "last_modified": None, "timeout": 20, "max_bytes": 100}
    options.update(overrides)
    ticks = iter([0.0, 0.25])
    return fetch(
        "https://example.invalid/x.json", opener=opener, monotonic=lambda: next(ticks), **options
    )


def test_success_returns_body_and_validators() -> None:
    seen: list[urllib.request.Request] = []

    def opener(request, timeout):
        seen.append(request)
        return _Response(
            b'{"ok":1}', {"ETag": '"abc:0"', "Last-Modified": "Thu, 24 Sep 2026 00:55:09 GMT"}
        )

    result = _fetch(opener, etag='"old:0"', last_modified="Thu, 24 Sep 2026 00:45:09 GMT")

    assert (result.status, result.body, result.etag) == (200, b'{"ok":1}', '"abc:0"')
    assert result.last_modified == "Thu, 24 Sep 2026 00:55:09 GMT"
    assert result.elapsed_ms == 250
    request = seen[0]
    assert request.get_header("If-none-match") == '"old:0"'
    assert request.get_header("If-modified-since") == "Thu, 24 Sep 2026 00:45:09 GMT"
    assert request.get_header("User-agent") == USER_AGENT


def test_not_modified_is_a_normal_result() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", _headers({}), None)

    result = _fetch(opener, etag='"abc:0"')

    assert (result.status, result.body, result.error_type, result.etag) == (
        304,
        None,
        None,
        '"abc:0"',
    )


def test_server_error_keeps_retry_after() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Busy", _headers({"Retry-After": "120"}), None
        )

    result = _fetch(opener)

    assert (result.status, result.error_type, result.retry_after) == (503, "HTTP503", 120.0)


def test_connection_errors_are_named() -> None:
    def reset(request, timeout):
        raise ConnectionResetError(10054, "reset")

    def timed_out(request, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    assert _fetch(reset).error_type == "ConnectionResetError"
    assert _fetch(timed_out).error_type == "TimeoutError"
    assert _fetch(reset).status is None


def test_oversized_response_is_refused() -> None:
    result = _fetch(lambda request, timeout: _Response(b"x" * 101, {}))

    assert (result.body, result.error_type) == (None, "ResponseTooLarge")


def test_retry_after_accepts_http_dates() -> None:
    now = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)

    assert parse_retry_after("Thu, 24 Sep 2026 00:10:00 GMT", now=now) == 600.0
    assert parse_retry_after("soon", now=now) is None
    assert parse_retry_after(None) is None
