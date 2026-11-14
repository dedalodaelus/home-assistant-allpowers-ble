"""Import public HIL archives as scoped, inconclusive review evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
import zipfile

import pytest

from scripts.hil.api import HAAPIError
from scripts.hil.evidence import (
    build_public,
    export_archive,
    verify_public_archive,
)
from scripts.hil.importer import import_archive
from scripts.hil.qualification import migrate_v1_to_v3, migrate_v2_to_v3
from scripts.hil.storage import write_run, write_scenario_progress
from scripts.hil.trace import _save_private_trace


def _fact(value: Any, source: str, verified: bool = True) -> dict[str, Any]:
    return {"value": value, "source": source, "verified": verified}


def _public_archive(
    path: Path,
    *,
    source_run_id: str | None = "0123456789abcdef",
    installed_version: str = "1.2.3",
    firmware_version: str = "1.1",
    verified_build: bool = False,
    guided_tier: bool = False,
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = path / "run"
    run.mkdir(parents=True)
    integration_facts = {
        "installed_manifest_version": _fact(installed_version, "ha_ws_manifest_get"),
        "build_match": _fact(
            "verified" if verified_build else "mismatch",
            "runner_build_verification",
        ),
        "runner_checkout_commit": (
            _fact("a" * 40, "runner_git_checkout")
            if verified_build
            else _fact(None, "unknown", False)
        ),
    }
    if verified_build:
        integration_facts.update(
            installed_commit=_fact("a" * 40, "runner_git_checkout"),
            installed_fingerprint=_fact("b" * 64, "ha_ws_hil_build"),
            checkout_fingerprint=_fact("b" * 64, "runner_integration_files"),
        )
    environment = {
        "schema_version": 1,
        "home_assistant": {},
        "runner_host": {},
        "integration": integration_facts,
    }
    session = {
        "schema_version": 1,
        "qualification_status": "inconclusive",
        "mode": "read_only_preflight",
        "scenario_id": "read_only",
        "started_at": "2026-11-09T12:00:00+00:00",
        "device": {
            "model": _fact("R600", "ha_device_registry"),
            "hardware_version": _fact("0.3", "ha_device_registry"),
            "firmware_version": _fact(firmware_version, "ha_device_registry"),
        },
    }
    if source_run_id is not None:
        session["run_id"] = source_run_id
    (run / "environment.json").write_text(json.dumps(environment), encoding="utf-8")
    (run / "session.json").write_text(json.dumps(session), encoding="utf-8")
    if guided_tier:
        artifact = run / "tier-dc_output_cycle.json"
        artifact.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "step_id": "dc_output_cycle",
                    "outcome": "pass",
                    "initial_state": "off",
                    "target_state": "on",
                    "physical_change_outcome": "correct",
                    "restored_state": "off",
                    "physical_restore_outcome": "correct",
                    "actions": [],
                }
            ),
            encoding="utf-8",
        )
        (run / "tier-execution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": source_run_id,
                    "requested_tier": "silver",
                    "attained_tier": None,
                    "review_state": "pending",
                    "steps": [
                        {
                            "step_id": "dc_output_cycle",
                            "outcome": "pass",
                            "human_review": "pending",
                            "machine_evidence": [
                                {
                                    "path": artifact.name,
                                    "sha256": hashlib.sha256(
                                        artifact.read_bytes()
                                    ).hexdigest(),
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    return export_archive(run, confirmed=True)


def _repack_archive(
    source: Path, destination: Path, timestamp: tuple[int, ...]
) -> Path:
    with (
        zipfile.ZipFile(source) as old_archive,
        zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED
        ) as new_archive,
    ):
        for name in old_archive.namelist():
            original = old_archive.getinfo(name)
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = original.external_attr
            new_archive.writestr(info, old_archive.read(name))
    return destination


def _rewrite_capture_archive(
    source: Path,
    destination: Path,
    *,
    capture_updates: dict[str, dict[str, Any]],
    member_updates: dict[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members.update(
        {f"public/{name}": value for name, value in (member_updates or {}).items()}
    )
    manifest = json.loads(members["public/manifest.json"])
    captures = manifest["captures"]
    for capture in captures["by_capture"]:
        capture.update(capture_updates.get(capture["capture_id"], {}))
    captures["dropped_events"] = sum(
        capture["dropped_events"] for capture in captures["by_capture"]
    )
    captures["dropped_frames"] = sum(
        capture["dropped_frames"] for capture in captures["by_capture"]
    )
    manifest["files"] = [
        {
            "name": name.removeprefix("public/"),
            "size": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
        }
        for name, contents in sorted(members.items())
        if name != "public/manifest.json"
    ]
    members["public/manifest.json"] = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    return destination


def _matrix(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    matrix = migrate_v1_to_v3(
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


def test_v2_migration_preserves_runs_as_unqualified_history() -> None:
    source_run = {"run_id": "old", "qualification_status": "pass"}
    migrated = migrate_v2_to_v3(
        {
            "schema_version": 2,
            "generated_at": "2026-11-09",
            "revisions": [{"id": "r", "runs": [source_run]}],
        }
    )

    assert migrated["schema_version"] == 3
    assert migrated["revisions"][0]["legacy_v2_runs"] == [source_run]
    assert migrated["revisions"][0]["runs"] == []


def _captured_archive(
    path: Path,
    route: str | list[str],
    firmware: str,
    run_id: str,
    *,
    bronze_reviewable: bool = False,
    bronze_decoded_packet_types: tuple[str, ...] = ("StatusData",),
    bronze_protocol_outcome: str = "pass",
    bronze_write_completion: str = "write_ok",
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
    if bronze_reviewable:
        environment["integration"].update(
            build_match=_fact("verified", "runner_build_verification"),
            installed_commit=_fact("a" * 40, "runner_git_checkout"),
            installed_fingerprint=_fact("b" * 64, "ha_ws_hil_build"),
            checkout_fingerprint=_fact("b" * 64, "runner_integration_files"),
        )
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
    if bronze_reviewable:
        session["runtime_capabilities"] = {
            "verified": True,
            "connected": True,
            "status_fresh": True,
            "pending_transaction": False,
            "capabilities": {"read_telemetry": True},
        }
    private = write_run(path, f"run-{run_id}", environment, session)
    write_scenario_progress(
        private,
        run_id,
        "read_only_snapshot",
        "a" * 64,
        [{"step_id": "route_evidence", "outcome": "pass"}],
    )
    stamp = (
        datetime.now(UTC).isoformat()
        if bronze_reviewable
        else "2026-11-09T12:00:00+00:00"
    )
    generation = 7 if bronze_reviewable else 1
    common = {
        "schema_version": 2,
        "run_id": run_id,
        "scenario_id": "read_only_snapshot",
        "layer": "gatt_api",
        "session_generation": generation,
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
                    "session_generation": generation,
                    "seq": 1,
                    "utc": stamp,
                    "elapsed_ms": 0.0,
                    "observed_route": observed_route,
                    **route_proof,
                }
            ],
        }
        if bronze_reviewable:
            status_frame = {
                **common,
                "seq": 2,
                "utc": stamp,
                "elapsed_ms": 1.0,
                "event": "write_attempt",
                "direction": "tx",
                "command_type": "status_request",
                "frame_validation": "validated_tx",
                "hex": "a565b1000106010000000000",
                "transaction_id": "bronze-tx-1",
            }
            notification_event = {
                **common,
                "seq": 4 if bronze_write_completion != "missing" else 3,
                "utc": stamp,
                "elapsed_ms": 3.0,
                "event": "notification_received",
            }
            write_completion_event = {
                **common,
                "seq": 3,
                "utc": stamp,
                "elapsed_ms": 2.0,
                "event": bronze_write_completion,
                "transaction_id": "bronze-tx-1",
            }
            decoded_event = {
                **common,
                "seq": 5 if bronze_write_completion != "missing" else 4,
                "utc": stamp,
                "elapsed_ms": 4.0,
                "event": "notification_decoded",
                "decoded_packet_types": list(bronze_decoded_packet_types),
            }
            trace["events"] = [
                route_event,
                {**status_frame, "direction": "write"},
                *(
                    [write_completion_event]
                    if bronze_write_completion != "missing"
                    else []
                ),
                notification_event,
                decoded_event,
            ]
            trace["frames"] = [
                status_frame,
                {
                    **notification_event,
                    "direction": "rx",
                    "command_type": "unknown",
                    "frame_validation": "unvalidated_chunk",
                    "hex": "aa55",
                },
            ]
            trace["event_count"] = len(trace["events"])
            trace["frame_count"] = 2
        trace_dir = private / "traces" / f"read_only_snapshot_{index}"
        trace_dir.mkdir(parents=True)
        _save_private_trace(trace_dir, trace)
        if bronze_reviewable:
            bronze_trace = private / "traces" / "bronze_read_only"
            bronze_trace.mkdir(parents=True)
            _save_private_trace(bronze_trace, trace)
            sample_time = datetime.now(UTC).isoformat()
            sample = {
                "sampled_at_utc": sample_time,
                "telemetry": {
                    "battery": {"value": 50, "verified": True, "sample_age_seconds": 5},
                    "telemetry_available": {"value": "on", "verified": True},
                },
            }
            (private / "tier-bronze-read-only.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "step_id": "read_only_probe",
                        "sample_sha256": [
                            hashlib.sha256(
                                json.dumps(sample, sort_keys=True).encode()
                            ).hexdigest()
                        ]
                        * 2,
                        "samples": [sample, sample],
                        "fresh_verified_samples": True,
                        "protocol_probe": {
                            "outcome": bronze_protocol_outcome,
                            "trace": "traces/bronze_read_only",
                            "trace_sha256": hashlib.sha256(
                                (bronze_trace / "trace.json").read_bytes()
                            ).hexdigest(),
                            "route": observed_route,
                            "session_generation": generation,
                            "status_request_seq": 2,
                            "status_write_ok_seq": 3,
                            "decoded_response_seq": decoded_event["seq"],
                            "status_request_hex": "a565b1000106010000000000",
                        },
                        "physical_observation": "plausible",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
    return export_archive(path / f"run-{run_id}", confirmed=True)


@pytest.mark.parametrize("packet_types", [("SettingsData",), ()])
def test_private_export_rejects_bronze_pass_without_decoded_status(
    tmp_path: Path, packet_types: tuple[str, ...]
) -> None:
    with pytest.raises(HAAPIError, match="Bronze protocol evidence binding failed"):
        _captured_archive(
            tmp_path / "source",
            "local_adapter",
            "1.1",
            "0123456789abcdef",
            bronze_reviewable=True,
            bronze_decoded_packet_types=packet_types,
            bronze_protocol_outcome="pass",
        )


@pytest.mark.parametrize("write_completion", ["missing", "write_error"])
def test_private_export_rejects_bronze_pass_without_successful_status_write(
    tmp_path: Path, write_completion: str
) -> None:
    with pytest.raises(HAAPIError, match="Bronze protocol evidence binding failed"):
        _captured_archive(
            tmp_path / "source",
            "local_adapter",
            "1.1",
            "0123456789abcdef",
            bronze_reviewable=True,
            bronze_write_completion=write_completion,
        )


def test_public_verifier_rejects_bronze_response_without_status_type(
    tmp_path: Path,
) -> None:
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
    )
    with zipfile.ZipFile(archive) as source:
        event_rows = [
            json.loads(line) for line in source.read("public/events.jsonl").splitlines()
        ]
    decoded = next(row for row in event_rows if row["event"] == "status_data_decoded")
    decoded["event"] = "notification_decoded"
    rewritten = _rewrite_capture_archive(
        archive,
        tmp_path / "untyped-bronze-response.zip",
        capture_updates={},
        member_updates={
            "events.jsonl": "".join(
                json.dumps(row, sort_keys=True) + "\n" for row in event_rows
            ).encode()
        },
    )
    with pytest.raises(
        HAAPIError, match="Bronze protocol evidence is not capture-bound"
    ):
        verify_public_archive(rewritten)


def test_public_verifier_rejects_bronze_response_without_write_completion(
    tmp_path: Path,
) -> None:
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
    )
    with zipfile.ZipFile(archive) as source:
        event_rows = [
            json.loads(line) for line in source.read("public/events.jsonl").splitlines()
        ]
    completion = next(row for row in event_rows if row["event"] == "write_ok")
    completion["event"] = "write_error"
    rewritten = _rewrite_capture_archive(
        archive,
        tmp_path / "uncompleted-bronze-write.zip",
        capture_updates={},
        member_updates={
            "events.jsonl": "".join(
                json.dumps(row, sort_keys=True) + "\n" for row in event_rows
            ).encode()
        },
    )
    with pytest.raises(
        HAAPIError, match="Bronze protocol evidence is not capture-bound"
    ):
        verify_public_archive(rewritten)


@pytest.mark.parametrize("write_completion", ["missing", "write_error"])
def test_bronze_review_stays_inconclusive_without_successful_status_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_completion: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_run

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_protocol_outcome="inconclusive",
        bronze_write_completion=write_completion,
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]
    review_path = tmp_path / "uncompleted-write-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_sha256": imported["archive_sha256"],
                "run_id": imported["run_id"],
                "reviewed_by": "independent reviewer",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "claims": {
                    claim: "pass"
                    for claim in ("identity", "protocol_probe", "fresh_telemetry")
                },
            }
        ),
        encoding="utf-8",
    )

    assert (
        review_imported_run(
            matrix_path,
            run_id=imported["run_id"],
            review_file=review_path,
            apply=True,
            repo_root=tmp_path,
        )
        == 0
    )
    assert '"tier": "unqualified"' in capsys.readouterr().out
    reviewed = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]
    assert reviewed["bronze_evidence"]["protocol_probe"]["status"] == "inconclusive"


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
    assert imported["authorization_context"] == "unknown"
    assert imported["authorization_context_source"] == "archive_unverified"
    assert imported["installed_commit"] is None
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
    matrix_after_first_import = matrix_path.read_bytes()
    repackaged = _repack_archive(
        archive,
        tmp_path / "same-session-different-zip-metadata.zip",
        (2024, 1, 2, 3, 4, 6),
    )
    assert (
        hashlib.sha256(repackaged.read_bytes()).digest()
        != hashlib.sha256(archive.read_bytes()).digest()
    )
    assert (
        import_archive(
            repackaged, matrix_path=matrix_path, apply=True, repo_root=tmp_path
        )
        == 0
    )
    assert matrix_path.read_bytes() == matrix_after_first_import
    assert len(json.loads(matrix_path.read_text())["revisions"][0]["runs"]) == 1
    matrix_before_conflict = matrix_path.read_bytes()
    evidence[0].write_text("conflicting contents", encoding="utf-8")
    with pytest.raises(HAAPIError, match="Existing import evidence conflicts"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert evidence[0].read_text(encoding="utf-8") == "conflicting contents"
    assert matrix_path.read_bytes() == matrix_before_conflict


def test_guided_tier_export_is_imported_as_unqualified_review_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.tiers import evaluate_tiers

    archive = _public_archive(tmp_path, guided_tier=True)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)

    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]
    evidence_paths = [tmp_path / item for item in imported["evidence"]]
    guided_file = next(
        path for path in evidence_paths if path.name == "tier-execution.json"
    )
    public_execution = json.loads(guided_file.read_text(encoding="utf-8"))
    assert public_execution["attained_tier"] is None
    assert public_execution["qualification_status"] == "inconclusive"
    assert public_execution["steps"][0]["machine_evidence_sha256"]
    artifacts_file = next(
        path for path in evidence_paths if path.name == "tier-artifacts.json"
    )
    public_artifacts = json.loads(artifacts_file.read_text(encoding="utf-8"))
    assert public_artifacts["artifacts"][0]["content"]["kind"] == (
        "guided_step_evidence"
    )
    assert all(
        result["status"] == "inconclusive"
        for result in imported["write_capabilities"].values()
    )
    assert (
        evaluate_tiers(json.loads(matrix_path.read_text(encoding="utf-8")))["tiers"][0][
            "tier"
        ]
        == "unqualified"
    )
    assert imported["session_id"].startswith("archive-session-")
    assert "0123456789abcdef" not in imported["session_id"]


def test_verified_build_provenance_is_retained_in_imported_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import validate_hil_qualification

    archive = _public_archive(tmp_path, verified_build=True)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)

    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    assert imported["build_verification"] == "verified"
    assert imported["installed_commit"] == "a" * 40
    assert imported["installed_fingerprint"] == "b" * 64
    assert imported["checkout_fingerprint"] == "b" * 64
    assert imported["qualification_status"] == "inconclusive"
    assert "Imported" in capsys.readouterr().out


def test_import_rejects_duplicate_json_keys_without_writing(
    tmp_path: Path,
) -> None:
    archive = _public_archive(tmp_path / "source")
    tampered = tmp_path / "duplicate-device-key.zip"
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}

    session_name = "public/session.json"
    session = members[session_name].decode("utf-8")
    device_key = '"device": {'
    assert device_key in session
    session = session.replace(
        device_key,
        '"device": {"private_marker": "DO_NOT_IMPORT_PRIVATE_DATA"},\n'
        f"    {device_key}",
        1,
    )
    members[session_name] = session.encode("utf-8")

    manifest_name = "public/manifest.json"
    manifest = json.loads(members[manifest_name])
    session_record = next(
        entry for entry in manifest["files"] if entry["name"] == "session.json"
    )
    session_record["size"] = len(members[session_name])
    session_record["sha256"] = hashlib.sha256(members[session_name]).hexdigest()
    members[manifest_name] = json.dumps(manifest, sort_keys=True, indent=2).encode()
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, contents in members.items():
            target.writestr(name, contents)

    matrix_path = _matrix(tmp_path / "repo")
    matrix_before = matrix_path.read_bytes()
    fixture_tree = tmp_path / "repo" / "tests" / "hil" / "fixtures"
    assert not fixture_tree.exists()

    with pytest.raises(HAAPIError, match="Invalid HIL archive manifest|Malformed JSON"):
        import_archive(
            tampered, matrix_path=matrix_path, apply=True, repo_root=tmp_path / "repo"
        )

    assert matrix_path.read_bytes() == matrix_before
    assert not fixture_tree.exists()


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    [
        ("route", "HIL capture route conflicts with route observations"),
        (
            "dropped_events",
            "Complete HIL capture declares dropped events or frames",
        ),
        (
            "dropped_frames",
            "Complete HIL capture declares dropped events or frames",
        ),
    ],
)
def test_import_rejects_capture_metadata_conflicts_without_writing(
    tmp_path: Path, tamper: str, expected_error: str
) -> None:
    archive = _captured_archive(
        tmp_path / "source", "local_adapter", "1.1", "abcdef0123456789"
    )
    tampered = tmp_path / f"capture-{tamper}-conflict.zip"
    member_updates = None
    if tamper == "route":
        with zipfile.ZipFile(archive) as source:
            report = source.read("public/report.md")
        old_route = b"Observed GATT route: local_adapter"
        assert old_route in report
        member_updates = {
            "report.md": report.replace(
                old_route, b"Observed GATT route: active_proxy", 1
            )
        }
        capture_updates = {"capture-0001": {"route": "active_proxy"}}
    else:
        capture_updates = {"capture-0001": {"complete": True, tamper: 1}}
    _rewrite_capture_archive(
        archive,
        tampered,
        capture_updates=capture_updates,
        member_updates=member_updates,
    )

    repo_root = tmp_path / "repo"
    matrix_path = _matrix(repo_root)
    matrix_before = matrix_path.read_bytes()
    fixture_tree = repo_root / "tests" / "hil" / "fixtures"
    assert not fixture_tree.exists()
    with pytest.raises(HAAPIError, match=expected_error):
        import_archive(
            tampered, matrix_path=matrix_path, apply=True, repo_root=repo_root
        )
    assert matrix_path.read_bytes() == matrix_before
    assert not fixture_tree.exists()


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

    with pytest.raises(HAAPIError, match="Source run identity conflicts"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert matrix_path.read_bytes() == matrix_before
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()


def test_gold_shared_recovery_exports_after_intervening_setting_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent_run_id = "0123456789abcdef"
    child_run_id = "fedcba9876543210"
    parent_device = {
        "model": "R600",
        "hardware_version": "0.3",
        "firmware_version": "1.1",
    }
    parent_root = tmp_path / "guided-parent"
    parent_private = write_run(
        tmp_path,
        parent_root.name,
        {"schema_version": 1, "integration": {}},
        {
            "schema_version": 1,
            "run_id": parent_run_id,
            "qualification_status": "inconclusive",
            "mode": "read_only_preflight",
            "scenario_id": "read_only_snapshot",
            "started_at": "2026-11-09T12:00:00+00:00",
            "device": {
                key: _fact(value, "ha_device_registry")
                for key, value in parent_device.items()
            },
        },
    )
    child_private = write_run(
        tmp_path,
        f"tier_{child_run_id}",
        {"schema_version": 1, "integration": {}},
        {
            "run_id": child_run_id,
            "parent_tier_run_id": parent_run_id,
            "tier_step_id": "basic_reconnect",
            "requested_route": "local_adapter",
            "device": parent_device,
        },
    )
    write_scenario_progress(
        child_private,
        child_run_id,
        "recovery_reconnect",
        "a" * 64,
        [
            {
                "step_id": "baseline",
                "outcome": "pass",
                "session_generation": 1,
                "connected": True,
                "status_fresh": True,
            },
            {
                "step_id": "operator_action",
                "outcome": "pass",
                "action": "route interruption and restoration",
                "operator_confirmed": True,
            },
            {
                "step_id": "recovered_session",
                "outcome": "pass",
                "session_generation_before": 1,
                "session_generation_after": 2,
                "disconnect_observed": True,
                "connected": True,
                "status_fresh": True,
                "route_verified": True,
                "trace_complete": True,
                "fresh_status_telemetry": True,
            },
        ],
    )

    stamp = "2026-11-09T12:00:01+00:00"
    common = {
        "schema_version": 2,
        "run_id": child_run_id,
        "scenario_id": "recovery_reconnect",
        "layer": "gatt_api",
        "session_generation": 1,
        "observed_route": "local_adapter",
    }
    route_proof = {
        "backend_kind": "bluez",
        "evidence": "bluez_device_path",
        "adapter_id": "hci0",
    }
    events = [
        {
            **common,
            "seq": 1,
            "utc": stamp,
            "elapsed_ms": 0.0,
            "event": "route_observed",
            **route_proof,
        },
        {
            **common,
            "seq": 2,
            "utc": stamp,
            "elapsed_ms": 1.0,
            "event": "notification_received",
        },
    ]
    trace = {
        "schema_version": 2,
        "capture_scope": "runtime_entry",
        "capture_id": f"capture-{child_run_id}",
        "run_id": child_run_id,
        "scenario_id": "recovery_reconnect",
        "requested_route": "local_adapter",
        "capture_layer": "gatt_api_not_rf",
        "private_raw_bytes": True,
        "has_gatt_frames": True,
        "closed": True,
        "expired": False,
        "termination_reason": "requested_stop",
        "qualification_allowed": False,
        "observed_route": "local_adapter",
        "complete": True,
        "dropped_events": 0,
        "dropped_frames": 0,
        "event_count": len(events),
        "frame_count": 1,
        "events": events,
        "frames": [
            {
                **events[1],
                "direction": "rx",
                "command_type": "unknown",
                "frame_validation": "unvalidated_chunk",
                "hex": "aa55",
            }
        ],
        "route_observations": [
            {
                "session_generation": 1,
                "seq": 1,
                "utc": stamp,
                "elapsed_ms": 0.0,
                "observed_route": "local_adapter",
                **route_proof,
            }
        ],
    }
    trace_dir = child_private / "traces" / "recovery_reconnect_1"
    trace_dir.mkdir(parents=True)
    _save_private_trace(trace_dir, trace)
    checkpoint = child_private / "checkpoint.json"
    evidence_paths = [
        checkpoint,
        trace_dir / "trace.json",
    ]
    execution = {
        "schema_version": 1,
        "run_id": parent_run_id,
        "requested_tier": "gold",
        "requested_route": "local_adapter",
        "device": parent_device,
        "review_state": "pending",
        "steps": [
            {
                "step_id": "basic_reconnect",
                "scenario_id": "recovery_reconnect",
                "outcome": "pass",
                "human_review": "pending",
                "evidence_run": f"../../tier_{child_run_id}/private",
                "machine_evidence": [
                    {
                        "path": f"../../tier_{child_run_id}/private/{path.relative_to(child_private).as_posix()}",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in evidence_paths
                ],
            },
            {
                "step_id": "eco_mode_reversal",
                "scenario_id": "settings_eco_mode",
                "outcome": "blocked",
                "human_review": "pending",
            },
            {
                "step_id": "guarded_recovery",
                "scenario_id": "recovery_reconnect",
                "outcome": "shared_evidence",
                "evidence_run": f"../../tier_{child_run_id}/private",
            },
        ],
    }
    (parent_private / "tier-execution.json").write_text(json.dumps(execution))

    archive = export_archive(parent_root, confirmed=True)
    capsys.readouterr()
    verified, public, _digest = verify_public_archive(archive, include_contents=True)
    assert verified["qualification_status"] == "inconclusive"
    tier = json.loads(public["tier-execution.json"])
    assert [step["outcome"] for step in tier["steps"]] == [
        "pass",
        "blocked",
        "shared_evidence",
    ]
    assert len(tier["steps"][0]["machine_evidence"]) == 4
    child_session_path = child_private / "session.json"
    child_session = json.loads(child_session_path.read_text())
    child_session["parent_tier_run_id"] = "1111111111111111"
    child_session_path.write_text(json.dumps(child_session))
    with pytest.raises(HAAPIError, match="child run identity"):
        build_public(parent_root)


def test_modified_contents_for_same_source_run_fail_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification

    repo_root = tmp_path / "repo"
    matrix_path = _matrix(repo_root)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    first = _public_archive(tmp_path / "first")
    changed = _public_archive(tmp_path / "changed", installed_version="1.2.4")
    changed_scope = _public_archive(tmp_path / "changed-scope", firmware_version="1.2")
    assert (
        import_archive(first, matrix_path=matrix_path, apply=True, repo_root=repo_root)
        == 0
    )
    matrix_before = matrix_path.read_bytes()
    revision = json.loads(matrix_before)["revisions"][0]
    imported = revision["runs"][0]
    evidence_before = {
        path: (repo_root / path).read_bytes() for path in imported["evidence"]
    }

    with pytest.raises(
        HAAPIError, match="Source run identity conflicts with existing contents"
    ):
        import_archive(
            changed, matrix_path=matrix_path, apply=True, repo_root=repo_root
        )

    assert matrix_path.read_bytes() == matrix_before
    assert {path: (repo_root / path).read_bytes() for path in evidence_before} == (
        evidence_before
    )
    with pytest.raises(
        HAAPIError,
        match="Source run identity conflicts with existing firmware or route scope",
    ):
        import_archive(
            changed_scope,
            matrix_path=matrix_path,
            apply=True,
            repo_root=repo_root,
        )
    assert matrix_path.read_bytes() == matrix_before
    assert {path: (repo_root / path).read_bytes() for path in evidence_before} == (
        evidence_before
    )


def test_missing_source_run_id_uses_canonical_member_content_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification

    repo_root = tmp_path / "repo"
    matrix_path = _matrix(repo_root)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    archive = _public_archive(tmp_path / "source-unavailable", source_run_id=None)
    repackaged = _repack_archive(
        archive,
        tmp_path / "source-unavailable-repacked.zip",
        (2022, 6, 8, 10, 12, 14),
    )

    assert (
        import_archive(
            archive, matrix_path=matrix_path, apply=True, repo_root=repo_root
        )
        == 0
    )
    first_matrix = matrix_path.read_bytes()
    assert (
        import_archive(
            repackaged, matrix_path=matrix_path, apply=True, repo_root=repo_root
        )
        == 0
    )
    assert matrix_path.read_bytes() == first_matrix
    runs = json.loads(first_matrix)["revisions"][0]["runs"]
    assert len(runs) == 1
    assert runs[0]["source_run_id"] is None
    assert runs[0]["content_sha256"]


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


@pytest.mark.parametrize(
    ("packet_types", "protocol_outcome"),
    [
        (("StatusData",), "pass"),
        (("SettingsData",), "inconclusive"),
        ((), "inconclusive"),
    ],
)
def test_export_import_and_independent_bronze_review_requires_status_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    packet_types: tuple[str, ...],
    protocol_outcome: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_run

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=packet_types,
        bronze_protocol_outcome=protocol_outcome,
    )
    from scripts.hil.evidence import verify_public_archive

    _, public_members, _ = verify_public_archive(archive, include_contents=True)
    bronze_public = json.loads(public_members["tier-bronze-read-only.json"])
    public_events = [
        json.loads(line) for line in public_members["events.jsonl"].splitlines()
    ]
    decoded_event = next(
        row
        for row in public_events
        if row["event"] in {"status_data_decoded", "notification_decoded"}
    )
    if packet_types == ("StatusData",):
        assert bronze_public["protocol_probe"] == "verified"
        assert decoded_event["event"] == "status_data_decoded"
        assert "decoded_packet_types" not in decoded_event
    else:
        assert bronze_public["protocol_probe"] == "inconclusive"
        assert decoded_event["event"] == "notification_decoded"
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]
    review_path = tmp_path / "bronze-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_sha256": imported["archive_sha256"],
                "run_id": imported["run_id"],
                "reviewed_by": "independent reviewer",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "claims": {
                    claim: "pass"
                    for claim in ("identity", "protocol_probe", "fresh_telemetry")
                },
            }
        ),
        encoding="utf-8",
    )
    mismatched_review = tmp_path / "mismatched-review.json"
    mismatched_review.write_text(
        json.dumps(
            json.loads(review_path.read_text(encoding="utf-8"))
            | {"archive_sha256": "0" * 64}
        ),
        encoding="utf-8",
    )
    before_review = matrix_path.read_bytes()
    with pytest.raises(HAAPIError, match="does not match"):
        review_imported_run(
            matrix_path,
            run_id=imported["run_id"],
            review_file=mismatched_review,
            apply=True,
            repo_root=tmp_path,
        )
    assert matrix_path.read_bytes() == before_review

    assert (
        review_imported_run(
            matrix_path,
            run_id=imported["run_id"],
            review_file=review_path,
            repo_root=tmp_path,
        )
        == 0
    )
    assert '"applied": false' in capsys.readouterr().out
    assert (
        json.loads(matrix_path.read_text())["revisions"][0]["runs"][0].get(
            "bronze_evidence"
        )
        is None
    )

    assert (
        review_imported_run(
            matrix_path,
            run_id=imported["run_id"],
            review_file=review_path,
            apply=True,
            repo_root=tmp_path,
        )
        == 0
    )
    review_output = capsys.readouterr().out
    reviewed = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]
    expected_status = "pass" if packet_types == ("StatusData",) else "inconclusive"
    assert {
        key: value["status"] for key, value in reviewed["bronze_evidence"].items()
    } == {
        "identity": "pass",
        "protocol_probe": expected_status,
        "fresh_telemetry": "pass",
    }
    if expected_status == "pass":
        assert '"tier": "bronze"' in review_output
    else:
        assert '"tier": "unqualified"' in review_output
        assert '"protocol_probe": "inconclusive"' in review_output
    if expected_status == "pass":
        capture_path = (
            tmp_path / reviewed["bronze_evidence"]["protocol_probe"]["capture_evidence"]
        )
        capture_rows = [
            json.loads(line) for line in capture_path.read_text().splitlines() if line
        ]
        assert [row["session_generation"] for row in capture_rows] == [7, 7, 7]
        assert [row["event"] for row in capture_rows] == [
            "write_attempt",
            "write_ok",
            "status_data_decoded",
        ]
        assert all("physical_outcome" not in row for row in capture_rows)
    errors, _ = validate_hil_qualification.validate_matrix(
        json.loads(matrix_path.read_text()),
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert errors == []
    reviewed_matrix = matrix_path.read_bytes()
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    assert matrix_path.read_bytes() == reviewed_matrix
    assert (
        json.loads(matrix_path.read_text())["revisions"][0]["runs"][0][
            "bronze_evidence"
        ]
        == reviewed["bronze_evidence"]
    )


def test_review_cannot_upgrade_archive_with_missing_machine_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_run

    archive = _captured_archive(
        tmp_path / "source", "local_adapter", "1.1", "0123456789abcdef"
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    review_path = tmp_path / "bronze-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_sha256": imported["archive_sha256"],
                "run_id": imported["run_id"],
                "reviewed_by": "independent reviewer",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "claims": {
                    claim: "pass"
                    for claim in ("identity", "protocol_probe", "fresh_telemetry")
                },
            }
        ),
        encoding="utf-8",
    )
    assert (
        review_imported_run(
            matrix_path,
            run_id=imported["run_id"],
            review_file=review_path,
            apply=True,
            repo_root=tmp_path,
        )
        == 0
    )
    reviewed = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    assert all(
        item["status"] == "inconclusive"
        for item in reviewed["bronze_evidence"].values()
    )
