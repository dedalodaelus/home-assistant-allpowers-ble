"""Fail-closed assessment of guided evidence before onboarding handoff."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import stat
from typing import Any


VERIFIED_ROUTES = {"local_adapter", "active_proxy"}
PASS_OUTCOMES = {"pass", "not_applicable"}
BRONZE_REQUIRED_STEP_CONTRACTS = {
    "read_only_probe": {
        "feature": "telemetry",
        "action": "read_only",
        "required_evidence": ["fresh telemetry", "physical plausibility"],
    },
    "physical_plausibility_observation": {
        "feature": "telemetry",
        "action": "manual",
        "required_evidence": [
            "separate operator observation",
            "fresh telemetry context",
        ],
    },
}

TIER_REQUIRED_STEP_IDS = {
    "bronze": set(BRONZE_REQUIRED_STEP_CONTRACTS),
    "silver": {
        *BRONZE_REQUIRED_STEP_CONTRACTS,
        "ac_output_cycle",
        "dc_output_cycle",
        "light_output_cycle",
        "basic_reconnect",
        "minimum_output_requirement",
    },
    "gold": {
        *BRONZE_REQUIRED_STEP_CONTRACTS,
        "ac_output_cycle",
        "dc_output_cycle",
        "light_output_cycle",
        "basic_reconnect",
        "eco_mode_reversal",
        "work_mode_reversal",
        "eco_timeout_reversal",
        "car_charger_reversal",
        "guarded_recovery",
        "minimum_output_requirement",
    },
}

TIER_OPTIONAL_ABSENCE_FEATURES = {
    "ac_output_cycle": "ac_output",
    "dc_output_cycle": "dc_output",
    "light_output_cycle": "light_output",
    "eco_mode_reversal": "eco_mode",
    "work_mode_reversal": "work_mode",
    "eco_timeout_reversal": "eco_timeout",
    "car_charger_reversal": "car_charger",
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def _finding(step_id: str, outcome: str, reason: str, evidence: Path) -> dict[str, str]:
    return {
        "step_id": step_id,
        "outcome": outcome,
        "reason": reason,
        "evidence": str(evidence),
    }


def _bronze_sample_window_exceeded(bronze: Any) -> bool:
    """Identify a valid runner-clock sample interval longer than 60 seconds."""
    samples = bronze.get("samples") if isinstance(bronze, dict) else None
    if not isinstance(samples, list) or len(samples) != 2:
        return False
    timestamps = []
    for sample in samples:
        value = sample.get("sampled_at_utc") if isinstance(sample, dict) else None
        if not isinstance(value, str):
            return False
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return False
        timestamps.append(timestamp.astimezone(UTC))
    return (timestamps[1] - timestamps[0]).total_seconds() > 60


def _bronze_sample_window_is_only_freshness_failure(bronze: Any) -> bool:
    """Check whether the 60-second runner sample interval is the sole failure."""
    if not _bronze_sample_window_exceeded(bronze):
        return False
    samples = bronze.get("samples")
    freshness = bronze.get("status_freshness")
    if (
        not isinstance(samples, list)
        or len(samples) != 2
        or not isinstance(freshness, dict)
    ):
        return False
    try:
        first_time = datetime.fromisoformat(
            samples[0]["sampled_at_utc"].replace("Z", "+00:00")
        )
    except AttributeError, KeyError, TypeError, ValueError:
        return False
    normalized_samples = [dict(samples[0]), dict(samples[1])]
    normalized_samples[1]["sampled_at_utc"] = (
        first_time + timedelta(seconds=60)
    ).isoformat()
    status_event = {
        "event": "notification_decoded",
        "capture_id": freshness.get("capture_id"),
        "session_generation": freshness.get("session_generation"),
        "seq": freshness.get("decoded_response_seq"),
        "utc": freshness.get("decoded_response_at_utc"),
        "status_measurement": freshness.get("status_measurement"),
    }
    try:
        from .evidence import _bronze_samples_fresh

        return _bronze_samples_fresh(
            normalized_samples,
            status_evidence=freshness,
            status_event=status_event,
        )
    except ImportError, TypeError, ValueError:
        return False


def _safe_read_file(run_directory: Path, relative: str) -> bytes | None:
    root = run_directory.parent.parent.resolve()
    candidate = Path(os.path.abspath(run_directory / relative))
    try:
        parts = candidate.relative_to(root).parts
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                return None
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return candidate.read_bytes()
    except OSError, ValueError:
        return None


def _verified_artifact_refs(
    execution_step: dict[str, Any], run_directory: Path
) -> list[dict[str, str]]:
    references = execution_step.get("machine_evidence")
    if not isinstance(references, list) or not references:
        return []
    valid: list[dict[str, str]] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        relative = reference.get("path")
        digest = reference.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            continue
        content = _safe_read_file(run_directory, relative)
        if content is None:
            continue
        if hashlib.sha256(content).hexdigest() != digest:
            continue
        valid.append({"path": relative, "sha256": digest})
    return valid


def _valid_tier_step_acquisition(
    run_directory: Path, step_id: str, references: list[dict[str, str]]
) -> bool:
    """Require the runner's reviewed-step artifact to prove safe restoration."""
    artifact = next(
        (
            item
            for item in references
            if Path(item["path"]).name == f"tier-{step_id}.json"
        ),
        None,
    )
    content = (
        _safe_read_file(run_directory, artifact["path"])
        if artifact is not None
        else None
    )
    try:
        evidence = json.loads(content) if content is not None else None
    except UnicodeError, ValueError:
        return False
    if not isinstance(evidence, dict) or evidence.get("step_id") != step_id:
        return False
    is_output = step_id.endswith("_output_cycle")
    actions = evidence.get("actions")
    expected_actions = 4 if is_output else 2
    if (
        evidence.get("outcome") != "pass"
        or evidence.get("restored_state") != evidence.get("initial_state")
        or evidence.get("physical_restore_outcome") != "correct"
        or not isinstance(actions, list)
        or len(actions) != expected_actions
    ):
        return False
    if is_output and (
        evidence.get("final_restore_action_index") != 3
        or evidence.get("final_restore_outcome") != "correct"
    ):
        return False
    for action in actions:
        trace_refs = action.get("trace_evidence") if isinstance(action, dict) else None
        if (
            not isinstance(action, dict)
            or action.get("outcome") != "confirmed"
            or action.get("confirmed") is not True
            or action.get("trace_valid") is not True
            or action.get("trace_complete") is not True
            or action.get("route") not in VERIFIED_ROUTES
            or action.get("trace_route") != action.get("route")
            or not isinstance(trace_refs, list)
            or not trace_refs
        ):
            return False
        for reference in trace_refs:
            if not isinstance(reference, dict):
                return False
            relative = reference.get("path")
            digest = reference.get("sha256")
            trace_content = (
                _safe_read_file(run_directory, relative)
                if isinstance(relative, str)
                else None
            )
            if (
                trace_content is None
                or not isinstance(digest, str)
                or hashlib.sha256(trace_content).hexdigest() != digest
            ):
                return False
    return True


