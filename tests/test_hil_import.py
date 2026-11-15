"""Import public HIL archives as scoped, inconclusive review evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
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
    guided_authorization_context: str | None = None,
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
                            **(
                                {
                                    "authorization_context": guided_authorization_context,
                                    "authorization_context_source": {
                                        "production": "runtime_production_authorized",
                                        "experimental_hil": "experimental_hil_grant",
                                    }.get(guided_authorization_context, "invalid"),
                                }
                                if guided_authorization_context is not None
                                else {}
                            ),
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


def _guided_setting_review_archive(
    path: Path, *, observed_route: str = "local_adapter"
) -> Path:
    """Build a synthetic public archive with one complete settings cycle."""
    from scripts.hil.evidence import _public_tier_artifact

    source_run_id = "abcdef0123456789"
    archive = _captured_archive(
        path / "source",
        "local_adapter",
        "1.1",
        source_run_id,
        bronze_reviewable=True,
    )
    artifacts = []
    refs = []
    all_hashes = []
    action_refs = []
    for index, state in enumerate((True, False), start=1):
        transaction_id = f"settings-transaction-{index}"
        frame = bytearray.fromhex(
            "a56500b1010202010100" if state else "a56500b1010202000100"
        )
        checksum = 0
        for value in frame[:-1]:
            checksum ^= value
        frame[-1] = checksum
        scenario_id = "settings_eco_mode"
        stamp = f"2026-11-09T12:00:0{index}+00:00"
        events = [
            {
                "schema_version": 2,
                "seq": seq,
                "utc": stamp,
                "elapsed_ms": float(seq * 100),
                "run_id": source_run_id,
                "scenario_id": scenario_id,
                "layer": "gatt_api",
                "session_generation": 3,
                "observed_route": observed_route,
                "event": event,
                "transaction_kind": "settings",
                "transaction_id": transaction_id,
                **(
                    {"reason": "matching_validated_telemetry"}
                    if event == "transaction_confirmed"
                    else {}
                ),
            }
            for seq, event in enumerate(
                ("write_attempt", "transaction_submitted", "transaction_confirmed"),
                start=1,
            )
        ]
        frames = [
            {
                **events[0],
                "direction": "tx",
                "command_type": "settings_control",
                "frame_validation": "validated_tx",
                "hex": frame.hex(),
            }
        ]
        events_bytes = b"".join(
            (json.dumps(row, sort_keys=True) + "\n").encode() for row in events
        )
        frames_bytes = b"".join(
            (json.dumps(row, sort_keys=True) + "\n").encode() for row in frames
        )
        trace_value = {
            "schema_version": 2,
            "scenario_id": scenario_id,
            "capture_id": f"synthetic-setting-capture-{index}",
            "requested_route": observed_route,
            "observed_route": observed_route,
            "complete": True,
            "event_count": len(events),
            "frame_count": len(frames),
            "dropped_events": 0,
            "dropped_frames": 0,
        }
        trace_bytes = (json.dumps(trace_value, sort_keys=True) + "\n").encode()
        source_rows = [
            (
                "trace.json",
                trace_bytes,
                _public_tier_artifact(
                    "trace.json",
                    trace_bytes,
                    trace_files={
                        "events.jsonl": events_bytes,
                        "frames.jsonl": frames_bytes,
                    },
                ),
            ),
            (
                "events.jsonl",
                events_bytes,
                _public_tier_artifact("events.jsonl", events_bytes),
            ),
            (
                "frames.jsonl",
                frames_bytes,
                _public_tier_artifact("frames.jsonl", frames_bytes),
            ),
        ]
        trace_hashes = []
        for _name, contents, projected in source_rows:
            digest = hashlib.sha256(contents).hexdigest()
            trace_hashes.append(digest)
            all_hashes.append(digest)
            artifact_id = f"step-01-evidence-{len(artifacts) + 1:02d}"
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "source_sha256": digest,
                    "content": projected,
                }
            )
            refs.append({"artifact_id": artifact_id, "source_sha256": digest})
        action_refs.append(
            {
                "outcome": "confirmed",
                "confirmed": True,
                "trace_valid": True,
                "trace_complete": True,
                "route": observed_route,
                "trace_route": observed_route,
                "session_generation": 3,
                "transaction": {
                    "transaction_id": transaction_id,
                    "write_seq": 1,
                    "submitted_seq": 2,
                    "confirmed_seq": 3,
                    "session_generation": 3,
                    "kind": "settings",
                    "command_type": "settings_control",
                },
                "trace_evidence": [
                    {"path": f"action-{index}/{name}", "sha256": digest}
                    for (name, _contents, _projected), digest in zip(
                        source_rows, trace_hashes, strict=True
                    )
                ],
            }
        )

    guide = {
        "schema_version": 1,
        "step_id": "eco_mode_reversal",
        "outcome": "pass",
        "initial_state": False,
        "target_state": True,
        "restored_state": False,
        "physical_change_outcome": "correct",
        "physical_restore_outcome": "correct",
        "actions": action_refs,
    }
    guide_bytes = (json.dumps(guide, sort_keys=True) + "\n").encode()
    guide_hash = hashlib.sha256(guide_bytes).hexdigest()
    all_hashes.append(guide_hash)
    guide_id = f"step-01-evidence-{len(artifacts) + 1:02d}"
    artifacts.append(
        {
            "artifact_id": guide_id,
            "source_sha256": guide_hash,
            "content": _public_tier_artifact(
                "tier-eco_mode_reversal.json", guide_bytes
            ),
        }
    )
    refs.append({"artifact_id": guide_id, "source_sha256": guide_hash})
    execution = {
        "schema_version": 2,
        "requested_tier": "gold",
        "execution_tier": "gold",
        "attained_tier": None,
        "review_state": "pending",
        "qualification_status": "inconclusive",
        "steps": [
            {
                "step_id": "eco_mode_reversal",
                "outcome": "pass",
                "human_review": "pending",
                "authorization_context": "production",
                "authorization_context_source": "runtime_production_authorized",
                "machine_evidence_sha256": sorted(set(all_hashes)),
                "machine_evidence": refs,
            }
        ],
    }
    return _rewrite_capture_archive(
        archive,
        path / "guided-settings.zip",
        capture_updates={},
        member_updates={
            "tier-execution.json": (
                json.dumps(execution, sort_keys=True) + "\n"
            ).encode(),
            "tier-artifacts.json": (
                json.dumps(
                    {"schema_version": 2, "artifacts": artifacts}, sort_keys=True
                )
                + "\n"
            ).encode(),
        },
    )


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


def _tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())


def _review_answers(*, reviewer: str, inspected: bool, apply: bool) -> list[str]:
    answers = [reviewer, "yes" if inspected else "no"]
    if inspected:
        for _ in range(3):
            answers.extend(("pass", "yes"))
    answers.append("APPLY REVIEW" if apply else "cancel")
    return answers


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
                "seq": 3,
                "utc": stamp,
                "elapsed_ms": 2.0,
                "event": "notification_received",
            }
            write_completion_event = {
                **common,
                "seq": 5,
                "utc": stamp,
                "elapsed_ms": 4.0,
                "event": bronze_write_completion,
                "transaction_id": "bronze-tx-1",
            }
            decoded_event = {
                **common,
                "seq": 4,
                "utc": stamp,
                "elapsed_ms": 3.0,
                "event": "notification_decoded",
                "decoded_packet_types": list(bronze_decoded_packet_types),
            }
            trace["events"] = [
                route_event,
                {**status_frame, "direction": "write"},
                notification_event,
                decoded_event,
                *(
                    [write_completion_event]
                    if bronze_write_completion != "missing"
                    else []
                ),
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
                            "status_write_ok_seq": 5,
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

    archive = _public_archive(
        tmp_path,
        guided_tier=True,
        guided_authorization_context="production",
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
    evidence_paths = [tmp_path / item for item in imported["evidence"]]
    guided_file = next(
        path for path in evidence_paths if path.name == "tier-execution.json"
    )
    public_execution = json.loads(guided_file.read_text(encoding="utf-8"))
    assert public_execution["attained_tier"] is None
    assert public_execution["qualification_status"] == "inconclusive"
    assert public_execution["steps"][0]["machine_evidence_sha256"]
    assert public_execution["steps"][0]["authorization_context"] == "production"
    assert (
        public_execution["steps"][0]["authorization_context_source"]
        == "runtime_production_authorized"
    )
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


def test_guided_export_rejects_unverified_authorization_context(
    tmp_path: Path,
) -> None:
    with pytest.raises(HAAPIError, match="authorization provenance"):
        _public_archive(
            tmp_path,
            guided_tier=True,
            guided_authorization_context="unsupported",
        )


def test_tier_review_audit_is_bound_to_feature_phase_and_archive(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta
    from scripts.hil.review import _tier_review_file

    now = datetime.now(UTC)
    run = {
        "run_id": "archive-sample",
        "archive_sha256": "a" * 64,
        "captured_at": (now - timedelta(seconds=1)).isoformat(),
    }

    def reference(index: int, state: bool) -> dict[str, Any]:
        return {
            "trace_sha256": ("b" if index == 0 else "c") * 64,
            "frame_sha256": ("d" if index == 0 else "e") * 64,
            "transaction_sha256": ("f" if index == 0 else "a") * 64,
            "frame_seq": index + 1,
            "session_generation": 4,
            "source_action_index": index,
            "action_stage": "transition" if index == 0 else "restoration",
            "source_step_id": "eco_mode_reversal",
            "requested_state": state,
        }

    payload = {
        "schema_version": 1,
        "tier": "gold",
        "archive_sha256": run["archive_sha256"],
        "run_id": run["run_id"],
        "reviewed_by": "reviewer one",
        "reviewed_at": now.isoformat(),
        "reviewer_independent_from_operator": True,
        "transitions": [
            {
                "step_id": "eco_mode_reversal",
                "initial_state": False,
                "target_state": True,
                "change_frame_evidence": reference(0, True),
                "restore_frame_evidence": reference(1, False),
                "change_trace_sha256": ["b" * 64],
                "restore_trace_sha256": ["c" * 64],
                "physical_change_confirmed": True,
                "physical_restoration_confirmed": True,
                "production_authorization_confirmed": True,
            }
        ],
        "scenarios": [],
    }
    path = tmp_path / "tier-review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _tier_review_file(path, run, "gold")[0] == "reviewer one"
    payload["transitions"][0]["restore_frame_evidence"]["source_action_index"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HAAPIError, match="frame reference"):
        _tier_review_file(path, run, "gold")


def test_public_guided_frame_projection_keeps_semantics_and_omits_payload() -> None:
    from scripts.hil.evidence import _public_tier_artifact, _valid_public_tier_artifact

    frame = bytearray.fromhex("a56500b10101000200")
    checksum = 0
    for value in frame[:-1]:
        checksum ^= value
    frame[-1] = checksum
    private_row = {
        "seq": 4,
        "event": "write_attempt",
        "direction": "tx",
        "command_type": "output_control",
        "frame_validation": "validated_tx",
        "session_generation": 2,
        "hex": frame.hex(),
    }
    projected = _public_tier_artifact(
        "frames.jsonl", (json.dumps(private_row) + "\n").encode()
    )

    row = projected["rows"][0]
    assert row["output_states"] == {
        "ac_output": True,
        "dc_output": False,
        "light_output": False,
    }
    assert row["frame_sha256"] == hashlib.sha256(frame).hexdigest()
    assert "hex" not in row
    assert _valid_public_tier_artifact(projected)

    frame[-1] ^= 1
    private_row["hex"] = frame.hex()
    invalid = _public_tier_artifact(
        "frames.jsonl", (json.dumps(private_row) + "\n").encode()
    )
    assert "output_states" not in invalid["rows"][0]

    settings_frame = bytearray.fromhex("a56500b1010202010400")
    checksum = 0
    for value in settings_frame[:-1]:
        checksum ^= value
    settings_frame[-1] = checksum
    private_row.update(
        command_type="settings_control",
        hex=settings_frame.hex(),
    )
    settings = _public_tier_artifact(
        "frames.jsonl", (json.dumps(private_row) + "\n").encode()
    )
    assert settings["rows"][0]["setting_states"] == {
        "eco_mode": True,
        "work_mode": 0,
        "car_charger": False,
        "eco_timeout": 4,
    }
    assert "hex" not in settings["rows"][0]


def test_public_guided_output_evidence_requires_two_restored_cycles() -> None:
    from scripts.hil.evidence import _public_tier_artifact, _valid_public_tier_artifact

    cycles = [
        {
            "initial_state": "off",
            "target_state": "on",
            "change_action_index": 0,
            "restore_action_index": 1,
            "physical_change_outcome": "correct",
            "physical_restore_outcome": "correct",
        },
        {
            "initial_state": "on",
            "target_state": "off",
            "change_action_index": 1,
            "restore_action_index": 2,
            "physical_change_outcome": "correct",
            "physical_restore_outcome": "correct",
        },
    ]
    private = {
        "step_id": "light_output_cycle",
        "outcome": "pass",
        "initial_state": "off",
        "target_state": "on",
        "restored_state": "off",
        "physical_change_outcome": "correct",
        "physical_restore_outcome": "correct",
        "output_cycles": cycles,
        "final_restore_action_index": 3,
        "final_restore_outcome": "correct",
        "actions": [{"outcome": "confirmed", "confirmed": True}] * 4,
    }
    projected = _public_tier_artifact(
        "tier-light_output_cycle.json", json.dumps(private).encode()
    )
    assert projected["output_cycles"] == cycles
    assert _valid_public_tier_artifact(projected)

    malformed = json.loads(json.dumps(private))
    malformed["output_cycles"][1]["restore_action_index"] = 1
    with pytest.raises(HAAPIError, match="output cycle"):
        _public_tier_artifact(
            "tier-light_output_cycle.json", json.dumps(malformed).encode()
        )


def test_reconnect_capture_is_bound_to_source_status_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification as validation

    monkeypatch.setattr(validation, "ROOT", tmp_path)
    trace_sha = "a" * 64
    frame = {
        "seq": 5,
        "event": "write_attempt",
        "direction": "tx",
        "command_type": "status_request",
        "frame_validation": "validated_tx",
        "session_generation": 2,
        "payload_omitted": True,
        "frame_sha256": hashlib.sha256(validation.STATUS_REQUEST).hexdigest(),
        "transaction_sha256": "e" * 64,
    }
    trace = {
        "kind": "gatt_trace_metadata",
        "schema_version": 2,
        "scenario_id": "recovery_reconnect",
        "requested_route": "local_adapter",
        "observed_route": "local_adapter",
        "complete": True,
        "event_count": 1,
        "frame_count": 1,
        "dropped_events": 0,
        "dropped_frames": 0,
        "events_sha256": "b" * 64,
        "frames_sha256": "c" * 64,
        "events": [
            {
                "seq": 6,
                "event": "write_ok",
                "utc": "2026-11-09T12:00:01+00:00",
                "elapsed_ms": 10.0,
                "session_generation": 2,
                "observed_route": "local_adapter",
                "transaction_sha256": "e" * 64,
            }
        ],
        "frames": [frame],
    }
    bundle = {
        "artifacts": [
            {
                "artifact_id": "step-01-evidence-01",
                "source_sha256": trace_sha,
                "content": trace,
            }
        ]
    }
    execution = {
        "steps": [
            {
                "step_id": "basic_reconnect",
                "machine_evidence_sha256": [trace_sha],
                "machine_evidence": [
                    {
                        "artifact_id": "step-01-evidence-01",
                        "source_sha256": trace_sha,
                    }
                ],
            }
        ]
    }
    (tmp_path / "tier-artifacts.json").write_text(json.dumps(bundle))
    (tmp_path / "tier-execution.json").write_text(json.dumps(execution))
    scope = {
        "route": "local_adapter",
        "revision_id": "sample",
        "firmware_version": "1.1",
        "run_id": "archive-sample",
        "captured_at": "2026-11-09T12:00:00+00:00",
        "session_id": "archive-session-sample",
        "build_fingerprint": "d" * 64,
    }
    row = {
        **scope,
        "scenario": "reconnect_failover",
        "operation_id": "reconnect_failover",
        "direction": "write",
        "frame_hex": validation.STATUS_REQUEST.hex(),
        "verdict": "ok",
        "physical_outcome": "pass",
        "source_trace_sha256": trace_sha,
        "source_frame_sha256": frame["frame_sha256"],
        "source_transaction_sha256": frame["transaction_sha256"],
        "source_frame_seq": 5,
        "source_step_id": "basic_reconnect",
        "session_generation": 2,
    }
    (tmp_path / "reconnect.jsonl").write_text(json.dumps(row) + "\n")
    evidence = [
        "tier-artifacts.json",
        "tier-execution.json",
        "reconnect.jsonl",
    ]

    assert validation._has_bound_capture(
        evidence,
        **scope,
        result_key="reconnect_failover",
        capability=False,
        expected_verdict="ok",
    )
    row["source_frame_seq"] = 4
    (tmp_path / "reconnect.jsonl").write_text(json.dumps(row) + "\n")
    assert not validation._has_bound_capture(
        evidence,
        **scope,
        result_key="reconnect_failover",
        capability=False,
        expected_verdict="ok",
    )


def test_public_guided_trace_binds_sanitized_events_and_frames() -> None:
    from scripts.hil.evidence import _public_tier_artifact, _valid_public_tier_artifact

    frame = bytearray.fromhex("a56500b10101000200")
    checksum = 0
    for value in frame[:-1]:
        checksum ^= value
    frame[-1] = checksum
    transaction_id = "public-output-transaction"
    transaction_sha256 = hashlib.sha256(transaction_id.encode()).hexdigest()
    event_rows = [
        {
            "seq": seq,
            "event": event,
            "utc": f"2026-11-09T12:00:0{seq}+00:00",
            "elapsed_ms": seq * 100,
            "session_generation": 2,
            "observed_route": "local_adapter",
            "transaction_kind": "output",
            **(
                {
                    "transaction_id": transaction_id,
                    **(
                        {"reason": "matching_validated_telemetry"}
                        if event == "transaction_confirmed"
                        else {}
                    ),
                }
                if event != "notification_decoded"
                else {}
            ),
        }
        for seq, event in enumerate(
            (
                "write_attempt",
                "transaction_submitted",
                "transaction_confirmed",
                "notification_decoded",
            ),
            1,
        )
    ]
    event_rows[-1]["decoded_packet_types"] = ["StatusData"]
    frame_rows = [
        {
            "seq": 1,
            "event": "write_attempt",
            "direction": "tx",
            "command_type": "output_control",
            "frame_validation": "validated_tx",
            "session_generation": 2,
            "transaction_id": transaction_id,
            "hex": frame.hex(),
        }
    ]
    trace = {
        "schema_version": 2,
        "scenario_id": "output_ac_cycle",
        "requested_route": "local_adapter",
        "observed_route": "local_adapter",
        "complete": True,
        "event_count": len(event_rows),
        "frame_count": len(frame_rows),
        "dropped_events": 0,
        "dropped_frames": 0,
    }
    projected = _public_tier_artifact(
        "trace.json",
        json.dumps(trace).encode(),
        trace_files={
            "events.jsonl": b"".join(
                (json.dumps(row) + "\n").encode() for row in event_rows
            ),
            "frames.jsonl": b"".join(
                (json.dumps(row) + "\n").encode() for row in frame_rows
            ),
        },
    )

    assert _valid_public_tier_artifact(projected)
    assert projected["events_sha256"]
    assert projected["frames_sha256"]
    assert projected["events"][-1]["decoded_packet_types"] == ["StatusData"]
    assert projected["frames"][0]["frame_sha256"] == hashlib.sha256(frame).hexdigest()
    assert projected["frames"][0]["event"] == "write_attempt"
    from scripts.hil.review_wizard import _has_trace_operation

    action = {
        "session_generation": 2,
        "transaction": {
            "kind": "output",
            "session_generation": 2,
            "transaction_sha256": transaction_sha256,
            "write_seq": 1,
            "submitted_seq": 2,
            "confirmed_seq": 3,
        },
        "trace_evidence_sha256": ["e" * 64],
    }
    assert _has_trace_operation(
        action,
        {"e" * 64: projected},
        feature="ac_output",
        state="on",
        route="local_adapter",
    )
    assert not _has_trace_operation(
        action,
        {"e" * 64: projected},
        feature="ac_output",
        state="on",
        route="active_proxy",
    )
    unrelated_confirmation = {
        **projected,
        "events": [dict(row) for row in projected["events"]],
    }
    unrelated_confirmation["events"][2]["transaction_sha256"] = "0" * 64
    assert not _has_trace_operation(
        action,
        {"e" * 64: unrelated_confirmation},
        feature="ac_output",
        state="on",
        route="local_adapter",
    )
    setting_trace = {
        "kind": "gatt_trace_metadata",
        "observed_route": "local_adapter",
        "complete": True,
        "dropped_events": 0,
        "dropped_frames": 0,
        "transaction_id": "public-setting-transaction",
        "frames": [
            {
                "seq": 4,
                "event": "write_attempt",
                "direction": "tx",
                "command_type": "settings_control",
                "frame_validation": "validated_tx",
                "session_generation": 2,
                "frame_sha256": "f" * 64,
                "transaction_sha256": hashlib.sha256(
                    b"public-setting-transaction"
                ).hexdigest(),
                "setting_states": {
                    "eco_mode": True,
                    "work_mode": 0,
                    "eco_timeout": 4,
                    "car_charger": False,
                },
            }
        ],
        "events": [
            {
                "seq": seq,
                "event": event,
                "session_generation": 2,
                "observed_route": "local_adapter",
                "transaction_kind": "settings",
                "transaction_sha256": hashlib.sha256(
                    b"public-setting-transaction"
                ).hexdigest(),
                **(
                    {"transaction_reason": "matching_validated_telemetry"}
                    if event == "transaction_confirmed"
                    else {}
                ),
            }
            for seq, event in (
                (4, "write_attempt"),
                (5, "transaction_submitted"),
                (6, "transaction_confirmed"),
            )
        ],
    }
    setting_action = {
        "session_generation": 2,
        "transaction": {
            "kind": "settings",
            "session_generation": 2,
            "transaction_sha256": hashlib.sha256(
                b"public-setting-transaction"
            ).hexdigest(),
            "write_seq": 4,
            "submitted_seq": 5,
            "confirmed_seq": 6,
        },
        "trace_evidence_sha256": ["f" * 64],
    }
    assert _has_trace_operation(
        setting_action,
        {"f" * 64: setting_trace},
        feature="eco_mode",
        state=True,
        route="local_adapter",
    )
    assert not _has_trace_operation(
        setting_action,
        {"f" * 64: setting_trace},
        feature="eco_mode",
        state=False,
        route="local_adapter",
    )


def test_schema_v3_validates_normalized_frame_against_imported_artifact_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification as validation
    from scripts.hil.evidence import _public_tier_artifact

    frame = bytearray.fromhex("a56500b10101000200")
    checksum = 0
    for value in frame[:-1]:
        checksum ^= value
    frame[-1] = checksum
    transaction_id = "sample-output-transaction"
    transaction_sha256 = hashlib.sha256(transaction_id.encode()).hexdigest()
    frame_content = (
        json.dumps(
            {
                "seq": 4,
                "event": "write_attempt",
                "direction": "tx",
                "command_type": "output_control",
                "frame_validation": "validated_tx",
                "session_generation": 2,
                "transaction_id": transaction_id,
                "hex": frame.hex(),
            }
        )
        + "\n"
    ).encode()
    events_content = (
        b"\n".join(
            json.dumps(
                {
                    "seq": seq,
                    "event": event,
                    "utc": f"2026-11-09T12:00:0{seq}+00:00",
                    "elapsed_ms": seq * 100,
                    "session_generation": 2,
                    "observed_route": "local_adapter",
                    "transaction_kind": "output",
                    "transaction_id": transaction_id,
                    **(
                        {"reason": "matching_validated_telemetry"}
                        if event == "transaction_confirmed"
                        else {}
                    ),
                }
            ).encode()
            for seq, event in (
                (4, "write_attempt"),
                (5, "transaction_submitted"),
                (6, "transaction_confirmed"),
            )
        )
        + b"\n"
    )
    trace = _public_tier_artifact(
        "trace.json",
        json.dumps(
            {
                "schema_version": 2,
                "scenario_id": "output_ac_cycle",
                "requested_route": "local_adapter",
                "observed_route": "local_adapter",
                "complete": True,
                "event_count": 3,
                "frame_count": 1,
                "dropped_events": 0,
                "dropped_frames": 0,
            }
        ).encode(),
        trace_files={"events.jsonl": events_content, "frames.jsonl": frame_content},
    )
    trace_sha = "a" * 64
    step_sha = "b" * 64
    bundle_path = "tests/hil/fixtures/sample/runs/archive-sample/tier-artifacts.json"
    execution_path = "tests/hil/fixtures/sample/runs/archive-sample/tier-execution.json"
    capture_path = "tests/hil/fixtures/sample/runs/archive-sample/review.jsonl"
    bundle = {
        "artifacts": [
            {
                "artifact_id": "step-01-evidence-01",
                "source_sha256": trace_sha,
                "content": trace,
            },
            {
                "artifact_id": "step-01-evidence-02",
                "source_sha256": step_sha,
                "content": {
                    "kind": "guided_step_evidence",
                    "step_id": "ac_output_cycle",
                    "outcome": "pass",
                    "target_state": "on",
                    "initial_state": "off",
                    "physical_change_outcome": "correct",
                    "physical_restore_outcome": "correct",
                    "output_cycles": [
                        {
                            "initial_state": "off",
                            "target_state": "on",
                            "change_action_index": 0,
                            "restore_action_index": 1,
                            "physical_change_outcome": "correct",
                            "physical_restore_outcome": "correct",
                        },
                        {
                            "initial_state": "on",
                            "target_state": "off",
                            "change_action_index": 1,
                            "restore_action_index": 2,
                            "physical_change_outcome": "correct",
                            "physical_restore_outcome": "correct",
                        },
                    ],
                    "final_restore_action_index": 3,
                    "final_restore_outcome": "correct",
                    "actions": [
                        {
                            "outcome": "confirmed",
                            "confirmed": True,
                            "trace_valid": True,
                            "trace_complete": True,
                            "route": "local_adapter",
                            "session_generation": 2,
                            "transaction": {
                                "kind": "output",
                                "session_generation": 2,
                                "transaction_sha256": transaction_sha256,
                                "write_seq": 4,
                                "submitted_seq": 5,
                                "confirmed_seq": 6,
                            },
                            "trace_evidence_sha256": [trace_sha],
                        }
                    ],
                },
            },
        ]
    }
    execution = {
        "steps": [
            {
                "step_id": "ac_output_cycle",
                "authorization_context": "production",
                "authorization_context_source": "runtime_production_authorized",
                "machine_evidence_sha256": [trace_sha],
                "machine_evidence": [
                    {"artifact_id": "step-01-evidence-02", "source_sha256": step_sha}
                ],
            }
        ]
    }
    for relative, content in (
        (bundle_path, bundle),
        (execution_path, execution),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(validation, "ROOT", tmp_path)
    scope = {
        "route": "local_adapter",
        "revision_id": "sample",
        "model": "ALLPOWERS R600",
        "hardware_revision": "0.3",
        "firmware_version": "1.1",
        "run_id": "archive-sample",
        "captured_at": "2026-11-09T12:00:00+00:00",
        "session_id": "archive-session-sample",
        "build_fingerprint": "c" * 64,
    }
    row = {
        **scope,
        "capability": "ac_output",
        "operation_id": "ac_output",
        "action_stage": "transition",
        "requested_state": True,
        "direction": "write",
        "verdict": "ok",
        "physical_outcome": "pass",
        "session_generation": 2,
        "source_trace_sha256": trace_sha,
        "source_frame_sha256": hashlib.sha256(frame).hexdigest(),
        "source_transaction_sha256": transaction_sha256,
        "source_frame_seq": 4,
        "source_step_id": "ac_output_cycle",
        "source_action_index": 0,
    }

    assert validation._valid_public_frame_capture(
        row,
        [bundle_path, execution_path, capture_path],
        scope=scope,
        feature="ac_output",
        requested_state=True,
        action_stage="transition",
    )
    row["source_transaction_sha256"] = "e" * 64
    assert not validation._valid_public_frame_capture(
        row,
        [bundle_path, execution_path, capture_path],
        scope=scope,
        feature="ac_output",
        requested_state=True,
        action_stage="transition",
    )
    row["source_transaction_sha256"] = transaction_sha256
    execution["steps"][0]["authorization_context"] = "experimental_hil"
    execution["steps"][0]["authorization_context_source"] = "experimental_hil_grant"
    execution_file = tmp_path / execution_path
    execution_file.write_text(json.dumps(execution), encoding="utf-8")
    assert not validation._valid_public_frame_capture(
        row,
        [bundle_path, execution_path, capture_path],
        scope=scope,
        feature="ac_output",
        requested_state=True,
        action_stage="transition",
    )
    execution["steps"][0]["authorization_context"] = "production"
    execution["steps"][0]["authorization_context_source"] = (
        "runtime_production_authorized"
    )
    execution_file.write_text(json.dumps(execution), encoding="utf-8")
    row["source_frame_sha256"] = "d" * 64
    assert not validation._valid_public_frame_capture(
        row,
        [bundle_path, execution_path, capture_path],
        scope=scope,
        feature="ac_output",
        requested_state=True,
        action_stage="transition",
    )


@pytest.mark.parametrize(
    ("tier", "blocker"),
    [
        (
            "silver",
            "imported machine proof did not produce a supported schema-v3 result",
        ),
        (
            "gold",
            "imported machine proof did not produce a supported schema-v3 result",
        ),
        ("platinum", "continuous 24-hour soak evidence"),
    ],
)
def test_higher_tier_review_fails_closed_when_public_proof_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier: str,
    blocker: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review

    archive = _public_archive(tmp_path / "source", guided_tier=True)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    run_id = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]["run_id"]

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    before = matrix_path.read_bytes()
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    assert (
        interactive_review(matrix_path, run_id=run_id, tier=tier, repo_root=tmp_path)
        == 2
    )
    output = capsys.readouterr().out
    assert blocker in output
    assert "Model / hardware revision: ALLPOWERS R600 / 0.3" in output
    assert f"Run ID: {run_id}" in output
    assert "Session ID:" in output
    assert "Build fingerprint:" in output
    assert matrix_path.read_bytes() == before


def test_interactive_higher_tier_review_does_not_infer_off_from_on_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.hil.review_wizard import (
        _has_trace_operation,
        _interactive_higher_tier_review,
    )

    def trace(target: bool) -> dict[str, Any]:
        transaction_id = "output-on" if target else "output-off"
        transaction_sha256 = hashlib.sha256(transaction_id.encode()).hexdigest()
        return {
            "kind": "gatt_trace_metadata",
            "observed_route": "local_adapter",
            "complete": True,
            "dropped_events": 0,
            "dropped_frames": 0,
            "frames": [
                {
                    "seq": 1,
                    "event": "write_attempt",
                    "direction": "tx",
                    "command_type": "output_control",
                    "frame_validation": "validated_tx",
                    "session_generation": 3,
                    "frame_sha256": ("a" if target else "b") * 64,
                    "transaction_sha256": transaction_sha256,
                    "output_states": {
                        "ac_output": target,
                        "dc_output": False,
                        "light_output": False,
                    },
                }
            ],
            "events": [
                {
                    "seq": seq,
                    "event": event,
                    "session_generation": 3,
                    "observed_route": "local_adapter",
                    "transaction_kind": "output",
                    "transaction_sha256": transaction_sha256,
                    **(
                        {"transaction_reason": "matching_validated_telemetry"}
                        if event == "transaction_confirmed"
                        else {}
                    ),
                }
                for seq, event in (
                    (1, "write_attempt"),
                    (2, "transaction_submitted"),
                    (3, "transaction_confirmed"),
                )
            ],
        }

    change_sha, restore_sha = "a" * 64, "b" * 64
    guide = {
        "kind": "guided_step_evidence",
        "step_id": "ac_output_cycle",
        "outcome": "pass",
        "initial_state": "off",
        "target_state": "on",
        "restored_state": "off",
        "physical_change_outcome": "correct",
        "physical_restore_outcome": "correct",
        "actions": [
            {
                "outcome": "confirmed",
                "confirmed": True,
                "trace_valid": True,
                "trace_complete": True,
                "route": "local_adapter",
                "session_generation": 3,
                "transaction": {
                    "kind": "output",
                    "session_generation": 3,
                    "transaction_sha256": hashlib.sha256(
                        "output-on".encode()
                    ).hexdigest(),
                    "write_seq": 1,
                    "submitted_seq": 2,
                    "confirmed_seq": 3,
                },
                "trace_evidence_sha256": [change_sha],
            },
            {
                "outcome": "confirmed",
                "confirmed": True,
                "trace_valid": True,
                "trace_complete": True,
                "route": "local_adapter",
                "session_generation": 3,
                "transaction": {
                    "kind": "output",
                    "session_generation": 3,
                    "transaction_sha256": hashlib.sha256(
                        "output-off".encode()
                    ).hexdigest(),
                    "write_seq": 1,
                    "submitted_seq": 2,
                    "confirmed_seq": 3,
                },
                "trace_evidence_sha256": [restore_sha],
            },
        ],
    }
    artifacts = {
        "artifacts": [
            {"artifact_id": "step-01-evidence-01", "content": guide},
            {"source_sha256": change_sha, "content": trace(True)},
            {"source_sha256": restore_sha, "content": trace(False)},
        ]
    }
    traces_by_digest = {change_sha: trace(True), restore_sha: trace(False)}
    assert _has_trace_operation(
        guide["actions"][0],
        traces_by_digest,
        feature="ac_output",
        state="on",
        route="local_adapter",
    )
    assert _has_trace_operation(
        guide["actions"][1],
        traces_by_digest,
        feature="ac_output",
        state="off",
        route="local_adapter",
    )
    execution = {
        "steps": [
            {
                "step_id": "ac_output_cycle",
                "machine_evidence": [{"artifact_id": "step-01-evidence-01"}],
                "authorization_context": "production",
                "authorization_context_source": "runtime_production_authorized",
            }
        ]
    }
    members = {
        "tier-artifacts.json": json.dumps(artifacts).encode(),
        "tier-execution.json": json.dumps(execution).encode(),
    }

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(["reviewer one", "yes", "yes", "yes", "no"])
    prompts: list[str] = []
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    result = _interactive_higher_tier_review(
        tier="silver",
        run={
            "run_id": "archive-example",
            "archive_sha256": "d" * 64,
            "route": "local_adapter",
        },
        members=members,
        repo_root=tmp_path,
    )

    assert result == 2
    assert len(prompts) == 0
    output = capsys.readouterr().out
    assert "both on and off transitions" in output
    assert "No PASS can be recorded" in output
    audit_files = list((tmp_path / "hil-results" / "tier-reviews").glob("*.json"))
    assert audit_files == []


def test_import_review_and_apply_generates_scoped_gold_setting_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archive = _guided_setting_review_archive(tmp_path / "archive")
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run_id = matrix["revisions"][0]["runs"][0]["run_id"]
    imported_run = matrix["revisions"][0]["runs"][0]

    _tty(monkeypatch)
    answers = iter(["independent reviewer", "yes", "yes", "yes", "yes", "APPLY REVIEW"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert (
        interactive_review(matrix_path, run_id=run_id, tier="gold", repo_root=tmp_path)
        == 0
    )
    output = capsys.readouterr().out
    assert "reviewed_capabilities" in output
    assert "Model / hardware revision: ALLPOWERS R600 / 0.3" in output
    assert f"Run ID: {run_id}" in output
    assert f"Session ID: {imported_run['session_id']}" in output
    assert f"Observed route: {imported_run['route']}" in output
    assert f"Firmware: {imported_run['firmware_version']}" in output
    assert f"Build fingerprint: {imported_run['build_fingerprint']}" in output

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run = matrix["revisions"][0]["runs"][0]
    capability = run["write_capabilities"]["eco_mode"]
    assert capability["status"] == "pass"
    assert capability["physical_review"]["outcome"] == "confirmed_pass"
    assert capability["physical_review"]["reviewed_by"] == "independent reviewer"
    assert run["authorization_context"] == "production"
    assert run["authorization_context_source"] == "reviewed_production_session"
    assert any("eco_mode-restoration.json" in item for item in capability["evidence"])
    errors, _ = validate_hil_qualification.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    assert evaluate_tiers(matrix)["tiers"][0]["tier"] == "unqualified"


def test_higher_tier_review_requires_reviewer_independence_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_tier_run

    archive = _guided_setting_review_archive(tmp_path / "archive")
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run = matrix["revisions"][0]["runs"][0]
    review_file = tmp_path / "hil-results" / "not-independent.json"
    review_file.parent.mkdir(parents=True)
    review_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tier": "gold",
                "archive_sha256": run["archive_sha256"],
                "run_id": run["run_id"],
                "reviewed_by": "original HIL operator",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "reviewer_independent_from_operator": False,
                "transitions": [],
                "scenarios": [],
            }
        ),
        encoding="utf-8",
    )

    before = matrix_path.read_bytes()
    assert (
        review_imported_tier_run(
            matrix_path,
            run_id=run["run_id"],
            review_file=review_file,
            tier="gold",
            apply=True,
            repo_root=tmp_path,
        )
        == 2
    )
    assert "did not confirm independence" in capsys.readouterr().out
    assert matrix_path.read_bytes() == before


def test_higher_tier_review_rejects_trace_from_another_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review

    archive = _guided_setting_review_archive(
        tmp_path / "archive", observed_route="active_proxy"
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    matrix_before = matrix_path.read_bytes()
    run_id = json.loads(matrix_before)["revisions"][0]["runs"][0]["run_id"]
    _tty(monkeypatch)

    assert (
        interactive_review(matrix_path, run_id=run_id, tier="gold", repo_root=tmp_path)
        == 2
    )
    assert matrix_path.read_bytes() == matrix_before
    assert "No PASS can be recorded" in capsys.readouterr().out


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
    assert review_output.count('"offline_regression_gate"') == 1
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
            "status_data_decoded",
            "write_ok",
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


def test_interactive_review_imports_reviews_and_qualifies_bronze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(
        [
            "public reviewer",
            "yes",
            "pass",
            "yes",
            "pass",
            "yes",
            "pass",
            "yes",
            "APPLY REVIEW",
        ]
    )
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    from scripts.hil.review import review_imported_run as strict_review

    backend_output: list[str] = []

    def capture_backend(*args: Any, **kwargs: Any) -> int:
        result = strict_review(*args, **kwargs)
        backend_output.append(capsys.readouterr().out)
        return result

    monkeypatch.setattr(
        "scripts.hil.review_wizard.review_imported_run", capture_backend
    )
    assert interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path) == 0
    reviewed = json.loads(matrix_path.read_text())
    run = reviewed["revisions"][0]["runs"][0]
    assert {claim["status"] for claim in run["bronze_evidence"].values()} == {"pass"}
    errors, _ = validate_hil_qualification.validate_matrix(
        reviewed, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    assert evaluate_tiers(reviewed)["tiers"][0]["tier"] == "bronze"
    audit_files = list((tmp_path / "hil-results" / "bronze-reviews").glob("*.json"))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text())
    assert set(audit) == {
        "schema_version",
        "archive_sha256",
        "run_id",
        "reviewed_by",
        "reviewed_at",
        "claims",
    }
    assert len(backend_output) == 2
    assert '"current_attained_tier": "unqualified"' in backend_output[0]
    assert '"attained_tier"' not in backend_output[0]
    assert '"attained_tier": "bronze"' in backend_output[1]


def test_onboard_import_review_stages_the_complete_reviewed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    template_target = repo_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    template_target.parent.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / ".github" / "PULL_REQUEST_TEMPLATE.md",
        template_target,
    )
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    output_dir = tmp_path / "operator-run"
    output_dir.mkdir()
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    answers = iter(
        [
            "yes",
            "APPLY IMPORT",
            "REVIEWER READY",
            *_review_answers(
                reviewer="independent reviewer", inspected=True, apply=True
            ),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert (
        cli._onboard_import_review(
            archive,
            output_dir,
            "bronze",
            repo_root=repo_root,
            matrix_path=matrix_path,
        )
        is True
    )
    output = capsys.readouterr().out
    generated_body = (output_dir / "pr.md").read_text(encoding="utf-8")
    assert "hardware-evidence checklist item is not applicable" not in generated_body
    assert (
        "- [x] Hardware model/revision evidence is attached or linked when required."
        in generated_body
    )
    assert (
        "- [ ] The related issue, if applicable, is linked, and existing issues and PRs were checked to avoid duplicate work."
        in generated_body
    )
    assert "R600, hardware 0.3, firmware 1.1" in generated_body
    assert "observed local_adapter route" in generated_body
    assert "validated status-request TX, a write acknowledgement" in generated_body
    assert (
        "decoded StatusData in the same complete capture and session generation"
        in generated_body
    )
    assert (
        "Two fresh telemetry samples and a plausible operator observation"
        in generated_body
    )
    assert (
        "identity, protocol, and freshness claims were independently reviewed and applied"
        in generated_body
    )
    assert "Telemetry sample interval:" in generated_body
    assert "Observation duration:" not in generated_body
    assert (
        "does not establish that the decoded notification was causally caused by that request"
        in generated_body
    )
    assert (
        "does not qualify output writes or other routes, firmware, or hardware revisions"
        in generated_body
    )
    add_line = next(line.strip() for line in output.splitlines() if "git add " in line)
    stage_paths = shlex.split(add_line)[2:]
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run = matrix["revisions"][0]["runs"][0]
    expected = {
        matrix_path.relative_to(repo_root).as_posix(),
        *run["evidence"],
    }
    assert set(stage_paths) == expected

    subprocess.run(["git", "-C", str(repo_root), "add", *stage_paths], check=True)
    staged = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(staged) == expected

    clean_checkout = tmp_path / "clean-checkout"
    clean_checkout.mkdir()
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "checkout-index",
            "--prefix",
            str(clean_checkout) + "/",
            "-a",
        ],
        check=True,
    )
    clean_matrix = clean_checkout / matrix_path.relative_to(repo_root)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", clean_checkout)
    errors, _ = validate_hil_qualification.validate_matrix(
        json.loads(clean_matrix.read_text(encoding="utf-8")),
        matrix_path=clean_matrix,
        require_pass=False,
        max_age_days=None,
    )
    assert errors == []
    assert all((clean_checkout / reference).is_file() for reference in run["evidence"])


@pytest.mark.parametrize("review_end", ["cancel", "inconclusive"])
def test_prior_scope_bronze_does_not_unlock_unreviewed_imported_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    review_end: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import importer
    from scripts.hil import cli
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    first_archive = _captured_archive(
        tmp_path / "first-source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    import_archive(
        first_archive, matrix_path=matrix_path, apply=True, repo_root=repo_root
    )
    first_run_id = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]["run_id"]
    first_answers = iter(
        _review_answers(reviewer="first reviewer", inspected=True, apply=True)
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(first_answers))
    assert (
        interactive_review(matrix_path, run_id=first_run_id, repo_root=repo_root) == 0
    )

    second_archive = _captured_archive(
        tmp_path / "second-source",
        "local_adapter",
        "1.1",
        "fedcba9876543210",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    second_proposal = importer._build_proposal(
        second_archive, matrix_path, repo_root=repo_root
    )
    second_answers = iter(
        [
            "yes",
            "APPLY IMPORT",
            "REVIEWER READY",
            *_review_answers(
                reviewer="second reviewer",
                inspected=False,
                apply=review_end == "inconclusive",
            ),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(second_answers))

    assert (
        cli._onboard_import_review(
            second_archive,
            tmp_path / "operator-run",
            "bronze",
            repo_root=repo_root,
            matrix_path=matrix_path,
        )
        is False
    )
    output = capsys.readouterr().out
    assert "READY FOR QUALIFICATION PR" not in output
    assert "gh pr create" not in output
    assert "git add " not in output

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    second_run = next(
        run
        for revision in matrix["revisions"]
        for run in revision["runs"]
        if run["run_id"] == second_proposal["run_id"]
    )
    assert evaluate_tiers(matrix)["tiers"][0]["tier"] == "bronze"
    assert second_run.get("qualification_status") != "pass"
    if review_end == "inconclusive":
        assert {
            claim["status"] for claim in second_run["bronze_evidence"].values()
        } == {"inconclusive"}


def test_interactive_review_cancel_keeps_matrix_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]
    before = matrix_path.read_bytes()

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(["reviewer", "no", "cancel"])
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path) == 2
    assert matrix_path.read_bytes() == before


def test_interactive_review_requires_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hil.api import HAAPIError
    from scripts.hil.review_wizard import interactive_review

    class Pipe:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", Pipe())
    with pytest.raises(HAAPIError, match="requires a TTY"):
        interactive_review(
            tmp_path / "missing.json", run_id="archive-test", repo_root=tmp_path
        )


def test_interactive_review_rejects_invalid_reviewer_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.api import HAAPIError
    from scripts.hil.review_wizard import interactive_review

    archive = _captured_archive(
        tmp_path / "source", "local_adapter", "1.1", "0123456789abcdef"
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]
    before = matrix_path.read_bytes()

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "   ")
    with pytest.raises(HAAPIError, match="public identifier"):
        interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path)
    assert matrix_path.read_bytes() == before
    assert not (tmp_path / "hil-results" / "bronze-reviews").exists()


@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_interactive_cli_rejects_legacy_mode_flags_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from scripts.hil.cli import main

    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text('{"unchanged": true}\n', encoding="utf-8")
    before = matrix_path.read_bytes()
    monkeypatch.setattr(
        "scripts.hil.review_wizard.interactive_review",
        lambda *_args, **_kwargs: pytest.fail("interactive wizard must not start"),
    )
    assert (
        main(
            [
                "review",
                "--matrix",
                str(matrix_path),
                "--run-id",
                "archive-test",
                "--interactive",
                mode,
            ]
        )
        == 2
    )
    assert matrix_path.read_bytes() == before


def test_nonreviewable_interactive_archive_stays_inconclusive_after_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review

    archive = _captured_archive(
        tmp_path / "source", "local_adapter", "1.1", "0123456789abcdef"
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(["reviewer", "yes", "APPLY REVIEW"])
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path) == 0
    run = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    assert {item["status"] for item in run["bronze_evidence"].values()} == {
        "inconclusive"
    }
    output = capsys.readouterr().out
    assert "Preflight blockers:" in output
    assert "installed build is not verified" in output
    assert "tests/hil/fixtures/r600-hw03/runs/" in output


def test_interactive_review_conflicting_generated_artifact_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.api import HAAPIError
    from scripts.hil.review_wizard import interactive_review

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    revision = json.loads(matrix_path.read_text())["revisions"][0]
    run = revision["runs"][0]
    run_id = run["run_id"]
    conflict = (
        tmp_path
        / "tests/hil/fixtures"
        / revision["id"]
        / "runs"
        / run_id
        / "bronze-fresh_telemetry-claim.json"
    )
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text("conflicting local evidence\n", encoding="utf-8")
    matrix_before = matrix_path.read_bytes()
    conflict_before = conflict.read_bytes()

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(
        ["reviewer", "yes", "pass", "yes", "pass", "yes", "pass", "yes", "APPLY REVIEW"]
    )
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    with pytest.raises(HAAPIError, match="Review evidence path conflicts"):
        interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path)
    assert matrix_path.read_bytes() == matrix_before
    assert conflict.read_bytes() == conflict_before
    assert not (conflict.parent / "bronze-identity-claim.json").exists()
    assert not (conflict.parent / "bronze-protocol-capture.jsonl").exists()
    assert not (conflict.parent / "bronze-protocol_probe-claim.json").exists()


def test_interactive_review_does_not_report_apply_before_matrix_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.api import HAAPIError
    from scripts.hil import review as review_module
    from scripts.hil.review_wizard import interactive_review

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]
    matrix_before = matrix_path.read_bytes()
    atomic_write = review_module._atomic_write

    def fail_matrix_write(path: Path, content: bytes) -> None:
        if path == matrix_path:
            raise HAAPIError("simulated matrix persistence failure")
        atomic_write(path, content)

    monkeypatch.setattr(review_module, "_atomic_write", fail_matrix_write)

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(
        ["reviewer", "yes", "pass", "yes", "pass", "yes", "pass", "yes", "APPLY REVIEW"]
    )
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    with pytest.raises(HAAPIError, match="simulated matrix persistence failure"):
        interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path)
    output = capsys.readouterr().out
    assert '"applied": true' not in output
    assert matrix_path.read_bytes() == matrix_before
    run_dir = tmp_path / "tests/hil/fixtures/r600-hw03/runs" / run_id
    assert not list(run_dir.glob("bronze-*-claim.json"))
    assert not (run_dir / "bronze-protocol-capture.jsonl").exists()


def test_interactive_review_repeat_apply_keeps_evidence_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    def run_wizard() -> None:
        answers = iter(
            [
                "reviewer",
                "yes",
                "pass",
                "yes",
                "pass",
                "yes",
                "pass",
                "yes",
                "APPLY REVIEW",
            ]
        )
        monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
        assert interactive_review(matrix_path, run_id=run_id, repo_root=tmp_path) == 0

    run_wizard()
    first = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    evidence_after_first = list(first["evidence"])
    run_wizard()
    second = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    assert len(second["evidence"]) == len(set(second["evidence"]))
    assert set(evidence_after_first) == set(second["evidence"])
    assert {item["status"] for item in second["bronze_evidence"].values()} == {"pass"}
    errors, _ = validate_hil_qualification.validate_matrix(
        json.loads(matrix_path.read_text()),
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert errors == []


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_interactive_review_interruption_never_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.cli import main
    from scripts.hil.review_wizard import interactive_review as run_interactive_review

    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    run_id = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]["run_id"]
    before = matrix_path.read_bytes()

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    answers = iter(["reviewer", "yes", "inconclusive", "inconclusive", "inconclusive"])
    interruption_reached: list[bool] = []

    def interrupted_input(_prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration as exc:
            interruption_reached.append(True)
            raise interruption from exc

    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", interrupted_input)

    def use_test_repository(matrix: Path, *, run_id: str) -> int:
        return run_interactive_review(matrix, run_id=run_id, repo_root=tmp_path)

    monkeypatch.setattr(
        "scripts.hil.review_wizard.interactive_review", use_test_repository
    )
    assert (
        main(
            [
                "review",
                "--matrix",
                str(matrix_path),
                "--run-id",
                run_id,
                "--interactive",
            ]
        )
        == 2
    )
    assert interruption_reached == [True]
    assert matrix_path.read_bytes() == before
    run_dir = tmp_path / "tests/hil/fixtures/r600-hw03/runs" / run_id
    assert not list(run_dir.glob("bronze-*-claim.json"))
