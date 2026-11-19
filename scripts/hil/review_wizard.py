"""Offline interactive front end for the strict imported Bronze reviewer."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
from typing import Any
from .api import HAAPIError
from .importer import _canonical_model, _reject_symlink_components
from .review import (
    CLAIMS,
    _claim_support,
    _reconnect_review_candidate,
    _verified_members,
    review_imported_run,
)
from scripts import validate_hil_qualification as validation

_SCOPE_LABELS = {
    "verified_observed_route": "observed BLE route is not verified",
    "complete_capture": "capture is not complete",
    "verified_build": "installed build is not verified",
    "installed_checkout_match": "installed and checkout fingerprints differ",
    "valid_build_fingerprint": "build fingerprint is invalid",
    "session_matches_capture": "session timestamp does not match the capture",
    "session_id_present": "session ID is missing or unknown",
    "read_only_session": "session is not a read-only preflight",
    "runtime_available": "runtime availability is not verified",
    "runtime_connected": "runtime connection is not verified",
    "status_fresh": "runtime status is not fresh",
    "no_pending_transaction": "a runtime transaction is pending",
    "manifest_build_verified": "archive build provenance is not verified",
}
_CLAIM_LABELS = {
    "identity": "model, hardware revision, and firmware identity are not all verified",
    "protocol_probe": "the complete status request and decoded status response are not verified",
    "fresh_telemetry": "fresh, verified, plausible telemetry samples are not established",
}
_FEATURE_STEPS = {
    "ac_output_cycle": "ac_output",
    "dc_output_cycle": "dc_output",
    "light_output_cycle": "light_output",
    "eco_mode_reversal": "eco_mode",
    "work_mode_reversal": "work_mode",
    "eco_timeout_reversal": "eco_timeout",
    "car_charger_reversal": "car_charger",
}


def _trace_operation_reference(
    action: dict[str, Any],
    traces_by_digest: dict[str, Any],
    *,
    feature: str,
    state: Any,
    route: str,
    action_index: int | None = None,
    action_stage: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any] | None:
    """Find a validated TX and matching transaction events in the action's trace."""
    state_key = (
        "output_states"
        if feature in {"ac_output", "dc_output", "light_output"}
        else "setting_states"
    )
    expected_state = state == "on" if state in {"on", "off"} else state
    generation = action.get("session_generation")
    transaction = action.get("transaction")
    if not isinstance(transaction, dict):
        return None
    transaction_sha256 = transaction.get("transaction_sha256")
    write_seq, submitted_seq, confirmed_seq = (
        transaction.get("write_seq"),
        transaction.get("submitted_seq"),
        transaction.get("confirmed_seq"),
    )
    transaction_kind = "output" if state_key == "output_states" else "settings"
    if (
        transaction.get("kind") != transaction_kind
        or transaction.get("session_generation") != generation
        or not isinstance(transaction_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", transaction_sha256) is None
        or any(
            type(seq) is not int or seq < 1
            for seq in (write_seq, submitted_seq, confirmed_seq)
        )
        or not write_seq < submitted_seq < confirmed_seq
    ):
        return None
    for digest in action.get("trace_evidence_sha256", []):
        trace = traces_by_digest.get(digest)
        if (
            not isinstance(trace, dict)
            or trace.get("kind") != "gatt_trace_metadata"
            or trace.get("complete") is not True
            or trace.get("dropped_events") != 0
            or trace.get("dropped_frames") != 0
            or trace.get("observed_route") != route
        ):
            continue
        for frame in trace.get("frames", []):
            if not isinstance(frame, dict):
                continue
            states = frame.get(state_key)
            frame_matches = (
                frame.get("event") == "write_attempt"
                and frame.get("direction") == "tx"
                and frame.get("frame_validation") == "validated_tx"
                and frame.get("session_generation") == generation
                and frame.get("seq") == write_seq
                and frame.get("transaction_sha256") == transaction_sha256
                and frame.get("command_type")
                == (
                    "output_control"
                    if state_key == "output_states"
                    else "settings_control"
                )
                and isinstance(states, dict)
                and states.get(feature) == expected_state
                and isinstance(frame.get("frame_sha256"), str)
            )
            if not frame_matches:
                continue
            events = trace.get("events", [])
            write = next(
                (
                    event
                    for event in events
                    if isinstance(event, dict)
                    and event.get("seq") == write_seq
                    and event.get("event") == "write_attempt"
                    and event.get("session_generation") == generation
                    and event.get("observed_route") == route
                    and event.get("transaction_sha256") == transaction_sha256
                ),
                None,
            )
            submitted = [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("event") == "transaction_submitted"
                and event.get("session_generation") == generation
                and event.get("transaction_kind") == transaction_kind
                and event.get("observed_route") == route
                and event.get("transaction_sha256") == transaction_sha256
                and event.get("seq") == submitted_seq
            ]
            confirmed = [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("event") == "transaction_confirmed"
                and event.get("session_generation") == generation
                and event.get("observed_route") == route
                and event.get("transaction_sha256") == transaction_sha256
                and event.get("transaction_reason") == "matching_validated_telemetry"
                and event.get("seq") == confirmed_seq
            ]
            if write is not None and len(submitted) == 1 and len(confirmed) == 1:
                reference = {
                    "trace_sha256": digest,
                    "frame_sha256": frame["frame_sha256"],
                    "frame_seq": frame["seq"],
                    "transaction_sha256": transaction_sha256,
                    "session_generation": generation,
                }
                if action_index is not None:
                    reference["source_action_index"] = action_index
                    reference["action_stage"] = action_stage or (
                        "transition" if action_index == 0 else "restoration"
                    )
                if step_id is not None:
                    reference["source_step_id"] = step_id
                reference["requested_state"] = expected_state
                return reference
    return None


def _has_trace_operation(
    action: dict[str, Any],
    traces_by_digest: dict[str, Any],
    *,
    feature: str,
    state: Any,
    route: str,
) -> bool:
    return (
        _trace_operation_reference(
            action,
            traces_by_digest,
            feature=feature,
            state=state,
            route=route,
        )
        is not None
    )


def _load_run(matrix_path: Path, run_id: str, root: Path) -> tuple[dict, dict]:
    matrix_path = matrix_path.absolute()
    _reject_symlink_components(matrix_path, root)
    try:
        matrix_path.relative_to(root)
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError) as exc:
        raise HAAPIError("Unable to read in-repository qualification matrix") from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != 3:
        raise HAAPIError("Qualification matrix must use schema v3")
    errors, _ = validation.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    if errors:
        raise HAAPIError("Qualification matrix validation failed: " + "; ".join(errors))
    matches = [
        (revision, run)
        for revision in matrix.get("revisions", [])
        if isinstance(revision, dict)
        for run in revision.get("runs", [])
        if isinstance(run, dict) and run.get("run_id") == run_id
    ]
    if not run_id.startswith("archive-") or len(matches) != 1:
        raise HAAPIError("Imported archive run is absent or ambiguous")
    return matches[0]