def _valid_bronze_capture(
    run_directory: Path,
    bronze: dict[str, Any],
    *,
    run_id: str,
    routes: list[str],
    requested_route: str,
    status_exchange: Any,
) -> bool:
    probe = bronze.get("protocol_probe")
    trace_dir = probe.get("trace") if isinstance(probe, dict) else None
    if (
        not isinstance(probe, dict)
        or probe.get("outcome") != "pass"
        or probe.get("route") not in routes
        or requested_route not in {"auto", *VERIFIED_ROUTES}
        or (requested_route != "auto" and probe.get("route") != requested_route)
        or type(probe.get("session_generation")) is not int
        or probe["session_generation"] < 1
        or not isinstance(trace_dir, str)
    ):
        return False
    try:
        from scripts import validate_hil_qualification as validation

        expected_request = validation.STATUS_REQUEST.hex()
    except ImportError, AttributeError:
        return False
    if probe.get("status_request_hex") != expected_request:
        return False
    trace_meta = _safe_read_file(run_directory, f"{trace_dir}/trace.json")
    events_bytes = _safe_read_file(run_directory, f"{trace_dir}/events.jsonl")
    frames_bytes = _safe_read_file(run_directory, f"{trace_dir}/frames.jsonl")
    expected_trace_digest = probe.get("trace_sha256")
    if (
        trace_meta is None
        or events_bytes is None
        or frames_bytes is None
        or not isinstance(expected_trace_digest, str)
        or hashlib.sha256(trace_meta).hexdigest() != expected_trace_digest
    ):
        return False
    try:
        metadata = json.loads(trace_meta)
        events = [json.loads(line) for line in events_bytes.splitlines() if line]
        frames = [json.loads(line) for line in frames_bytes.splitlines() if line]
    except UnicodeError, ValueError:
        return False
    if (
        not isinstance(metadata, dict)
        or metadata.get("run_id") != run_id
        or metadata.get("scenario_id") != "read_only_snapshot"
        or metadata.get("capture_id") != probe.get("capture_id")
        or metadata.get("complete") is not True
        or metadata.get("dropped_events") != 0
        or metadata.get("dropped_frames") != 0
        or metadata.get("observed_route") != probe.get("route")
    ):
        return False
    status_tx, write_ok, decoded, write_failed = status_exchange(events, frames)
    correlated_records = (status_tx, write_ok, decoded)
    correlated = bool(
        status_tx is not None
        and write_ok is not None
        and decoded is not None
        and not write_failed
        and all(
            record.get("capture_id", metadata["capture_id"]) == probe.get("capture_id")
            for record in correlated_records
            if isinstance(record, dict)
        )
        and status_tx.get("hex") == expected_request
        and status_tx.get("session_generation") == probe.get("session_generation")
        and status_tx.get("seq") == probe.get("status_request_seq")
        and write_ok.get("seq") == probe.get("status_write_ok_seq")
        and decoded.get("seq") == probe.get("decoded_response_seq")
    )
    if not correlated:
        return False
    freshness = bronze.get("status_freshness")
    if freshness is None:
        return True
    capture_started = [
        event for event in events if event.get("event") == "capture_started"
    ]
    capture_stopped = [
        event for event in events if event.get("event") == "capture_stopped"
    ]
    return bool(
        isinstance(freshness, dict)
        and set(freshness)
        == {
            "capture_id",
            "session_generation",
            "status_notification_count_before",
            "status_notification_count_after",
            "decoded_response_seq",
            "decoded_response_at_utc",
            "capture_started_seq",
            "capture_stopped_seq",
            "status_measurement",
        }
        and freshness.get("capture_id") == probe.get("capture_id")
        and freshness.get("session_generation") == status_tx.get("session_generation")
        and freshness.get("decoded_response_seq") == decoded.get("seq")
        and freshness.get("decoded_response_at_utc") == decoded.get("utc")
        and freshness.get("status_measurement") == decoded.get("status_measurement")
        and len(capture_started) == 1
        and len(capture_stopped) == 1
        and freshness.get("capture_started_seq") == capture_started[0].get("seq")
        and freshness.get("capture_stopped_seq") == capture_stopped[0].get("seq")
        and type(freshness.get("capture_started_seq")) is int
        and type(freshness.get("capture_stopped_seq")) is int
        and capture_started[0].get("session_generation")
        == status_tx.get("session_generation")
        and freshness["capture_started_seq"]
        < decoded["seq"]
        < freshness["capture_stopped_seq"]
        and type(freshness.get("status_notification_count_before")) is int
        and type(freshness.get("status_notification_count_after")) is int
        and 0
        <= freshness["status_notification_count_before"]
        < freshness["status_notification_count_after"]
        <= 1_000_000_000
    )


