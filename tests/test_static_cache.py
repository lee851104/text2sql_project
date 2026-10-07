"""Browsers must pick up a new frontend right after an upgrade.

靜態檔原本沒有 Cache-Control，瀏覽器依 Last-Modified 自行推估新鮮度，升級後可能好幾天都用舊的
app.js；新頁面配上舊程式，即時發電面板會一直停在「讀取中…」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.realtime_panel import RealtimePanel

STATIC = Path(__file__).resolve().parents[1] / "src" / "serving" / "static"
ASSETS = ("app.js", "app.css", "styles.css")


@pytest.fixture(scope="module")
def client():
    application = create_app(
        auth_manager=AdminAuthManager(
            username="cache-admin", password="cache-test-password", pbkdf2_iterations=1_000
        ),
        realtime_panel=RealtimePanel(None),
    )
    with TestClient(application) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/", *(f"/static/{name}" for name in ASSETS)])
def test_the_page_and_its_assets_are_revalidated_on_every_load(
    client: TestClient, path: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_an_unchanged_asset_costs_only_a_304(client: TestClient) -> None:
    first = client.get("/static/app.js")

    again = client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304


def test_the_page_points_at_versioned_assets_so_stale_caches_are_bypassed(
    client: TestClient,
) -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    for name in ASSETS:
        match = re.search(rf'"/static/{re.escape(name)}\?v=([0-9A-Za-z._-]+)"', html)
        assert match, f"{name} 沒有版本參數"
        assert client.get(f"/static/{name}?v={match.group(1)}").status_code == 200


def test_api_responses_keep_their_own_cache_policy(client: TestClient) -> None:
    response = client.get("/api/realtime/overview")

    assert "no-store" in response.headers["cache-control"]
