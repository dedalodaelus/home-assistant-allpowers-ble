"""Archive confirmation and integrity tests for HIL exports."""

import json
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any
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


def _rewrite_archive_members(
    archive: Path, destination: Path, updates: dict[str, bytes]
) -> None:
    with zipfile.ZipFile(archive) as source:
        contents = {name: source.read(name) for name in source.namelist()}
    contents.update({f"public/{name}": payload for name, payload in updates.items()})
    manifest = json.loads(contents["public/manifest.json"])
    manifest["files"] = [
        {
            "name": name.removeprefix("public/"),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(contents.items())
        if name != "public/manifest.json"
    ]
    contents["public/manifest.json"] = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, payload in contents.items():
            target.writestr(name, payload)


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


def test_blocked_output_run_is_retained_in_public_report(tmp_path: Path) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from scripts.hil.discovery import Device
    from scripts.hil.outputs import _execute_output_suite
    from scripts.hil.scenarios import load_scenarios
    from scripts.hil.storage import write_scenario_progress

    run_id = "0123456789abcdef"
    run = write_run(
        tmp_path,
        "blocked-output",
        {"schema_version": 1},
        {
            "schema_version": 1,
            "run_id": run_id,
            "qualification_status": "inconclusive",
        },
    )
    services = SimpleNamespace(
        load_scenarios=load_scenarios,
        device_fingerprint=lambda *args: "a" * 64,
        write_scenario_progress=write_scenario_progress,
    )
    device = Device(
        device_id="private-device-id",
        model="S200",
        hardware=None,
        firmware=None,
        state_entities={},
        entry_state=None,
        config_entry_id=None,
    )

    result = _execute_output_suite(
        SimpleNamespace(origin="http://home-assistant.local"),
        Namespace(
            scenario_id="output_ac_cycle", non_interactive=True, route="local_adapter"
        ),
        device,
        {},
        run,
        run_id,
        services,
    )

    assert result == 2
    archive = export_archive(run, confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        scenarios = json.loads(exported.read("public/scenarios.json"))
        report = exported.read("public/report.md").decode("utf-8")

    assert scenarios["qualification_status"] == "inconclusive"
    assert len(scenarios["steps"]) == 1
    step = scenarios["steps"][0]
    assert step["step_id"] == "output_preflight"
    assert step["outcome"] == "not_applicable"
    assert step["limitation_reason"] == "precondition_or_observation_incomplete"
    assert "`output_preflight`: **not_applicable**" in report
    assert "limitation `precondition_or_observation_incomplete`" in report
    assert verify_public_archive(archive)["qualification_status"] == "inconclusive"


def test_interrupted_zip_export_cleans_temp_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    _run(run)
    archive = export_archive(run, confirmed=True)
    previous = archive.read_bytes()

    def interrupt_write(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(zipfile.ZipFile, "writestr", interrupt_write)
    with pytest.raises(KeyboardInterrupt):
        export_archive(run, confirmed=True)
    assert archive.read_bytes() == previous
    assert list(run.glob(".hil-evidence.zip.*.tmp")) == []

    monkeypatch.undo()
    retried = export_archive(run, confirmed=True)
    assert retried == archive
    assert verify_public_archive(retried)["export_confirmed"] is True


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


def test_archive_reimport_rejects_extra_member(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _run(run)
    archive = export_archive(run, confirmed=True)
    tampered = tmp_path / "extra-member.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for member in source.infolist():
            target.writestr(member.filename, source.read(member.filename))
        target.writestr("public/unexpected.txt", b"private data")

    with pytest.raises(HAAPIError, match="Unsafe HIL archive members"):
        verify_public_archive(tampered)


def test_archive_reimport_rejects_malformed_zip(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.zip"
    malformed.write_bytes(b"this is not a ZIP archive")

    with pytest.raises(HAAPIError, match="Invalid HIL archive"):
        verify_public_archive(malformed)


@pytest.mark.parametrize(
    ("member", "payload", "error"),
    [
        (
            "environment.json",
            {"schema_version": 1, "source_schema_version": 1, "data": {}},
            "Unsupported JSON schema",
        ),
        (
            "scenarios.json",
            {
                "schema_version": 1,
                "source_schema_version": 2,
                "scenario_id": "output_ac_cycle",
                "qualification_status": "inconclusive",
                "steps": [],
            },
            "Unsupported JSON schema",
        ),
        (
            "session.json",
            {
                "schema_version": 1,
                "source_schema_version": 1,
                "data": {"qualification_status": "inconclusive"},
            },
            "Unsupported JSON schema",
        ),
        (
            "session.json",
            {
                "schema_version": 2,
                "source_schema_version": 1,
                "data": {
                    "run_id": "0123456789abcdef",
                    "qualification_status": "inconclusive",
                    "started_at": {"unexpected": "object"},
                },
            },
            "Invalid public session timestamp",
        ),
        (
            "frames.jsonl",
            {
                "schema_version": 2,
                "capture_id": "capture-0001",
                "seq": 1,
                "direction": "tx",
                "command_type": "status_request",
                "frame_validation": "validated_tx",
                "session_generation": "1",
                "payload_omitted": True,
            },
            "Malformed JSONL",
        ),
        (
            "frames.jsonl",
            {
                "schema_version": 2,
                "capture_id": "capture-0001",
                "seq": 1,
                "direction": "tx",
                "command_type": "status_request",
                "frame_validation": "validated_tx",
                "session_generation": 1_000_001,
                "payload_omitted": True,
            },
            "Malformed JSONL",
        ),
    ],
)
def test_archive_verifier_rejects_rehashed_schema_and_type_tampering(
    tmp_path: Path, member: str, payload: dict[str, Any], error: str
) -> None:
    run = tmp_path / "run"
    _run(run)
    archive = export_archive(run, confirmed=True)
    tampered = tmp_path / "tampered-contract.zip"
    if member.endswith(".jsonl"):
        data = (json.dumps(payload, sort_keys=True) + "\n").encode()
        updates = {member: data}
    else:
        updates = {
            member: (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        }
    _rewrite_archive_members(archive, tampered, updates)

    with pytest.raises(HAAPIError, match=error):
        verify_public_archive(tampered)


def test_environment_export_omits_sensitive_and_unknown_source_fields(
    tmp_path: Path,
) -> None:
    run_id = "0123456789abcdef"
    private_ip = "192.0.2.123"
    host_secret = "secret-hostname.internal"
    env = {
        "schema_version": 1,
        "home_assistant": {
            "os": {"value": "Linux", "source": "runner_platform"},
            "cpu": {"value": private_ip, "source": "runner_platform"},
            "hostname": host_secret,
        },
        "private_network": private_ip,
    }
    write_run(
        tmp_path,
        "sensitive-environment",
        env,
        {
            "schema_version": 1,
            "run_id": run_id,
            "qualification_status": "inconclusive",
        },
    )

    archive = export_archive(tmp_path / "sensitive-environment", confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        members = b"".join(exported.read(name) for name in exported.namelist())
        environment = json.loads(exported.read("public/environment.json"))

    assert private_ip.encode() not in members
    assert host_secret.encode() not in members
    assert "cpu" not in environment["data"]["home_assistant"]
    assert "hostname" not in environment["data"]["home_assistant"]
    assert environment["omitted"] == ["unknown_source_fields"]
    verify_public_archive(archive)


def test_environment_export_rejects_embedded_ip_and_hostname_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hil import environment

    monkeypatch.setattr(
        environment,
        "platform",
        type(
            "PlatformFacts",
            (),
            {
                "system": staticmethod(lambda: "Linux"),
                "machine": staticmethod(lambda: "x86_64"),
                "release": staticmethod(lambda: "6.8.0"),
                "processor": staticmethod(lambda: "Intel Core i7 homebox.local"),
                "python_version": staticmethod(lambda: "3.14.0"),
            },
        ),
    )
    monkeypatch.setattr(
        environment, "local_commit", lambda: environment.unknown("unavailable")
    )
    monkeypatch.setattr(environment, "local_manifest_version", lambda: "1.2.3")

    malicious_version = "2026.10.4.192.168.1.25"
    env = environment.build_environment(
        {"version": malicious_version, "installation_type": "Home Assistant OS"},
        {"version": "1.2.3"},
    )
    write_run(
        tmp_path,
        "embedded-identifiers",
        env,
        {"schema_version": 1, "run_id": "0123456789abcdef"},
    )
    archive = export_archive(tmp_path / "embedded-identifiers", confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        public_members = {name: exported.read(name) for name in exported.namelist()}
    combined = b"".join(public_members.values())
    assert b"192.168.1.25" not in combined
    assert b"homebox.local" not in combined
    public_environment = json.loads(public_members["public/environment.json"])
    assert "version" not in public_environment["data"]["home_assistant"]
    assert "cpu" not in public_environment["data"]["runner_host"]
    monkeypatch.setattr(
        environment.platform,
        "processor",
        lambda: "Intel Core i7-10700K",
    )
    normal_env = environment.build_environment(
        {"version": "2026.10.4", "installation_type": "Home Assistant OS"},
        {"version": "1.2.3"},
    )
    write_run(
        tmp_path,
        "ordinary-environment",
        normal_env,
        {"schema_version": 1, "run_id": "1123456789abcdef"},
    )
    normal_archive = export_archive(tmp_path / "ordinary-environment", confirmed=True)
    with zipfile.ZipFile(normal_archive) as exported:
        normal_public = json.loads(exported.read("public/environment.json"))
    assert normal_public["data"]["home_assistant"]["version"]["value"] == "2026.10.4"
    assert (
        normal_public["data"]["runner_host"]["cpu"]["value"] == "Intel Core i7-10700K"
    )


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
                "pre_state": "off",
                "post_state": "on",
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
        assert scenarios["scenario_id"] == "output_ac_cycle"
        assert scenarios["steps"][0]["target"] == "ac_output"
        assert scenarios["steps"][0]["expected"] == "on"
        assert scenarios["steps"][0]["actual"] == "on"
        assert scenarios["steps"][0]["observation"]["pre_state"] == "off"
        assert scenarios["steps"][0]["observation_evidence"] == ["capture-0001:1"]
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
        assert b"Scenario: `output_ac_cycle`" in evidence.read("public/report.md")
        assert b"expected `on`; observed `on`" in evidence.read("public/report.md")
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


def test_public_nonwrite_observation_summaries_are_typed_and_redacted() -> None:
    from scripts.hil.evidence import _public_observation

    recovery = _public_observation(
        "recovery_reconnect",
        {
            "step_id": "recovered_session",
            "outcome": "pass",
            "connected": True,
            "status_fresh": True,
            "session_generation_before": 4,
            "session_generation_after": 5,
            "disconnect_observed": True,
            "route": "local_adapter",
            "reason": "private address aa:bb:cc:dd:ee:ff recovered",
        },
    )
    assert recovery["expected"] == "new session generation and fresh status"
    assert recovery["actual"] == "reconnected"
    assert recovery["observation"]["session_generation_after"] == 5
    assert recovery["observation"]["route"] == "local_adapter"
    assert recovery["limitation_reason"] == "precondition_or_observation_incomplete"
    assert "aa:bb:cc:dd:ee:ff" not in json.dumps(recovery)

    soak = _public_observation(
        "soak_24h_observation",
        {
            "step_id": "soak_segment",
            "outcome": "inconclusive",
            "segment": 2,
            "sample_count": 14,
            "measured_seconds": 3900.0,
            "maximum_gap_seconds": 300.0,
            "trace_count": 1,
            "dropped_traces": 0,
            "clock_discontinuities": 0,
            "restart_discontinuity": True,
            "reason": "process restart makes an unobserved interval unknowable",
        },
    )
    assert soak["expected"] == "24 hours of uninterrupted measured observations"
    assert soak["actual"] == "inconclusive"
    assert soak["observation"]["sample_count"] == 14
    assert soak["observation"]["maximum_gap_seconds"] == 300.0
    assert soak["limitation_reason"] == "restart_discontinuity"


def test_recovery_route_observation_exports_with_controlled_scenario_summary(
    tmp_path: Path,
) -> None:
    run_id = "0123456789abcdef"
    private = write_run(
        tmp_path,
        "recovery",
        {"schema_version": 1},
        {"schema_version": 1, "run_id": run_id},
    )
    event = {
        "schema_version": 1,
        "seq": 1,
        "utc": "2026-11-05T00:00:00+00:00",
        "elapsed_ms": 1.0,
        "run_id": run_id,
        "scenario_id": "recovery_reconnect",
        "layer": "gatt_api",
        "session_generation": 2,
        "observed_route": "unverified",
        "event": "route_observed",
    }
    trace_dir = private / "traces" / "recovery"
    trace_dir.mkdir(parents=True)
    _save_private_trace(
        trace_dir,
        {
            "schema_version": 1,
            "capture_id": "private-recovery-capture",
            "run_id": run_id,
            "scenario_id": "recovery_reconnect",
            "requested_route": "auto",
            "capture_layer": "gatt_api_not_rf",
            "private_raw_bytes": True,
            "has_gatt_frames": False,
            "closed": True,
            "expired": False,
            "termination_reason": "requested_stop",
            "qualification_allowed": False,
            "observed_route": "unverified",
            "complete": True,
            "dropped_events": 0,
            "dropped_frames": 0,
            "event_count": 1,
            "frame_count": 0,
            "events": [event],
            "frames": [],
        },
    )
    write_scenario_progress(
        private,
        run_id,
        "recovery_reconnect",
        "c" * 64,
        [
            {
                "step_id": "baseline",
                "outcome": "pass",
                "connected": True,
                "status_fresh": True,
                "session_generation": 1,
            },
            {
                "step_id": "operator_action",
                "outcome": "pass",
                "action": "vendor-app contention",
                "operator_confirmed": True,
            },
            {
                "step_id": "recovered_session",
                "outcome": "pass",
                "trace": "traces/recovery",
                "connected": True,
                "status_fresh": True,
                "session_generation_before": 1,
                "session_generation_after": 2,
                "disconnect_observed": True,
                "trace_complete": True,
                "route": "unverified",
                "physical_outcome": "yes",
            },
        ],
    )
    archive = export_archive(tmp_path / "recovery", confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        scenarios = json.loads(exported.read("public/scenarios.json"))
        assert scenarios["scenario_id"] == "recovery_reconnect"
        recovered = scenarios["steps"][2]
        assert recovered["expected"] == "new session generation and fresh status"
        assert recovered["actual"] == "reconnected"
        assert recovered["observation_evidence"] == ["capture-0001:1"]
        assert recovered["missing_observation_evidence"] is False
        report = exported.read("public/report.md")
        assert b"Scenario: `recovery_reconnect`" in report
        assert b"new session generation and fresh status" in report
        assert verify_public_archive(archive)["qualification_status"] == "inconclusive"


def test_soak_summary_exports_metrics_without_private_samples(tmp_path: Path) -> None:
    run_id = "0123456789abcdef"
    private = write_run(
        tmp_path,
        "soak",
        {"schema_version": 1},
        {"schema_version": 1, "run_id": run_id},
    )
    write_scenario_progress(
        private,
        run_id,
        "soak_24h_observation",
        "d" * 64,
        [
            {
                "step_id": "soak_segment",
                "segment": 1,
                "outcome": "inconclusive",
                "measured_seconds": 1800.0,
                "sample_count": 7,
                "trace_count": 0,
                "maximum_gap_seconds": 300.0,
                "dropped_traces": 1,
                "clock_discontinuities": 0,
                "restart_discontinuity": True,
                "reason": "process restart makes the unobserved interval unknowable",
            }
        ],
    )
    (private / "soak_samples_001.jsonl").write_text(
        '{"event":"sample","private_state":"192.168.1.25"}\n', encoding="utf-8"
    )
    archive = export_archive(tmp_path / "soak", confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        scenarios = json.loads(exported.read("public/scenarios.json"))
        step = scenarios["steps"][0]
        assert scenarios["scenario_id"] == "soak_24h_observation"
        assert step["expected"] == "24 hours of uninterrupted measured observations"
        assert step["actual"] == "inconclusive"
        assert step["observation"]["sample_count"] == 7
        assert step["observation"]["maximum_gap_seconds"] == 300.0
        assert "soak_samples_001.jsonl" not in exported.namelist()
        assert b"192.168.1.25" not in b"".join(
            exported.read(name) for name in exported.namelist()
        )
        assert verify_public_archive(archive)["qualification_status"] == "inconclusive"


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
