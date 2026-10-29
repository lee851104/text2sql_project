from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

import serving.data_management as data_management
from serving.data_management import (
    DATA_SLOTS,
    AuditIntegrityError,
    DataManagementConflictError,
    DataManagementNotFoundError,
    DataManagementService,
    DataManagementStateError,
    DataManagementValidationError,
    SeparationOfDutiesError,
)


def _csv_bytes(slot: str, marker: str = "baseline") -> bytes:
    headers = sorted(DATA_SLOTS[slot].required_columns)
    values = {header: marker for header in headers}
    values.update(
        {
            "日期": "20260720",
            "開始日期": "20260720",
            "結束日期": "20260721",
            "商轉日期": "20200101",
            "裝置容量(瓩)": "10000",
            "ratio": "1.0",
            "is_residual": "0",
            "is_bucket": "0",
            "n_plants": "1",
            "n_units": "1",
            "cap_a_wankw": "1.0",
            "obs_max_b": "1.0",
            "尖峰出力_萬瓩": "1.0",
        }
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerow({header: values[header] for header in headers})
    return stream.getvalue().encode("utf-8-sig")


class Harness:
    def __init__(self) -> None:
        self.builds: list[tuple[Path, dict[str, Path]]] = []
        self.switches: list[Path] = []
        self.fail_build = False
        self.fail_switch = False

    def build(self, target: Path, sources: dict[str, Path]) -> dict[str, Any]:
        self.builds.append((target, dict(sources)))
        if self.fail_build:
            raise ValueError("deliberate build failure with unsafe implementation detail")
        identity = {
            slot: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for slot, path in sorted(sources.items())
        }
        target.write_bytes(json.dumps(identity, sort_keys=True).encode("utf-8"))
        return {
            "status": "pass",
            "source_presence": {slot: path.is_file() for slot, path in sources.items()},
        }

    def switch(self, database: Path) -> None:
        if self.fail_switch:
            raise RuntimeError("deliberate runtime failure with unsafe implementation detail")
        self.switches.append(database)


def _service(
    tmp_path: Path, *, allow_self_approval: bool = False
) -> tuple[DataManagementService, Harness, dict[str, Path]]:
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True)
    paths: dict[str, Path] = {}
    for slot, definition in DATA_SLOTS.items():
        path = inputs / definition.filename
        path.write_bytes(_csv_bytes(slot))
        paths[slot] = path
    database = tmp_path / "power.db"
    database.write_bytes(b"initial-database")
    harness = Harness()
    service = DataManagementService(
        workspace=tmp_path / "workspace",
        source_paths=paths,
        initial_database=database,
        build_database=harness.build,
        switch_runtime=harness.switch,
        allow_self_approval=allow_self_approval,
    )
    return service, harness, paths


def _encoded(slot: str, marker: str) -> tuple[str, bytes]:
    payload = _csv_bytes(slot, marker)
    return base64.b64encode(payload).decode("ascii"), payload


def _reopen(
    service: DataManagementService,
    harness: Harness,
    paths: dict[str, Path],
    tmp_path: Path,
) -> DataManagementService:
    return DataManagementService(
        workspace=service.workspace,
        source_paths=paths,
        initial_database=tmp_path / "ignored.db",
        build_database=harness.build,
        switch_runtime=harness.switch,
    )


def test_bootstrap_rebuilds_unverifiable_database_and_reports_real_status(tmp_path: Path) -> None:
    service, harness, original_paths = _service(tmp_path)
    baseline_version = service.active_version
    managed_outage = service.source_file("outage_csv")
    managed_payload = managed_outage.read_bytes()

    original_paths["outage_csv"].write_bytes(_csv_bytes("outage_csv", "changed outside"))
    status = service.status()

    assert baseline_version.startswith("data-")
    assert service.active_database.read_bytes() != b"initial-database"
    assert managed_outage.read_bytes() == managed_payload
    assert status["active_version"] == baseline_version
    assert status["published_versions"] == 1
    assert status["sources"]["outage_csv"]["present"] is True
    assert status["runtime_switch_configured"] is True
    assert status["audit_chain_valid"] is True
    assert status["audit_events"] == 1
    assert len(harness.builds) == 1
    assert harness.switches == []
    assert service.list_audit()[0]["event"] == "workspace_initialized"