def _prompt_claim(claim: str, support: bool, failures: list[str]) -> str:
    if not support:
        print(f"  Preflight blockers: {'; '.join(failures)}")
        print("  This claim is unavailable and will remain inconclusive.")
        return "inconclusive"
    answer = (
        input("  Independent decision [inconclusive/pass] (default inconclusive): ")
        .strip()
        .lower()
    )
    if answer in {"", "n", "no", "unknown", "inconclusive"}:
        return "inconclusive"
    if answer != "pass":
        print("  Invalid answer; keeping claim inconclusive.")
        return "inconclusive"
    if (
        input(
            "  Confirm this claim is supported by inspected physical evidence? [no/yes]: "
        )
        .strip()
        .lower()
        != "yes"
    ):
        return "inconclusive"
    return "pass"


def interactive_review(
    matrix_path: Path,
    *,
    run_id: str,
    tier: str = "bronze",
    repo_root: Path | None = None,
) -> int:
    """Collect independent decisions, dry-run them, then optionally apply."""
    root = (repo_root or Path(__file__).resolve().parents[2]).absolute()
    if not sys.stdin.isatty():
        raise HAAPIError("Interactive review requires a TTY")
    revision, run = _load_run(matrix_path, run_id, root)
    members = _verified_members(run, root)
    if tier != "bronze":
        return _interactive_higher_tier_review(
            tier=tier,
            run=run,
            revision=revision,
            members=members,
            repo_root=root,
            matrix_path=matrix_path,
        )
    print(
        "Offline independent physical review (a reviewer response is not cryptographic attestation)."
    )
    print(
        f"Model / hardware / firmware: {revision.get('model')} / {revision.get('hardware_version')} / {run.get('firmware_version')}"
    )
    print(f"Observed BLE route: {run.get('route')}")
    print(f"Run ID: {run_id}")
    print(f"Archive SHA-256: {run.get('archive_sha256')}")
    print(
        f"Installed build: {run.get('build_verification')} ({run.get('installed_fingerprint')})"
    )
    print(f"Capture completeness: {run.get('capture_completeness')}")
    print("Verified public evidence files:")
    for relative in sorted(
        path for path in run.get("evidence", []) if isinstance(path, str)
    ):
        print(f"  {relative}")
    reviewer = input("Reviewer (public identifier): ").strip()
    if re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", reviewer) is None:
        raise HAAPIError(
            "Reviewer must be a nonempty public identifier of at most 80 characters"
        )
    inspected = (
        input("Independently inspected real-device evidence? [no/yes] (default no): ")
        .strip()
        .lower()
    )
    independent = inspected == "yes"
    claims: dict[str, str] = {}
    for claim in CLAIMS:
        supported, evidence, checks = _claim_support(claim, revision, run, members)
        print(
            f"{claim}: objective preflight {'supported' if supported else 'not supported'}"
        )
        print(
            f"  Evidence references checked: {', '.join(evidence) if evidence else 'none'}"
        )
        failures = [
            label
            for key, label in _SCOPE_LABELS.items()
            if checks["scope"].get(key) is False
        ]
        if checks.get(claim) is False:
            failures.append(_CLAIM_LABELS[claim])
        if independent:
            claims[claim] = _prompt_claim(claim, supported, failures)
        else:
            print(
                "  No independent physical inspection confirmed; keeping inconclusive."
            )
            claims[claim] = "inconclusive"
    payload = {
        "schema_version": 1,
        "archive_sha256": run["archive_sha256"],
        "run_id": run["run_id"],
        "reviewed_by": reviewer,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "claims": claims,
    }
    audit_dir = root / "hil-results" / "bronze-reviews"
    _reject_symlink_components(audit_dir, root)
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    audit_path = audit_dir / f"{run_id}-{stamp}.json"
    _reject_symlink_components(audit_path, root)
    try:
        with audit_path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise HAAPIError("Unable to save local review audit copy") from exc
    print(f"Review audit copy: {audit_path.relative_to(root)}")
    review_imported_run(
        matrix_path, run_id=run_id, review_file=audit_path, apply=False, repo_root=root
    )
    if (
        input(
            "Type APPLY REVIEW to update the matrix (anything else cancels): "
        ).strip()
        != "APPLY REVIEW"
    ):
        print("Review cancelled; qualification matrix unchanged.")
        return 2
    return review_imported_run(
        matrix_path, run_id=run_id, review_file=audit_path, apply=True, repo_root=root
    )


