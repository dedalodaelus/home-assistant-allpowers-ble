"""Archive confirmation and integrity tests for HIL exports."""

import json
import hashlib
from copy import deepcopy
from pathlib import Path
import zipfile

import pytest

from scripts.hil.api import HAAPIError
from scripts.hil.evidence import export_archive, verify_public_archive
from scripts.hil.storage import write_run, write_scenario_progress
from scripts.hil.trace import _save_private_trace


def _run(path: Path) -> None:
    path.mkdir()
    (path / "environment.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    (path / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "0123456789abcdef",
                "qualification_status": "inconclusive",
            }
        ),
        encoding="utf-8",
    )


def test_export_requires_confirmation_and_archive_verifies(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _run(run)
    with pytest.raises(HAAPIError):
        export_archive(run, confirmed=False)
    assert not (run / "hil-evidence.zip").exists()

    archive = export_archive(run, confirmed=True)
    manifest = verify_public_archive(archive)
    assert manifest["export_confirmed"] is True
    assert manifest["qualification_status"] == "inconclusive"
    previous = archive.read_bytes()
    with pytest.raises(HAAPIError):
        export_archive(run, confirmed=False)
    assert archive.read_bytes() == previous


def test_archive_reimport_rejects_tampered_public_member(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _run(run)
    archive = export_archive(run, confirmed=True)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == "public/report.md":
                content += b"private-token"
            target.writestr(member.filename, content)
    with pytest.raises(HAAPIError):
        verify_public_archive(tampered)


def test_validated_private_trace_exports_allowlisted_events_and_frame_metadata(
    tmp_path: Path,
) -> None:
    run_id = "0123456789abcdef"
    private = write_run(
        tmp_path,
        "trace-run",
        {"schema_version": 1},
        {"schema_version": 1, "run_id": run_id},
    )
    route_event = {
        "schema_version": 2,
        "seq": 1,
        "utc": "2026-11-05T00:00:00+00:00",
        "elapsed_ms": 0.0,
        "run_id": run_id,
        "scenario_id": "output_ac_cycle",
        "layer": "gatt_api",
        "session_generation": 1,
        "observed_route": "local_adapter",
        "event": "route_observed",
        "backend_kind": "bluez",
        "evidence": "bluez_device_path",
    }
    event = {
        "schema_version": 2,
        "seq": 2,
        "utc": "2026-11-05T00:00:00+00:00",
        "elapsed_ms": 1.0,
        "run_id": run_id,
        "scenario_id": "output_ac_cycle",
        "layer": "gatt_api",
        "session_generation": 1,
        "observed_route": "local_adapter",
        "event": "write_attempt",
        "transaction_id": "private-tx-id",
        "vendor_private": "do-not-export-token",
    }
    submitted = {
        **{key: value for key, value in event.items() if key != "vendor_private"},
        "seq": 3,
        "event": "transaction_submitted",
        "transaction_kind": "output",
    }
    confirmed = {
        **{key: value for key, value in event.items() if key != "vendor_private"},
        "seq": 4,
        "event": "transaction_confirmed",
        "reason": "matching_validated_telemetry",
    }
    received = {
        **{key: value for key, value in event.items() if key != "vendor_private"},
        "seq": 5,
        "event": "notification_received",
    }
    frame = {
        **{k: v for k, v in event.items() if k != "vendor_private"},
        "direction": "tx",
        "command_type": "output_control",
        "frame_validation": "validated_tx",
        "hex": "a565b1000106010000000000",
    }
    rx_frame = {
        **received,
        "direction": "rx",
        "command_type": "unknown",
        "frame_validation": "unvalidated_chunk",
        "hex": "deadbeef",
    }
    trace = {
        "schema_version": 2,
        "capture_scope": "runtime_entry",
        "capture_id": "capture-private-id",
        "run_id": run_id,
        "scenario_id": "output_ac_cycle",
        "requested_route": "auto",
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
        "event_count": 5,
        "frame_count": 2,
        "events": [route_event, event, submitted, confirmed, received],
        "frames": [frame, rx_frame],
        "route_observations": [
            {
                "session_generation": 1,
                "seq": 1,
                "utc": "2026-11-05T00:00:00+00:00",
                "elapsed_ms": 0.0,
                "observed_route": "local_adapter",
                "backend_kind": "bluez",
                "evidence": "bluez_device_path",
                "adapter_id": "hci0",
            }
        ],
    }
    trace_dir = private / "traces" / "output_ac_cycle"
    trace_dir.mkdir(parents=True)
    _save_private_trace(trace_dir, trace)
    cleanup_trace = deepcopy(trace)
    cleanup_trace["capture_id"] = "cleanup-capture-id"
    for record in cleanup_trace["events"] + cleanup_trace["frames"]:
        if record.get("transaction_id"):
            record["transaction_id"] = "private-cleanup-tx-id"
    cleanup_dir = private / "traces" / "output_ac_cycle_cleanup"
    cleanup_dir.mkdir(parents=True)
    _save_private_trace(cleanup_dir, cleanup_trace)
    write_scenario_progress(
        private,
        run_id,
        "output_ac_cycle",
        "a" * 64,
        [
            {
                "step_id": "ac_on",
                "outcome": "pass",
                "trace": "traces/output_ac_cycle",
                "transaction": {
                    "transaction_id": "private-tx-id",
                    "session_generation": 1,
                    "kind": "output",
                    "command_type": "output_control",
                    "write_seq": 2,
                    "submitted_seq": 3,
                    "confirmed_seq": 4,
                },
                "cleanup_trace": "traces/output_ac_cycle_cleanup",
                "cleanup_transaction": {
                    "transaction_id": "private-cleanup-tx-id",
                    "session_generation": 1,
                    "kind": "output",
                    "command_type": "output_control",
                    "write_seq": 2,
                    "submitted_seq": 3,
                    "confirmed_seq": 4,
                },
                "physical_outcome": "yes",
                "started_utc": "2026-11-05T00:00:00+00:00",
                "elapsed_seconds": 1.5,
            }
        ],
    )

    archive = export_archive(tmp_path / "trace-run", confirmed=True)
    with zipfile.ZipFile(archive) as evidence:
        members = evidence.namelist()
        assert "public/events.jsonl" in members
        assert "public/frames.jsonl" in members
        combined = b"".join(evidence.read(name) for name in members)
        scenarios = json.loads(evidence.read("public/scenarios.json"))
        assert scenarios["steps"][0]["evidence"] == [
            "capture-0001:2",
            "capture-0001:3",
            "capture-0001:4",
        ]
        assert scenarios["steps"][0]["missing_evidence"] is False
        assert scenarios["steps"][0]["cleanup_evidence"] == [
            "capture-0002:2",
            "capture-0002:3",
            "capture-0002:4",
        ]
        assert scenarios["steps"][0]["cleanup_missing_evidence"] is False
        public_frames = [
            json.loads(line)
            for line in evidence.read("public/frames.jsonl").splitlines()
        ]
        assert public_frames[0]["payload_omitted"] is True
        assert public_frames[0]["frame_validation"] == "validated_tx"
        assert public_frames[1]["direction"] == "rx"
        assert public_frames[1]["command_type"] == "unknown"
        assert public_frames[1]["frame_validation"] == "unvalidated_chunk"
        assert b"capture-0001:2" in evidence.read("public/report.md")
    assert b"do-not-export-token" not in combined
    assert b"a565b1000106010000000000" not in combined
    assert b"deadbeef" not in combined
    assert b"capture-0001" in combined
    assert verify_public_archive(archive)["captures"]["complete"] is True

    tampered = tmp_path / "wrong-control-command.zip"
    with zipfile.ZipFile(archive) as source:
        contents = {name: source.read(name) for name in source.namelist()}
    frame_rows = [
        json.loads(line) for line in contents["public/frames.jsonl"].splitlines()
    ]
    frame_rows[0]["command_type"] = "status_request"
    contents["public/frames.jsonl"] = b"".join(
        (json.dumps(row, sort_keys=True) + "\n").encode() for row in frame_rows
    )
    manifest = json.loads(contents["public/manifest.json"])
    frame_manifest = next(
        row for row in manifest["files"] if row["name"] == "frames.jsonl"
    )
    frame_manifest["size"] = len(contents["public/frames.jsonl"])
    frame_manifest["sha256"] = hashlib.sha256(
        contents["public/frames.jsonl"]
    ).hexdigest()
    contents["public/manifest.json"] = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode()
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, content in contents.items():
            target.writestr(name, content)
    with pytest.raises(HAAPIError, match="matching validated control frame"):
        verify_public_archive(tampered)

    cleanup_only_root = write_run(
        tmp_path,
        "cleanup-only",
        {"schema_version": 1},
        {"schema_version": 1, "run_id": run_id},
    )
    incomplete_action_trace = deepcopy(trace)
    incomplete_action_trace["complete"] = False
    incomplete_action_trace["dropped_events"] = 1
    action_dir = cleanup_only_root / "traces" / "action"
    action_dir.mkdir(parents=True)
    _save_private_trace(action_dir, incomplete_action_trace)
    cleanup_only_dir = cleanup_only_root / "traces" / "cleanup"
    cleanup_only_dir.mkdir(parents=True)
    _save_private_trace(cleanup_only_dir, cleanup_trace)
    write_scenario_progress(
        cleanup_only_root,
        run_id,
        "output_ac_cycle",
        "b" * 64,
        [
            {
                "step_id": "ac_on",
                "outcome": "pass",
                "trace": "traces/action",
                "transaction": {
                    "transaction_id": "private-tx-id",
                    "session_generation": 1,
                    "kind": "output",
                    "command_type": "output_control",
                    "write_seq": 2,
                    "submitted_seq": 3,
                    "confirmed_seq": 4,
                },
                "cleanup_trace": "traces/cleanup",
                "cleanup_transaction": {
                    "transaction_id": "private-cleanup-tx-id",
                    "session_generation": 1,
                    "kind": "output",
                    "command_type": "output_control",
                    "write_seq": 2,
                    "submitted_seq": 3,
                    "confirmed_seq": 4,
                },
            },
            {
                "step_id": "restore_original",
                "outcome": "pass",
                "trace": "traces/action",
                "cleanup_trace": "traces/cleanup",
                "cleanup_transaction": {
                    "transaction_id": "private-cleanup-tx-id",
                    "session_generation": 1,
                    "kind": "output",
                    "command_type": "output_control",
                    "write_seq": 2,
                    "submitted_seq": 3,
                    "confirmed_seq": 4,
                },
            },
        ],
    )
    cleanup_only_archive = export_archive(tmp_path / "cleanup-only", confirmed=True)
    with zipfile.ZipFile(cleanup_only_archive) as evidence_zip:
        steps = json.loads(evidence_zip.read("public/scenarios.json"))["steps"]
    incomplete_action, cleanup_only_step = steps
    assert incomplete_action["evidence"] == [
        "capture-0001:2",
        "capture-0001:3",
        "capture-0001:4",
    ]
    assert incomplete_action["missing_evidence"] is True
    assert incomplete_action["cleanup_evidence"] == [
        "capture-0002:2",
        "capture-0002:3",
        "capture-0002:4",
    ]
    assert incomplete_action["cleanup_missing_evidence"] is False
    assert cleanup_only_step["evidence"] == []
    assert cleanup_only_step["missing_evidence"] is True
    assert (
        cleanup_only_step["cleanup_evidence"] == incomplete_action["cleanup_evidence"]
    )
    assert cleanup_only_step["cleanup_missing_evidence"] is False


def test_transaction_reference_requires_submission_and_confirmation() -> None:
    from scripts.hil.evidence import _verified_transaction_references

    event = {"transaction_id": "private-id", "session_generation": 1}
    transaction = {
        "transaction_id": "private-id",
        "session_generation": 1,
        "kind": "output",
        "command_type": "output_control",
        "write_seq": 1,
        "submitted_seq": 2,
        "confirmed_seq": 3,
    }
    events = {
        1: {**event, "seq": 1, "event": "write_attempt"},
        2: {
            **event,
            "seq": 2,
            "event": "transaction_submitted",
            "transaction_kind": "output",
        },
        3: {
            **event,
            "seq": 3,
            "event": "transaction_confirmed",
            "reason": "matching_validated_telemetry",
        },
    }
    frames = {
        1: {
            "event": "write_attempt",
            "direction": "tx",
            "frame_validation": "validated_tx",
            "command_type": "output_control",
            "session_generation": 1,
            "transaction_id": "private-id",
        }
    }
    assert _verified_transaction_references(transaction, events, frames) == [1, 2, 3]
    wrong_command = {1: {**frames[1], "command_type": "status_request"}}
    assert _verified_transaction_references(transaction, events, wrong_command) == []
    assert _verified_transaction_references(transaction, events, {}) == []
    del events[3]
    assert _verified_transaction_references(transaction, events, frames) == []


def test_real_environment_checkout_and_mismatch_facts_verify(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.hil import environment

    monkeypatch.setattr(
        environment,
        "local_commit",
        lambda: {"value": "a" * 40, "source": "runner_git_checkout", "verified": True},
    )
    monkeypatch.setattr(environment, "local_manifest_version", lambda: "2.0.0")
    env = environment.build_environment(
        {"version": "2026.10.4", "installation_type": "Home Assistant OS"},
        {"version": "1.0.0"},
    )
    write_run(
        tmp_path,
        "environment",
        env,
        {
            "schema_version": 1,
            "run_id": "0123456789abcdef",
            "qualification_status": "inconclusive",
        },
    )
    archive = export_archive(tmp_path / "environment", confirmed=True)
    manifest = verify_public_archive(archive)
    assert manifest["build_provenance"]["runner_checkout_commit"] == "a" * 40
    assert manifest["build_provenance"]["verification"] == "mismatch"