def test_upload_remains_pending_until_review_then_switches_runtime(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version
    encoded, payload = _encoded("outage_csv", "new outage")

    change = service.stage_upload(
        "outage_csv",
        encoded,
        actor="uploader",
        filename="replacement.csv",
        reason="new Taipower snapshot",
    )

    assert change["status"] == "pending_review"
    assert change["base_version"] == baseline
    assert change["candidate_version"] != baseline
    assert change["upload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert change["row_count"] == 1
    assert change["request_reason"] == "new Taipower snapshot"
    assert service.active_version == baseline
    assert harness.switches == []
    candidate_database = service.workspace / change["candidate_database"]
    candidate_before = candidate_database.read_bytes()
    assert candidate_database.is_file()

    approved = service.review(
        change["id"],
        approve=True,
        reviewer="reviewer",
        reason="verified source",
    )

    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "reviewer"
    assert service.active_version == change["candidate_version"]
    assert harness.switches == [candidate_database]
    assert service.source_file("outage_csv").read_bytes() == payload
    assert candidate_database.read_bytes() == candidate_before
    assert sum(version["active"] for version in service.list_versions()) == 1
    assert service.status()["changes"] == {
        "total": 1,
        "pending_review": 0,
        "approved": 1,
        "rejected": 0,
        "failed": 0,
    }


def test_optional_removal_and_rollback_both_require_review(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version

    removal = service.stage_remove("outage_csv", actor="operator", note="demonstrate hot unplug")
    assert removal["request_reason"] == "demonstrate hot unplug"
    assert service.list_sources()["files"]["outage_csv"]["present"] is True
    service.review(removal["id"], approve=True, reviewer="reviewer")

    assert service.list_sources()["files"]["outage_csv"]["present"] is False
    with pytest.raises(DataManagementNotFoundError):
        service.source_file("outage_csv")

    rollback = service.stage_rollback(
        baseline, actor="operator", reason="restore reviewed snapshot"
    )
    assert rollback["action"] == "rollback"
    assert rollback["status"] == "pending_review"
    assert rollback["request_reason"] == "restore reviewed snapshot"
    assert service.active_version != baseline

    service.review(rollback["id"], approve=True, reviewer="reviewer")

    assert service.active_version == baseline
    assert service.source_file("outage_csv").is_file()
    assert len(harness.switches) == 2
    assert [event["event"] for event in reversed(service.list_audit())].count(
        "change_approved"
    ) == 2


def test_required_removal_and_malformed_uploads_are_rejected(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)

    with pytest.raises(DataManagementValidationError, match="不可移除"):
        service.stage_remove("daily_csv", actor="operator")
    with pytest.raises(DataManagementValidationError, match="不支援"):
        service.stage_remove("arbitrary_csv", actor="operator")
    with pytest.raises(DataManagementValidationError, match="base64"):
        service.stage_upload("outage_csv", "not base64!", actor="operator")
    with pytest.raises(DataManagementValidationError, match="outage[.]csv"):
        service.stage_upload(
            "outage_csv",
            base64.b64encode(b"only,wrong,headers\n1,2,3\n").decode(),
            actor="operator",
        )
    valid, _payload = _encoded("outage_csv", "valid")
    with pytest.raises(DataManagementValidationError, match="CSV"):
        service.stage_upload(
            "outage_csv",
            valid,
            actor="operator",
            filename="outage.xlsx",
        )


def test_rejection_never_switches_or_changes_active_version(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version
    encoded, _payload = _encoded("outage_csv", "reject me")
    change = service.stage_upload("outage_csv", encoded, actor="operator")

    rejected = service.review(
        change["id"],
        approve=False,
        reviewer="reviewer",
        reason="source not trusted",
    )

    assert rejected["status"] == "rejected"
    assert rejected["review_reason"] == "source not trusted"
    assert service.active_version == baseline
    assert harness.switches == []
    with pytest.raises(DataManagementStateError, match="不可重複"):
        service.review(change["id"], approve=True, reviewer="reviewer")


def test_stale_base_version_cannot_overwrite_a_newer_approval(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    first_encoded, _payload = _encoded("outage_csv", "first")
    second_encoded, _payload = _encoded("outage_csv", "second")
    first = service.stage_upload("outage_csv", first_encoded, actor="one")
    second = service.stage_upload("outage_csv", second_encoded, actor="two")

    service.review(first["id"], approve=True, reviewer="reviewer")
    with pytest.raises(DataManagementConflictError, match="舊版本"):
        service.review(second["id"], approve=True, reviewer="reviewer")

    assert service.active_version == first["candidate_version"]
    assert service.get_change(second["id"])["status"] == "pending_review"
    assert harness.switches == [service.active_database]
    assert service.list_audit()[0]["event"] == "change_conflict"


def test_candidate_database_tampering_is_detected_before_publication(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version
    encoded, _payload = _encoded("outage_csv", "tamper target")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    candidate_database = service.workspace / change["candidate_database"]
    candidate_database.write_bytes(candidate_database.read_bytes() + b"tampered")

    with pytest.raises(DataManagementValidationError, match="篡改"):
        service.review(change["id"], approve=True, reviewer="reviewer")

    assert service.active_version == baseline
    assert harness.switches == []


def test_runtime_switch_failure_keeps_change_pending_and_active_unchanged(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version
    encoded, _payload = _encoded("outage_csv", "runtime failure")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    harness.fail_switch = True

    with pytest.raises(DataManagementStateError, match="無法切換"):
        service.review(change["id"], approve=True, reviewer="reviewer")

    assert service.active_version == baseline
    assert service.get_change(change["id"])["status"] == "pending_review"
    assert service.list_audit()[0]["event"] == "change_publish_failed"


def test_build_failure_is_audited_without_exposing_callback_message(tmp_path: Path) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version
    encoded, _payload = _encoded("outage_csv", "build failure")
    harness.fail_build = True

    with pytest.raises(DataManagementValidationError, match="建置失敗") as error_info:
        service.stage_upload("outage_csv", encoded, actor="operator")

    assert "unsafe implementation detail" not in str(error_info.value)
    assert service.active_version == baseline
    event = service.list_audit()[0]
    assert event["event"] == "change_build_failed"
    assert event["details"]["error_type"] == "DataManagementValidationError"
    failed = service.list_changes(state="failed")
    assert len(failed) == 1
    assert failed[0]["candidate_database"] is None
    assert failed[0]["review_reason"] == "candidate_build_failed"


def test_staged_change_and_audit_pair_recovers_after_audit_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "recover staged")
    original_write = data_management._atomic_bytes_write
    failed = False

    def fail_audit_write(path: Path, payload: bytes) -> None:
        nonlocal failed
        if not failed and path == service.audit_path:
            failed = True
            raise PermissionError(5, "simulated audit crash", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_bytes_write", fail_audit_write)
    with pytest.raises(PermissionError, match="audit crash"):
        service.stage_upload("outage_csv", encoded, actor="operator")

    journal = json.loads(service.mutation_journal_path.read_text(encoding="utf-8"))
    change_id = journal["change_id"]
    assert service._change_path(change_id).is_file()
    monkeypatch.setattr(data_management, "_atomic_bytes_write", original_write)

    reopened = _reopen(service, harness, paths, tmp_path)

    assert reopened.get_change(change_id)["status"] == "pending_review"
    assert [event["event"] for event in reopened.list_audit()].count("change_staged") == 1
    assert not reopened.mutation_journal_path.exists()


def test_staged_change_recovers_when_change_write_crashes_after_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "recover pre-change")
    original_write = data_management._atomic_json_write
    failed = False

    def fail_change_write(path: Path, payload: Any) -> None:
        nonlocal failed
        if not failed and path.parent == service.changes_dir:
            failed = True
            raise PermissionError(5, "simulated change crash", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_json_write", fail_change_write)
    with pytest.raises(PermissionError, match="change crash"):
        service.stage_upload("outage_csv", encoded, actor="operator")

    journal = json.loads(service.mutation_journal_path.read_text(encoding="utf-8"))
    change_id = journal["change_id"]
    assert not service._change_path(change_id).exists()
    monkeypatch.setattr(data_management, "_atomic_json_write", original_write)
    reopened = _reopen(service, harness, paths, tmp_path)

    assert reopened.get_change(change_id)["status"] == "pending_review"
    assert [event["event"] for event in reopened.list_audit()].count("change_staged") == 1
    assert not reopened.mutation_journal_path.exists()


def test_failed_change_and_audit_pair_recovers_after_audit_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "recover failed")
    harness.fail_build = True
    original_write = data_management._atomic_bytes_write
    failed = False

    def fail_audit_write(path: Path, payload: bytes) -> None:
        nonlocal failed
        if not failed and path == service.audit_path:
            failed = True
            raise PermissionError(5, "simulated failed-change crash", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_bytes_write", fail_audit_write)
    with pytest.raises(PermissionError, match="failed-change crash"):
        service.stage_upload("outage_csv", encoded, actor="operator")

    change_id = json.loads(service.mutation_journal_path.read_text(encoding="utf-8"))["change_id"]
    monkeypatch.setattr(data_management, "_atomic_bytes_write", original_write)
    reopened = _reopen(service, harness, paths, tmp_path)

    assert reopened.get_change(change_id)["status"] == "failed"
    assert [event["event"] for event in reopened.list_audit()].count("change_build_failed") == 1
    assert not reopened.mutation_journal_path.exists()


def test_rejected_change_and_audit_pair_recovers_after_audit_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "recover rejection")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    original_write = data_management._atomic_bytes_write
    failed = False

    def fail_audit_write(path: Path, payload: bytes) -> None:
        nonlocal failed
        if not failed and path == service.audit_path:
            failed = True
            raise PermissionError(5, "simulated rejection crash", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_bytes_write", fail_audit_write)
    with pytest.raises(PermissionError, match="rejection crash"):
        service.review(change["id"], approve=False, reviewer="reviewer")

    monkeypatch.setattr(data_management, "_atomic_bytes_write", original_write)
    reopened = _reopen(service, harness, paths, tmp_path)

    assert reopened.get_change(change["id"])["status"] == "rejected"
    assert [event["event"] for event in reopened.list_audit()].count("change_rejected") == 1
    assert not reopened.mutation_journal_path.exists()


def test_all_mutations_fail_closed_when_active_audit_invariant_is_broken(
    tmp_path: Path,
) -> None:
    service, harness, _paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "must not stage")
    pending = service.stage_upload("outage_csv", encoded, actor="operator")
    before_changes = len(service.list_changes())
    before_audits = len(service.list_audit())
    active = json.loads(service.active_path.read_text(encoding="utf-8"))
    active["revision"] += 7
    service.active_path.write_text(json.dumps(active), encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="revision"):
        service.stage_upload(
            "outage_csv",
            _encoded("outage_csv", "blocked stage")[0],
            actor="operator",
        )
    with pytest.raises(AuditIntegrityError, match="revision"):
        service.review(pending["id"], approve=False, reviewer="reviewer")
    with pytest.raises(AuditIntegrityError, match="revision"):
        service.record_audit("runtime_settings_changed", actor="operator")

    assert len(list(service.changes_dir.glob("change-*.json"))) == before_changes
    assert len(service.audit_path.read_text(encoding="utf-8").splitlines()) == before_audits
    assert harness.switches == []


def test_all_public_management_reads_fail_closed_on_active_audit_tamper(
    tmp_path: Path,
) -> None:
    service, _harness, _paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "read target")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    active = json.loads(service.active_path.read_text(encoding="utf-8"))
    active["revision"] += 3
    service.active_path.write_text(json.dumps(active), encoding="utf-8")

    reads = (
        lambda: service.active_version,
        lambda: service.active_database,
        lambda: service.get_change(change["id"]),
        service.list_sources,
        lambda: service.source_file("outage_csv"),
        service.list_versions,
        service.list_changes,
        service.list_audit,
    )
    for read in reads:
        with pytest.raises(AuditIntegrityError, match="revision"):
            read()


def test_publish_commit_rechecks_active_audit_after_runtime_and_record_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, _paths = _service(tmp_path)
    baseline = service.active_version
    encoded, _payload = _encoded("outage_csv", "precommit tamper")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    original_append = service._append_audit_payload
    tampered = False

    def append_then_tamper(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal tampered
        result = original_append(payload)
        if not tampered and payload.get("event") == "change_approved":
            tampered = True
            active = json.loads(service.active_path.read_text(encoding="utf-8"))
            active["revision"] += 10
            data_management._atomic_json_write(service.active_path, active)
        return result

    monkeypatch.setattr(service, "_append_audit_payload", append_then_tamper)
    with pytest.raises(DataManagementStateError, match="journal"):
        service.review(change["id"], approve=True, reviewer="reviewer")

    persisted = json.loads(service.active_path.read_text(encoding="utf-8"))
    assert persisted["version"] == baseline
    assert persisted["version"] != change["candidate_version"]
    assert service.publish_journal_path.is_file()
    assert harness.switches[-1] == service.workspace / persisted["database"]


def test_hash_chain_detects_modified_audit_history(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "audit event")
    service.stage_upload("outage_csv", encoded, actor="operator")
    lines = service.audit_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["actor"] = "tampered"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    service.audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="hash chain"):
        service.list_audit()


def test_audit_anchor_detects_missing_log_and_truncated_tail(tmp_path: Path) -> None:
    missing_service, _harness, _paths = _service(tmp_path / "missing")
    encoded, _payload = _encoded("outage_csv", "anchored missing event")
    missing_service.stage_upload("outage_csv", encoded, actor="operator")
    assert missing_service.audit_head_path.is_file()
    missing_service.audit_path.unlink()

    with pytest.raises(AuditIntegrityError, match="遺失"):
        missing_service.list_audit()

    truncated_service, _harness, _paths = _service(tmp_path / "truncated")
    encoded, _payload = _encoded("outage_csv", "anchored tail event")
    truncated_service.stage_upload("outage_csv", encoded, actor="operator")
    lines = truncated_service.audit_path.read_text(encoding="utf-8").splitlines()
    truncated_service.audit_path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="截短"):
        truncated_service.list_audit()


def test_valid_v1_audit_is_migrated_to_a_durable_head(tmp_path: Path) -> None:
    service, harness, paths = _service(tmp_path)
    service.audit_head_path.unlink()
    service.audit_head_established_path.unlink()

    reopened = _reopen(service, harness, paths, tmp_path)

    assert reopened.audit_head_path.is_file()
    assert reopened.audit_head_established_path.is_file()
    assert reopened.status()["audit_chain_valid"] is True


def test_established_audit_head_loss_is_not_retreated_as_v1_migration(
    tmp_path: Path,
) -> None:
    service, harness, paths = _service(tmp_path)
    assert service.audit_head_established_path.is_file()
    service.audit_head_path.unlink()

    with pytest.raises(AuditIntegrityError, match="錨點遺失"):
        _reopen(service, harness, paths, tmp_path)


def test_orphan_database_without_sidecar_is_recovered_without_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, _paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "orphan candidate")
    original_write = data_management._atomic_json_write
    failed = False

    def fail_first_database_sidecar(path: Path, payload: Any) -> None:
        nonlocal failed
        if (
            not failed
            and path.parent == service.databases_dir
            and path.name.startswith("power-data-")
        ):
            failed = True
            raise PermissionError(5, "simulated OneDrive lock", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_json_write", fail_first_database_sidecar)
    builds_before = len(harness.builds)
    with pytest.raises(DataManagementValidationError, match="建置失敗"):
        service.stage_upload("outage_csv", encoded, actor="operator")

    pending = service.stage_upload("outage_csv", encoded, actor="operator")

    assert pending["status"] == "pending_review"
    assert len(harness.builds) == builds_before + 1
    metadata = service.workspace / "databases" / f"power-{pending['candidate_version']}.json"
    assert json.loads(metadata.read_text(encoding="utf-8"))["report"] == {
        "origin": "recovered_verified_build_journal"
    }


def test_tampered_orphan_database_is_rebuilt_instead_of_freshly_blessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, _paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "tampered orphan")
    original_write = data_management._atomic_json_write
    failed = False

    def fail_database_sidecar(path: Path, payload: Any) -> None:
        nonlocal failed
        if (
            not failed
            and path.parent == service.databases_dir
            and path.name.startswith("power-data-")
        ):
            failed = True
            raise PermissionError(5, "simulated sidecar crash", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_json_write", fail_database_sidecar)
    builds_before = len(harness.builds)
    with pytest.raises(DataManagementValidationError, match="建置失敗"):
        service.stage_upload("outage_csv", encoded, actor="operator")

    orphan = next(service.databases_dir.glob("power-data-*.db"))
    if orphan == service.active_database:
        orphan = next(
            path for path in service.databases_dir.glob("power-data-*.db") if path != orphan
        )
    orphan.write_bytes(orphan.read_bytes() + b"tampered")
    pending = service.stage_upload("outage_csv", encoded, actor="operator")

    assert pending["status"] == "pending_review"
    assert len(harness.builds) == builds_before + 2


def test_publish_journal_recovers_failure_before_active_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, harness, paths = _service(tmp_path)
    baseline = service.active_version
    encoded, _payload = _encoded("outage_csv", "recover publish")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    original_write = data_management._atomic_json_write
    failed = False

    def fail_first_version_record(path: Path, payload: Any) -> None:
        nonlocal failed
        if not failed and path.parent == service.versions_dir and not path.exists():
            failed = True
            raise PermissionError(5, "simulated OneDrive lock", str(path))
        original_write(path, payload)

    monkeypatch.setattr(data_management, "_atomic_json_write", fail_first_version_record)
    with pytest.raises(DataManagementStateError, match="journal"):
        service.review(change["id"], approve=True, reviewer="reviewer")

    persisted = json.loads(service.active_path.read_text(encoding="utf-8"))
    assert persisted["version"] == baseline
    assert service.publish_journal_path.is_file()
    assert harness.switches[-1] == service.workspace / persisted["database"]

    reopened = DataManagementService(
        workspace=service.workspace,
        source_paths=paths,
        initial_database=tmp_path / "ignored.db",
        build_database=harness.build,
        switch_runtime=harness.switch,
    )

    assert reopened.active_version == change["candidate_version"]
    assert reopened.get_change(change["id"])["status"] == "approved"
    assert not reopened.publish_journal_path.exists()
    assert [event["event"] for event in reopened.list_audit()].count("change_approved") == 1


def test_active_snapshot_rejects_rollback_of_pointer_without_audit(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)
    baseline_active = json.loads(service.active_path.read_text(encoding="utf-8"))
    encoded, _payload = _encoded("outage_csv", "new active pointer")
    change = service.stage_upload("outage_csv", encoded, actor="operator")
    service.review(change["id"], approve=True, reviewer="reviewer")

    service.active_path.write_text(
        json.dumps(baseline_active, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(AuditIntegrityError, match="revision"):
        service.active_snapshot()


def test_atomic_publish_retries_transient_sharing_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = data_management.os.replace
    attempts = 0

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError(5, "simulated sharing violation", str(target))
        original_replace(source, target)

    monkeypatch.setattr(data_management.os, "replace", flaky_replace)
    monkeypatch.setattr(data_management.time, "sleep", lambda _seconds: None)

    service, _harness, _paths = _service(tmp_path)

    assert service.active_snapshot()["version"] == service.active_version
    assert attempts >= 3


def test_data_uri_upload_and_reopening_existing_workspace(tmp_path: Path) -> None:
    service, harness, paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "data uri")
    change = service.stage_upload(
        "outage_csv",
        f"data:text/csv;base64,{encoded}",
        actor="operator",
    )
    service.review(change["id"], approve=True, reviewer="reviewer")
    active = service.active_version

    reopened = DataManagementService(
        workspace=service.workspace,
        source_paths=paths,
        initial_database=tmp_path / "ignored.db",
        build_database=harness.build,
        switch_runtime=harness.switch,
    )

    assert reopened.active_version == active
    assert reopened.status()["audit_chain_valid"] is True
    assert len(reopened.list_versions()) == 2


def test_public_audit_hook_redacts_credentials_and_extends_chain(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)

    event = service.record_audit(
        "runtime_settings_changed",
        actor="administrator",
        details={
            "mode": "online",
            "api_key": "must-not-be-written",
            "nested": {"access-token": "also-secret", "model": "example-model"},
        },
    )

    assert event["details"] == {
        "mode": "online",
        "api_key": "[REDACTED]",
        "nested": {"access-token": "[REDACTED]", "model": "example-model"},
    }
    assert event["previous_hash"] != "0" * 64
    assert "must-not-be-written" not in service.audit_path.read_text(encoding="utf-8")
    assert service.list_audit()[0]["event_hash"] == event["event_hash"]


def test_change_reason_rejects_control_characters_or_conflicting_note(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)
    encoded, _payload = _encoded("outage_csv", "reason validation")

    with pytest.raises(DataManagementValidationError, match="reason"):
        service.stage_upload(
            "outage_csv",
            encoded,
            actor="operator",
            reason="line one\nline two",
        )
    with pytest.raises(DataManagementValidationError, match="reason.*note"):
        service.stage_upload(
            "outage_csv",
            encoded,
            actor="operator",
            reason="one reason",
            note="different note",
        )


def _pending_upload(service: DataManagementService, actor: str) -> dict[str, Any]:
    encoded, _payload = _encoded("outage_csv", "four-eyes")
    return service.stage_upload("outage_csv", encoded, actor=actor, reason="four-eyes case")


def test_the_proposer_cannot_approve_their_own_change(tmp_path: Path) -> None:
    """核准是發布邊界；一個人就能跨過去時，稽核鏈證明不了任何分工。"""

    service, _harness, _paths = _service(tmp_path)
    change = _pending_upload(service, "uploader")

    with pytest.raises(SeparationOfDutiesError, match="不可核准自己的變更"):
        service.review(change["id"], approve=True, reviewer="uploader")

    assert service.get_change(change["id"])["status"] == "pending_review"
    assert service.active_version == change["base_version"]


def test_a_refused_self_approval_is_recorded_in_the_audit_chain(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)
    change = _pending_upload(service, "uploader")

    with pytest.raises(SeparationOfDutiesError):
        service.review(change["id"], approve=True, reviewer="uploader")

    events = service.list_audit(limit=200)
    refused = [event for event in events if event["event"] == "self_approval_refused"]
    assert len(refused) == 1
    assert refused[0]["actor"] == "uploader"
    assert refused[0]["details"]["created_by"] == "uploader"


def test_another_account_may_approve_the_same_change(tmp_path: Path) -> None:
    service, _harness, _paths = _service(tmp_path)
    change = _pending_upload(service, "uploader")

    approved = service.review(change["id"], approve=True, reviewer="reviewer")

    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "reviewer"
    assert approved["self_approved"] is False


def test_the_proposer_may_still_withdraw_their_own_change(tmp_path: Path) -> None:
    """駁回不受四眼限制：撤回自己的提案不會讓任何東西上線。"""

    service, _harness, _paths = _service(tmp_path)
    change = _pending_upload(service, "uploader")

    rejected = service.review(change["id"], approve=False, reviewer="uploader", reason="rethinking")

    assert rejected["status"] == "rejected"
    assert rejected["reviewed_by"] == "uploader"


def test_a_single_operator_override_still_marks_the_publication(tmp_path: Path) -> None:
    """覆寫可以讓單人部署運作，但不會讓「沒有第二個人看過」這件事消失。"""

    service, _harness, _paths = _service(tmp_path, allow_self_approval=True)
    change = _pending_upload(service, "solo")

    approved = service.review(change["id"], approve=True, reviewer="solo")

    assert approved["status"] == "approved"
    assert approved["self_approved"] is True
    events = service.list_audit(limit=200)
    published = [event for event in events if event["event"] == "change_approved"]
    assert published[-1]["details"]["self_approved"] is True
