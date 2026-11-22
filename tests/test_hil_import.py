"""Import public HIL archives as scoped, inconclusive review evidence."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any
import zipfile

import pytest

from scripts.hil.api import HAAPIError, ReadOnlyRefreshButton
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
    path: Path,
    *,
    observed_route: str = "local_adapter",
    safe_disabled_car_charger: bool = False,
) -> Path:
    """Build a synthetic public archive with one complete settings cycle."""
    from scripts.hil.evidence import _public_tier_artifact, _public_tier_plan

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
        "schema_version": 1,
        "run_id": source_run_id,
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
    public_execution = {
        key: value for key, value in execution.items() if key != "run_id"
    }
    public_execution["schema_version"] = 2
    member_updates = {
        "tier-execution.json": (
            json.dumps(public_execution, sort_keys=True) + "\n"
        ).encode(),
        "tier-artifacts.json": (
            json.dumps({"schema_version": 2, "artifacts": artifacts}, sort_keys=True)
            + "\n"
        ).encode(),
    }
    if safe_disabled_car_charger:
        execution["steps"].append(
            {
                "step_id": "car_charger_reversal",
                "outcome": "pending_review",
                "reason": "optional control is safely disabled; pending explicit reviewed exclusion",
                "safe_disable_proof": {
                    "runtime_option_enabled": False,
                    "normal_control_available": False,
                },
                "machine_evidence_sha256": [],
                "machine_evidence": [],
            }
        )
        public_execution["steps"] = execution["steps"]
        member_updates["tier-execution.json"] = (
            json.dumps(public_execution, sort_keys=True) + "\n"
        ).encode()
        _manifest, public_members, _archive_digest = verify_public_archive(
            archive, include_contents=True
        )
        session = json.loads(public_members["session.json"])
        session_data = session["data"]
        session_data["runtime_capabilities"]["capabilities"]["car_charger_enabled"] = (
            False
        )
        session_data["runtime_capabilities"].update(
            available=True,
            schema_version=2,
            verified=True,
            identity_conflicted=False,
            model="R600",
            hardware_revision="0.3",
            firmware_revision="1.1",
            connected=True,
            settings_fresh=True,
            pending_transaction=False,
            session_generation=1,
            features={
                "car_charger": {
                    "physical_presence": "unknown",
                    "runtime_ready": True,
                }
            },
        )
        session_data["controls"] = {
            "car_charger": {
                "available": False,
                "availability_verified": True,
            }
        }
        session_data["test_plan"] = {
            "schema_version": 1,
            "requested_tier": "gold",
            "execution_tier": "gold",
            "attained_tier": None,
            "review_state": "pending",
            "device": {
                "model": "R600",
                "hardware_revision": "0.3",
                "firmware_revision": "1.1",
                "verified_route": "local_adapter",
            },
            "operator_actions_estimate": 1,
            "restoration_plan": "Restore each changed control to its original state.",
            "steps": [
                {
                    "id": "car_charger_reversal",
                    "feature": "car_charger",
                    "action": "exclude",
                    "eligibility": "review_required",
                    "physical_presence": "unknown",
                    "outcome": "pending",
                    "human_review": "pending",
                    "reasons": [
                        "optional control is disabled by policy and its normal HA control is unavailable; explicit review is required"
                    ],
                    "required_evidence": [],
                    "safe_disable_proof": {
                        "runtime_option_enabled": False,
                        "normal_control_available": False,
                    },
                }
            ],
        }
        session_data["test_plan"] = _public_tier_plan(session_data["test_plan"])
        public_execution = json.loads(member_updates["tier-execution.json"])
        public_artifacts = json.loads(member_updates["tier-artifacts.json"])
        captures = _manifest["captures"]
        route = (
            captures["by_capture"][0]["route"]
            if len({item["route"] for item in captures["by_capture"]}) == 1
            else "mixed"
            if captures["by_capture"]
            else "unverified"
        )
        from scripts.hil.evidence import _render_report

        member_updates["session.json"] = (
            json.dumps(session, sort_keys=True) + "\n"
        ).encode()
        member_updates["report.md"] = _render_report(
            session_data["run_id"],
            json.loads(public_members["environment.json"]),
            session,
            json.loads(public_members["scenarios.json"]),
            captures["events"],
            captures["count"],
            route,
            public_execution,
            public_artifacts,
            json.loads(public_members["tier-bronze-read-only.json"]),
            captures["by_capture"],
        )
    return _rewrite_capture_archive(
        archive,
        path / "guided-settings.zip",
        capture_updates={},
        member_updates=member_updates,
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
    answers = ["2", reviewer, "yes" if inspected else "no", "yes"]
    if inspected:
        for _ in range(3):
            answers.extend(("pass", "yes"))
    answers.append("APPLY REVIEW" if apply else "cancel")
    return answers


def _operator_review_answers(*, reviewer: str, apply: bool) -> list[str]:
    answers = [reviewer, "yes"]
    for _ in range(3):
        answers.extend(("pass", "yes"))
    answers.append("APPLY REVIEW" if apply else "cancel")
    return answers


def _selected_independent_review_answers(*, reviewer: str, apply: bool) -> list[str]:
    answers = [reviewer, "yes", "yes"]
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
    stale_rest_fallback: bool = False,
    scenario_progress: bool = True,
    guided_bronze_execution: bool = False,
    started_at: str | None = None,
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
        "started_at": started_at or "2026-11-09T12:00:00+00:00",
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
    if scenario_progress:
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
            capture_bound_probe = stale_rest_fallback or guided_bronze_execution
            sequence_offset = int(capture_bound_probe)
            status_frame = {
                **common,
                "seq": 2 + sequence_offset,
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
                "seq": 3 + sequence_offset,
                "utc": stamp,
                "elapsed_ms": 2.0,
                "event": "notification_received",
            }
            write_completion_event = {
                **common,
                "seq": 5 + sequence_offset,
                "utc": stamp,
                "elapsed_ms": 4.0,
                "event": bronze_write_completion,
                "transaction_id": "bronze-tx-1",
            }
            decoded_event = {
                **common,
                "seq": 4 + sequence_offset,
                "utc": stamp,
                "elapsed_ms": 3.0,
                "event": "notification_decoded",
                "decoded_packet_types": list(bronze_decoded_packet_types),
            }
            status_measurement = {
                "battery": 50,
                "input_power": 0,
                "output_power": 0,
                "remaining_time": 0,
            }
            if capture_bound_probe:
                decoded_event["status_measurement"] = status_measurement
            trace["events"] = [
                route_event,
                *(
                    [
                        {
                            **common,
                            "seq": 2,
                            "utc": stamp,
                            "elapsed_ms": 0.5,
                            "event": "capture_started",
                            "capture_id": trace["capture_id"],
                        }
                    ]
                    if capture_bound_probe
                    else []
                ),
                {**status_frame, "direction": "write"},
                notification_event,
                decoded_event,
                *(
                    [write_completion_event]
                    if bronze_write_completion != "missing"
                    else []
                ),
                *(
                    [
                        {
                            **common,
                            "seq": 7,
                            "utc": stamp,
                            "elapsed_ms": 5.0,
                            "event": "capture_stopped",
                            "capture_id": trace["capture_id"],
                            "session_generation": None,
                            "observed_route": "unverified",
                        }
                    ]
                    if capture_bound_probe
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
            sample_time = datetime.now(UTC)
            sample = {
                "sampled_at_utc": (
                    (
                        sample_time - timedelta(seconds=1)
                        if capture_bound_probe
                        else sample_time
                    ).isoformat()
                ),
                "telemetry": {
                    "battery": {
                        "value": 50,
                        "verified": True,
                        "source": "ha_rest_entity_state",
                        "sample_age_seconds": 815 if stale_rest_fallback else 5,
                    },
                    "telemetry_available": {"value": "on", "verified": True},
                },
            }
            second_sample = {
                **sample,
                "sampled_at_utc": (
                    (
                        sample_time + timedelta(seconds=1)
                        if capture_bound_probe
                        else sample_time
                    ).isoformat()
                ),
                "telemetry": {
                    key: (
                        {**record, "sample_age_seconds": 816}
                        if key == "battery" and stale_rest_fallback
                        else record
                    )
                    for key, record in sample["telemetry"].items()
                },
            }
            status_freshness = (
                {
                    "capture_id": trace["capture_id"],
                    "session_generation": generation,
                    "status_notification_count_before": 0,
                    "status_notification_count_after": 1,
                    "decoded_response_seq": decoded_event["seq"],
                    "decoded_response_at_utc": stamp,
                    "capture_started_seq": 2,
                    "capture_stopped_seq": 7,
                    "status_measurement": status_measurement,
                }
                if capture_bound_probe
                else None
            )
            (private / "tier-bronze-read-only.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "step_id": "read_only_probe",
                        "sample_sha256": [
                            hashlib.sha256(
                                json.dumps(item, sort_keys=True).encode()
                            ).hexdigest()
                            for item in (sample, second_sample)
                        ],
                        "samples": [sample, second_sample],
                        "fresh_verified_samples": True,
                        "status_freshness": status_freshness,
                        "protocol_probe": {
                            "outcome": bronze_protocol_outcome,
                            "trace": "traces/bronze_read_only",
                            "trace_sha256": hashlib.sha256(
                                (bronze_trace / "trace.json").read_bytes()
                            ).hexdigest(),
                            "route": observed_route,
                            "session_generation": generation,
                            "status_request_seq": status_frame["seq"],
                            "status_write_ok_seq": write_completion_event["seq"],
                            "decoded_response_seq": decoded_event["seq"],
                            "status_request_hex": "a565b1000106010000000000",
                        },
                        "physical_observation": "plausible",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            if guided_bronze_execution:
                bronze_path = private / "tier-bronze-read-only.json"
                bronze_digest = hashlib.sha256(bronze_path.read_bytes()).hexdigest()
                bronze_artifact = json.loads(bronze_path.read_text(encoding="utf-8"))
                bronze_outcome = (
                    "pass"
                    if (
                        bronze_protocol_outcome == "pass"
                        and bronze_write_completion == "write_ok"
                        and "StatusData" in bronze_decoded_packet_types
                        and bronze_artifact.get("fresh_verified_samples") is True
                        and bronze_artifact.get("physical_observation") == "plausible"
                    )
                    else "inconclusive"
                )
                (private / "tier-execution.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "requested_tier": "bronze",
                            "execution_tier": "bronze",
                            "review_state": "pending",
                            "attained_tier": None,
                            "steps": [
                                {
                                    "step_id": step_id,
                                    "outcome": bronze_outcome,
                                    "machine_evidence": [
                                        {
                                            "path": bronze_path.name,
                                            "sha256": bronze_digest,
                                        }
                                    ],
                                    "human_review": "pending",
                                }
                                for step_id in (
                                    "read_only_probe",
                                    "physical_plausibility_observation",
                                )
                            ],
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
    return export_archive(path / f"run-{run_id}", confirmed=True)


def _standard_tier_suite_archive(
    path: Path,
    *,
    run_id: str = "abcdef0123456789",
    started_at: str = "2026-11-09T12:00:00+00:00",
) -> Path:
    """Export schema-realistic Bronze, Silver, Gold, and reconnect fixtures."""
    from scripts.hil.cli import _reviewable_tier_step_artifact
    from scripts.hil.evidence import export_archive
    from scripts.hil.runner import correlate_transaction

    route = "local_adapter"
    source_root = path / "source"
    archive = _captured_archive(
        source_root,
        route,
        "1.1",
        run_id,
        bronze_reviewable=True,
        started_at=started_at,
    )
    del archive
    parent_root = source_root / f"run-{run_id}"
    parent_private = parent_root / "private"
    environment = json.loads((parent_private / "environment.json").read_text())
    device = {
        "model": "ALLPOWERS R600",
        "hardware_version": "0.3",
        "firmware_version": "1.1",
    }
    execution: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "requested_tier": "gold",
        "execution_tier": "gold",
        "requested_route": route,
        "device": device,
        "build_provenance": {},
        "review_state": "pending",
        "steps": [],
    }
    generation = 7
    action_counter = 0

    def feature_frame(feature: str, state: Any) -> bytes:
        if feature in {"ac_output", "dc_output", "light_output"}:
            mask = {"ac_output": 0x02, "dc_output": 0x01, "light_output": 0x20}[feature]
            frame = bytearray.fromhex("a56500b10101000000")
            frame[7] = mask if state == "on" else 0
        else:
            frame = bytearray.fromhex("a56500b1010202000100")
            # Match the production encoder: unrelated reserved flags survive
            # every settings update and remain part of the checksummed frame.
            frame[7] = 0xA0
            if feature == "eco_mode":
                frame[7] |= 1 if state is True or state == "on" else 0
            elif feature == "work_mode":
                frame[7] |= {"mute": 0, "standard": 1, "fast": 2}.get(state, state) << 1
            elif feature == "car_charger":
                frame[7] |= 0x10 if state is True or state == "on" else 0
            elif feature == "eco_timeout":
                frame[8] = {
                    "one_hour": 1,
                    "two_hours": 2,
                    "four_hours": 4,
                    "six_hours": 6,
                }.get(state, state)
        checksum = 0
        for byte in frame[:-1]:
            checksum ^= byte
        frame[-1] = checksum
        return bytes(frame)

    def add_action(
        child_private: Path,
        child_id: str,
        scenario_id: str,
        feature: str,
        target: Any,
    ) -> dict[str, Any]:
        nonlocal action_counter
        action_counter += 1
        transaction_id = f"synthetic-tier-action-{action_counter}"
        stamp = f"2026-11-09T12:00:{action_counter:02d}+00:00"
        common = {
            "schema_version": 2,
            "run_id": child_id,
            "scenario_id": scenario_id,
            "layer": "gatt_api",
            "session_generation": generation,
            "observed_route": route,
        }
        route_event = {
            **common,
            "seq": 1,
            "utc": stamp,
            "elapsed_ms": 0.0,
            "event": "route_observed",
            "backend_kind": "bluez",
            "evidence": "bluez_device_path",
            "adapter_id": "hci0",
        }
        write_event = {
            **common,
            "seq": 2,
            "utc": stamp,
            "elapsed_ms": 100.0,
            "event": "write_attempt",
            "transaction_kind": (
                "output"
                if feature in {"ac_output", "dc_output", "light_output"}
                else "settings"
            ),
            "transaction_id": transaction_id,
        }
        submitted_event = {
            **common,
            "seq": 3,
            "utc": stamp,
            "elapsed_ms": 200.0,
            "event": "transaction_submitted",
            "transaction_kind": write_event["transaction_kind"],
            "transaction_id": transaction_id,
        }
        confirmed_event = {
            **common,
            "seq": 4,
            "utc": stamp,
            "elapsed_ms": 300.0,
            "event": "transaction_confirmed",
            "transaction_kind": write_event["transaction_kind"],
            "transaction_id": transaction_id,
            "reason": "matching_validated_telemetry",
        }
        frame = {
            **write_event,
            "direction": "tx",
            "command_type": (
                "output_control"
                if feature in {"ac_output", "dc_output", "light_output"}
                else "settings_control"
            ),
            "frame_validation": "validated_tx",
            "hex": feature_frame(feature, target).hex(),
        }
        events = [route_event, write_event, submitted_event, confirmed_event]
        trace = {
            "schema_version": 2,
            "capture_scope": "runtime_entry",
            "capture_id": f"synthetic-{child_id}-{action_counter}",
            "run_id": child_id,
            "scenario_id": scenario_id,
            "requested_route": route,
            "capture_layer": "gatt_api_not_rf",
            "private_raw_bytes": True,
            "has_gatt_frames": True,
            "closed": True,
            "expired": False,
            "termination_reason": "requested_stop",
            "qualification_allowed": False,
            "observed_route": route,
            "complete": True,
            "dropped_events": 0,
            "dropped_frames": 0,
            "event_count": len(events),
            "frame_count": 1,
            "events": events,
            "frames": [frame],
            "route_observations": [
                {
                    "session_generation": generation,
                    "seq": 1,
                    "utc": stamp,
                    "elapsed_ms": 0.0,
                    "observed_route": route,
                    "backend_kind": "bluez",
                    "evidence": "bluez_device_path",
                    "adapter_id": "hci0",
                }
            ],
        }
        trace_name = f"traces/action_{action_counter}"
        trace_dir = child_private / trace_name
        trace_dir.mkdir(parents=True)
        _save_private_trace(trace_dir, trace)
        transaction = correlate_transaction(
            trace,
            expected_generation=generation,
            kind=write_event["transaction_kind"],
        )
        assert transaction is not None
        return {
            "target": feature,
            "outcome": "pass",
            "trace": trace_name,
            "session_generation": generation,
            "route": route,
            "transaction": transaction,
            "physical_outcome": "yes",
            "step_id": f"action_{action_counter}",
        }

    def add_feature_step(
        step_id: str,
        scenario_id: str,
        feature: str,
        actions: list[tuple[Any, Any, Any]],
    ) -> None:
        child_id = f"{len(execution['steps']) + 1:016x}"
        child_private = write_run(
            source_root,
            f"tier_{child_id}",
            environment,
            {
                "run_id": child_id,
                "parent_tier_run_id": run_id,
                "tier_step_id": step_id,
                "requested_route": route,
                "device": device,
            },
        )
        checkpoint_rows = []
        scenario_step_ids = {
            "output_ac_cycle": [
                "ac_on",
                "ac_off",
                "ac_on_second_cycle",
                "ac_final_restore",
            ],
            "output_dc_cycle": [
                "dc_on",
                "dc_off",
                "dc_on_second_cycle",
                "dc_final_restore",
            ],
            "output_light_cycle": [
                "light_on",
                "light_off",
                "light_on_second_cycle",
                "light_final_restore",
            ],
            "settings_eco_mode": ["eco_mode_on"],
            "settings_work_mode": ["work_mode_standard"],
            "settings_eco_timeout": ["eco_timeout_two_hours"],
            "output_car_charger_cycle": ["car_charger_on", "car_charger_off"],
        }
        for action_index, (before, target, after) in enumerate(actions):
            row = add_action(child_private, child_id, scenario_id, feature, target)
            row.update(pre_state=before, expected=target, post_state=after)
            row["step_id"] = scenario_step_ids[scenario_id][
                min(action_index, len(scenario_step_ids[scenario_id]) - 1)
            ]
            checkpoint_rows.append(row)
        write_scenario_progress(
            child_private,
            child_id,
            scenario_id,
            "a" * 64,
            checkpoint_rows,
        )
        review_artifact = _reviewable_tier_step_artifact(
            parent_private=parent_private,
            child_private=child_private,
            step_id=step_id,
            feature=feature,
            checkpoint_rows=checkpoint_rows,
        )
        assert review_artifact is not None
        review_name = f"tier-{step_id}.json"
        (parent_private / review_name).write_text(
            json.dumps(review_artifact, sort_keys=True), encoding="utf-8"
        )
        machine_evidence = []
        for relative in [
            "checkpoint.json",
            *(f"{row['trace']}/trace.json" for row in checkpoint_rows),
        ]:
            source = child_private / relative
            machine_evidence.append(
                {
                    "path": f"../../tier_{child_id}/private/{relative}",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
        review_path = parent_private / review_name
        machine_evidence.append(
            {
                "path": review_name,
                "sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            }
        )
        execution["steps"].append(
            {
                "step_id": step_id,
                "scenario_id": scenario_id,
                "outcome": "pass",
                "observed_routes": [route],
                "human_review": "pending",
                "authorization_context": "production",
                "authorization_context_source": "runtime_production_authorized",
                "machine_evidence": machine_evidence,
                "evidence_run": f"../../tier_{child_id}/private",
            }
        )

    for feature, step_id, scenario_id in (
        ("ac_output", "ac_output_cycle", "output_ac_cycle"),
        ("dc_output", "dc_output_cycle", "output_dc_cycle"),
        ("light_output", "light_output_cycle", "output_light_cycle"),
    ):
        add_feature_step(
            step_id,
            scenario_id,
            feature,
            [
                ("off", "on", "on"),
                ("on", "off", "off"),
                ("off", "on", "on"),
                ("on", "off", "off"),
            ],
        )
    for feature, step_id, scenario_id, initial, target in (
        ("eco_mode", "eco_mode_reversal", "settings_eco_mode", "off", "on"),
        ("work_mode", "work_mode_reversal", "settings_work_mode", "mute", "fast"),
        (
            "eco_timeout",
            "eco_timeout_reversal",
            "settings_eco_timeout",
            "one_hour",
            "four_hours",
        ),
        (
            "car_charger",
            "car_charger_reversal",
            "output_car_charger_cycle",
            "off",
            "on",
        ),
    ):
        add_feature_step(
            step_id,
            scenario_id,
            feature,
            [(initial, target, target), (target, initial, initial)],
        )

    reconnect_id = "0000000000000008"
    reconnect_private = write_run(
        source_root,
        f"tier_{reconnect_id}",
        environment,
        {
            "run_id": reconnect_id,
            "parent_tier_run_id": run_id,
            "tier_step_id": "basic_reconnect",
            "requested_route": route,
            "device": device,
        },
    )
    from unittest.mock import patch

    from custom_components.allpowers_ble.hil_trace import Capture
    from custom_components.allpowers_ble.protocol.codec import encode_status_request
    from custom_components.allpowers_ble.route_attribution import ConnectedRoute
    from scripts.hil import cli as hil_cli
    from scripts.hil.discovery import Device

    class RunnerGeneratedReconnectHA:
        origin = "https://ha.example.test"

        def __init__(self) -> None:
            self.capture: Capture | None = None
            self.capability_calls = 0

        def hil_capabilities(self, entry_id: str) -> dict[str, Any]:
            self.capability_calls += 1
            disconnected = self.capability_calls == 2
            current_generation = (
                generation + 1 if self.capability_calls >= 3 else generation
            )
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": not disconnected,
                "status_fresh": not disconnected,
                "session_generation": current_generation,
            }

        def request_read_only_refresh(self, button: ReadOnlyRefreshButton) -> None:
            assert button.entity_id == "button.station_refresh"
            assert self.capture is not None
            txid = "runner-generated-status-request"
            self.capture.record(
                "write_attempt",
                frame=encode_status_request(),
                direction="tx",
                session_generation=generation + 1,
                transaction_id=txid,
            )
            self.capture.record(
                "write_ok", session_generation=generation + 1, transaction_id=txid
            )
            self.capture.record(
                "notification_decoded",
                session_generation=generation + 1,
                decoded_packet_types=["StatusData"],
            )

        def websocket(self, commands: list[dict[str, Any]]) -> list[Any]:
            command = commands[0]
            if command["type"].endswith("/start"):
                self.capture = Capture(
                    "entry-safe", "operator", reconnect_id, "recovery_reconnect", route
                )
                self.capture.record(
                    "capture_started", session_generation=generation + 1
                )
                self.capture.observe_route(
                    generation + 1,
                    ConnectedRoute(
                        "local_adapter", "bluez", "bluez_device_path", adapter_id="hci0"
                    ),
                )
                return [
                    {
                        "schema_version": 2,
                        "capture_id": self.capture.capture_id,
                        "ttl_seconds": 120,
                    }
                ]
            if command["type"].endswith("/fetch"):
                assert self.capture is not None
                return [self.capture.snapshot()]
            assert self.capture is not None
            self.capture.stop()
            return [self.capture.snapshot()]

    api_fake = RunnerGeneratedReconnectHA()
    reconnect_device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="1.1",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
        refresh_button=ReadOnlyRefreshButton(
            entity_id="button.station_refresh",
            unique_id="private_station_refresh",
            platform="allpowers_ble",
            device_id="private-device",
            config_entry_id="entry-safe",
        ),
    )
    with (
        patch("builtins.input", side_effect=("1", "1", "1")),
        patch("scripts.hil.cli.sleep", return_value=None),
    ):
        assert (
            hil_cli._execute_recovery_suite(
                api_fake,
                argparse.Namespace(
                    non_interactive=False,
                    route=route,
                    guided_reconnect_required=True,
                ),
                reconnect_device,
                reconnect_private,
                reconnect_id,
            )
            == 0
        )
    reconnect_checkpoint = json.loads(
        (reconnect_private / "checkpoint.json").read_text(encoding="utf-8")
    )
    recovered_step = next(
        row
        for row in reconnect_checkpoint["steps"]
        if row.get("step_id") == "recovered_session"
    )
    reconnect_trace = recovered_step["trace"]
    reconnect_evidence = []
    for relative in ("checkpoint.json", f"{reconnect_trace}/trace.json"):
        source = reconnect_private / relative
        reconnect_evidence.append(
            {
                "path": f"../../tier_{reconnect_id}/private/{relative}",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
    execution["steps"].append(
        {
            "step_id": "basic_reconnect",
            "scenario_id": "recovery_reconnect",
            "outcome": "pass",
            "observed_routes": [route],
            "human_review": "pending",
            "machine_evidence": reconnect_evidence,
            "evidence_run": f"../../tier_{reconnect_id}/private",
        }
    )
    (parent_private / "tier-execution.json").write_text(
        json.dumps(execution, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return export_archive(parent_root, confirmed=True)


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
                "schema_version": 2,
                "assurance": "independently_reviewed",
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
                "final_restore_frame_evidence": reference(1, False),
                "change_trace_sha256": ["b" * 64],
                "restore_trace_sha256": ["c" * 64],
                "final_restore_trace_sha256": ["c" * 64],
                "final_restore_state": False,
                "physical_change_confirmed": True,
                "physical_restoration_confirmed": True,
                "final_restoration_confirmed": True,
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
        "event_count": 2,
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
            },
            {
                "seq": 7,
                "event": "notification_decoded",
                "utc": "2026-11-09T12:00:02+00:00",
                "elapsed_ms": 20.0,
                "session_generation": 2,
                "observed_route": "local_adapter",
                "decoded_packet_types": ["StatusData"],
            },
        ],
        "frames": [frame],
    }
    bundle = {
        "artifacts": [
            {
                "artifact_id": "step-01-evidence-01",
                "source_sha256": trace_sha,
                "content": trace,
            },
            {
                "artifact_id": "scenario-checkpoint",
                "source_sha256": "f" * 64,
                "content": {
                    "kind": "scenario_checkpoint",
                    "scenario_id": "recovery_reconnect",
                    "steps": [
                        {
                            "step_id": "recovered_session",
                            "outcome": "pass",
                            "observation": {
                                "session_generation_before": 1,
                                "session_generation_after": 2,
                                "fresh_status_telemetry": True,
                                "trace_complete": True,
                                "route_verified": True,
                                "route": "local_adapter",
                            },
                        }
                    ],
                },
            },
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
                    },
                    {
                        "artifact_id": "scenario-checkpoint",
                        "source_sha256": "f" * 64,
                    },
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
    trace["events"] = trace["events"][:1]
    trace["event_count"] = 1
    (tmp_path / "tier-artifacts.json").write_text(json.dumps(bundle))
    assert not validation._has_bound_capture(
        evidence,
        **scope,
        result_key="reconnect_failover",
        capability=False,
        expected_verdict="ok",
    )
    trace["events"] = [
        {
            **trace["events"][0],
            "seq": 4,
            "event": "notification_decoded",
            "decoded_packet_types": ["StatusData"],
        },
        {
            **trace["events"][0],
            "seq": 6,
            "event": "write_ok",
        },
    ]
    trace["event_count"] = 2
    (tmp_path / "tier-artifacts.json").write_text(json.dumps(bundle))
    assert not validation._has_bound_capture(
        evidence,
        **scope,
        result_key="reconnect_failover",
        capability=False,
        expected_verdict="ok",
    )
    trace["events"] = [
        {
            **trace["events"][1],
            "seq": 6,
            "event": "write_ok",
        },
        {
            **trace["events"][0],
            "seq": 7,
            "event": "notification_decoded",
        },
    ]
    checkpoint = bundle["artifacts"][1]["content"]["steps"][0]["observation"]
    checkpoint["session_generation_before"] = 2
    (tmp_path / "tier-artifacts.json").write_text(json.dumps(bundle))
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
    answers = iter(["1", "HIL operator", "yes", "yes", "yes", "APPLY REVIEW"])
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
    assert capability["physical_review"]["reviewed_by"] == "HIL operator"
    assert capability["physical_review"]["assurance"] == "operator_attested"
    assert run["authorization_context"] == "production"
    assert run["authorization_context_source"] == "reviewed_production_session"
    assert any("eco_mode-restoration.json" in item for item in capability["evidence"])
    errors, _ = validate_hil_qualification.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    assert evaluate_tiers(matrix)["tiers"][0]["tier"] == "unqualified"


def test_reviewed_safe_disabled_car_charger_is_excluded_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archive = _guided_setting_review_archive(
        tmp_path / "archive", safe_disabled_car_charger=True
    )
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run_id = matrix["revisions"][0]["runs"][0]["run_id"]

    _tty(monkeypatch)
    answers = iter(["1", "HIL operator", "yes", "yes", "yes", "yes", "APPLY REVIEW"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert (
        interactive_review(matrix_path, run_id=run_id, tier="gold", repo_root=tmp_path)
        == 0
    )

    reviewed = json.loads(matrix_path.read_text(encoding="utf-8"))
    contract = reviewed["revisions"][0]["feature_contract"]["car_charger"]
    assert contract["status"] == "excluded"
    assert contract["safely_disabled"] is True
    assert "excluded/unverified" in contract["rationale"]
    errors, _ = validate_hil_qualification.validate_matrix(
        reviewed,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert errors == []
    tier = evaluate_tiers(reviewed)["tiers"][0]
    assert tier["tier"] != "gold"
    assert any(
        "car_charger (excluded/unverified" in item for item in tier["exclusions"]
    )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("verified", "Invalid public runtime capabilities"),
        ("features", "Invalid public runtime capabilities"),
        ("hardware_revision", "Invalid public runtime capabilities"),
    ],
)
def test_public_verifier_rejects_invalid_identity_runtime_schema(
    tmp_path: Path, mutation: str, expected_error: str
) -> None:
    archive = _guided_setting_review_archive(
        tmp_path / "source", safe_disabled_car_charger=True
    )
    with zipfile.ZipFile(archive) as source:
        public_session = json.loads(source.read("public/session.json"))
    runtime = public_session["data"]["runtime_capabilities"]
    if mutation == "verified":
        runtime["verified"] = "yes"
    elif mutation == "features":
        runtime["features"]["car_charger"]["unexpected"] = True
    else:
        runtime["hardware_revision"] = None
    rewritten = _rewrite_capture_archive(
        archive,
        tmp_path / f"invalid-identity-runtime-{mutation}.zip",
        capture_updates={},
        member_updates={
            "session.json": (json.dumps(public_session, sort_keys=True) + "\n").encode()
        },
    )
    with pytest.raises(HAAPIError, match=expected_error):
        verify_public_archive(rewritten)


def test_safe_disabled_car_charger_public_export_review_and_gold_are_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from argparse import Namespace
    import importlib.util

    from custom_components.allpowers_ble.model_support import (
        ModelFeature,
        identify_model,
        model_feature_capability,
    )
    from scripts import validate_hil_qualification
    from scripts.hil import cli
    from scripts.hil.discovery import Device
    from scripts.hil.evidence import export_archive
    from scripts.hil.guided import build_tier_plan
    from scripts.hil.onboard import assess_run_gate
    from scripts.hil.review_wizard import interactive_review

    tier_tests_spec = importlib.util.spec_from_file_location(
        "issue351_test_hil_tiers", Path(__file__).with_name("test_hil_tiers.py")
    )
    assert tier_tests_spec is not None and tier_tests_spec.loader is not None
    tier_tests = importlib.util.module_from_spec(tier_tests_spec)
    tier_tests_spec.loader.exec_module(tier_tests)
    matrix_fixture = tier_tests._matrix_fixture

    source_run_id = "abcdef0123456789"
    _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        source_run_id,
        bronze_reviewable=True,
    )
    run_root = tmp_path / "source" / f"run-{source_run_id}"
    private = run_root / "private"
    session_path = private / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))

    support = identify_model("R600", hardware_version="0.3", raw_hardware_version=0x03)
    capabilities = {
        "schema_version": 2,
        "model": support.model,
        "profile": support.profile,
        "hardware_revision": "0.3",
        "firmware_revision": "1.1",
        "verified": True,
        "identity_conflicted": False,
        "connected": True,
        "status_fresh": True,
        "settings_fresh": True,
        "pending_transaction": False,
        "session_generation": 4,
        "capabilities": {
            "read_telemetry": True,
            "write_output_controls": True,
            "write_settings_controls": True,
            "car_charger_enabled": False,
        },
        "features": {
            feature.value: {
                "physical_presence": model_feature_capability(
                    support, feature
                ).physical_presence.value,
                "production_authorized": model_feature_capability(
                    support, feature
                ).production_authorized,
                "candidate_hil_eligible": model_feature_capability(
                    support, feature
                ).hil_candidate_eligible,
                "runtime_ready": True,
            }
            for feature in ModelFeature
        },
    }
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="1.1",
        state_entities={},
        entry_state="loaded",
        config_entry_id="private-entry",
        control_entities={},
    )
    controls = cli._scenario_controls(object(), device, capabilities)
    assert controls["car_charger"] == {
        "available": False,
        "state": None,
        "options": [],
        "availability_verified": True,
        "eligible": False,
        "candidate_eligible": False,
    }
    plan = build_tier_plan(
        "gold",
        capabilities,
        controls,
        observed_route="local_adapter",
    )
    exclusion = next(
        step for step in plan["steps"] if step["id"] == "car_charger_reversal"
    )
    assert exclusion["action"] == "exclude"
    assert exclusion["eligibility"] == "review_required"
    session["route"] = {"requested": "local_adapter"}
    session["runtime_capabilities"] = capabilities
    session["controls"] = controls
    session["test_plan"] = plan
    session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")

    class NoWriteAPI:
        def __init__(self) -> None:
            self.write_calls: list[Any] = []

        @staticmethod
        def discovery() -> tuple[list[Any], list[Any], None, list[Any]]:
            return (
                [],
                [
                    {
                        "id": "private-device",
                        "config_entry_id": "private-entry",
                        "identifiers": [["allpowers_ble", "private-id"]],
                        "model": "R600",
                    }
                ],
                None,
                [
                    {
                        "entry_id": "private-entry",
                        "domain": "allpowers_ble",
                        "state": "loaded",
                    }
                ],
            )

        @staticmethod
        def hil_capabilities(_entry_id: str) -> dict[str, Any]:
            return capabilities

        def call_control(self, *args: Any, **kwargs: Any) -> None:
            self.write_calls.append((args, kwargs))
            raise AssertionError("an exclusion must not issue a control write")

        def websocket(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("an exclusion must not start a GATT trace")

    api_fake = NoWriteAPI()
    focused_plan = {**plan, "steps": [exclusion]}
    assert (
        cli._execute_tier_suite(
            api_fake,
            Namespace(route="local_adapter", output_root=private, non_interactive=True),
            device,
            {},
            private,
            source_run_id,
            focused_plan,
            capabilities,
        )
        == 0
    )
    assert api_fake.write_calls == []
    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="gold",
    )
    assert not any(item["outcome"] == "invalid_exclusion" for item in blockers)

    archive = export_archive(run_root, confirmed=True)
    _manifest, public, _archive_sha = verify_public_archive(
        archive, include_contents=True
    )
    public_execution = json.loads(public["tier-execution.json"])
    public_exclusion = next(
        row
        for row in public_execution["steps"]
        if row["step_id"] == "car_charger_reversal"
    )
    assert public_exclusion["reason"] == (
        "optional control is safely disabled; pending explicit reviewed exclusion"
    )
    assert public_exclusion["safe_disable_proof"] == {
        "runtime_option_enabled": False,
        "normal_control_available": False,
    }
    public_session = json.loads(public["session.json"])["data"]
    assert public_session["controls"]["car_charger"] == {
        "available": False,
        "availability_verified": True,
    }
    assert public_session["runtime_capabilities"]["settings_fresh"] is True
    assert {
        key: public_session["runtime_capabilities"][key]
        for key in ("model", "hardware_revision", "firmware_revision")
    } == {
        "model": "R600",
        "hardware_revision": "0.3",
        "firmware_revision": "1.1",
    }
    assert public_session["runtime_capabilities"]["features"]["car_charger"] == {
        "physical_presence": "unknown",
        "runtime_ready": True,
    }

    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text(encoding="utf-8"))
    imported_run = imported["revisions"][0]["runs"][0]
    _tty(monkeypatch)
    answers = iter(["1", "HIL reviewer", "yes", "APPLY REVIEW"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert (
        interactive_review(
            matrix_path,
            run_id=imported_run["run_id"],
            tier="gold",
            repo_root=tmp_path,
        )
        == 0
    )
    capsys.readouterr()
    reviewed_import = json.loads(matrix_path.read_text(encoding="utf-8"))
    exclusion_contract = reviewed_import["revisions"][0]["feature_contract"]

    # Supply independently validated Gold evidence for the mandatory features;
    # this imported run contributes only the reviewed optional-control exclusion.
    gold = matrix_fixture(tmp_path, "gold", run_id="other-gold-run")
    gold_revision = gold["revisions"][0]
    gold_revision["runs"][0]["feature_presence"]["car_charger"] = {"status": "unknown"}
    gold_revision["runs"][0]["write_capabilities"]["car_charger"] = {
        "status": "pending",
        "evidence": [],
    }
    gold_revision["feature_contract"] = exclusion_contract
    gold_revision["runs"].extend(reviewed_import["revisions"][0]["runs"])
    combined = {**gold, "revisions": [gold_revision]}
    matrix_path.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    errors, _ = validate_hil_qualification.validate_matrix(
        combined,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert errors == []
    tier = __import__("scripts.hil.tiers", fromlist=["evaluate_tiers"]).evaluate_tiers(
        combined
    )["tiers"][0]
    assert tier["tier"] == "gold"
    assert "car_charger (excluded/unverified; safely disabled)" in tier["exclusions"]

    # Any regression in the independent Gold requirements still downgrades the
    # same reviewed matrix, despite the valid car_charger exclusion contract.
    for failure in ("output", "setting", "reconnect", "restoration"):
        changed = json.loads(json.dumps(combined))
        mandatory_run = next(
            run
            for run in changed["revisions"][0]["runs"]
            if run["run_id"] == "other-gold-run"
        )
        if failure == "output":
            mandatory_run["write_capabilities"]["ac_output"]["status"] = "pending"
        elif failure == "setting":
            mandatory_run["write_capabilities"]["eco_mode"]["status"] = "pending"
        elif failure == "reconnect":
            mandatory_run["scenarios"]["reconnect_failover"]["status"] = "pending"
        else:
            mandatory_run["write_capabilities"]["ac_output"]["physical_review"][
                "output_transitions"
            ] = []
        assert (
            __import__("scripts.hil.tiers", fromlist=["evaluate_tiers"]).evaluate_tiers(
                changed
            )["tiers"][0]["tier"]
            != "gold"
        )

    source_session_path = tmp_path / exclusion_contract["car_charger"]["evidence"][0]
    feature_contract_artifact = json.loads(source_session_path.read_text())
    source_path = tmp_path / feature_contract_artifact["source_session"]
    original_source = source_path.read_bytes()
    altered_session = json.loads(original_source)
    altered_session["data"]["controls"]["car_charger"]["availability_verified"] = False
    source_path.write_text(json.dumps(altered_session), encoding="utf-8")
    tampered_errors, _ = validate_hil_qualification.validate_matrix(
        combined,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert any("feature_contract" in error for error in tampered_errors)
    assert (
        __import__("scripts.hil.tiers", fromlist=["evaluate_tiers"]).evaluate_tiers(
            combined
        )["tiers"][0]["tier"]
        != "gold"
    )
    source_path.write_bytes(original_source)


def test_inventory_preflight_survives_public_archive_and_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from custom_components.allpowers_ble.model_support import (
        MODEL_FEATURE_PROFILES,
        ModelFeature,
        identify_model,
        model_feature_capability,
    )
    from scripts import validate_hil_qualification
    from scripts.hil.cli import _collect_physical_inventory_preflight
    from scripts.hil.evidence import _public_tier_plan, export_archive
    from scripts.hil.guided import build_tier_plan
    from scripts.hil.review_wizard import interactive_review

    support = identify_model("R600", hardware_version="0.3", raw_hardware_version=3)
    capabilities = {
        "model": support.model,
        "profile": support.profile,
        "hardware_revision": "0.3",
        "firmware_revision": "1.1",
        "verified": support.verified,
        "identity_conflicted": False,
        "features": {
            feature.value: {
                "physical_presence": model_feature_capability(
                    support, feature
                ).physical_presence.value,
                "production_authorized": model_feature_capability(
                    support, feature
                ).production_authorized,
                "candidate_hil_eligible": model_feature_capability(
                    support, feature
                ).hil_candidate_eligible,
                "runtime_ready": True,
            }
            for feature in ModelFeature
        },
    }
    controls = {
        name: {"available": True, "eligible": True, "state": "off"}
        for name in (
            "ac_output",
            "dc_output",
            "light",
            "eco_mode",
            "work_mode",
            "eco_timeout",
            "car_charger",
        )
    }
    inventory = _collect_physical_inventory_preflight(
        capabilities, controls, prompt=lambda _message: "PRESENT"
    )
    plan = build_tier_plan(
        "gold",
        capabilities,
        controls,
        observed_route="local_adapter",
        inventory_preflight=inventory,
    )
    projected_plan = _public_tier_plan(plan)
    assert projected_plan is not None
    assert _public_tier_plan(projected_plan) == projected_plan
    tampered_plan = json.loads(json.dumps(projected_plan))
    tampered_plan["physical_inventory_preflight"]["features"]["ac_output"]["basis"] = (
        "ha_entity_visible"
    )
    assert _public_tier_plan(tampered_plan) is None
    tampered_plan = json.loads(json.dumps(projected_plan))
    next(step for step in tampered_plan["steps"] if step["feature"] == "ac_output")[
        "physical_presence"
    ] = "present"
    assert _public_tier_plan(tampered_plan) is None
    expected_inspected = {
        step["feature"]
        for step in plan["steps"]
        if step.get("inventory_preflight") == "operator_inspected_present"
    }
    assert expected_inspected
    assert set(projected_plan["physical_inventory_preflight"]["features"]) == (
        expected_inspected
    )
    for step in projected_plan["steps"]:
        if step["feature"] in expected_inspected:
            assert step["physical_presence"] == "unknown"
            assert step["inventory_preflight"] == "operator_inspected_present"

    uninspected_plan = build_tier_plan(
        "gold", capabilities, controls, observed_route="local_adapter"
    )
    uninspected_public = _public_tier_plan(uninspected_plan)
    assert uninspected_public is not None
    assert uninspected_public["physical_inventory_preflight"]["features"] == {}
    for step in uninspected_public["steps"]:
        if step["feature"] in expected_inspected:
            assert step["physical_presence"] == "unknown"
            assert step["inventory_preflight"] == "unknown"
    unauthorized_capabilities = json.loads(json.dumps(capabilities))
    unauthorized_capabilities["features"]["eco_mode"]["production_authorized"] = False
    unauthorized_plan = build_tier_plan(
        "gold",
        unauthorized_capabilities,
        controls,
        observed_route="local_adapter",
        inventory_preflight=inventory,
    )
    unauthorized_public = _public_tier_plan(unauthorized_plan)
    assert unauthorized_public is not None
    assert (
        "eco_mode"
        not in unauthorized_public["physical_inventory_preflight"]["features"]
    )
    unauthorized_eco = next(
        step for step in unauthorized_public["steps"] if step["feature"] == "eco_mode"
    )
    assert unauthorized_eco["physical_presence"] == "unknown"
    assert unauthorized_eco["inventory_preflight"] == "unknown"
    lite_profile = MODEL_FEATURE_PROFILES["ap-r1500-lite-hw-0.1-evidence"]
    lite_capabilities = {
        "model": lite_profile.model,
        "profile": lite_profile.profile,
        "hardware_revision": lite_profile.hardware_revision,
        "firmware_revision": "1.1",
        "verified": False,
        "identity_conflicted": False,
        "features": {
            feature.value: {
                "physical_presence": declaration.physical_presence.value,
                "production_authorized": declaration.production_authorized,
                "candidate_hil_eligible": declaration.hil_candidate_eligible,
                "runtime_ready": True,
            }
            for feature, declaration in lite_profile.features.items()
        },
    }
    lite_controls = {
        name: {
            "available": name != "light",
            "eligible": False,
            "state": "off",
        }
        for name in controls
    }
    absent_plan = build_tier_plan(
        "gold",
        lite_capabilities,
        lite_controls,
        observed_route="local_adapter",
        inventory_preflight={
            "light_output": {
                "outcome": "present",
                "basis": "operator_physical_inspection",
            }
        },
    )
    absent_public = _public_tier_plan(absent_plan)
    assert absent_public is not None
    assert absent_public["physical_inventory_preflight"]["features"] == {}
    light_step = next(
        step for step in absent_public["steps"] if step["feature"] == "light_output"
    )
    assert light_step["physical_presence"] == "absent"
    assert light_step["inventory_preflight"] == "not_required"

    source = tmp_path / "source"
    archive = _captured_archive(
        source,
        "local_adapter",
        "1.1",
        "abcdef0123456789",
        bronze_reviewable=True,
    )
    run_root = source / "run-abcdef0123456789"
    private_session = run_root / "private" / "session.json"
    session = json.loads(private_session.read_text(encoding="utf-8"))
    session["test_plan"] = plan
    private_session.write_text(json.dumps(session), encoding="utf-8")
    archive = export_archive(run_root, confirmed=True)
    _verified, public_members, _digest = verify_public_archive(
        archive, include_contents=True
    )
    public_session = json.loads(public_members["session.json"])
    exported_plan = public_session["data"]["test_plan"]
    assert (
        exported_plan["physical_inventory_preflight"]["features"]
        == (projected_plan["physical_inventory_preflight"]["features"])
    )
    assert _public_tier_plan(exported_plan) == exported_plan

    matrix_path = _matrix(tmp_path / "imported")
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run_id = matrix["revisions"][0]["runs"][0]["run_id"]
    _tty(monkeypatch)
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="silver", repo_root=tmp_path
        )
        == 2
    )
    wizard_output = capsys.readouterr().out
    assert "Operator physical-inventory preflight (acquisition eligibility only;" in (
        wizard_output
    )
    assert "not qualification evidence by itself" in wizard_output
    assert "confirm the actual transitions and restorations below" in (wizard_output)


def test_reviewed_physical_absence_applies_across_equivalent_model_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import UTC, datetime

    from scripts import validate_hil_qualification
    from scripts.hil.evidence import export_archive
    from scripts.hil.importer import import_archive
    from scripts.hil.review import review_imported_tier_run
    from scripts.hil.review_wizard import interactive_review

    def make_archive(path: Path, *, plan_model: str) -> Path:
        run_id = "abcdef0123456789"
        source = path / "source"
        _captured_archive(
            source,
            "local_adapter",
            "1.1",
            run_id,
            bronze_reviewable=True,
        )
        run_root = source / f"run-{run_id}"
        private = run_root / "private"
        session_path = private / "session.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session["test_plan"] = {
            "schema_version": 1,
            "requested_tier": "silver",
            "attained_tier": None,
            "review_state": "pending",
            "device": {
                "model": plan_model,
                "hardware_revision": "0.3",
                "firmware_revision": "1.1",
                "verified_route": "local_adapter",
            },
            "operator_actions_estimate": 1,
            "steps": [
                {
                    "id": "light_output_cycle",
                    "feature": "light_output",
                    "eligibility": "not_applicable",
                    "physical_presence": "absent",
                    "outcome": "pending",
                    "reasons": ["runtime profile substantiates physical absence"],
                    "human_review": "pending",
                }
            ],
        }
        session_path.write_text(json.dumps(session), encoding="utf-8")
        (private / "tier-execution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "requested_tier": "silver",
                    "execution_tier": "silver",
                    "requested_route": "local_adapter",
                    "device": {
                        "model": "R600",
                        "hardware_revision": "0.3",
                        "firmware_revision": "1.1",
                    },
                    "review_state": "pending",
                    "steps": [],
                }
            ),
            encoding="utf-8",
        )
        return export_archive(run_root, confirmed=True)

    archive = make_archive(tmp_path / "positive-source", plan_model="R600")
    positive_root = tmp_path / "positive"
    matrix_path = _matrix(positive_root)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", positive_root)
    assert (
        import_archive(
            archive, matrix_path=matrix_path, apply=True, repo_root=positive_root
        )
        == 0
    )
    imported = json.loads(matrix_path.read_text(encoding="utf-8"))
    run_id = imported["revisions"][0]["runs"][0]["run_id"]
    _tty(monkeypatch)
    positive_answers = iter(
        ["2", "independent reviewer", "yes", "yes", "yes", "APPLY REVIEW"]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(positive_answers))
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="silver", repo_root=positive_root
        )
        == 0
    )
    capsys.readouterr()
    reviewed = json.loads(matrix_path.read_text(encoding="utf-8"))
    revision = reviewed["revisions"][0]
    run = revision["runs"][0]
    assert revision["model"] == "ALLPOWERS R600"
    assert run["feature_presence"]["light_output"]["status"] == "absent"
    assert run["write_capabilities"]["light_output"]["status"] == "not_applicable"
    errors, _ = validate_hil_qualification.validate_matrix(
        reviewed,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert errors == []

    independent_root = tmp_path / "not-independent"
    independent_matrix = _matrix(independent_root)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", independent_root)
    assert (
        import_archive(
            archive,
            matrix_path=independent_matrix,
            apply=True,
            repo_root=independent_root,
        )
        == 0
    )
    before = independent_matrix.read_bytes()
    independent_run_id = json.loads(before)["revisions"][0]["runs"][0]["run_id"]
    _tty(monkeypatch)
    non_independent_answers = iter(["1", "operator", "no", "yes", "yes"])
    monkeypatch.setattr(
        "builtins.input", lambda _prompt="": next(non_independent_answers)
    )
    assert (
        interactive_review(
            independent_matrix,
            run_id=independent_run_id,
            tier="silver",
            repo_root=independent_root,
        )
        == 2
    )
    capsys.readouterr()
    assert independent_matrix.read_bytes() == before
    unreviewed = json.loads(before)["revisions"][0]["runs"][0]
    assert unreviewed["feature_presence"]["light_output"]["status"] == "unknown"

    mismatched_archive = make_archive(
        tmp_path / "mismatched-source", plan_model="VOLIX P1800"
    )
    mismatched_root = tmp_path / "mismatched"
    mismatched_matrix = _matrix(mismatched_root)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", mismatched_root)
    assert (
        import_archive(
            mismatched_archive,
            matrix_path=mismatched_matrix,
            apply=True,
            repo_root=mismatched_root,
        )
        == 0
    )
    mismatched_before = mismatched_matrix.read_bytes()
    mismatched_data = json.loads(mismatched_before)
    mismatched_run = mismatched_data["revisions"][0]["runs"][0]
    audit_path = mismatched_root / "hil-results" / "mismatched-absence.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tier": "silver",
                "archive_sha256": mismatched_run["archive_sha256"],
                "run_id": mismatched_run["run_id"],
                "reviewed_by": "independent reviewer",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "reviewer_independent_from_operator": True,
                "transitions": [],
                "scenarios": [],
                "absences": [
                    {
                        "feature": "light_output",
                        "source_step_id": "light_output_cycle",
                        "physical_absence_confirmed": True,
                        "safe_disable_confirmed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        review_imported_tier_run(
            mismatched_matrix,
            run_id=mismatched_run["run_id"],
            review_file=audit_path,
            tier="silver",
            apply=True,
            repo_root=mismatched_root,
        )
        == 2
    )
    assert mismatched_matrix.read_bytes() == mismatched_before


def test_standard_tier_export_review_apply_reaches_silver_and_gold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archive = _standard_tier_suite_archive(tmp_path / "standard")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    imported = json.loads(matrix_path.read_text(encoding="utf-8"))
    run_id = imported["revisions"][0]["runs"][0]["run_id"]

    _tty(monkeypatch)

    def answer_review(responses: list[str]) -> None:
        iterator = iter(responses)
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(iterator))

    answer_review(
        _review_answers(reviewer="Bronze reviewer", inspected=True, apply=True)
    )
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="bronze", repo_root=tmp_path
        )
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert evaluate_tiers(matrix)["tiers"][0]["tier"] == "bronze"

    # Six output cycles each require authorization, change, and restore
    # confirmations; each output's final restoration is also confirmed.
    answer_review(["2", "Silver reviewer", *(["yes"] * 24), "APPLY REVIEW"])
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="silver", repo_root=tmp_path
        )
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    silver = evaluate_tiers(matrix)["tiers"][0]
    assert silver["tier"] == "silver"
    assert set(silver["qualified_features"]) >= {
        "ac_output",
        "dc_output",
        "light_output",
    }
    run = matrix["revisions"][0]["runs"][0]
    from scripts.hil.cli import _verified_review_stage_paths

    silver_stage = _verified_review_stage_paths(
        matrix,
        matrix_path,
        tmp_path,
        revision_id="r600-hw03",
        run_id=run_id,
        tier="silver",
    )
    assert silver_stage == [
        "qualification_matrix.json",
        *sorted(run["evidence"]),
    ]
    assert all((tmp_path / path).is_file() for path in silver_stage)
    assert run["scenarios"]["reconnect_failover"]["status"] == "pass"
    for feature in ("ac_output", "dc_output", "light_output"):
        physical_review = run["write_capabilities"][feature]["physical_review"]
        final_restoration = physical_review["final_restoration"]
        assert final_restoration["state"] == "off"
        assert final_restoration["confirmed"] is True
        assert final_restoration["reviewer_independent_from_operator"] is True
        assert final_restoration["frame_evidence"]["source_action_index"] == 3
        final_rows = [
            json.loads(line)
            for line in (tmp_path / final_restoration["capture_evidence"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert len(final_rows) == 1
        assert final_rows[0]["source_action_index"] == 3
        transitions = physical_review["output_transitions"]
        for transition in transitions:
            capture = tmp_path / transition["restoration_capture"]
            rows = [json.loads(line) for line in capture.read_text().splitlines()]
            assert all(row["source_action_index"] != 3 for row in rows)

    # Recreate the complete pre-#359 state: applied Bronze and Silver reviews
    # both lack assurance, with their schema-v1 independence declarations kept
    # only as untrusted historical fields.
    legacy = json.loads(json.dumps(matrix))
    legacy_run = legacy["revisions"][0]["runs"][0]
    historical_evidence = set(legacy_run["evidence"])
    silver_history = next(
        item for item in legacy_run["review_history"] if item.get("tier") == "silver"
    )
    legacy_independent_review = {
        key: silver_history[key]
        for key in ("reviewed_by", "reviewed_at", "archive_sha256")
    }
    silver_history.pop("assurance")
    silver_history["reviewer_independent_from_operator"] = True
    silver_audits = list(
        (tmp_path / "hil-results" / "tier-reviews").glob(f"{run_id}-silver-*.json")
    )
    assert len(silver_audits) == 1
    legacy_audit = json.loads(silver_audits[0].read_text(encoding="utf-8"))
    assert legacy_audit["schema_version"] == 2
    assert legacy_audit["reviewer_independent_from_operator"] is True
    legacy_audit["schema_version"] = 1
    legacy_audit.pop("assurance")
    legacy_audit.pop("exclusions", None)
    silver_audits[0].write_text(json.dumps(legacy_audit, indent=2) + "\n")
    for result in [
        legacy_run["scenarios"]["reconnect_failover"],
        *(
            legacy_run["write_capabilities"][feature]
            for feature in ("ac_output", "dc_output", "light_output")
        ),
    ]:
        physical_review = result["physical_review"]
        physical_review.pop("assurance")
        physical_review["reviewer_independent_from_operator"] = True
        final = physical_review.get("final_restoration")
        if final is not None:
            final.pop("assurance")
            final["reviewer_independent_from_operator"] = True
    old_silver_evidence = {
        category: values
        for category, values in {
            "scenarios": {
                "reconnect_failover": json.loads(
                    json.dumps(legacy_run["scenarios"]["reconnect_failover"])
                )
            },
            "write_capabilities": {
                feature: json.loads(
                    json.dumps(legacy_run["write_capabilities"][feature])
                )
                for feature in ("ac_output", "dc_output", "light_output")
            },
            "feature_presence": {
                feature: json.loads(json.dumps(item))
                for feature, item in legacy_run["feature_presence"].items()
                if item.get("status") in {"present", "absent"}
                and "assurance" not in item
            },
        }.items()
        if values
    }
    old_bronze_evidence = {}
    for claim, result in legacy_run["bronze_evidence"].items():
        physical_review = result["physical_review"]
        old_bronze_evidence[claim] = json.loads(json.dumps(physical_review))
        physical_review.pop("assurance")
        physical_review.pop("evidence_sha256")
        physical_review["reviewer_independent_from_operator"] = True
        old_bronze_evidence[claim]["reviewer_independent_from_operator"] = True
    bronze_history = next(
        item for item in legacy_run["review_history"] if item.get("tier") == "bronze"
    )
    bronze_history.pop("assurance")
    bronze_history["reviewer_independent_from_operator"] = True
    bronze_audits = list(
        (tmp_path / "hil-results" / "bronze-reviews").glob(f"{run_id}-*.json")
    )
    assert len(bronze_audits) == 1
    old_bronze_audit_bytes = bronze_audits[0].read_bytes()
    old_bronze_audit = json.loads(old_bronze_audit_bytes)
    old_bronze_audit["schema_version"] = 1
    old_bronze_audit.pop("assurance")
    old_bronze_audit["reviewer_independent_from_operator"] = True
    bronze_audits[0].write_text(json.dumps(old_bronze_audit, indent=2) + "\n")
    old_bronze_audit_bytes = bronze_audits[0].read_bytes()
    legacy_bytes = (json.dumps(legacy, indent=2, ensure_ascii=False) + "\n").encode()
    matrix_path.write_bytes(legacy_bytes)
    legacy_errors, _ = validate_hil_qualification.validate_matrix(
        legacy, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert any(
        "physical_review must record valid assurance" in e for e in legacy_errors
    )
    assert evaluate_tiers(legacy)["tiers"][0]["tier"] == "unqualified"

    answer_review(
        [
            "1",
            "Silver operator",
            "yes",
            *(item for _ in range(3) for item in ("pass", "yes")),
            "APPLY REVIEW",
        ]
    )
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="bronze", repo_root=tmp_path
        )
        == 0
    )
    after_bronze = json.loads(matrix_path.read_text(encoding="utf-8"))
    strict_errors, _ = validate_hil_qualification.validate_matrix(
        after_bronze, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert strict_errors == []
    bronze_run = after_bronze["revisions"][0]["runs"][0]
    assert evaluate_tiers(after_bronze)["tiers"][0]["tier"] == "bronze"
    assert (
        evaluate_tiers(after_bronze)["tiers"][0]["assurance"]["bronze"]
        == "operator_attested"
    )
    migration_record = next(
        item
        for item in bronze_run["review_history"]
        if item.get("legacy_tier_evidence")
    )
    assert migration_record["assurance"] == "unknown"
    assert migration_record["legacy_tier_evidence"] == old_silver_evidence
    assert all(
        item["status"] == "inconclusive"
        for item in bronze_run["write_capabilities"].values()
    )
    assert {item["status"] for item in bronze_run["scenarios"].values()} == {
        "inconclusive"
    }
    assert historical_evidence.issubset(bronze_run["evidence"])
    assert bronze_audits[0].read_bytes() == old_bronze_audit_bytes

    answer_review(["1", "Silver operator", *("yes" for _ in range(23)), "APPLY REVIEW"])
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="silver", repo_root=tmp_path
        )
        == 0
    )
    legacy = json.loads(matrix_path.read_text(encoding="utf-8"))
    strict_errors, _ = validate_hil_qualification.validate_matrix(
        legacy, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert strict_errors == []
    reattested_run = legacy["revisions"][0]["runs"][0]
    migrated_history = reattested_run["review_history"]
    assert any(
        item.get("reviewer_independent_from_operator") is True
        and all(
            item.get(key) == value for key, value in legacy_independent_review.items()
        )
        for item in migrated_history
    )
    bronze_legacy_history = next(
        item
        for item in migrated_history
        if item.get("tier") == "bronze" and item.get("assurance") == "unknown"
    )
    assert bronze_legacy_history["legacy_physical_reviews"] == {
        claim: {
            key: value
            for key, value in physical_review.items()
            if key not in {"assurance", "evidence_sha256"}
        }
        for claim, physical_review in old_bronze_evidence.items()
    }
    assert historical_evidence.issubset(reattested_run["evidence"])
    assert json.loads(silver_audits[0].read_text(encoding="utf-8")) == legacy_audit
    assert bronze_audits[0].read_bytes() == old_bronze_audit_bytes
    assert reattested_run["archive_sha256"] == archive_sha256
    legacy_silver = evaluate_tiers(legacy)["tiers"][0]
    assert legacy_silver["tier"] == "silver"
    assert legacy_silver["assurance"]["silver"] == "operator_attested"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha256

    # Gold repeats the independent output review and additionally reviews all
    # four settings cycles before dry-run and apply.
    answer_review(["2", "Gold reviewer", *(["yes"] * 36), "APPLY REVIEW"])
    assert (
        interactive_review(matrix_path, run_id=run_id, tier="gold", repo_root=tmp_path)
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    errors, _ = validate_hil_qualification.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    gold = evaluate_tiers(matrix)["tiers"][0]
    assert gold["tier"] == "gold"
    assert set(gold["qualified_features"]) == {
        "ac_output",
        "dc_output",
        "light_output",
        "eco_mode",
        "work_mode",
        "eco_timeout",
        "car_charger",
    }
    run = matrix["revisions"][0]["runs"][0]
    gold_stage = _verified_review_stage_paths(
        matrix,
        matrix_path,
        tmp_path,
        revision_id="r600-hw03",
        run_id=run_id,
        tier="gold",
    )
    assert gold_stage == [
        "qualification_matrix.json",
        *sorted(run["evidence"]),
    ]
    assert all((tmp_path / path).is_file() for path in gold_stage)
    assert run["scenarios"]["reconnect_failover"]["status"] == "pass"

    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    reviewed_state = {
        key: json.loads(json.dumps(run[key]))
        for key in (
            "archive_sha256",
            "feature_presence",
            "review_history",
            "scenarios",
            "write_capabilities",
        )
    }
    assert run["archive_sha256"] == archive_sha256
    assert evaluate_tiers(matrix)["tiers"][0]["assurance"]["gold"] == ("mixed")
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    assert "This archive is already imported; no changes needed." in (
        capsys.readouterr().out
    )
    reimported = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert len(reimported["revisions"][0]["runs"]) == 1
    reimported_run = reimported["revisions"][0]["runs"][0]
    assert {key: reimported_run[key] for key in reviewed_state} == reviewed_state
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha256
    reimported_tier = evaluate_tiers(reimported)["tiers"][0]
    assert reimported_tier["tier"] == "gold"
    assert reimported_tier["assurance"]["gold"] == "mixed"

    # An older qualified run may keep the scope's tier, but cannot qualify a
    # newly imported run with no reviewed Bronze, control, or scenario results.
    from scripts.hil.cli import _isolated_run_tier

    mixed = json.loads(json.dumps(matrix))
    incomplete = json.loads(json.dumps(run))
    incomplete["run_id"] = "archive-" + "a" * 64
    incomplete["session_id"] = "archive-session-incomplete"
    incomplete["bronze_evidence"] = {}
    incomplete["write_capabilities"] = {}
    incomplete["scenarios"] = {}
    mixed["revisions"][0]["runs"].append(incomplete)
    assert evaluate_tiers(mixed)["tiers"][0]["tier"] == "gold"
    assert (
        _isolated_run_tier(
            mixed,
            revision_id="r600-hw03",
            run_id=incomplete["run_id"],
            firmware="1.1",
            route="local_adapter",
        )
        == "unqualified"
    )

    # Removing the optional digest and final proof cannot turn off the archive
    # gate: imported origin comes from the archive identity and retained member
    # provenance, and the ordinary digest manifest is recomputed here.
    missing_proof = json.loads(json.dumps(matrix))
    missing_run = missing_proof["revisions"][0]["runs"][0]
    missing_result = missing_run["write_capabilities"]["ac_output"]
    missing_review = missing_result["physical_review"]
    missing_capture = missing_review["final_restoration"]["capture_evidence"]
    missing_review.pop("final_restoration")
    missing_run.pop("archive_sha256")
    missing_result["evidence"].remove(missing_capture)
    missing_review["evidence_sha256"] = validate_hil_qualification._evidence_digests(
        missing_result["evidence"]
    )
    errors, _ = validate_hil_qualification.validate_matrix(
        missing_proof, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert any("archive provenance" in error for error in errors)
    assert any("final restoration" in error for error in errors)
    regressed = evaluate_tiers(missing_proof)["tiers"][0]
    assert regressed["tier"] == "bronze"
    assert "ac_output" not in regressed["qualified_features"]

    # A syntactically valid raw frame is not a substitute for the normalized
    # action-3 TX in the imported graph, even when its fake source references
    # agree with both the row and the edited review metadata.
    raw_bypass = json.loads(json.dumps(matrix))
    raw_run = raw_bypass["revisions"][0]["runs"][0]
    raw_result = raw_run["write_capabilities"]["ac_output"]
    raw_review = raw_result["physical_review"]
    raw_restoration = raw_review["final_restoration"]
    original_capture = tmp_path / raw_restoration["capture_evidence"]
    raw_path = original_capture.with_name("ac-output-final-restoration-raw.jsonl")
    raw_row = json.loads(original_capture.read_text(encoding="utf-8"))
    assert raw_row["source_action_index"] == 3
    fake_references = {
        "trace_sha256": "1" * 64,
        "frame_sha256": "2" * 64,
        "transaction_sha256": "3" * 64,
        "frame_seq": 999,
    }
    raw_row.update(
        source_trace_sha256=fake_references["trace_sha256"],
        source_frame_sha256=fake_references["frame_sha256"],
        source_transaction_sha256=fake_references["transaction_sha256"],
        source_frame_seq=fake_references["frame_seq"],
        frame_hex="a56500b10101000071",
    )
    raw_path.write_text(json.dumps(raw_row, sort_keys=True) + "\n")
    raw_result["evidence"].append(raw_path.relative_to(tmp_path).as_posix())
    raw_restoration["capture_evidence"] = raw_path.relative_to(tmp_path).as_posix()
    raw_restoration["frame_evidence"].update(fake_references)
    raw_review["evidence_sha256"] = validate_hil_qualification._evidence_digests(
        raw_result["evidence"]
    )
    raw_scope = {
        "route": raw_run["route"],
        "revision_id": raw_bypass["revisions"][0]["id"],
        "firmware_version": raw_run["firmware_version"],
        "run_id": raw_run["run_id"],
        "captured_at": raw_run["captured_at"],
        "session_id": raw_run["session_id"],
        "build_fingerprint": raw_run["build_fingerprint"],
    }
    assert validate_hil_qualification._valid_tier_capture_row(
        raw_row,
        raw_scope,
        "ac_output",
        False,
        "restoration",
        raw_result["evidence"],
        final_restoration=True,
    )
    errors, _ = validate_hil_qualification.validate_matrix(
        raw_bypass, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert any("final restoration" in error for error in errors)
    regressed = evaluate_tiers(raw_bypass)["tiers"][0]
    assert regressed["tier"] == "bronze"
    assert "ac_output" not in regressed["qualified_features"]

    # A Gold setting PASS needs durable evidence for both the changed target
    # and its restoration. Keeping only the valid restoration row must not
    # preserve the setting qualification after the review digest is resealed.
    missing_setting_change = json.loads(json.dumps(matrix))
    setting_run = missing_setting_change["revisions"][0]["runs"][0]
    setting_result = setting_run["write_capabilities"]["eco_mode"]
    setting_review = setting_result["physical_review"]
    setting_capture = tmp_path / setting_review["restoration_capture"]
    setting_rows = [
        json.loads(line) for line in setting_capture.read_text().splitlines()
    ]
    assert {row["action_stage"] for row in setting_rows} == {
        "transition",
        "restoration",
    }
    setting_rows = [row for row in setting_rows if row["action_stage"] != "transition"]
    setting_capture.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in setting_rows)
    )
    setting_review["evidence_sha256"] = validate_hil_qualification._evidence_digests(
        setting_result["evidence"]
    )
    errors, _ = validate_hil_qualification.validate_matrix(
        missing_setting_change,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert any("safe-restoration evidence" in error for error in errors)
    regressed = evaluate_tiers(missing_setting_change)["tiers"][0]
    assert "eco_mode" not in regressed["qualified_features"]

    # Archive-backed output transitions must also remain bound to the imported
    # trace graph. A valid command frame cannot replace the source references
    # on action 0 while the genuine final action-3 proof remains intact.
    raw_transition = json.loads(json.dumps(matrix))
    transition_run = raw_transition["revisions"][0]["runs"][0]
    transition_result = transition_run["write_capabilities"]["ac_output"]
    transition_review = transition_result["physical_review"]
    on_transition = next(
        entry
        for entry in transition_review["output_transitions"]
        if entry["state"] == "on"
    )
    transition_path = tmp_path / on_transition["capture_evidence"]
    transition_rows = [
        json.loads(line) for line in transition_path.read_text().splitlines()
    ]
    transition_rows_to_mutate = [
        row for row in transition_rows if row["action_stage"] == "transition"
    ]
    assert len(transition_rows_to_mutate) == 1
    transition_row = transition_rows_to_mutate[0]
    assert transition_row["source_action_index"] == 0
    transition_row.update(
        source_trace_sha256="4" * 64,
        source_frame_sha256="5" * 64,
        source_transaction_sha256="6" * 64,
        source_frame_seq=999,
        frame_hex="a56500b10101000273",
    )
    transition_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in transition_rows)
    )
    transition_review["evidence_sha256"] = validate_hil_qualification._evidence_digests(
        transition_result["evidence"]
    )
    errors, _ = validate_hil_qualification.validate_matrix(
        raw_transition,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    assert any("on transition lacks bound observation" in error for error in errors)
    regressed = evaluate_tiers(raw_transition)["tiers"][0]
    assert "ac_output" not in regressed["qualified_features"]


def test_bronze_re_review_preserves_verified_silver_authorization_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archive = _standard_tier_suite_archive(tmp_path / "standard")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    run_id = json.loads(matrix_path.read_text(encoding="utf-8"))["revisions"][0][
        "runs"
    ][0]["run_id"]
    _tty(monkeypatch)

    def answer_review(responses: list[str]) -> None:
        iterator = iter(responses)
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(iterator))

    answer_review(
        [
            "1",
            "Bronze operator",
            "yes",
            *(item for _ in range(3) for item in ("pass", "yes")),
            "APPLY REVIEW",
        ]
    )
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="bronze", repo_root=tmp_path
        )
        == 0
    )

    answer_review(["1", "Silver operator", *("yes" for _ in range(23)), "APPLY REVIEW"])
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="silver", repo_root=tmp_path
        )
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run = matrix["revisions"][0]["runs"][0]
    assert run["authorization_context"] == "production"
    assert run["authorization_context_source"] == "reviewed_production_session"
    silver_state = {
        key: json.loads(json.dumps(run[key]))
        for key in ("feature_presence", "scenarios", "write_capabilities")
    }
    silver_audits = list(
        (tmp_path / "hil-results" / "tier-reviews").glob(f"{run_id}-silver-*.json")
    )
    assert len(silver_audits) == 1
    silver_audit_bytes = silver_audits[0].read_bytes()

    answer_review(
        _review_answers(
            reviewer="Independent Bronze reviewer", inspected=True, apply=True
        )
    )
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="bronze", repo_root=tmp_path
        )
        == 0
    )
    capsys.readouterr()
    reviewed = json.loads(matrix_path.read_text(encoding="utf-8"))
    errors, _ = validate_hil_qualification.validate_matrix(
        reviewed, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    run = reviewed["revisions"][0]["runs"][0]
    assert run["authorization_context"] == "production"
    assert run["authorization_context_source"] == "reviewed_production_session"
    assert {key: run[key] for key in silver_state} == silver_state
    assert silver_audits[0].read_bytes() == silver_audit_bytes
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha256
    tier = evaluate_tiers(reviewed)["tiers"][0]
    assert tier["tier"] == "silver"
    assert tier["assurance"]["bronze"] == "independently_reviewed"
    assert tier["assurance"]["silver"] == "mixed"


def test_output_review_without_final_restoration_confirmation_stays_unqualified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archive = _standard_tier_suite_archive(tmp_path / "standard")
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run_id = matrix["revisions"][0]["runs"][0]["run_id"]

    _tty(monkeypatch)
    answers = iter(
        _review_answers(reviewer="Bronze reviewer", inspected=True, apply=True)
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="bronze", repo_root=tmp_path
        )
        == 0
    )

    # Confirm both cycles for all outputs, then decline the separately prompted
    # final action 4 restoration for every feature. Reconnect can still pass, but
    # no output may receive a Silver PASS without that independent observation.
    answers = iter(
        [
            "2",
            "Silver reviewer",
            "yes",
            *("yes" for _ in range(18)),
            "no",
            "no",
            "no",
            "yes",
            "yes",
            "APPLY REVIEW",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert (
        interactive_review(
            matrix_path, run_id=run_id, tier="silver", repo_root=tmp_path
        )
        == 0
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    run = matrix["revisions"][0]["runs"][0]
    assert evaluate_tiers(matrix)["tiers"][0]["tier"] == "bronze"
    assert all(
        run["write_capabilities"][feature]["status"] != "pass"
        for feature in ("ac_output", "dc_output", "light_output")
    )


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
    assert "no recorded assurance" in capsys.readouterr().out
    assert matrix_path.read_bytes() == before

    forged = tmp_path / "hil-results" / "conflicting-assurance.json"
    forged.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tier": "gold",
                "archive_sha256": run["archive_sha256"],
                "run_id": run["run_id"],
                "reviewed_by": "operator",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "reviewer_independent_from_operator": True,
                "assurance": "operator_attested",
                "transitions": [],
                "scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HAAPIError, match="does not match this imported run"):
        review_imported_tier_run(
            matrix_path,
            run_id=run["run_id"],
            review_file=forged,
            tier="gold",
            apply=False,
            repo_root=tmp_path,
        )
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
    ("packet_types", "protocol_outcome", "stale_rest_fallback"),
    [
        (("StatusData",), "pass", False),
        (("SettingsData",), "inconclusive", False),
        ((), "inconclusive", False),
        (("StatusData",), "pass", True),
    ],
)
def test_export_import_and_independent_bronze_review_requires_status_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    packet_types: tuple[str, ...],
    protocol_outcome: str,
    stale_rest_fallback: bool,
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
        stale_rest_fallback=stale_rest_fallback,
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
                "schema_version": 2,
                "assurance": "independently_reviewed",
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
        assert '"tier": "bronze"' in review_output, review_output
        if stale_rest_fallback:
            claim_path = (
                tmp_path
                / reviewed["bronze_evidence"]["fresh_telemetry"]["claim_artifact"]
            )
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            assert claim["sample_age_seconds"] == 816
            assert claim["freshness_basis"] == "capture_bound_status"
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
                "schema_version": 2,
                "assurance": "independently_reviewed",
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


@pytest.mark.parametrize(
    ("mode", "reviewer", "assurance"),
    [
        ("2", "independent reviewer", "independently_reviewed"),
        ("1", "HIL operator", "operator_attested"),
    ],
)
def test_interactive_review_imports_reviews_and_qualifies_bronze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    reviewer: str,
    assurance: str,
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
    original_archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
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

    answers = [mode, reviewer, "yes"]
    if assurance == "independently_reviewed":
        answers.append("yes")
    answers.extend(["pass", "yes", "pass", "yes", "pass", "yes", "APPLY REVIEW"])
    answer_iter = iter(answers)
    monkeypatch.setattr("scripts.hil.review_wizard.sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answer_iter))
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
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_archive_sha256
    assert run["review_history"][-1]["assurance"] == assurance
    assert {claim["status"] for claim in run["bronze_evidence"].values()} == {"pass"}
    assert {
        claim["physical_review"]["assurance"]
        for claim in run["bronze_evidence"].values()
    } == {assurance}
    errors, _ = validate_hil_qualification.validate_matrix(
        reviewed, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    tier = evaluate_tiers(reviewed)["tiers"][0]
    assert tier["tier"] == "bronze"
    assert tier["assurance"]["bronze"] == assurance
    audit_files = list((tmp_path / "hil-results" / "bronze-reviews").glob("*.json"))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text())
    assert set(audit) == {
        "schema_version",
        "archive_sha256",
        "run_id",
        "reviewed_by",
        "reviewed_at",
        "assurance",
        "claims",
    }
    assert len(backend_output) == 2
    assert '"current_attained_tier": "unqualified"' in backend_output[0]
    assert '"attained_tier"' not in backend_output[0]
    assert '"attained_tier": "bronze"' in backend_output[1]


def test_legacy_bronze_review_without_assurance_stays_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_run
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
    run = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    review_path = tmp_path / "legacy-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_sha256": run["archive_sha256"],
                "run_id": run["run_id"],
                "reviewed_by": "reviewer",
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
            run_id=run["run_id"],
            review_file=review_path,
            apply=True,
            repo_root=tmp_path,
        )
        == 0
    )
    reviewed = json.loads(matrix_path.read_text())
    result = reviewed["revisions"][0]["runs"][0]
    assert {item["status"] for item in result["bronze_evidence"].values()} == {
        "inconclusive"
    }
    assert result["review_history"][-1]["assurance"] == "unknown"
    assert evaluate_tiers(reviewed)["tiers"][0]["tier"] == "unqualified"


@pytest.mark.parametrize("review_surface", ["backend", "interactive"])
def test_existing_legacy_bronze_pass_can_be_reattested_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_surface: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_run
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
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    run = json.loads(matrix_path.read_text())["revisions"][0]["runs"][0]
    initial_review_path = tmp_path / "initial-independent-review.json"
    initial_review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "assurance": "independently_reviewed",
                "archive_sha256": run["archive_sha256"],
                "run_id": run["run_id"],
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
            run_id=run["run_id"],
            review_file=initial_review_path,
            apply=True,
            repo_root=tmp_path,
        )
        == 0
    )

    legacy = json.loads(matrix_path.read_text())
    legacy_run = legacy["revisions"][0]["runs"][0]
    old_physical_reviews = {}
    for claim, item in legacy_run["bronze_evidence"].items():
        physical_review = item["physical_review"]
        old_physical_reviews[claim] = json.loads(json.dumps(physical_review))
        physical_review.pop("assurance")
    legacy_run["review_history"] = []
    matrix_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")

    strict_errors, _ = validate_hil_qualification.validate_matrix(
        legacy, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert any(
        "physical_review must record valid assurance" in error
        for error in strict_errors
    )
    assert evaluate_tiers(legacy)["tiers"][0]["tier"] == "unqualified"

    if review_surface == "backend":
        operator_review = tmp_path / "operator-reattestation.json"
        operator_review.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "assurance": "operator_attested",
                    "archive_sha256": legacy_run["archive_sha256"],
                    "run_id": legacy_run["run_id"],
                    "reviewed_by": "original HIL operator",
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
                run_id=legacy_run["run_id"],
                review_file=operator_review,
                apply=True,
                repo_root=tmp_path,
            )
            == 0
        )
    else:
        from scripts.hil.review_wizard import interactive_review

        class TTY:
            @staticmethod
            def isatty() -> bool:
                return True

        answers = iter(
            [
                "1",
                "original HIL operator",
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
        assert (
            interactive_review(
                matrix_path,
                run_id=legacy_run["run_id"],
                tier="bronze",
                repo_root=tmp_path,
            )
            == 0
        )

    reviewed = json.loads(matrix_path.read_text())
    reviewed_run = reviewed["revisions"][0]["runs"][0]
    errors, _ = validate_hil_qualification.validate_matrix(
        reviewed, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha256
    assert all(
        item["status"] == "pass"
        and item["physical_review"]["assurance"] == "operator_attested"
        for item in reviewed_run["bronze_evidence"].values()
    )
    history = reviewed_run["review_history"]
    legacy_history = next(
        item for item in history if item.get("assurance") == "unknown"
    )
    expected_legacy_reviews = json.loads(json.dumps(old_physical_reviews))
    for physical_review in expected_legacy_reviews.values():
        physical_review.pop("assurance")
    assert legacy_history["legacy_physical_reviews"] == expected_legacy_reviews
    assert history[-1]["assurance"] == "operator_attested"
    bronze = evaluate_tiers(reviewed)["tiers"][0]
    assert bronze["tier"] == "bronze"
    assert bronze["assurance"]["bronze"] == "operator_attested"


def test_multi_run_legacy_bronze_reviews_can_be_reattested_sequentially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil.review import review_imported_run
    from scripts.hil.review_wizard import interactive_review
    from scripts.hil.tiers import evaluate_tiers

    archives = {
        "local_adapter": _captured_archive(
            tmp_path / "local-source",
            "local_adapter",
            "1.1",
            "0123456789abcdef",
            bronze_reviewable=True,
        ),
        "active_proxy": _captured_archive(
            tmp_path / "proxy-source",
            "active_proxy",
            "1.1",
            "fedcba9876543210",
            bronze_reviewable=True,
        ),
    }
    archive_digests = {
        route: hashlib.sha256(path.read_bytes()).hexdigest()
        for route, path in archives.items()
    }
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    runs = {}
    for route, archive in archives.items():
        assert (
            import_archive(
                archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path
            )
            == 0
        )
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        run = next(
            item
            for revision in matrix["revisions"]
            for item in revision["runs"]
            if item["route"] == route
        )
        review_path = tmp_path / f"{route}-initial-review.json"
        reviewed_at = datetime.now(UTC).isoformat()
        reviewer = f"initial {route} reviewer"
        review_payload = {
            "schema_version": 2,
            "assurance": "independently_reviewed",
            "archive_sha256": run["archive_sha256"],
            "run_id": run["run_id"],
            "reviewed_by": reviewer,
            "reviewed_at": reviewed_at,
            "claims": {
                claim: "pass"
                for claim in ("identity", "protocol_probe", "fresh_telemetry")
            },
        }
        review_path.write_text(json.dumps(review_payload), encoding="utf-8")
        assert (
            review_imported_run(
                matrix_path,
                run_id=run["run_id"],
                review_file=review_path,
                apply=True,
                repo_root=tmp_path,
            )
            == 0
        )
        runs[route] = {
            "run_id": run["run_id"],
            "reviewed_by": reviewer,
            "reviewed_at": reviewed_at,
        }

    legacy = json.loads(matrix_path.read_text(encoding="utf-8"))
    legacy_records = {}
    legacy_audits = {}
    legacy_histories = {}
    audit_dir = tmp_path / "hil-results" / "bronze-reviews"
    audit_dir.mkdir(parents=True)
    for route, facts in runs.items():
        run = next(
            item
            for revision in legacy["revisions"]
            for item in revision["runs"]
            if item["run_id"] == facts["run_id"]
        )
        for item in run["bronze_evidence"].values():
            physical_review = item["physical_review"]
            physical_review.pop("assurance")
            physical_review.pop("evidence_sha256")
            physical_review["reviewer_independent_from_operator"] = True
        legacy_records[route] = json.loads(json.dumps(run["bronze_evidence"]))
        history = next(
            item for item in run["review_history"] if item.get("tier") == "bronze"
        )
        history.pop("assurance")
        history["reviewer_independent_from_operator"] = True
        legacy_histories[route] = json.loads(json.dumps(run["review_history"]))
        audit_path = audit_dir / f"{facts['run_id']}-schema-v1.json"
        audit_payload = {
            "schema_version": 1,
            "archive_sha256": run["archive_sha256"],
            "run_id": run["run_id"],
            "reviewed_by": facts["reviewed_by"],
            "reviewed_at": facts["reviewed_at"],
            "reviewer_independent_from_operator": True,
            "claims": {
                claim: "pass"
                for claim in ("identity", "protocol_probe", "fresh_telemetry")
            },
        }
        audit_path.write_text(json.dumps(audit_payload, indent=2) + "\n")
        legacy_audits[route] = {
            "path": audit_path,
            "bytes": audit_path.read_bytes(),
        }
    matrix_path.write_text(
        json.dumps(legacy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    strict_errors, _ = validate_hil_qualification.validate_matrix(
        legacy, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert any(
        "physical_review must record valid assurance" in e for e in strict_errors
    )

    from scripts.hil.cli import _isolated_run_tier

    assert all(
        _isolated_run_tier(
            legacy,
            revision_id="r600-hw03",
            run_id=facts["run_id"],
            firmware="1.1",
            route=route,
        )
        == "unqualified"
        for route, facts in runs.items()
    )
    _tty(monkeypatch)

    def reattest(route: str) -> dict[str, Any]:
        facts = runs[route]
        answers = iter(
            [
                "1",
                f"operator {route}",
                "yes",
                *(answer for _ in range(3) for answer in ("pass", "yes")),
                "APPLY REVIEW",
            ]
        )
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
        assert (
            interactive_review(
                matrix_path,
                run_id=facts["run_id"],
                tier="bronze",
                repo_root=tmp_path,
            )
            == 0
        )
        reviewed = json.loads(matrix_path.read_text(encoding="utf-8"))
        errors, _ = validate_hil_qualification.validate_matrix(
            reviewed, matrix_path=matrix_path, require_pass=False, max_age_days=None
        )
        assert errors == []
        assert (
            hashlib.sha256(archives[route].read_bytes()).hexdigest()
            == archive_digests[route]
        )
        for _prior_route, prior_audit in legacy_audits.items():
            assert prior_audit["path"].read_bytes() == prior_audit["bytes"]
        for prior_route, prior_history in legacy_histories.items():
            current_run = next(
                item
                for revision in reviewed["revisions"]
                for item in revision["runs"]
                if item["run_id"] == runs[prior_route]["run_id"]
            )
            assert all(
                entry in current_run["review_history"] for entry in prior_history
            )
        return reviewed

    after_first = reattest("local_adapter")
    assert (
        _isolated_run_tier(
            after_first,
            revision_id="r600-hw03",
            run_id=runs["local_adapter"]["run_id"],
            firmware="1.1",
            route="local_adapter",
        )
        == "bronze"
    )
    proxy_run = next(
        item
        for revision in after_first["revisions"]
        for item in revision["runs"]
        if item["run_id"] == runs["active_proxy"]["run_id"]
    )
    assert {item["status"] for item in proxy_run["bronze_evidence"].values()} == {
        "inconclusive"
    }
    assert (
        _isolated_run_tier(
            after_first,
            revision_id="r600-hw03",
            run_id=runs["active_proxy"]["run_id"],
            firmware="1.1",
            route="active_proxy",
        )
        == "unqualified"
    )
    proxy_legacy_history = next(
        item
        for item in proxy_run["review_history"]
        if item.get("legacy_bronze_evidence")
    )
    assert proxy_legacy_history["assurance"] == "unknown"
    assert (
        proxy_legacy_history["legacy_bronze_evidence"] == legacy_records["active_proxy"]
    )
    assert {item["status"] for item in proxy_run["bronze_evidence"].values()} == {
        "inconclusive"
    }

    after_second = reattest("active_proxy")
    assert all(
        _isolated_run_tier(
            after_second,
            revision_id="r600-hw03",
            run_id=facts["run_id"],
            firmware="1.1",
            route=route,
        )
        == "bronze"
        for route, facts in runs.items()
    )
    for route, facts in runs.items():
        run = next(
            item
            for revision in after_second["revisions"]
            for item in revision["runs"]
            if item["run_id"] == facts["run_id"]
        )
        assert run["archive_sha256"] == next(
            old["archive_sha256"]
            for revision in legacy["revisions"]
            for old in revision["runs"]
            if old["run_id"] == facts["run_id"]
        )
        assert set(run["evidence"]) >= {
            path for item in legacy_records[route].values() for path in item["evidence"]
        }
    assert evaluate_tiers(after_second)["tiers"]


def test_onboard_operator_attestation_stages_the_complete_reviewed_run(
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
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "scripts",
        repo_root / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
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
        stale_rest_fallback=False,
        scenario_progress=False,
        guided_bronze_execution=True,
    )
    archive_before_review = archive.read_bytes()
    from scripts.hil.evidence import verify_public_archive

    _manifest, archive_members, _digest = verify_public_archive(
        archive, include_contents=True
    )
    assert "scenarios.json" not in archive_members
    public_bronze = json.loads(archive_members["tier-bronze-read-only.json"])
    assert public_bronze["fresh_verified_samples"] is True
    assert public_bronze["protocol_probe"] == "verified"
    assert public_bronze["status_freshness"] is not None
    assert all(
        sample["telemetry"]["battery"]["sample_age_seconds"] <= 60
        for sample in public_bronze["samples"]
    )
    archived_report = archive_members["report.md"].decode("utf-8")
    assert "`read_only_probe`: **pass**" in archived_report
    assert "`physical_plausibility_observation`: **pass**" in archived_report
    assert "fresh verified telemetry samples present" in archived_report
    assert "read-only protocol probe `verified`" in archived_report
    assert "No correlated scenario steps were recorded" in archived_report
    answers = iter(
        [
            "yes",
            "APPLY IMPORT",
            "1",
            *_operator_review_answers(reviewer="HIL operator", apply=True),
        ]
    )
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)

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
    verify_public_archive(archive)
    assert archive.read_bytes() == archive_before_review
    assert "Import this verified acquisition evidence? [y/N]: " in prompts
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
        "identity, protocol, and freshness claims were operator-attested and applied"
        in generated_body
    )
    assert "operator_attested" in generated_body
    assert "REVIEWED TIER: Bronze; ASSURANCE: operator_attested" in output
    assert (
        "POST-REVIEW QUALIFICATION: attained bronze; assurance operator_attested"
        in output
    )
    assert (
        "Review basis: schema-v3 matrix evaluation isolated to imported run" in output
    )
    assert "independently reviewed" not in generated_body
    assert "Telemetry sample interval:" in generated_body
    assert "Telemetry sample interval: 2 seconds" in generated_body
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

    direct_env = os.environ.copy()
    direct_env.pop("PYTHONPATH", None)
    direct_validation = subprocess.run(
        [
            sys.executable,
            "scripts/validate_hil_qualification.py",
            "--matrix",
            matrix_path.relative_to(repo_root).as_posix(),
        ],
        cwd=repo_root,
        env=direct_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "HIL qualification validation passed" in direct_validation.stdout


def test_onboard_without_independent_reviewer_leaves_offline_resume_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    output_dir = tmp_path / "operator-run"
    output_dir.mkdir()
    answers = iter(["yes", "APPLY IMPORT", "2", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert not cli._onboard_import_review(
        archive,
        output_dir,
        "bronze",
        repo_root=repo_root,
        matrix_path=matrix_path,
    )

    output = capsys.readouterr().out
    assert "An independent reviewer must be available" not in output
    assert "To continue the offline review, run these commands in order:" in output
    assert "python -m scripts.hil review --matrix " in output
    assert "--run-id archive-" in output
    assert "--interactive" in output
    assert "git add " not in output
    assert not (output_dir / "pr.md").exists()


@pytest.mark.parametrize("tier", ["silver", "gold"])
@pytest.mark.parametrize("review_choice", ["declined", "reviewer_unavailable"])
def test_onboard_silver_gold_before_review_prints_ordered_resume_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier: str,
    review_choice: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli
    from scripts.hil.importer import _build_proposal

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    proposal = _build_proposal(archive, matrix_path, repo_root=repo_root)
    output_dir = tmp_path / "operator-run"
    output_dir.mkdir()
    answers = (
        ["yes", "APPLY IMPORT", ""]
        if review_choice == "declined"
        else ["yes", "APPLY IMPORT", "2", ""]
    )
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    assert not cli._onboard_import_review(
        archive,
        output_dir,
        tier,
        repo_root=repo_root,
        matrix_path=matrix_path,
    )

    output = capsys.readouterr().out
    bronze_command = cli._review_resume_command(matrix_path, proposal["run_id"])
    tier_command = cli._review_resume_command(matrix_path, proposal["run_id"], tier)
    assert "To continue the offline review, run these commands in order:" in output
    assert bronze_command in output
    assert tier_command in output
    assert output.index(bronze_command) < output.index(tier_command)
    assert "READY FOR QUALIFICATION PR" not in output
    assert "git add " not in output
    assert not (output_dir / "pr.md").exists()


@pytest.mark.parametrize("tier", ["silver", "gold"])
def test_onboard_silver_gold_bronze_review_exception_resumes_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier: str,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli, review_wizard
    from scripts.hil.importer import _build_proposal

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    proposal = _build_proposal(archive, matrix_path, repo_root=repo_root)
    output_dir = tmp_path / "operator-run"
    output_dir.mkdir()
    answers = iter(["yes", "APPLY IMPORT", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    def interrupt_review(*_args: Any, **_kwargs: Any) -> int:
        raise EOFError("review interrupted")

    monkeypatch.setattr(review_wizard, "interactive_review", interrupt_review)

    assert not cli._onboard_import_review(
        archive,
        output_dir,
        tier,
        repo_root=repo_root,
        matrix_path=matrix_path,
    )

    output = capsys.readouterr().out
    bronze_command = cli._review_resume_command(matrix_path, proposal["run_id"])
    tier_command = cli._review_resume_command(matrix_path, proposal["run_id"], tier)
    assert "Explicit review did not complete: review interrupted" in output
    assert "To continue the offline review, run these commands in order:" in output
    assert bronze_command in output
    assert tier_command in output
    assert output.index(bronze_command) < output.index(tier_command)
    assert "READY FOR QUALIFICATION PR" not in output
    assert "git add " not in output
    assert not (output_dir / "pr.md").exists()


def test_onboard_higher_tier_exception_resumes_only_pending_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli, review_wizard
    from scripts.hil.importer import _build_proposal

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    archive = _captured_archive(
        tmp_path / "source",
        "local_adapter",
        "1.1",
        "0123456789abcdef",
        bronze_reviewable=True,
        bronze_decoded_packet_types=("StatusData",),
        bronze_protocol_outcome="pass",
    )
    proposal = _build_proposal(archive, matrix_path, repo_root=repo_root)
    output_dir = tmp_path / "operator-run"
    output_dir.mkdir()
    answers = iter(
        [
            "yes",
            "APPLY IMPORT",
            "1",
            *_operator_review_answers(reviewer="HIL operator", apply=True),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    review_bronze = review_wizard.interactive_review

    def interrupt_silver(*args: Any, **kwargs: Any) -> int:
        if kwargs.get("tier") == "silver":
            raise EOFError("Silver review interrupted")
        return review_bronze(*args, **kwargs)

    monkeypatch.setattr(review_wizard, "interactive_review", interrupt_silver)

    assert not cli._onboard_import_review(
        archive,
        output_dir,
        "silver",
        repo_root=repo_root,
        matrix_path=matrix_path,
    )

    output = capsys.readouterr().out
    bronze_command = cli._review_resume_command(matrix_path, proposal["run_id"])
    silver_command = cli._review_resume_command(
        matrix_path, proposal["run_id"], "silver"
    )
    assert "Explicit review did not complete: Silver review interrupted" in output
    assert "To continue the offline review, run these commands in order:" in output
    commands = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith("python -m scripts.hil review ")
    ]
    assert commands == [silver_command]
    assert bronze_command != commands[0]
    assert "READY FOR QUALIFICATION PR" not in output
    assert "git add " not in output
    assert not (output_dir / "pr.md").exists()


def _onboard_review_answers(tier: str, *, reviewer: str) -> list[str]:
    count = {"silver": 24, "gold": 36}[tier]
    return [
        "yes",
        "APPLY IMPORT",
        "2",
        "REVIEWER READY",
        *_selected_independent_review_answers(reviewer="Bronze reviewer", apply=True),
        reviewer,
        "yes",
        *("yes" for _ in range(count - 1)),
        "APPLY REVIEW",
    ]


@pytest.mark.parametrize(("tier", "prior_gold"), [("silver", True), ("gold", False)])
def test_onboard_silver_and_gold_review_reach_self_contained_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier: str,
    prior_gold: bool,
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli
    from scripts.hil.importer import _build_proposal

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    template_target = repo_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    template_target.parent.mkdir()
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / ".github/PULL_REQUEST_TEMPLATE.md",
        template_target,
    )
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "scripts",
        repo_root / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)

    def complete_handoff(archive: Path, requested_tier: str, output: Path) -> str:
        output.mkdir(parents=True, exist_ok=True)
        responses = iter(
            _onboard_review_answers(requested_tier, reviewer="independent reviewer")
        )
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))
        proposal = _build_proposal(archive, matrix_path, repo_root=repo_root)
        assert cli._onboard_import_review(
            archive,
            output,
            requested_tier,
            repo_root=repo_root,
            matrix_path=matrix_path,
        )
        return proposal["run_id"]

    if prior_gold:
        old_archive = _standard_tier_suite_archive(
            tmp_path / "older-gold", run_id="1111111111111111"
        )
        complete_handoff(old_archive, "gold", tmp_path / "old-output")
        capsys.readouterr()

    archive = _standard_tier_suite_archive(
        tmp_path / "new-acquisition",
        run_id="2222222222222222",
        started_at=(
            "2026-11-08T12:00:00+00:00" if prior_gold else "2026-11-09T12:00:00+00:00"
        ),
    )
    expected_run_id = complete_handoff(archive, tier, tmp_path / "operator-run")
    output = capsys.readouterr().out
    generated_body = (tmp_path / "operator-run" / "pr.md").read_text(encoding="utf-8")
    expected_tier = tier.title()
    assert f"REVIEWED TIER: {expected_tier}" in output
    assert f"add independently reviewed R600 {expected_tier} evidence" in output
    assert expected_run_id in generated_body
    if prior_gold:
        assert "add reviewed R600 Gold evidence" not in output
    assert (
        "No new production write authorization is requested or granted"
        in generated_body
    )
    assert "scope-specific reviewed control evidence" in generated_body

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    runs = [run for revision in matrix["revisions"] for run in revision["runs"]]
    from scripts.hil.tiers import evaluate_tiers

    scope_tier = evaluate_tiers(matrix)["tiers"][0]["tier"]
    if prior_gold:
        assert scope_tier == "gold"
        assert expected_tier == "Silver"
    isolated = json.loads(json.dumps(matrix))
    for revision in isolated["revisions"]:
        revision["runs"] = [
            run for run in revision["runs"] if run["run_id"] == expected_run_id
        ]
    assert evaluate_tiers(isolated)["tiers"][0]["tier"] == tier
    expected_paths = {matrix_path.relative_to(repo_root).as_posix()}
    for run in runs:
        expected_paths.update(run["evidence"])
        for section in (
            "bronze_evidence",
            "write_capabilities",
            "scenarios",
            "feature_presence",
        ):
            for record in run.get(section, {}).values():
                expected_paths.update(record.get("evidence", []))
    add_line = next(line.strip() for line in output.splitlines() if "git add " in line)
    stage_paths = shlex.split(add_line)[2:]
    assert set(stage_paths) == expected_paths
    current_prefix = f"tests/hil/fixtures/r600-hw03/runs/{expected_run_id}/"
    current_evidence = {
        path for path in expected_paths if path.startswith(current_prefix)
    }
    assert current_evidence
    assert all(path in generated_body for path in current_evidence)
    assert f"stages all {len(stage_paths) - 1} exact public" in generated_body
    if prior_gold:
        old_run = next(item for item in runs if item["run_id"] != expected_run_id)
        old_prefix = f"tests/hil/fixtures/r600-hw03/runs/{old_run['run_id']}/"
        old_evidence = {path for path in expected_paths if path.startswith(old_prefix)}
        assert old_evidence
        assert not any(path in generated_body for path in old_evidence)
    subprocess.run(["git", "-C", str(repo_root), "add", *stage_paths], check=True)
    staged = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(staged) == expected_paths

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
    run = next(item for item in runs if item["run_id"] == expected_run_id)
    assert (clean_checkout / run["evidence"][0]).is_file()


def test_inconclusive_silver_review_does_not_generate_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import cli

    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    matrix_path = _matrix(repo_root / "tests" / "hil")
    template_target = repo_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    template_target.parent.mkdir()
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / ".github/PULL_REQUEST_TEMPLATE.md",
        template_target,
    )
    subprocess.run(["git", "init", "-b", "feat/test", str(repo_root)], check=True)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "_current_feature_branch", lambda _root: "feat/test")
    _tty(monkeypatch)
    archive = _standard_tier_suite_archive(
        tmp_path / "inconclusive", run_id="3333333333333333"
    )
    (tmp_path / "operator-run").mkdir()
    responses = iter(
        [
            "yes",
            "APPLY IMPORT",
            "2",
            "REVIEWER READY",
            *_selected_independent_review_answers(
                reviewer="Bronze reviewer", apply=True
            ),
            "Silver reviewer",
            "yes",
            *("no" for _ in range(23)),
            "APPLY REVIEW",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    assert not cli._onboard_import_review(
        archive,
        tmp_path / "operator-run",
        "silver",
        repo_root=repo_root,
        matrix_path=matrix_path,
    )
    output = capsys.readouterr().out
    assert "READY FOR QUALIFICATION PR" not in output
    assert "git add " not in output
    assert not (tmp_path / "operator-run" / "pr.md").exists()


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
            "2",
            "REVIEWER READY",
            "second reviewer",
            "no",
            "yes",
            "APPLY REVIEW" if review_end == "inconclusive" else "cancel",
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

    answers = iter(["2", "reviewer", "no", "yes", "cancel"])
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

    answers = iter(["2", "reviewer", "yes", "yes", "APPLY REVIEW"])
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
        [
            "2",
            "reviewer",
            "yes",
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
        [
            "2",
            "reviewer",
            "yes",
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
                "2",
                "reviewer",
                "yes",
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

    answers = iter(
        ["2", "reviewer", "yes", "yes", "inconclusive", "inconclusive", "inconclusive"]
    )
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
