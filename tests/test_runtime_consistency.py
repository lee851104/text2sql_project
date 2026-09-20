from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT, resolve_configured_paths
from serving import app as app_module
from serving.admin_auth import AdminAuthManager
from serving.app import create_app
from serving.runtime import RuntimeManager, build_runtime


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("runtime-consistency") / "power.db"
    build_database(database, root=PROJECT_ROOT)
    return database


def _auth() -> AdminAuthManager:
    return AdminAuthManager(
        username="runtime-admin",
        password="runtime-consistency-password",
        pbkdf2_iterations=1_000,
    )


def _application(base_database: Path, directory: Path):
    database = directory / "power.db"
    shutil.copyfile(base_database, database)
    runtime = build_runtime(database=database, root=PROJECT_ROOT, mode="offline")
    return create_app(runtime, auth_manager=_auth())


def test_runtime_manager_pins_database_and_mode_in_one_lock(
    base_database: Path,
    tmp_path: Path,
) -> None:
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(base_database, replacement)
    manager = RuntimeManager(database=base_database, default_mode="offline")

    selected = manager.get_runtime_for_database(replacement, "offline")

    assert selected.database == replacement.resolve()
    assert selected.mode == "offline"
    assert manager.active_runtime is selected


@pytest.mark.e2e
def test_query_keeps_runtime_and_provenance_on_same_snapshot_during_publish(
    base_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(base_database, tmp_path)
    started = Event()
    release = Event()

    with TestClient(application) as client:
        assert client.get("/api/stats").status_code == 200
        managed = application.state.data_manager
        old_snapshot = managed.active_snapshot()
        old_runtime = application.state.runtime_manager.get_runtime_for_database(
            old_snapshot["database"],
            "offline",
        )
        original_query = old_runtime.pipeline.query

        def blocking_query(question: str, *, plant: str | None = None):
            started.set()
            if not release.wait(timeout=15):
                raise AssertionError("timed out waiting for hot-swap publication")
            return original_query(question, plant=plant)

        monkeypatch.setattr(old_runtime.pipeline, "query", blocking_query)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending_response = pool.submit(
                client.post,
                "/api/query",
                json={"question": "2026年三月有哪些機組在歲修？"},
            )
            assert started.wait(timeout=15)

            change = managed.stage_remove(
                "outage_csv",
                actor="runtime-admin",
                reason="concurrency regression",
            )
            approved = managed.review(
                change["id"],
                approve=True,
                reviewer="runtime-reviewer",
                reason="approved during an old query",
            )
            assert approved["status"] == "approved"
            new_snapshot = managed.active_snapshot()
            assert new_snapshot["version"] != old_snapshot["version"]
            release.set()
            response = pending_response.result(timeout=20)

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        provenance = payload["data"]["data_provenance"]
        assert provenance["database_version"] == old_snapshot["version"]
        assert provenance["database_sha256"] == old_snapshot["database_sha256"]
        assert {source["version"] for source in provenance["data_sources"]} == {
            old_snapshot["version"]
        }
        assert (
            next(
                source for source in provenance["data_sources"] if source["dataset"] == "outage_csv"
            )["present"]
            is True
        )

        fresh = client.post(
            "/api/query",
            json={"question": "2026年三月有哪些機組在歲修？"},
        ).json()
        assert fresh["success"] is True
        assert fresh["data"]["record_count"] == 0
        assert fresh["data"]["data_provenance"]["database_version"] == new_snapshot["version"]


@pytest.mark.e2e
def test_second_worker_synchronizes_from_shared_active_pointer(
    base_database: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "power.db"
    shutil.copyfile(base_database, database)
    first = create_app(
        build_runtime(database=database, root=PROJECT_ROOT, mode="offline"),
        auth_manager=_auth(),
    )
    second = create_app(
        build_runtime(database=database, root=PROJECT_ROOT, mode="offline"),
        auth_manager=_auth(),
    )

    with TestClient(first) as first_client, TestClient(second) as second_client:
        assert first_client.get("/api/stats").json()["data"]["outage_records"] == 138
        assert second_client.get("/api/stats").json()["data"]["outage_records"] == 138

        first_managed = first.state.data_manager
        second_managed = second.state.data_manager
        change = first_managed.stage_remove(
            "outage_csv",
            actor="runtime-admin",
            reason="cross-worker synchronization",
        )
        first_managed.review(
            change["id"],
            approve=True,
            reviewer="runtime-reviewer",
            reason="publish for all workers",
        )

        active = second_managed.active_snapshot()
        assert second.state.runtime_manager.active_runtime.database != active["database"]
        response = second_client.get("/api/stats")
        assert response.status_code == 200
        assert response.json()["data"]["outage_records"] == 0
        assert second.state.runtime_manager.active_runtime.database == active["database"]


@pytest.mark.e2e
def test_tampered_database_is_rejected_before_cached_runtime_can_serve(
    base_database: Path,
    tmp_path: Path,
) -> None:
    application = _application(base_database, tmp_path)

    with TestClient(application) as client:
        assert client.get("/api/stats").status_code == 200
        snapshot = application.state.data_manager.active_snapshot()
        with Path(snapshot["database"]).open("ab") as database:
            database.write(b"tampered")

        response = client.get("/api/stats")

    assert response.status_code == 503
    assert response.json()["detail"] == "作用中資料版本未通過完整性驗證。"


@pytest.mark.e2e
def test_rewound_active_pointer_is_rejected_even_when_old_files_are_valid(
    base_database: Path,
    tmp_path: Path,
) -> None:
    application = _application(base_database, tmp_path)

    with TestClient(application) as client:
        assert client.get("/api/stats").status_code == 200
        managed = application.state.data_manager
        original_active = json.loads(managed.active_path.read_text(encoding="utf-8"))
        change = managed.stage_remove(
            "outage_csv",
            actor="runtime-admin",
            reason="create a second valid version",
        )
        managed.review(
            change["id"],
            approve=True,
            reviewer="runtime-reviewer",
            reason="publish before pointer rewind",
        )
        assert client.get("/api/stats").json()["data"]["outage_records"] == 0

        # Both versions and their database/source hashes remain individually
        # valid.  Rewinding only the commit pointer must still be detected via
        # the anchored publication audit state.
        managed.active_path.write_text(
            json.dumps(original_active, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        response = client.get("/api/stats")

    assert response.status_code == 503


@pytest.mark.e2e
def test_restart_rejects_mismatched_version_hash_before_building_runtime(
    base_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _application(base_database, tmp_path)
    with TestClient(first) as client:
        assert client.get("/api/stats").status_code == 200
        snapshot = first.state.data_manager.active_snapshot()
        workspace = Path(snapshot["database"]).parents[1]

    version_path = workspace / "versions" / f"{snapshot['version']}.json"
    version_record = json.loads(version_path.read_text(encoding="utf-8"))
    version_record["database_sha256"] = "0" * 64
    version_path.write_text(
        json.dumps(version_record, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    configured = dict(resolve_configured_paths(PROJECT_ROOT))
    configured["database"] = tmp_path / "power.db"
    monkeypatch.setattr(app_module, "_configured_database", lambda _root: configured["database"])
    monkeypatch.setattr(app_module, "resolve_configured_paths", lambda _root: configured)
    restarted = create_app(auth_manager=_auth())

    with TestClient(restarted) as client:
        response = client.get("/api/stats")

    assert response.status_code == 503
    assert restarted.state.runtime_manager is None


def test_a_snapshot_missing_a_slot_degrades_provenance_instead_of_failing(
    base_database: Path,
    tmp_path: Path,
) -> None:
    """舊快照缺了後來才加的 slot 時，查詢仍要答得出來。

    版本 id 只由來源內容雜湊決定、不含 schema 版本（SPEC「已知待修」與 CP-039），
    所以來源沒變時作用中快照會停在舊版、少掉新加的 slot。實測它讓再生能源類問句
    全部回 HTTP 500 —— 查詢其實成功了，炸掉的只是「資料從哪來」那段補充說明。
    出處組不出來就少列一筆，不該把答得出來的查詢一起拖垮。
    """

    application = _application(base_database, tmp_path)

    with TestClient(application) as client:
        assert client.get("/api/stats").status_code == 200  # data manager 是延遲初始化的
        manager = application.state.data_manager
        available = set(manager.active_snapshot()["sources"])
        assert not available & {"re_sites_csv", "re_generation_csv", "re_sites_supplement_csv"}, (
            "這個測試的前提是快照缺再生能源那組 slot"
        )

        # 一、整組 slot 都不在快照裡 —— 使用者實際踩到的那個 500。
        whole_view = client.post("/api/query", json={"question": "離岸風力的發電量"})

        # 二、只缺其中一個 —— 其餘仍要列出來，不能因為一筆缺就整段放棄。
        snapshot = dict(manager.active_snapshot())
        partial = dict(snapshot["sources"])
        partial.pop("units_csv", None)
        snapshot["sources"] = partial
        manager.active_snapshot = lambda: snapshot  # type: ignore[method-assign]
        one_missing = client.post("/api/query", json={"question": "台中發電廠有哪些機組"})

    assert whole_view.status_code == 200, whole_view.text
    assert whole_view.json()["success"] is True
    assert whole_view.json()["data"]["data_provenance"]["data_sources"] == [], (
        "一個 slot 都對不上時列空的，不要憑空捏造出處"
    )

    assert one_missing.status_code == 200, one_missing.text
    payload = one_missing.json()
    assert payload["success"] is True
    listed = {item["dataset"] for item in payload["data"]["data_provenance"]["data_sources"]}
    assert "units_csv" not in listed, "缺的那一筆不該出現"
