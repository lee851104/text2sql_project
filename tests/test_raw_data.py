from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from serving import raw_data as raw_data_module
from serving.accounts import MINIMUM_ITERATIONS, Account, hash_password
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.raw_data import RawDataError, RawDataService

ORIGIN = "http://testserver"
PASSWORD = "raw-scope-test-password"
ALL_ACCOUNT = "supervisor"
PLANT_ACCOUNT = "linkou"


def _accounts() -> list[Account]:
    password_hash = hash_password(PASSWORD, iterations=MINIMUM_ITERATIONS)
    return [
        Account(username=ALL_ACCOUNT, password_hash=password_hash, plant_id=None),
        Account(username=PLANT_ACCOUNT, password_hash=password_hash, plant_id=15),
    ]


def _app_with_accounts(raw_service: object):
    """原始檔端點要登入，所以這個檔案的 app 一律帶名冊：全廠一個、電廠一個。"""

    accounts = _accounts()
    return create_app(
        raw_data_service=raw_service,
        auth_manager=AdminAuthManager.from_roster(accounts, environ={}),
        accounts=accounts,
    )


def _login(client: TestClient, username: str = ALL_ACCOUNT) -> None:
    response = client.post(
        "/api/admin/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _snapshot(tmp_path: Path) -> Path:
    raw = tmp_path / "data" / "raw"
    resources = [
        (6064, "台灣電力公司公用售電業售電統計資料", "CSV", "name,value\nalpha,1\n"),
        (8931, "台灣電力公司各機組發電量即時資訊", "JSON", '[{"name":"beta","value":2}]'),
        (
            8934,
            "台灣電力公司水火力發電廠位置及機組設備",
            "XML",
            "<rows><row><name>gamma</name><value>3</value></row></rows>",
        ),
    ]
    manifest: list[dict[str, object]] = []
    for dataset_id, title, format_name, content in resources:
        suffix = format_name.lower()
        relative = Path("files") / f"{dataset_id}_{title}" / f"resource-01.{suffix}"
        target = raw / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        manifest.append(
            {
                "DatasetId": dataset_id,
                "Title": title,
                "Format": format_name,
                "ResourceIndex": 1,
                "Url": f"https://example.invalid/{dataset_id}",
                "RelativePath": str(relative),
                "Bytes": target.stat().st_size,
                "Status": "downloaded",
                "Error": "",
                "UpdatedAt": "2026-09-13 12:00:00",
                "License": "政府資料開放授權條款-第1版",
            }
        )

    zip_title = "台灣電力公司壓縮測試資料"
    zip_relative = Path("files") / f"9999_{zip_title}" / "resource-01.zip"
    zip_target = raw / zip_relative
    zip_target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_target, "w") as archive:
        archive.writestr("inside.csv", "name,value\ndelta,4\n")
    manifest.append(
        {
            "DatasetId": 9999,
            "Title": zip_title,
            "Format": "ZIP",
            "ResourceIndex": 1,
            "Url": "https://example.invalid/9999",
            "RelativePath": str(zip_relative),
            "Bytes": zip_target.stat().st_size,
            "Status": "downloaded",
            "Error": "",
            "UpdatedAt": "2026-09-13 12:00:00",
            "License": "政府資料開放授權條款-第1版",
        }
    )
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "manifest.json").write_text(
        json.dumps({"fetched_at": "2026-09-13T12:42:33+08:00", "resources": manifest}),
        encoding="utf-8",
    )
    return tmp_path