def assess_run_gate(
    run_directory: Path,
    *,
    run_result: int,
    requested_tier: str,
    status_exchange: Any = None,
) -> list[dict[str, str]]:
    """Return objective blockers; an empty list means eligible for review only."""
    blockers: list[dict[str, str]] = []
    session_path = run_directory / "session.json"
    execution_path = run_directory / "tier-execution.json"
    environment_path = run_directory / "environment.json"
    if run_result != 0:
        blockers.append(
            _finding(
                "runner",
                str(run_result),
                "guided runner returned a nonzero status",
                execution_path,
            )
        )
    session = _read_json(session_path)
    execution = _read_json(execution_path)
    environment = _read_json(environment_path)
    if session is None:
        blockers.append(
            _finding(
                "session",
                "missing_or_invalid",
                "session evidence is unavailable",
                session_path,
            )
        )
    if execution is None:
        blockers.append(
            _finding(
                "tier_execution",
                "missing_or_invalid",
                "tier execution evidence is unavailable or malformed",
                execution_path,
            )
        )
    if session is None or execution is None:
        return blockers

    if execution.get("run_id") != session.get("run_id"):
        blockers.append(
            _finding(
                "session_binding",
                "mismatch",
                "tier evidence does not match the current run session",
                session_path,
            )
        )
    if execution.get("schema_version") != 1 or session.get("schema_version") != 1:
        blockers.append(
            _finding(
                "evidence_schema",
                "unsupported",
                "session or tier execution schema version is not supported",
                execution_path,
            )
        )

    session_device = session.get("device")
    execution_device = execution.get("device")
    plan = session.get("test_plan")
    plan_device = plan.get("device") if isinstance(plan, dict) else None
    expected_scope = (
        ("model", "model", "model"),
        ("hardware_version", "hardware_revision", "hardware_version"),
        ("firmware_version", "firmware_revision", "firmware_version"),
    )
    scope_verified = (
        isinstance(session_device, dict)
        and isinstance(execution_device, dict)
        and isinstance(plan_device, dict)
    )
    if scope_verified:
        for session_key, plan_key, execution_key in expected_scope:
            fact = session_device.get(session_key)
            if (
                not isinstance(fact, dict)
                or fact.get("verified") is not True
                or not isinstance(fact.get("value"), str)
                or fact.get("value") != plan_device.get(plan_key)
                or fact.get("value") != execution_device.get(execution_key)
            ):
                scope_verified = False
                break
    if not scope_verified:
        blockers.append(
            _finding(
                "device_scope",
                "unverified_or_mismatched",
                "verified model, hardware revision, and firmware do not match the selected execution scope",
                session_path,
            )
        )

    provenance = (
        plan_device.get("build_provenance") if isinstance(plan_device, dict) else None
    )
    session_build = session.get("integration_build")
    environment_integration = (
        environment.get("integration") if isinstance(environment, dict) else None
    )
    if (
        not isinstance(provenance, dict)
        or provenance.get("build_match") != "verified"
        or not isinstance(session_build, dict)
        or session_build.get("build_match") != "verified"
        or not isinstance(environment_integration, dict)
        or environment_integration.get("build_match") != "verified"
    ):
        blockers.append(
            _finding(
                "build_verification",
                "unverified",
                "installed integration build does not match the verified checkout",
                session_path,
            )
        )

    if requested_tier not in TIER_REQUIRED_STEP_IDS:
        blockers.append(
            _finding(
                "independent_review",
                "unsupported",
                "Platinum remains blocked because the public contract cannot verify continuous 24-hour evidence",
                execution_path,
            )
        )
    actual_requested = execution.get("requested_tier")
    actual_executed = execution.get("execution_tier")
    plan_requested = plan.get("requested_tier") if isinstance(plan, dict) else None
    if (
        actual_requested != requested_tier
        or plan_requested != requested_tier
        or actual_executed != requested_tier
    ):
        blockers.append(
            _finding(
                "tier_scope",
                f"requested={actual_requested}, executed={actual_executed}",
                "requested and executed tiers do not match the selected target",
                execution_path,
            )
        )

    routes = execution.get("verified_routes")
    if (
        not isinstance(routes, list)
        or not routes
        or any(
            not isinstance(route, str) or route not in VERIFIED_ROUTES
            for route in routes
        )
        or len(set(routes)) != 1
    ):
        blockers.append(
            _finding(
                "observed_route",
                "unverified",
                "no single supported connected BLE route is verified",
                execution_path,
            )
        )

    requested_route = execution.get("requested_route")
    route_mismatch = (
        not isinstance(requested_route, str)
        or requested_route not in {"auto", *VERIFIED_ROUTES}
        or isinstance(routes, list)
        and requested_route != "auto"
        and any(route != requested_route for route in routes)
    )
    session_route = session.get("route")
    if (
        route_mismatch
        or not isinstance(session_route, dict)
        or session_route.get("requested") != requested_route
    ):
        blockers.append(
            _finding(
                "requested_route",
                str(requested_route or "missing"),
                "requested route does not match the run session or observed route",
                execution_path,
            )
        )

    plan_steps = plan.get("steps") if isinstance(plan, dict) else None
    execution_steps = execution.get("steps")
    if not isinstance(plan_steps, list):
        blockers.append(
            _finding(
                "required_steps",
                "missing_or_invalid",
                "planned step list is missing",
                execution_path,
            )
        )
        for step_id in TIER_REQUIRED_STEP_IDS.get(
            requested_tier, TIER_REQUIRED_STEP_IDS["bronze"]
        ):
            blockers.append(
                _finding(
                    step_id,
                    "missing_plan_step",
                    "mandatory Bronze step cannot be verified without a stored plan",
                    session_path,
                )
            )
        return blockers
    if not isinstance(execution_steps, list):
        blockers.append(
            _finding(
                "required_steps",
                "missing_or_invalid",
                "executed step list is missing",
                execution_path,
            )
        )
        execution_steps = []

    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in execution_steps:
        if isinstance(row, dict) and isinstance(row.get("step_id"), str):
            by_id.setdefault(row["step_id"], []).append(row)
            if row.get("state_safe_to_continue") is False:
                blockers.append(
                    _finding(
                        row["step_id"],
                        "unsafe_restoration",
                        "execution evidence reports that the physical state is not safe to continue",
                        execution_path,
                    )
                )
    planned_by_id: dict[str, list[dict[str, Any]]] = {}
    for planned in plan_steps:
        if isinstance(planned, dict) and isinstance(planned.get("id"), str):
            planned_by_id.setdefault(planned["id"], []).append(planned)
    for step_id in TIER_REQUIRED_STEP_IDS.get(requested_tier, set()) - set(
        BRONZE_REQUIRED_STEP_CONTRACTS
    ):
        if len(planned_by_id.get(step_id, [])) != 1:
            blockers.append(
                _finding(
                    step_id,
                    "missing_or_ambiguous_plan_step",
                    f"mandatory {requested_tier.title()} step is absent or duplicated in the stored plan",
                    session_path,
                )
            )
    for step_id, contract in BRONZE_REQUIRED_STEP_CONTRACTS.items():
        matching = planned_by_id.get(step_id, [])
        if len(matching) != 1:
            blockers.append(
                _finding(
                    step_id,
                    "missing_or_ambiguous_plan_step",
                    "mandatory Bronze step is absent or duplicated in the stored plan",
                    session_path,
                )
            )
            continue
        planned = matching[0]
        if (
            planned.get("feature") != contract["feature"]
            or planned.get("action") != contract["action"]
            or planned.get("required_evidence") != contract["required_evidence"]
            or planned.get("eligibility") != "eligible"
        ):
            blockers.append(
                _finding(
                    step_id,
                    str(planned.get("eligibility", "missing")),
                    "mandatory Bronze step does not match its required eligible contract",
                    session_path,
                )
            )
    for planned in plan_steps:
        if not isinstance(planned, dict) or not isinstance(planned.get("id"), str):
            blockers.append(
                _finding(
                    "plan_step",
                    "invalid",
                    "planned step record is malformed",
                    session_path,
                )
            )
            continue
        step_id = planned["id"]
        if (
            planned.get("eligibility") == "not_applicable"
            or planned.get("action") == "not_applicable"
        ):
            capabilities = session.get("runtime_capabilities")
            features = (
                capabilities.get("features") if isinstance(capabilities, dict) else None
            )
            feature = planned.get("feature")
            feature_facts = (
                features.get(feature) if isinstance(features, dict) else None
            )
            reasons = planned.get("reasons")
            expected_absent_feature = TIER_OPTIONAL_ABSENCE_FEATURES.get(step_id)
            substantiated_absence = (
                planned.get("action") == "not_applicable"
                and planned.get("eligibility") == "not_applicable"
                and (
                    step_id not in TIER_REQUIRED_STEP_IDS.get(requested_tier, set())
                    or (
                        expected_absent_feature is not None
                        and feature == expected_absent_feature
                    )
                )
                and isinstance(feature_facts, dict)
                and feature_facts.get("physical_presence") == "absent"
                and isinstance(reasons, list)
                and "runtime profile substantiates physical absence" in reasons
            )
            rows = by_id.get(step_id, [])
            if (
                not substantiated_absence
                or len(rows) != 1
                or rows[0].get("outcome") != "not_applicable"
            ):
                blockers.append(
                    _finding(
                        step_id,
                        "invalid_exclusion",
                        "step exclusion is not backed by a unique execution result and verified physical absence",
                        execution_path,
                    )
                )
            continue
        if planned.get("eligibility") not in {"eligible", "pending_authorization"}:
            if step_id == "minimum_output_requirement" and requested_tier in {
                "silver",
                "gold",
            }:
                continue
            blockers.append(
                _finding(
                    step_id,
                    str(planned.get("eligibility", "missing")),
                    "planned step was not eligible for execution",
                    session_path,
                )
            )
            continue
        rows = by_id.get(step_id, [])
        if len(rows) != 1:
            blockers.append(
                _finding(
                    step_id,
                    "missing_or_ambiguous",
                    "required planned step has no unique execution record",
                    execution_path,
                )
            )
            continue
        row = rows[0]
        outcome = row.get("outcome")
        if step_id == "guarded_recovery" and outcome == "shared_evidence":
            reconnect_rows = by_id.get("basic_reconnect", [])
            source = reconnect_rows[0] if len(reconnect_rows) == 1 else {}
            source_artifacts = _verified_artifact_refs(source, run_directory)
            if (
                source.get("outcome") == "pass"
                and row.get("evidence_run") == source.get("evidence_run")
                and source_artifacts
                and isinstance(source.get("observed_routes"), list)
                and any(route in routes for route in source["observed_routes"])
            ):
                # The runner deliberately reuses the one real reconnect recovery
                # acquisition for Gold's guarded recovery step.
                continue
            blockers.append(
                _finding(
                    step_id,
                    "invalid_shared_evidence",
                    "guarded recovery does not reference the unique verified basic reconnect acquisition",
                    execution_path,
                )
            )
            continue
        if (
            step_id == "minimum_output_requirement"
            and requested_tier in {"silver", "gold"}
            and outcome == "pending_review"
        ):
            # The independent reviewer and tier evaluator resolve this gate from
            # reviewed output evidence. It is not an acquisition failure.
            continue
        if outcome not in PASS_OUTCOMES:
            failure_category = row.get("failure_category")
            if step_id == "read_only_probe":
                artifacts = _verified_artifact_refs(row, run_directory)
                bronze_artifact = next(
                    (
                        item
                        for item in artifacts
                        if Path(item["path"]).name == "tier-bronze-read-only.json"
                    ),
                    None,
                )
                bronze_bytes = (
                    _safe_read_file(run_directory, bronze_artifact["path"])
                    if bronze_artifact is not None
                    else None
                )
                try:
                    bronze = (
                        json.loads(bronze_bytes) if bronze_bytes is not None else None
                    )
                except UnicodeError, ValueError:
                    bronze = None
                if (
                    isinstance(bronze, dict)
                    and bronze.get("fresh_verified_samples") is not True
                ):
                    freshness = bronze.get("status_freshness")
                    marker_advanced = bool(
                        isinstance(freshness, dict)
                        and type(freshness.get("status_notification_count_before"))
                        is int
                        and type(freshness.get("status_notification_count_after"))
                        is int
                        and freshness["status_notification_count_after"]
                        > freshness["status_notification_count_before"]
                    )
                    window_exceeded = bool(
                        marker_advanced
                        and _bronze_sample_window_is_only_freshness_failure(bronze)
                        and status_exchange is not None
                        and isinstance(routes, list)
                        and _valid_bronze_capture(
                            run_directory,
                            bronze,
                            run_id=execution.get("run_id", ""),
                            routes=routes,
                            requested_route=requested_route,
                            status_exchange=status_exchange,
                        )
                    )
                    blockers.append(
                        _finding(
                            step_id,
                            (
                                "sample_window_exceeded"
                                if marker_advanced and window_exceeded
                                else "uncorrelated_capture"
                                if marker_advanced
                                else "missing_new_status_notification"
                            ),
                            (
                                "the interval between REST samples exceeds the 60-second freshness window"
                                if marker_advanced and window_exceeded
                                else "the accepted status notification is not correlated with the sampled values"
                                if marker_advanced
                                else "no advancing status-notification marker was observed in the current session"
                            ),
                            execution_path,
                        )
                    )
            refresh_failed = failure_category in {
                "refresh_failed",
                "refresh_unavailable",
            }
            blockers.append(
                _finding(
                    step_id,
                    str(failure_category)
                    if failure_category in {"refresh_failed", "refresh_unavailable"}
                    else str(outcome or "missing"),
                    (
                        "the read-only refresh button could not be safely resolved "
                        "or invoked; check that exactly one enabled ALLPOWERS Refresh "
                        "status button is linked to the selected device and loaded entry"
                        if refresh_failed
                        else "required applicable step did not pass"
                    ),
                    execution_path,
                )
            )
            continue
        if outcome == "not_applicable":
            if planned.get("eligibility") != "not_applicable":
                blockers.append(
                    _finding(
                        step_id,
                        outcome,
                        "step exclusion is not substantiated by the plan",
                        execution_path,
                    )
                )
            continue
        artifacts = _verified_artifact_refs(row, run_directory)
        if not artifacts:
            blockers.append(
                _finding(
                    step_id,
                    "missing_or_invalid_artifact",
                    "passed step has no valid referenced checkpoint or artifact",
                    execution_path,
                )
            )
            continue
        if planned.get("action") == "write" and not _valid_tier_step_acquisition(
            run_directory, step_id, artifacts
        ):
            blockers.append(
                _finding(
                    step_id,
                    "incomplete_or_unsafe_restoration",
                    "the action artifact does not prove complete transactions and confirmed final restoration",
                    execution_path,
                )
            )
            continue
        observed = row.get("observed_routes")
        if not isinstance(observed, list) or not any(
            route in routes for route in observed
        ):
            blockers.append(
                _finding(
                    step_id,
                    "route_mismatch",
                    "step evidence is not bound to the verified connected route",
                    execution_path,
                )
            )
            continue
        if step_id in {"read_only_probe", "physical_plausibility_observation"}:
            bronze_artifact = next(
                (
                    item
                    for item in artifacts
                    if Path(item["path"]).name == "tier-bronze-read-only.json"
                ),
                None,
            )
            bronze_bytes = (
                _safe_read_file(run_directory, bronze_artifact["path"])
                if bronze_artifact is not None
                else None
            )
            try:
                bronze = json.loads(bronze_bytes) if bronze_bytes is not None else None
            except UnicodeError, ValueError:
                bronze = None
            try:
                from .evidence import _bronze_samples_fresh

                samples_fresh = bool(
                    isinstance(bronze, dict)
                    and _bronze_samples_fresh(
                        bronze.get("samples", []),
                        status_evidence=bronze.get("status_freshness"),
                        status_event=(
                            {
                                "event": "notification_decoded",
                                "capture_id": bronze["status_freshness"].get(
                                    "capture_id"
                                ),
                                "session_generation": bronze["status_freshness"].get(
                                    "session_generation"
                                ),
                                "seq": bronze["status_freshness"].get(
                                    "decoded_response_seq"
                                ),
                                "utc": bronze["status_freshness"].get(
                                    "decoded_response_at_utc"
                                ),
                                "capture_started_seq": bronze["status_freshness"].get(
                                    "capture_started_seq"
                                ),
                                "capture_stopped_seq": bronze["status_freshness"].get(
                                    "capture_stopped_seq"
                                ),
                                "status_measurement": bronze["status_freshness"].get(
                                    "status_measurement"
                                ),
                            }
                            if isinstance(bronze.get("status_freshness"), dict)
                            else None
                        ),
                    )
                )
            except ImportError, TypeError:
                samples_fresh = False
            status_capture_valid = bool(
                isinstance(bronze, dict)
                and status_exchange is not None
                and isinstance(routes, list)
                and _valid_bronze_capture(
                    run_directory,
                    bronze,
                    run_id=execution.get("run_id", ""),
                    routes=routes,
                    requested_route=requested_route,
                    status_exchange=status_exchange,
                )
            )
            if not samples_fresh:
                freshness = (
                    bronze.get("status_freshness") if isinstance(bronze, dict) else None
                )
                if (
                    not isinstance(freshness, dict)
                    or type(freshness.get("status_notification_count_before"))
                    is not int
                    or type(freshness.get("status_notification_count_after")) is not int
                    or freshness["status_notification_count_after"]
                    <= freshness["status_notification_count_before"]
                ):
                    freshness_reason = "missing_new_status_notification"
                    freshness_message = "no advancing status-notification marker was observed in the current session"
                elif not status_capture_valid:
                    freshness_reason = "uncorrelated_capture"
                    freshness_message = "the status notification is not bound to the captured request and current session"
                elif _bronze_sample_window_is_only_freshness_failure(bronze):
                    freshness_reason = "sample_window_exceeded"
                    freshness_message = "the interval between REST samples exceeds the 60-second freshness window"
                else:
                    freshness_reason = "rest_timestamp_stale"
                    freshness_message = "REST sample timestamps are stale and the accepted status does not prove both samples fresh within 60 seconds"
                blockers.append(
                    _finding(
                        step_id,
                        freshness_reason,
                        freshness_message,
                        execution_path,
                    )
                )
            if (
                not isinstance(bronze, dict)
                or bronze.get("fresh_verified_samples") is not True
                or not samples_fresh
                or bronze.get("physical_observation") != "plausible"
                or not status_capture_valid
            ):
                blockers.append(
                    _finding(
                        step_id,
                        "incomplete_protocol_proof",
                        "physical observation or correlated complete status trace is missing",
                        execution_path,
                    )
                )
    return blockers
