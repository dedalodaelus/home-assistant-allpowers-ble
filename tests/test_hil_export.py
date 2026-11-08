"""Archive confirmation and integrity tests for HIL exports."""

import json
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


def test_validated_private_trace_exports_allowlisted_events_but_no_frames(
    tmp_path: Path,
) -> None:
    run_id = "0123456789abcdef"
    private = write_run(
        tmp_path,
        "trace-run",
        {"schema_version": 1},
        {"schema_version": 1, "run_id": run_id},
    )
    event = {
        "schema_version": 1,
        "seq": 1,
        "utc": "2026-11-05T00:00:00+00:00",
        "elapsed_ms": 1.0,
        "run_id": run_id,
        "scenario_id": "output_ac_cycle",
        "layer": "gatt_api",
        "session_generation": 1,
        "observed_route": "unverified",
        "event": "write_attempt",
        "transaction_id": None,
        "vendor_private": "do-not-export-token",
    }
    frame = {
        **{k: v for k, v in event.items() if k != "vendor_private"},
        "direction": "tx",
        "command_type": "status_request",
        "frame_validation": "validated_tx",
        "hex": "a565b1000106010000000000",
    }
    trace = {
        "schema_version": 1,
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
        "observed_route": "unverified",
        "complete": True,
        "dropped_events": 0,
        "dropped_frames": 0,
        "event_count": 1,
        "frame_count": 1,
        "events": [event],
        "frames": [frame],
    }
    trace_dir = private / "traces" / "output_ac_cycle"
    trace_dir.mkdir(parents=True)
    _save_private_trace(trace_dir, trace)
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
        assert "public/frames.jsonl" not in members
        combined = b"".join(evidence.read(name) for name in members)
        scenarios = json.loads(evidence.read("public/scenarios.json"))
        assert scenarios["steps"][0]["evidence"] == ["capture-0001:1"]
        assert scenarios["steps"][0]["missing_evidence"] is False
        assert b"capture-0001:1" in evidence.read("public/report.md")
    assert b"do-not-export-token" not in combined
    assert b"a565b1000106010000000000" not in combined
    assert b"capture-0001" in combined
    assert verify_public_archive(archive)["captures"]["complete"] is True