def test_catalog_preserves_every_manifest_resource_and_format(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    service = RawDataService(root=root, database=root / "data" / "processed" / "raw.db")

    status = service.rebuild()

    assert status["total_resources"] == 4
    assert status["queryable_resources"] == 4
    assert status["formats"] == {"CSV": 1, "JSON": 1, "XML": 1, "ZIP": 1}
    assert len(service.list_resources(limit=10)) == 4


def test_readers_keep_source_schemas_separate_and_read_zip_members(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    service = RawDataService(root=root, database=root / "raw.db")
    service.rebuild()

    csv_rows = service.read_rows("6064-01")
    json_rows = service.read_rows("8931-01")
    xml_rows = service.read_rows("8934-01")
    zip_members = service.read_rows("9999-01")
    zip_rows = service.read_rows("9999-01", member="inside.csv")

    assert csv_rows["columns"] == ["name", "value"]
    assert csv_rows["rows"] == [["alpha", "1"]]
    assert json_rows["rows"] == [["beta", 2]]
    assert xml_rows["rows"] == [["gamma", "3"]]
    assert zip_members["rows"][0][0] == "inside.csv"
    assert zip_rows["rows"] == [["delta", "4"]]


def test_large_json_array_is_streamed_instead_of_loaded_as_one_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _snapshot(tmp_path)
    target = next((root / "data" / "raw" / "files").rglob("resource-01.json"))
    target.write_text(
        json.dumps(
            {
                "records": {
                    "CATALOG": "UNIT_NET_P",
                    "NET_P": [
                        {"UNIT_NAME": "核三#1", "NET_P": "0.0"},
                        {"UNIT_NAME": "龍潭ES", "NET_P": "1.5"},
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(raw_data_module, "MAX_JSON_BYTES", 32)
    service = RawDataService(root=root, database=root / "raw.db")

    service.rebuild()
    rows = service.read_rows("8931-01")

    assert rows["columns"] == ["UNIT_NAME", "NET_P"]
    assert rows["rows"] == [["核三#1", "0.0"], ["龍潭ES", "1.5"]]


def _service_point_snapshot(tmp_path: Path) -> tuple[Path, RawDataService]:
    root = _snapshot(tmp_path)
    manifest_path = root / "data" / "raw" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resource = manifest["resources"][0]
    resource["DatasetId"] = 6570
    resource["Title"] = "台灣電力公司服務據點相關資訊"
    original = root / "data" / "raw" / Path(resource["RelativePath"])
    replacement = original.with_name("service-points.csv")
    replacement.write_text(
        "服務單位,地址,開放對外電話\n西莒發電廠,連江縣莒光鄉田沃村77號,(0836)88135\n",
        encoding="utf-8",
    )
    resource["RelativePath"] = str(replacement.relative_to(root / "data" / "raw"))
    resource["Bytes"] = replacement.stat().st_size
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    service = RawDataService(root=root, database=root / "raw.db")
    service.rebuild()
    return root, service


def test_raw_question_returns_matching_address_row_with_source(tmp_path: Path) -> None:
    _root, service = _service_point_snapshot(tmp_path)

    response = service.query("西莒發電廠地址")

    assert response["success"] is True
    assert response["data"]["intent"] == "raw_resource_lookup"
    assert response["data"]["columns"] == ["服務單位", "地址", "開放對外電話"]
    assert response["data"]["rows"] == [["西莒發電廠", "連江縣莒光鄉田沃村77號", "(0836)88135"]]
    assert response["data"]["source_resource"]["resource_id"] == "6570-01"


def test_auto_scope_falls_back_to_a_direct_raw_row_match(tmp_path: Path) -> None:
    root, raw_service = _service_point_snapshot(tmp_path)

    class FailedResponse:
        @staticmethod
        def to_dict() -> dict[str, object]:
            return {
                "success": False,
                "error": "可信 schema 沒有地址欄位。",
                "error_code": "GENERATION_FAILED",
                "severity": "error",
            }

    class FailedPipeline:
        @staticmethod
        def query(_question: str, *, plant: str | None = None) -> FailedResponse:
            del plant
            return FailedResponse()

    class Runtime:
        def __init__(self) -> None:
            self.pipeline = FailedPipeline()
            self.database = root / "power.db"
            self.root = root
            self.mode = "offline"
            self.provider = "offline"
            self.model = "offline-router"

    runtime = Runtime()

    class Manager:
        @staticmethod
        def get_runtime(_mode: str | None = None) -> Runtime:
            return runtime

    class Learning:
        database = runtime.database

    application = _app_with_accounts(raw_service)
    application.state.runtime_manager = Manager()
    application.state.learning_service = Learning()
    application.state.learning_pipelines.add(runtime.pipeline)
    with TestClient(application, base_url=ORIGIN) as client:
        anonymous = client.post(
            "/api/query",
            json={"question": "西莒發電廠地址", "query_scope": "auto"},
        )
        _login(client)
        response = client.post(
            "/api/query",
            json={"question": "西莒發電廠地址", "query_scope": "auto"},
        )

    # auto 是「盡量答」，不是「換條路拿原始檔」：沒有權限時安靜地不退回，
    # 拿到的是 trusted 自己的失敗，而不是 401，也不是原始檔的內容。
    assert anonymous.status_code == 200
    assert anonymous.json()["success"] is False
    assert "fallback_from" not in anonymous.json().get("data", {})

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["query_scope"] == "raw"
    assert response.json()["data"]["fallback_from"] == "trusted"
    assert response.json()["data"]["rows"][0][1] == "連江縣莒光鄉田沃村77號"


class _FakeRawDataService:
    def status(self) -> dict[str, object]:
        return {"total_resources": 204, "queryable_resources": 204}

    def list_resources(self, **_kwargs: object) -> list[dict[str, object]]:
        return [{"resource_id": "6064-01", "title": "售電統計", "format": "CSV"}]

    def read_rows(self, resource_id: str, **_kwargs: object) -> dict[str, object]:
        return {"resource_id": resource_id, "columns": ["name"], "rows": [["alpha"]]}

    def query(self, question: str) -> dict[str, object]:
        return {
            "success": True,
            "data": {
                "question": question,
                "query_scope": "raw",
                "columns": ["resource_id"],
                "rows": [["6064-01"]],
                "record_count": 1,
            },
        }

    def rebuild(self) -> dict[str, object]:
        raise AssertionError("anonymous request must not rebuild the catalog")


def test_raw_api_serves_an_all_plant_login_and_query_scope_routes_to_raw_service() -> None:
    application = _app_with_accounts(_FakeRawDataService())
    with TestClient(application, base_url=ORIGIN) as client:
        _login(client)
        assert client.get("/api/raw/status").json()["data"]["total_resources"] == 204
        assert client.get("/api/raw/resources").status_code == 200
        assert client.get("/api/raw/resources/6064-01/rows").status_code == 200

        query = client.post(
            "/api/query",
            json={"question": "查詢售電統計", "query_scope": "raw"},
        )

    assert query.status_code == 200
    assert query.json()["data"]["query_scope"] == "raw"


def test_raw_read_endpoints_refuse_anonymous_and_plant_accounts() -> None:
    """原始檔不經 ScopeGuard，所以讀取端點要和 `/api/query` 的 403 同一個標準。

    先前這三個端點完全沒有權限，`/api/query` 那句「電廠帳號不能查原始開放資料檔」
    因此擋不住任何人 —— 同一個人換個網址就拿到整份來源檔，連登入都不用。
    """

    paths = ("/api/raw/status", "/api/raw/resources", "/api/raw/resources/6064-01/rows")
    application = _app_with_accounts(_FakeRawDataService())
    with TestClient(application, base_url=ORIGIN) as client:
        for path in paths:
            assert client.get(path).status_code == 401, path
        assert (
            client.post(
                "/api/query", json={"question": "查詢售電統計", "query_scope": "raw"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/raw/rebuild",
                headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
            ).status_code
            == 401
        )

        _login(client, PLANT_ACCOUNT)
        for path in paths:
            assert client.get(path).status_code == 403, path
        assert (
            client.post(
                "/api/query", json={"question": "查詢售電統計", "query_scope": "raw"}
            ).status_code
            == 403
        )


def test_failed_raw_query_creates_a_safe_downloadable_diagnostic_id() -> None:
    class FailingRawDataService(_FakeRawDataService):
        def query(self, question: str) -> dict[str, object]:
            del question
            raise RawDataError("原始資料解析失敗。")

    class RecordingLog:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def record_failure(self, **values: object) -> str:
            self.calls.append(values)
            return "raw-diagnostic-id"

    application = _app_with_accounts(FailingRawDataService())
    log = RecordingLog()
    application.state.query_error_log = log
    with TestClient(application, base_url=ORIGIN) as client:
        _login(client)
        response = client.post(
            "/api/query",
            json={"question": "讀取損壞的原始檔", "query_scope": "raw"},
        )

    assert response.json()["diagnostic_id"] == "raw-diagnostic-id"
    assert log.calls[0]["requested_scope"] == "raw"
