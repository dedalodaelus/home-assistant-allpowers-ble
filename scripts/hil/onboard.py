"""Fail-closed assessment of guided evidence before onboarding handoff."""

from __future__ import annotations

import hashlib
import json
import os
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
            "status_measurement",
        }
        and freshness.get("capture_id") == probe.get("capture_id")
        and freshness.get("session_generation") == status_tx.get("session_generation")
        and freshness.get("decoded_response_seq") == decoded.get("seq")
        and freshness.get("decoded_response_at_utc") == decoded.get("utc")
        and freshness.get("status_measurement") == decoded.get("status_measurement")
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

    if requested_tier != "bronze":
        blockers.append(
            _finding(
                "independent_review",
                "unsupported",
                "onboard review currently supports Bronze only; higher-tier review is not available",
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
        for step_id in BRONZE_REQUIRED_STEP_CONTRACTS:
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
            if step_id in BRONZE_REQUIRED_STEP_CONTRACTS:
                continue
            capabilities = session.get("runtime_capabilities")
            features = (
                capabilities.get("features") if isinstance(capabilities, dict) else None
            )
            feature = planned.get("feature")
            feature_facts = (
                features.get(feature) if isinstance(features, dict) else None
            )
            reasons = planned.get("reasons")
            substantiated_absence = (
                planned.get("action") == "not_applicable"
                and planned.get("eligibility") == "not_applicable"
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
                        "optional step exclusion is not backed by verified physical absence",
                        execution_path,
                    )
                )
            continue
        if planned.get("eligibility") not in {"eligible", "pending_authorization"}:
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
                    blockers.append(
                        _finding(
                            step_id,
                            (
                                "uncorrelated_capture"
                                if marker_advanced
                                else "missing_new_status_notification"
                            ),
                            (
                                "the accepted status notification is not correlated with the sampled values"
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
