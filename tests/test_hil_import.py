"""Import public HIL archives as scoped, inconclusive review evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.hil.api import HAAPIError
from scripts.hil.evidence import export_archive
from scripts.hil.importer import import_archive
from scripts.hil.qualification import migrate_v1_to_v2
from scripts.hil.storage import write_run, write_scenario_progress
from scripts.hil.trace import _save_private_trace


def _fact(value: Any, source: str, verified: bool = True) -> dict[str, Any]:
    return {"value": value, "source": source, "verified": verified}


def _public_archive(path: Path) -> Path:
    run = path / "run"
    run.mkdir(parents=True)
    environment = {
        "schema_version": 1,
        "home_assistant": {},
        "runner_host": {},
        "integration": {
            "installed_manifest_version": _fact("1.2.3", "ha_ws_manifest_get"),
            "build_match": _fact("mismatch", "runner_repository_manifest"),
            "runner_checkout_commit": _fact(None, "unknown", False),
        },
    }
    session = {
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "qualification_status": "inconclusive",
        "mode": "read_only_preflight",
        "scenario_id": "read_only",
        "started_at": "2026-11-09T12:00:00+00:00",
        "device": {
            "model": _fact("R600", "ha_device_registry"),
            "hardware_version": _fact("0.3", "ha_device_registry"),
            "firmware_version": _fact("1.1", "ha_device_registry"),
        },
    }
    (run / "environment.json").write_text(json.dumps(environment), encoding="utf-8")
    (run / "session.json").write_text(json.dumps(session), encoding="utf-8")
    return export_archive(run, confirmed=True)


def _matrix(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    matrix = migrate_v1_to_v2(
        {
            "schema_version": 1,
            "generated_at": "2026-11-09",
            "retention": "Sanitized public evidence only.",
            "verified_revisions": [
                {
                    "id": "r600-hw03",
                    "model": "ALLPOWERS R600",
                    "hardware_version": "0.3",
                    "raw_hardware_version": "0x03",
                    "privacy_review": "pass",
                    "routes": {},
                    "scenarios": {},
                    "write_capabilities": {},
                }
            ],
        }
    )
    result = path / "qualification_matrix.json"
    result.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    return result


def _captured_archive(
    path: Path, route: str | list[str], firmware: str, run_id: str
) -> Path:
    environment = {
        "schema_version": 1,
        "home_assistant": {},
        "runner_host": {},
        "integration": {
            "installed_manifest_version": _fact("1.2.3", "ha_ws_manifest_get"),
            "build_match": _fact("mismatch", "runner_repository_manifest"),
            "runner_checkout_commit": _fact(None, "unknown", False),
        },
    }
    session = {
        "schema_version": 1,
        "run_id": run_id,
        "qualification_status": "inconclusive",
        "mode": "read_only_preflight",
        "scenario_id": "read_only_snapshot",
        "started_at": "2026-11-09T12:00:00+00:00",
        "device": {
            "model": _fact("R600", "ha_device_registry"),
            "hardware_version": _fact("0.3", "ha_device_registry"),
            "firmware_version": _fact(firmware, "ha_device_registry"),
        },
    }
    private = write_run(path, f"run-{run_id}", environment, session)
    write_scenario_progress(
        private,
        run_id,
        "read_only_snapshot",
        "a" * 64,
        [{"step_id": "route_evidence", "outcome": "pass"}],
    )
    stamp = "2026-11-09T12:00:00+00:00"
    common = {
        "schema_version": 2,
        "run_id": run_id,
        "scenario_id": "read_only_snapshot",
        "layer": "gatt_api",
        "session_generation": 1,
        "observed_route": route,
    }
    routes = [route] if isinstance(route, str) else route
    for index, observed_route in enumerate(routes, 1):
        route_proof = (
            {
                "backend_kind": "bluez",
                "evidence": "bluez_device_path",
                "adapter_id": "hci0",
            }
            if observed_route == "local_adapter"
            else {
                "backend_kind": "esphome",
                "evidence": "esphome_scanner_binding",
                "proxy_id": f"proxy-opaque-{index}",
            }
        )
        route_event = {
            **common,
            "seq": 1,
            "utc": stamp,
            "elapsed_ms": 0.0,
            "event": "route_observed",
            "observed_route": observed_route,
            **route_proof,
        }
        notification = {
            **common,
            "seq": 2,
            "utc": stamp,
            "elapsed_ms": 1.0,
            "event": "notification_received",
            "observed_route": observed_route,
        }
        frame = {
            **notification,
            "direction": "rx",
            "command_type": "unknown",
            "frame_validation": "unvalidated_chunk",
            "hex": "aa55",
        }
        trace = {
            "schema_version": 2,
            "capture_scope": "runtime_entry",
            "capture_id": f"capture-{run_id}-{index}",
            "run_id": run_id,
            "scenario_id": "read_only_snapshot",
            "requested_route": "auto" if len(routes) > 1 else observed_route,
            "capture_layer": "gatt_api_not_rf",
            "private_raw_bytes": True,
            "has_gatt_frames": True,
            "closed": True,
            "expired": False,
            "termination_reason": "requested_stop",
            "qualification_allowed": False,
            "observed_route": observed_route,
            "complete": True,
            "dropped_events": 0,
            "dropped_frames": 0,
            "event_count": 2,
            "frame_count": 1,
            "events": [route_event, notification],
            "frames": [frame],
            "route_observations": [
                {
                    "session_generation": 1,
                    "seq": 1,
                    "utc": stamp,
                    "elapsed_ms": 0.0,
                    "observed_route": observed_route,
                    **route_proof,
                }
            ],
        }
        trace_dir = private / "traces" / f"read_only_snapshot_{index}"
        trace_dir.mkdir(parents=True)
        _save_private_trace(trace_dir, trace)
    return export_archive(path / f"run-{run_id}", confirmed=True)


def test_import_defaults_to_preview_and_apply_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import validate_hil_qualification

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)

    assert import_archive(archive, matrix_path=matrix_path, repo_root=tmp_path) == 0
    preview = capsys.readouterr().out
    assert "no PASS results will be created" in preview
    assert "Observed route: unverified" in preview
    assert len(json.loads(matrix_path.read_text())["revisions"][0]["runs"]) == 0
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()

    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    revision = payload["revisions"][0]
    assert revision["id"] == "r600-hw03"
    imported = revision["runs"][0]
    assert imported["qualification_status"] == "inconclusive"
    assert imported["firmware_version"] == "1.1"
    assert imported["route"] == "unverified"
    assert imported["capture_completeness"] == "unknown"
    assert imported["build_verification"] == "mismatch"
    assert all(
        result["status"] == "inconclusive" for result in imported["scenarios"].values()
    )
    assert all(
        result["status"] == "inconclusive"
        for result in imported["write_capabilities"].values()
    )
    evidence = [tmp_path / item for item in imported["evidence"]]
    assert evidence and all(item.is_file() for item in evidence)

    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    assert len(json.loads(matrix_path.read_text())["revisions"][0]["runs"]) == 1
    matrix_before_conflict = matrix_path.read_bytes()
    evidence[0].write_text("conflicting contents", encoding="utf-8")
    with pytest.raises(HAAPIError, match="Existing import evidence conflicts"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert evidence[0].read_text(encoding="utf-8") == "conflicting contents"
    assert matrix_path.read_bytes() == matrix_before_conflict


def test_conflicting_run_identity_fails_before_writing_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import importer

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    proposal = importer._build_proposal(archive, matrix_path, repo_root=tmp_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    conflicting_run = dict(proposal["run"])
    conflicting_run["evidence"] = []
    conflicting_run["reported_scenario_id"] = "different-run-content"
    matrix["revisions"][0]["runs"] = [conflicting_run]
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    matrix_before = matrix_path.read_bytes()

    with pytest.raises(HAAPIError, match="Archive identity conflicts"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert matrix_path.read_bytes() == matrix_before
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()


def test_fixture_collision_does_not_overwrite_or_leave_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import importer

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    proposal = importer._build_proposal(archive, matrix_path, repo_root=tmp_path)
    destinations = list(proposal["destination_files"].items())
    first_path = destinations[0][0]
    collision_path = destinations[1][0]
    collision_path.parent.mkdir(parents=True)
    collision_path.write_bytes(b"pre-existing unrelated evidence")
    matrix_before = matrix_path.read_bytes()

    with pytest.raises(HAAPIError, match="Evidence file collision"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert collision_path.read_bytes() == b"pre-existing unrelated evidence"
    assert not first_path.exists()
    assert matrix_path.read_bytes() == matrix_before


def test_failed_matrix_validation_rolls_back_imported_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hil import importer
    from scripts import validate_hil_qualification

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    before = matrix_path.read_bytes()
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    validate_original = importer.validate_matrix
    validation_calls = 0

    def fail_after_file_writes(
        *args: Any, **kwargs: Any
    ) -> tuple[list[str], list[str]]:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            return validate_original(*args, **kwargs)
        return ["forced validation failure"], []

    monkeypatch.setattr(importer, "validate_matrix", fail_after_file_writes)

    with pytest.raises(HAAPIError, match="Proposed matrix is invalid"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert matrix_path.read_bytes() == before
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()


def test_exported_captures_import_with_route_firmware_and_steps_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification

    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    archives = [
        _captured_archive(tmp_path, "local_adapter", "1.1", "0123456789abcdef"),
        _captured_archive(tmp_path, "active_proxy", "1.2", "fedcba9876543210"),
        _captured_archive(
            tmp_path,
            ["local_adapter", "active_proxy"],
            "1.3",
            "0011223344556677",
        ),
    ]

    for archive in archives:
        assert (
            import_archive(
                archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path
            )
            == 0
        )

    revision = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0]
    imported = revision["runs"]
    assert len(imported) == 3
    assert {(run["route"], run["firmware_version"]) for run in imported} == {
        ("local_adapter", "1.1"),
        ("active_proxy", "1.2"),
        ("mixed", "1.3"),
    }
    assert len({run["run_id"] for run in imported}) == 3
    for run in imported:
        assert run["qualification_status"] == "inconclusive"
        assert run["capture_completeness"] == "complete"
        assert run["reported_scenario_id"] == "read_only_snapshot"
        assert run["reported_step_ids"] == ["route_evidence"]
        assert {Path(path).name for path in run["evidence"]} >= {
            "events.jsonl",
            "frames.jsonl",
            "manifest.json",
            "scenarios.json",
        }
        assert all((tmp_path / evidence).is_file() for evidence in run["evidence"])
