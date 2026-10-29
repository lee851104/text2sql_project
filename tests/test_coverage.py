"""The coverage description must stay true to the database it describes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT
from serving.app import create_app
from serving.coverage import (
    CoverageConfigurationError,
    Limitation,
    describe_coverage,
    load_coverage_document,
    verify_limitations,
)
from serving.runtime import build_runtime
from text2sql.sql_guard import ALLOWED_COLUMNS

COVERAGE_PATH = PROJECT_ROOT / "configs" / "coverage.yaml"


@pytest.fixture(scope="module")
def document():
    return load_coverage_document(COVERAGE_PATH)


def test_every_declared_limitation_still_holds(document) -> None:
    """限制說明必須可驗證，而且現在就要驗。

    「答不出什麼」無法從現有資料推導，所以只能宣告；但宣告會過期，而過期的限制說明
    比沒有更糟 —— 它讓使用者相信一件不再為真的事。資料長出新欄位時，這裡要先紅。
    """

    _views, limitations = document

    stale = verify_limitations(limitations, ALLOWED_COLUMNS)

    assert stale == (), "限制說明已經不成立，請更新 configs/coverage.yaml：" + "；".join(stale)


def test_a_limitation_that_became_answerable_is_reported() -> None:
    limitation = Limitation(topic="碳排放", detail="沒有碳排放資料。", absent={"v_unit": ("燃料",)})

    stale = verify_limitations([limitation], ALLOWED_COLUMNS)

    assert len(stale) == 1
    assert "已不成立" in stale[0]


def test_a_limitation_pointing_at_a_missing_view_is_reported() -> None:
    """條件無法驗證，等於沒有條件。"""

    limitation = Limitation(topic="x", detail="y", absent={"v_gone": ("任何",)})

    stale = verify_limitations([limitation], ALLOWED_COLUMNS)

    assert len(stale) == 1
    assert "不存在的檢視" in stale[0]


def test_a_limitation_without_a_condition_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "coverage.yaml"
    path.write_text(
        "views:\n  v_unit:\n    title: t\n    answers: a\n"
        "limitations:\n  - topic: 無法驗證\n    detail: 說明\n",
        encoding="utf-8",
    )

    with pytest.raises(CoverageConfigurationError, match="沒有 absent 條件"):
        load_coverage_document(path)


def test_every_described_view_is_one_the_guard_allows(document) -> None:
    views, _limitations = document

    assert set(views) == set(ALLOWED_COLUMNS), "檢視說明與可查詢的檢視必須一一對應"


@pytest.mark.integration
def test_the_numbers_come_from_the_database_not_the_document(tmp_path: Path, document) -> None:
    views, limitations = document
    database = tmp_path / "power.db"
    build_database(database, root=PROJECT_ROOT)

    payload = describe_coverage(database, views=views, limitations=limitations)

    assert payload["date_range"]["start"] and payload["date_range"]["end"]
    assert payload["counts"]["units"] > 0
    assert payload["counts"]["peak_rows"] > 0
    # 電廠主檔比「有機組明細的電廠」多，這個落差對使用者是有意義的。
    assert payload["counts"]["plants_in_roster"] >= payload["counts"]["plants_with_units"]
    assert payload["fuels"], "燃料別必須從資料查出來"
    assert {item["view"] for item in payload["answerable"]} <= set(views)
    assert payload["pitfalls"], "已知陷阱應該一併揭露"


@pytest.mark.integration
def test_the_coverage_endpoint_is_open_to_everyone(tmp_path: Path) -> None:
    """涵蓋範圍是給使用者看的，不該需要登入才知道能查什麼。"""

    database = tmp_path / "power.db"
    build_database(database, root=PROJECT_ROOT)
    application = create_app(build_runtime(database=database, root=PROJECT_ROOT, mode="offline"))

    with TestClient(application, base_url="http://testserver") as client:
        response = client.get("/api/coverage")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["limitations"], "答不出什麼要一起講"
    assert data["answerable"], "答得出什麼也要講"
