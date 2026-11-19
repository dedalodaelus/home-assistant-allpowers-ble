"""Fail-closed onboarding gate tests for real persisted run artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.hil.onboard import assess_run_gate


@pytest.fixture(autouse=True)
def configure_event_loop():
    """Pure gate tests do not need the HA plugin's event-loop fixture."""
    yield


def _run(tmp_path):
    private = tmp_path / "run" / "private"
    private.mkdir(parents=True)
    artifact = private / "tier-bronze-read-only.json"
    artifact.write_text('{"protocol_probe":"verified"}\n', encoding="utf-8")
    trace_dir = private / "traces" / "bronze_read_only"
    trace_dir.mkdir(parents=True)
    from scripts.hil.cli import _bronze_status_exchange
    from scripts.validate_hil_qualification import STATUS_REQUEST

    capture_id = "capture-1"
    generation = 4
    sampled_at = datetime.now(UTC)
    status_measurement = {
        "battery": 50,
        "input_power": 100,
        "output_power": 25,
        "remaining_time": 90,
    }
    events = [
        {
            "event": "capture_started",
            "seq": 0,
            "session_generation": generation,
        },
        {
            "event": "write_ok",
            "seq": 2,
            "session_generation": generation,
            "transaction_id": "tx-1",
            "capture_id": capture_id,
        },
        {
            "event": "notification_decoded",
            "seq": 3,
            "session_generation": generation,
            "decoded_packet_types": ["StatusData"],
            "capture_id": capture_id,
            "utc": (sampled_at + timedelta(seconds=1)).isoformat(),
            "status_measurement": status_measurement,
        },
        {
            "event": "capture_stopped",
            "seq": 4,
            "session_generation": generation,
        },
    ]
    frames = [
        {
            "direction": "tx",
            "command_type": "status_request",
            "frame_validation": "validated_tx",
            "transaction_id": "tx-1",
            "session_generation": generation,
            "capture_id": capture_id,
            "seq": 1,
            "hex": STATUS_REQUEST.hex(),
        }
    ]
    trace_metadata = {
        "run_id": "test-run",
        "scenario_id": "read_only_snapshot",
        "capture_id": capture_id,
        "complete": True,
        "dropped_events": 0,
        "dropped_frames": 0,
        "observed_route": "active_proxy",
    }
    trace_bytes = (json.dumps(trace_metadata) + "\n").encode()
    trace_meta_path = trace_dir / "trace.json"
    trace_meta_path.write_bytes(trace_bytes)
    (trace_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (trace_dir / "frames.jsonl").write_text(
        "".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8"
    )
    bronze = {
        "fresh_verified_samples": True,
        "physical_observation": "plausible",
        "status_freshness": {
            "capture_id": capture_id,
            "session_generation": generation,
            "status_notification_count_before": 7,
            "status_notification_count_after": 8,
            "decoded_response_seq": 3,
            "decoded_response_at_utc": (sampled_at + timedelta(seconds=1)).isoformat(),
            "capture_started_seq": 0,
            "capture_stopped_seq": 4,
            "status_measurement": status_measurement,
        },
        "samples": [
            {
                "sampled_at_utc": sampled_at.isoformat(),
                "telemetry": {
                    "battery": {
                        "value": 50,
                        "verified": True,
                        "source": "ha_rest_entity_state",
                        "sample_age_seconds": 0,
                    },
                    "telemetry_available": {"verified": True, "value": "on"},
                },
            },
            {
                "sampled_at_utc": (sampled_at + timedelta(seconds=2)).isoformat(),
                "telemetry": {
                    "battery": {
                        "value": 50,
                        "verified": True,
                        "source": "ha_rest_entity_state",
                        "sample_age_seconds": 0,
                    },
                    "telemetry_available": {"verified": True, "value": "on"},
                },
            },
        ],
        "protocol_probe": {
            "outcome": "pass",
            "trace": "traces/bronze_read_only",
            "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
            "route": "active_proxy",
            "capture_id": capture_id,
            "session_generation": generation,
            "status_request_seq": 1,
            "status_write_ok_seq": 2,
            "decoded_response_seq": 3,
            "status_request_hex": STATUS_REQUEST.hex(),
        },
    }
    artifact.write_text(json.dumps(bronze), encoding="utf-8")
    evidence = {
        "path": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    session = {
        "schema_version": 1,
        "run_id": "test-run",
        "device": {
            "model": {"value": "R600", "verified": True},
            "hardware_version": {"value": "0.3", "verified": True},
            "firmware_version": {"value": "1.1", "verified": True},
        },
        "route": {"requested": "auto"},
        "integration_build": {"build_match": "verified"},
        "test_plan": {
            "requested_tier": "bronze",
            "device": {
                "model": "R600",
                "hardware_revision": "0.3",
                "firmware_revision": "1.1",
                "build_provenance": {"build_match": "verified"},
            },
            "steps": [
                {
                    "id": "read_only_probe",
                    "feature": "telemetry",
                    "action": "read_only",
                    "eligibility": "eligible",
                    "required_evidence": ["fresh telemetry", "physical plausibility"],
                },
                {
                    "id": "physical_plausibility_observation",
                    "feature": "telemetry",
                    "action": "manual",
                    "eligibility": "eligible",
                    "required_evidence": [
                        "separate operator observation",
                        "fresh telemetry context",
                    ],
                },
            ],
        },
        "runtime_capabilities": {
            "features": {"ac_output": {"physical_presence": "absent"}}
        },
    }
    execution = {
        "schema_version": 1,
        "run_id": "test-run",
        "requested_tier": "bronze",
        "execution_tier": "bronze",
        "requested_route": "auto",
        "device": {
            "model": "R600",
            "hardware_version": "0.3",
            "firmware_version": "1.1",
        },
        "verified_routes": ["active_proxy"],
        "steps": [
            {
                "step_id": step_id,
                "outcome": "pass",
                "observed_routes": ["active_proxy"],
                "machine_evidence": [evidence],
            }
            for step_id in ("read_only_probe", "physical_plausibility_observation")
        ],
    }
    (private / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (private / "tier-execution.json").write_text(
        json.dumps(execution), encoding="utf-8"
    )
    environment = {"integration": {"build_match": "verified"}}
    (private / "environment.json").write_text(json.dumps(environment), encoding="utf-8")
    return private, session, execution, artifact, _bronze_status_exchange


def _save(private, session, execution):
    (private / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (private / "tier-execution.json").write_text(
        json.dumps(execution), encoding="utf-8"
    )


def _save_bronze_artifact(private, session, execution, artifact, bronze):
    artifact.write_text(json.dumps(bronze), encoding="utf-8")
    execution["steps"][0]["machine_evidence"][0]["sha256"] = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    _save(private, session, execution)


def test_complete_bronze_run_is_eligible_for_review(tmp_path):
    private, _, _, _, status_exchange = _run(tmp_path)

    assert (
        assess_run_gate(
            private,
            run_result=0,
            requested_tier="bronze",
            status_exchange=status_exchange,
        )
        == []
    )


@pytest.mark.parametrize("bad_measurement", ["missing", "non_numeric", "wrong_source"])
def test_bronze_gate_rejects_recent_samples_without_numeric_rest_measurement(
    tmp_path, bad_measurement
):
    private, session, execution, artifact, status_exchange = _run(tmp_path)
    bronze = json.loads(artifact.read_text(encoding="utf-8"))
    for sample in bronze["samples"]:
        battery = sample["telemetry"]["battery"]
        if bad_measurement == "missing":
            battery.pop("value")
        elif bad_measurement == "non_numeric":
            battery["value"] = "50"
        else:
            battery["source"] = "unverified"
    _save_bronze_artifact(private, session, execution, artifact, bronze)

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert any(
        item["step_id"] == "read_only_probe"
        and item["outcome"] == "rest_timestamp_stale"
        for item in blockers
    )


def test_bronze_gate_reports_overlong_sample_window_separately(tmp_path):
    private, session, execution, artifact, status_exchange = _run(tmp_path)
    bronze = json.loads(artifact.read_text(encoding="utf-8"))
    first_sample_time = datetime.now(UTC)
    decoded_time = first_sample_time + timedelta(seconds=30)
    bronze["samples"][0]["sampled_at_utc"] = first_sample_time.isoformat()
    bronze["samples"][1]["sampled_at_utc"] = (
        first_sample_time + timedelta(seconds=61)
    ).isoformat()
    for index, sample in enumerate(bronze["samples"]):
        sample["telemetry"]["battery"]["sample_age_seconds"] = 815 + index
    bronze["fresh_verified_samples"] = False
    bronze["status_freshness"]["decoded_response_at_utc"] = decoded_time.isoformat()
    execution["steps"][0]["outcome"] = "inconclusive"
    _save_bronze_artifact(private, session, execution, artifact, bronze)

    events_path = private / "traces" / "bronze_read_only" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    decoded = next(
        event for event in events if event["event"] == "notification_decoded"
    )
    decoded["utc"] = decoded_time.isoformat()
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert any(
        item["step_id"] == "read_only_probe"
        and item["outcome"] == "sample_window_exceeded"
        for item in blockers
    )
    assert not any(
        item["step_id"] == "read_only_probe"
        and item["outcome"] == "uncorrelated_capture"
        for item in blockers
    )


@pytest.mark.parametrize(
    ("failure", "expected_step"),
    [
        ("bad_build", "build_verification"),
        ("requested_tier", "tier_scope"),
        ("downgraded_tier", "tier_scope"),
        ("unverified_route", "observed_route"),
        ("inconclusive_step", "read_only_probe"),
        ("missing_step", "physical_plausibility_observation"),
        ("duplicate_step", "read_only_probe"),
        ("digest_mismatch", "read_only_probe"),
        ("wrong_step_route", "read_only_probe"),
        ("unreviewed_exclusion", "physical_plausibility_observation"),
    ],
)
def test_inconsistent_run_fails_closed(tmp_path, failure, expected_step):
    private, session, execution, artifact, status_exchange = _run(tmp_path)
    read_only = execution["steps"][0]
    if failure == "bad_build":
        session["test_plan"]["device"]["build_provenance"]["build_match"] = "unverified"
    elif failure == "requested_tier":
        execution["requested_tier"] = "silver"
    elif failure == "downgraded_tier":
        execution["execution_tier"] = "silver"
    elif failure == "unverified_route":
        execution["verified_routes"] = ["auto"]
    elif failure == "inconclusive_step":
        read_only["outcome"] = "inconclusive"
    elif failure == "missing_step":
        execution["steps"].pop()
    elif failure == "duplicate_step":
        execution["steps"].append(dict(read_only))
    elif failure == "digest_mismatch":
        artifact.write_text("changed\n", encoding="utf-8")
    elif failure == "wrong_step_route":
        read_only["observed_routes"] = ["local_adapter"]
    elif failure == "unreviewed_exclusion":
        execution["steps"][1]["outcome"] = "not_applicable"
    _save(private, session, execution)

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert expected_step in {item["step_id"] for item in blockers}


def test_runner_failure_is_reported_even_when_files_exist(tmp_path):
    private, _, _, _, status_exchange = _run(tmp_path)

    blockers = assess_run_gate(
        private,
        run_result=2,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert blockers[0]["step_id"] == "runner"
    assert blockers[0]["outcome"] == "2"


def test_missing_and_malformed_run_files_stop_gate(tmp_path):
    private = tmp_path / "run" / "private"
    private.mkdir(parents=True)
    (private / "session.json").write_text("[]", encoding="utf-8")
    (private / "tier-execution.json").write_text("{", encoding="utf-8")

    blockers = assess_run_gate(private, run_result=0, requested_tier="bronze")

    assert {item["step_id"] for item in blockers} == {"session", "tier_execution"}


def test_confirmed_not_applicable_plan_step_is_not_a_blocker(tmp_path):
    private, session, execution, _, status_exchange = _run(tmp_path)
    session["test_plan"]["steps"].append(
        {
            "id": "absent_control",
            "feature": "ac_output",
            "action": "not_applicable",
            "eligibility": "not_applicable",
            "reasons": ["runtime profile substantiates physical absence"],
        }
    )
    execution["steps"].append(
        {
            "step_id": "absent_control",
            "outcome": "not_applicable",
        }
    )
    _save(private, session, execution)

    assert (
        assess_run_gate(
            private,
            run_result=0,
            requested_tier="bronze",
            status_exchange=status_exchange,
        )
        == []
    )


@pytest.mark.parametrize(
    ("plan_change", "expected_step"),
    [
        ("absent", "read_only_probe"),
        ("empty", "read_only_probe"),
        ("omit_physical", "physical_plausibility_observation"),
        ("mandatory_exclusion", "read_only_probe"),
    ],
)
def test_mandatory_bronze_steps_cannot_be_removed_or_excluded(
    tmp_path, plan_change, expected_step
):
    private, session, execution, _, status_exchange = _run(tmp_path)
    steps = session["test_plan"]["steps"]
    if plan_change == "absent":
        session.pop("test_plan")
    elif plan_change == "empty":
        session["test_plan"]["steps"] = []
        execution["steps"] = []
    elif plan_change == "omit_physical":
        session["test_plan"]["steps"] = steps[:1]
        execution["steps"] = execution["steps"][:1]
    else:
        steps[0]["action"] = "not_applicable"
        steps[0]["eligibility"] = "not_applicable"
    _save(private, session, execution)

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert expected_step in {item["step_id"] for item in blockers}


def test_optional_exclusion_without_absence_evidence_fails_closed(tmp_path):
    private, session, execution, _, status_exchange = _run(tmp_path)
    session["test_plan"]["steps"].append(
        {
            "id": "ac_output_cycle",
            "feature": "ac_output",
            "action": "not_applicable",
            "eligibility": "not_applicable",
            "reasons": [],
        }
    )
    execution["steps"].append(
        {"step_id": "ac_output_cycle", "outcome": "not_applicable"}
    )
    session["runtime_capabilities"]["features"]["ac_output"] = {
        "physical_presence": "unknown"
    }
    _save(private, session, execution)

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert any(
        item["step_id"] == "ac_output_cycle" and item["outcome"] == "invalid_exclusion"
        for item in blockers
    )


def test_unsupported_tier_cannot_enter_bronze_review(tmp_path):
    private, session, execution, _, status_exchange = _run(tmp_path)
    session["test_plan"]["requested_tier"] = "silver"
    execution["requested_tier"] = "silver"
    execution["execution_tier"] = "silver"
    _save(private, session, execution)

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="silver",
        status_exchange=status_exchange,
    )

    assert any(item["step_id"] == "independent_review" for item in blockers)


def test_artifact_path_cannot_escape_run_tree(tmp_path):
    private, _, execution, _, status_exchange = _run(tmp_path)
    outside = tmp_path.parent / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    evidence = execution["steps"][0]["machine_evidence"][0]
    evidence["path"] = "../../../outside.json"
    evidence["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    _save(private, json.loads((private / "session.json").read_text()), execution)

    blockers = assess_run_gate(
        private,
        run_result=0,
        requested_tier="bronze",
        status_exchange=status_exchange,
    )

    assert any(item["outcome"] == "missing_or_invalid_artifact" for item in blockers)