def _interactive_higher_tier_review(
    *,
    tier: str,
    run: dict,
    revision: dict | None = None,
    members: dict[str, bytes],
    repo_root: Path,
    matrix_path: Path | None = None,
) -> int:
    """Explain which imported proof is reviewable without promoting unsupported data."""
    if tier not in {"silver", "gold", "platinum"}:
        raise HAAPIError("Unsupported review tier")
    revision = revision or {}
    print(f"Independent {tier.title()} review")
    print(
        "Model / hardware revision: "
        f"{revision.get('model', 'unknown')} / "
        f"{revision.get('hardware_version', 'unknown')}"
    )
    print("Machine evidence and physical observations are assessed separately.")
    print(f"Run ID: {run.get('run_id')}")
    print(f"Session ID: {run.get('session_id')}")
    print(f"Archive SHA-256: {run.get('archive_sha256')}")
    print(f"Observed route: {run.get('route')}")
    print(f"Firmware: {run.get('firmware_version')}")
    print(f"Build fingerprint: {run.get('build_fingerprint')}")
    if tier == "platinum":
        print(
            "Blocked: the public archive/import contract does not preserve the "
            "scoped continuous 24-hour soak evidence required by the evaluator."
        )
        return 2
    try:
        execution = json.loads(members["tier-execution.json"])
        artifacts = json.loads(members["tier-artifacts.json"])
    except KeyError, UnicodeError, ValueError:
        execution, artifacts = {}, {}
    artifact_rows = artifacts.get("artifacts", [])
    by_id = {
        item.get("artifact_id"): item.get("content", {})
        for item in artifact_rows
        if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
    }
    by_source_digest = {
        item.get("source_sha256"): item.get("content", {})
        for item in artifact_rows
        if isinstance(item, dict) and isinstance(item.get("source_sha256"), str)
    }
    steps = execution.get("steps", []) if isinstance(execution, dict) else []
    absence_candidates = []
    inventory_preflight: dict[str, Any] = {}
    try:
        session = json.loads(members["session.json"])
        session_data = session.get("data", {})
        test_plan = session_data.get("test_plan", {})
        plan_device = test_plan.get("device", {})
        session_device = session_data.get("device", {})
        model = session_device.get("model", {}).get("value")
        hardware = session_device.get("hardware_version", {}).get("value")
        firmware = session_device.get("firmware_version", {}).get("value")
        canonical_model = _canonical_model(model)
        revision_model = _canonical_model(revision.get("model"))
        plan_model = _canonical_model(plan_device.get("model"))
        if (
            canonical_model is not None
            and canonical_model == revision_model == plan_model
            and hardware == revision.get("hardware_version")
            and firmware == run.get("firmware_version")
        ):
            inventory = test_plan.get("physical_inventory_preflight", {})
            declared_inventory = (
                inventory.get("features") if isinstance(inventory, dict) else None
            )
            if (
                isinstance(inventory, dict)
                and inventory.get("source") == "operator_physical_inspection"
                and isinstance(declared_inventory, dict)
            ):
                inventory_preflight = {
                    feature: item
                    for feature, item in declared_inventory.items()
                    if feature
                    in {
                        "ac_output",
                        "dc_output",
                        "light_output",
                        "eco_mode",
                        "work_mode",
                        "eco_timeout",
                        "car_charger",
                    }
                    and isinstance(item, dict)
                    and item.get("outcome") == "present"
                    and item.get("basis") == "operator_physical_inspection"
                }
            absence_candidates = [
                step
                for step in test_plan.get("steps", [])
                if isinstance(step, dict)
                and step.get("feature") in {"ac_output", "dc_output", "light_output"}
                and step.get("physical_presence") == "absent"
                and step.get("eligibility") == "not_applicable"
                and "runtime profile substantiates physical absence"
                in step.get("reasons", [])
            ]
    except KeyError, TypeError, ValueError, UnicodeError:
        absence_candidates = []
    execution_by_step = {
        item.get("step_id"): item
        for item in steps
        if isinstance(item, dict) and isinstance(item.get("step_id"), str)
    }
    reconnect_candidate = next(
        (
            candidate
            for step in steps
            if isinstance(step, dict) and step.get("step_id") == "basic_reconnect"
            if (candidate := _reconnect_review_candidate(run, step, artifact_rows))
            is not None
        ),
        None,
    )
    recorded_targets: dict[str, set[str]] = {}
    recorded_cycles: dict[str, list[dict[str, Any]]] = {}
    final_restorations: dict[str, dict[str, Any]] = {}
    print("Imported guided evidence:")
    if inventory_preflight:
        print(
            "Operator physical-inventory preflight (acquisition eligibility only; "
            "not independent qualification):"
        )
        for feature in sorted(inventory_preflight):
            print(
                f"  {feature}: operator reported physically present; independently "
                "inspect the actual transitions and restorations below."
            )
    for step in steps:
        if not isinstance(step, dict):
            continue
        print(f"  {step.get('step_id')}: machine outcome {step.get('outcome')}")
        for reference in step.get("machine_evidence", []):
            if not isinstance(reference, dict):
                continue
            content = by_id.get(reference.get("artifact_id"), {})
            kind = (
                content.get("kind", "unknown")
                if isinstance(content, dict)
                else "unknown"
            )
            print(f"    {reference.get('artifact_id')}: {kind}")
            if kind == "guided_step_evidence" and isinstance(content, dict):
                print(
                    "      physical change / restoration reported by operator: "
                    f"{content.get('physical_change_outcome', 'unavailable')} / "
                    f"{content.get('physical_restore_outcome', 'unavailable')}"
                )
                for index, action in enumerate(content.get("actions", []), start=1):
                    if not isinstance(action, dict):
                        continue
                    print(
                        f"      action {index}: {action.get('outcome')} on "
                        f"{action.get('route', 'unknown')} route, "
                        f"session generation {action.get('session_generation', 'unknown')}"
                    )
                    for digest in action.get("trace_evidence_sha256", []):
                        trace = by_source_digest.get(digest, {})
                        if (
                            not isinstance(trace, dict)
                            or trace.get("kind") != "gatt_trace_metadata"
                        ):
                            continue
                        print(
                            f"        capture {digest[:12]}: "
                            f"route {trace.get('observed_route')}, "
                            f"complete={trace.get('complete')}"
                        )
                        for frame in trace.get("frames", []):
                            if not isinstance(frame, dict) or not (
                                "output_states" in frame or "setting_states" in frame
                            ):
                                continue
                            decoded = frame.get(
                                "output_states", frame.get("setting_states")
                            )
                            print(
                                f"          seq {frame.get('seq')}, "
                                f"generation {frame.get('session_generation')}, "
                                f"{decoded}, frame SHA-256 {frame.get('frame_sha256')}"
                            )
                feature = _FEATURE_STEPS.get(content.get("step_id"))
                step_execution = execution_by_step.get(content.get("step_id"), {})
                production_authorized = (
                    isinstance(step_execution, dict)
                    and step_execution.get("authorization_context") == "production"
                    and step_execution.get("authorization_context_source")
                    == "runtime_production_authorized"
                )
                if not production_authorized:
                    print(
                        "      This write has no verified production authorization "
                        "context; it cannot contribute to a Silver or Gold PASS."
                    )
                actions = content.get("actions", [])
                is_output = feature in {"ac_output", "dc_output", "light_output"}
                is_setting = feature in {
                    "eco_mode",
                    "work_mode",
                    "eco_timeout",
                    "car_charger",
                }
                output_cycles = content.get("output_cycles")
                if (
                    production_authorized
                    and is_output
                    and content.get("outcome") == "pass"
                    and len(actions) == 4
                    and isinstance(output_cycles, list)
                    and len(output_cycles) == 2
                    and content.get("final_restore_action_index") == 3
                    and content.get("final_restore_outcome") == "correct"
                ):
                    final_state = content.get("initial_state")
                    final_restore = (
                        _trace_operation_reference(
                            actions[3],
                            by_source_digest,
                            feature=feature,
                            state=final_state,
                            route=run.get("route"),
                            action_index=3,
                            action_stage="restoration",
                            step_id=content.get("step_id"),
                        )
                        if final_state in {"on", "off"}
                        else None
                    )
                    if final_restore is None:
                        output_cycles = []
                    else:
                        final_restorations[feature] = {
                            "step_id": content.get("step_id"),
                            "frame_evidence": final_restore,
                            "trace_sha256": sorted(
                                actions[3].get("trace_evidence_sha256", [])
                            ),
                        }
                    for cycle in output_cycles:
                        if not isinstance(cycle, dict):
                            continue
                        change_index = cycle.get("change_action_index")
                        restore_index = cycle.get("restore_action_index")
                        initial = cycle.get("initial_state")
                        target = cycle.get("target_state")
                        if (
                            type(change_index) is not int
                            or type(restore_index) is not int
                            or change_index not in range(4)
                            or restore_index not in range(4)
                            or initial not in {"on", "off"}
                            or target not in {"on", "off"}
                            or initial == target
                            or cycle.get("physical_change_outcome") != "correct"
                            or cycle.get("physical_restore_outcome") != "correct"
                        ):
                            continue
                        change = actions[change_index]
                        restore = actions[restore_index]
                        change_reference = _trace_operation_reference(
                            change,
                            by_source_digest,
                            feature=feature,
                            state=target,
                            route=run.get("route"),
                            action_index=change_index,
                            action_stage="transition",
                            step_id=content.get("step_id"),
                        )
                        restore_reference = _trace_operation_reference(
                            restore,
                            by_source_digest,
                            feature=feature,
                            state=initial,
                            route=run.get("route"),
                            action_index=restore_index,
                            action_stage="restoration",
                            step_id=content.get("step_id"),
                        )
                        if change_reference and restore_reference:
                            recorded_targets.setdefault(feature, set()).add(target)
                            record = {
                                "step_id": content.get("step_id"),
                                "initial_state": initial,
                                "target_state": target,
                                "change_frame_evidence": change_reference,
                                "restore_frame_evidence": restore_reference,
                                "change_trace_sha256": sorted(
                                    change.get("trace_evidence_sha256", [])
                                ),
                                "restore_trace_sha256": sorted(
                                    restore.get("trace_evidence_sha256", [])
                                ),
                                "final_restore_frame_evidence": final_restore,
                                "final_restore_trace_sha256": sorted(
                                    actions[3].get("trace_evidence_sha256", [])
                                ),
                                "final_restore_state": content.get("initial_state"),
                            }
                            if record not in recorded_cycles.setdefault(feature, []):
                                recorded_cycles[feature].append(record)
                if (
                    production_authorized
                    and (tier == "gold" and is_setting)
                    and content.get("outcome") == "pass"
                    and content.get("physical_change_outcome") == "correct"
                    and content.get("physical_restore_outcome") == "correct"
                    and len(actions) == 2
                ):
                    change, restore = actions
                    target = content.get("target_state")
                    initial = content.get("initial_state")
                    if (
                        isinstance(change, dict)
                        and isinstance(restore, dict)
                        and change.get("confirmed") is True
                        and restore.get("confirmed") is True
                        and change.get("trace_valid") is True
                        and restore.get("trace_valid") is True
                        and change.get("route") == run.get("route")
                        and restore.get("route") == run.get("route")
                        and type(change.get("session_generation")) is int
                        and change.get("session_generation")
                        == restore.get("session_generation")
                        and target in {"on", "off"}
                        and initial in {"on", "off"}
                        and type(target) is type(initial)
                        and target != initial
                    ):
                        change_reference = _trace_operation_reference(
                            change,
                            by_source_digest,
                            feature=feature,
                            state=target,
                            route=run.get("route"),
                            action_index=0,
                            step_id=content.get("step_id"),
                        )
                        restore_reference = _trace_operation_reference(
                            restore,
                            by_source_digest,
                            feature=feature,
                            state=initial,
                            route=run.get("route"),
                            action_index=1,
                            step_id=content.get("step_id"),
                        )
                        if change_reference and restore_reference:
                            recorded_targets.setdefault(feature, set()).add(target)
                            cycle = {
                                "step_id": content.get("step_id"),
                                "initial_state": initial,
                                "target_state": target,
                                "change_frame_evidence": change_reference,
                                "restore_frame_evidence": restore_reference,
                                "change_trace_sha256": sorted(
                                    change.get("trace_evidence_sha256", [])
                                ),
                                "restore_trace_sha256": sorted(
                                    restore.get("trace_evidence_sha256", [])
                                ),
                                "final_restore_frame_evidence": restore_reference,
                                "final_restore_trace_sha256": sorted(
                                    restore.get("trace_evidence_sha256", [])
                                ),
                                "final_restore_state": initial,
                            }
                            if cycle not in recorded_cycles.setdefault(feature, []):
                                recorded_cycles[feature].append(cycle)
                    elif (
                        production_authorized
                        and tier == "gold"
                        and is_setting
                        and isinstance(change, dict)
                        and isinstance(restore, dict)
                        and content.get("outcome") == "pass"
                        and content.get("physical_change_outcome") == "correct"
                        and content.get("physical_restore_outcome") == "correct"
                        and change.get("confirmed") is True
                        and restore.get("confirmed") is True
                        and change.get("trace_valid") is True
                        and restore.get("trace_valid") is True
                        and change.get("route") == run.get("route")
                        and restore.get("route") == run.get("route")
                        and type(change.get("session_generation")) is int
                        and change.get("session_generation")
                        == restore.get("session_generation")
                        and type(target) is type(initial)
                        and target != initial
                        and _trace_operation_reference(
                            change,
                            by_source_digest,
                            feature=feature,
                            state=target,
                            route=run.get("route"),
                            action_index=0,
                            step_id=content.get("step_id"),
                        )
                        and _trace_operation_reference(
                            restore,
                            by_source_digest,
                            feature=feature,
                            state=initial,
                            route=run.get("route"),
                            action_index=1,
                            step_id=content.get("step_id"),
                        )
                    ):
                        change_reference = _trace_operation_reference(
                            change,
                            by_source_digest,
                            feature=feature,
                            state=target,
                            route=run.get("route"),
                            action_index=0,
                            step_id=content.get("step_id"),
                        )
                        restore_reference = _trace_operation_reference(
                            restore,
                            by_source_digest,
                            feature=feature,
                            state=initial,
                            route=run.get("route"),
                            action_index=1,
                            step_id=content.get("step_id"),
                        )
                        cycle = {
                            "step_id": content.get("step_id"),
                            "initial_state": initial,
                            "target_state": target,
                            "change_frame_evidence": change_reference,
                            "restore_frame_evidence": restore_reference,
                            "change_trace_sha256": sorted(
                                change.get("trace_evidence_sha256", [])
                            ),
                            "restore_trace_sha256": sorted(
                                restore.get("trace_evidence_sha256", [])
                            ),
                            "final_restore_frame_evidence": restore_reference,
                            "final_restore_trace_sha256": sorted(
                                restore.get("trace_evidence_sha256", [])
                            ),
                            "final_restore_state": initial,
                        }
                        if cycle not in recorded_cycles.setdefault(feature, []):
                            recorded_cycles[feature].append(cycle)
    if tier in {"silver", "gold"}:
        print("Output transition coverage recorded by machine evidence:")
        for feature in ("ac_output", "dc_output", "light_output"):
            states = recorded_targets.get(feature, set())
            print(
                f"  {feature}: {', '.join(sorted(states)) if states else 'none'}; "
                "both on and off transitions, each separately restored, are required"
            )
    if tier == "gold":
        print("Gold setting changes with a matching restoration:")
        for feature in ("eco_mode", "work_mode", "eco_timeout", "car_charger"):
            cycles = recorded_cycles.get(feature, [])
            print(f"  {feature}: {len(cycles)} verified cycle(s)")
    reviewable = [
        (feature, cycle)
        for feature, cycles in recorded_cycles.items()
        for cycle in cycles
    ]
    reviewed_by = None
    reviewed_at = datetime.now(UTC).isoformat()
    decisions = []
    scenario_decisions = []
    absence_decisions = []
    reviewer_independent_from_operator = False
    if reviewable or reconnect_candidate is not None or absence_candidates:
        print(
            "The public archive does not identify the HIL operator. Do not review "
            "your own run."
        )
        candidate = input("Independent reviewer (public identifier): ").strip()
        if re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", candidate):
            reviewed_by = candidate
            reviewer_independent_from_operator = (
                input(
                    "Confirm you are not the HIL operator/contributor who collected "
                    "this run? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
        else:
            print("Invalid reviewer identifier; all observations remain inconclusive.")
    if reviewed_by is not None:
        for feature, cycle in reviewable:
            print(
                f"Physical evidence for {feature}: {cycle['step_id']}, "
                f"{cycle['initial_state']} → {cycle['target_state']} → "
                f"{cycle['initial_state']}"
            )
            print(
                "  Change trace SHA-256 references: "
                + (", ".join(cycle["change_trace_sha256"]) or "none")
            )
            for label in ("change", "restore"):
                reference = cycle.get(f"{label}_frame_evidence")
                if isinstance(reference, dict):
                    print(
                        f"  {label.title()} frame: trace {reference['trace_sha256']}, "
                        f"seq {reference['frame_seq']}, "
                        f"frame SHA-256 {reference['frame_sha256']}, "
                        f"session generation {reference['session_generation']}"
                    )
            production_authorization_confirmed = (
                input(
                    "  Did you verify this used the production authorization path, "
                    "not a temporary HIL grant? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            change_confirmed = (
                input(
                    f"  Did you independently observe {feature} change to "
                    f"{cycle['target_state']}? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            restore_confirmed = (
                input(
                    f"  Did you independently observe {feature} restore to "
                    f"{cycle['initial_state']}? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            decisions.append(
                {
                    **cycle,
                    "production_authorization_confirmed": production_authorization_confirmed,
                    "physical_change_confirmed": change_confirmed,
                    "physical_restoration_confirmed": restore_confirmed,
                    "final_restoration_confirmed": (
                        restore_confirmed
                        if feature not in {"ac_output", "dc_output", "light_output"}
                        else False
                    ),
                }
            )
        reviewed_output_steps = sorted(
            {
                decision["step_id"]
                for decision in decisions
                if _FEATURE_STEPS.get(decision["step_id"])
                in {"ac_output", "dc_output", "light_output"}
                and _FEATURE_STEPS[decision["step_id"]] in final_restorations
            }
        )
        for step_id in reviewed_output_steps:
            feature = _FEATURE_STEPS[step_id]
            final = final_restorations[feature]
            reference = final["frame_evidence"]
            final_state = "on" if reference["requested_state"] else "off"
            print(
                f"Final physical restoration for {feature}: action 4 returns to "
                f"the original {final_state} state."
            )
            print(
                f"  Final frame: trace {reference['trace_sha256']}, seq "
                f"{reference['frame_seq']}, frame SHA-256 "
                f"{reference['frame_sha256']}, session generation "
                f"{reference['session_generation']}"
            )
            final_confirmed = (
                input(
                    "  Did you independently confirm the unit physically returned "
                    "to its original safe state after this final action? "
                    "[no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            for decision in decisions:
                if decision["step_id"] == step_id:
                    decision["final_restoration_confirmed"] = final_confirmed
        if reconnect_candidate is not None:
            print(
                "Physical reconnect evidence: source step basic_reconnect, "
                f"trace {reconnect_candidate['trace_sha256']}, frame "
                f"{reconnect_candidate['frame_sha256']} at seq "
                f"{reconnect_candidate['frame_seq']}, session generation "
                f"{reconnect_candidate['session_generation']}"
            )
            physical_action_confirmed = (
                input(
                    "  Did you independently confirm the route interruption and "
                    "restoration? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            physical_recovery_confirmed = (
                input(
                    "  Did you independently confirm the recovered session and "
                    "fresh status telemetry? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            scenario_decisions.append(
                {
                    **reconnect_candidate,
                    "physical_action_confirmed": physical_action_confirmed,
                    "physical_recovery_confirmed": physical_recovery_confirmed,
                }
            )
        for candidate in absence_candidates:
            feature = candidate["feature"]
            print(
                f"Profile-declared physical absence for {feature}; source plan step "
                f"{candidate['id']}. Unknown or unsupported assertions cannot exclude it."
            )
            physically_absent = (
                input(
                    "  Did independent physical inspection confirm this control is absent? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            safe_disable_confirmed = (
                input(
                    "  Did you confirm the feature is safely unavailable/disabled? [no/yes] (default no): "
                )
                .strip()
                .lower()
                == "yes"
            )
            absence_decisions.append(
                {
                    "feature": feature,
                    "source_step_id": candidate["id"],
                    "physical_absence_confirmed": physically_absent,
                    "safe_disable_confirmed": safe_disable_confirmed,
                }
            )
        audit_dir = repo_root / "hil-results" / "tier-reviews"
        _reject_symlink_components(audit_dir, repo_root)
        audit_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        audit_path = audit_dir / f"{run['run_id']}-{tier}-{stamp}.json"
        _reject_symlink_components(audit_path, repo_root)
        audit = {
            "schema_version": 1,
            "tier": tier,
            "archive_sha256": run["archive_sha256"],
            "run_id": run["run_id"],
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "reviewer_independent_from_operator": reviewer_independent_from_operator,
            "transitions": decisions,
            "scenarios": scenario_decisions,
            "absences": absence_decisions,
        }
        try:
            with audit_path.open("x", encoding="utf-8") as stream:
                json.dump(audit, stream, indent=2, sort_keys=True)
                stream.write("\n")
        except OSError as exc:
            raise HAAPIError("Unable to save local higher-tier review audit") from exc
        print(f"Inconclusive review audit: {audit_path.relative_to(repo_root)}")
        if matrix_path is not None:
            from .review import review_imported_tier_run

            dry_run_result = review_imported_tier_run(
                matrix_path,
                run_id=run["run_id"],
                review_file=audit_path,
                tier=tier,
                apply=False,
                repo_root=repo_root,
            )
            if dry_run_result != 0:
                return dry_run_result
            if (
                input(
                    "Type APPLY REVIEW to apply supported schema-v3 results "
                    "(anything else cancels): "
                ).strip()
                != "APPLY REVIEW"
            ):
                print("Review cancelled; qualification matrix unchanged.")
                return 2
            return review_imported_tier_run(
                matrix_path,
                run_id=run["run_id"],
                review_file=audit_path,
                tier=tier,
                apply=True,
                repo_root=repo_root,
            )
    print("No PASS can be recorded from these imported records yet.")
    print(
        "Blocked: the imported machine proof did not produce a supported "
        "schema-v3 result. The physical observations above remain inconclusive "
        "and no qualification PASS was recorded."
    )
    return 2
