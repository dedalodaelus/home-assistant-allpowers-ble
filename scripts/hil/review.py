"""Independently review privacy-safe imported Bronze evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import tempfile
import stat
from typing import Any

from .api import HAAPIError
from .evidence import _bronze_samples_fresh
from .importer import (
    _atomic_write,
    _canonical_model,
    _canonical_content_sha256,
    _reject_symlink_components,
)
from scripts import validate_hil_qualification as validation

ROOT = Path(__file__).resolve().parents[2]
CLAIMS = ("identity", "protocol_probe", "fresh_telemetry")
STATUS_REQUEST_HEX = validation.STATUS_REQUEST.hex()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HAAPIError("Unable to read imported review evidence") from exc
    if not isinstance(value, dict):
        raise HAAPIError("Invalid imported review evidence")
    return value


def _evidence(run: dict[str, Any], root: Path, name: str) -> tuple[str, Path]:
    refs = run.get("evidence")
    matches = [
        item for item in refs or [] if isinstance(item, str) and Path(item).name == name
    ]
    if len(matches) != 1:
        raise HAAPIError(f"Imported run must contain one {name}")
    relative = matches[0]
    target = root / relative
    _reject_symlink_components(target, root)
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise HAAPIError("Imported evidence path escapes repository") from exc
    if not stat.S_ISREG(resolved.lstat().st_mode):
        raise HAAPIError("Imported evidence is not a regular file")
    return relative, resolved


def _verified_members(run: dict[str, Any], root: Path) -> dict[str, bytes]:
    _, manifest_path = _evidence(run, root, "manifest.json")
    manifest = _json(manifest_path)
    records = manifest.get("files")
    if not isinstance(records, list):
        raise HAAPIError("Imported manifest has no file list")
    members: dict[str, bytes] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "size", "sha256"}
            or not isinstance(record.get("name"), str)
            or Path(record["name"]).name != record["name"]
            or type(record.get("size")) is not int
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", record["sha256"]) is None
        ):
            raise HAAPIError("Invalid imported manifest entry")
        _, path = _evidence(run, root, record["name"])
        content = path.read_bytes()
        if (
            len(content) != record["size"]
            or hashlib.sha256(content).hexdigest() != record["sha256"]
        ):
            raise HAAPIError("Imported evidence digest mismatch")
        members[record["name"]] = content
    members["manifest.json"] = manifest_path.read_bytes()
    if _canonical_content_sha256(members) != run.get("content_sha256"):
        raise HAAPIError("Imported archive content binding mismatch")
    return members


def _review_file(path: Path, run: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    value = _json(path)
    if (
        set(value)
        != {
            "schema_version",
            "archive_sha256",
            "run_id",
            "reviewed_by",
            "reviewed_at",
            "claims",
        }
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("archive_sha256") != run.get("archive_sha256")
        or not isinstance(value.get("archive_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", value["archive_sha256"]) is None
        or value.get("run_id") != run.get("run_id")
        or not isinstance(value.get("reviewed_by"), str)
        or re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", value["reviewed_by"].strip()) is None
        or not isinstance(value.get("reviewed_at"), str)
        or not isinstance(value.get("claims"), dict)
        or set(value["claims"]) != set(CLAIMS)
        or any(
            not isinstance(value["claims"].get(key), str)
            or value["claims"][key] not in {"pass", "inconclusive"}
            for key in CLAIMS
        )
    ):
        raise HAAPIError("Review file does not match this imported run")
    try:
        timestamp = datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00"))
        captured = datetime.fromisoformat(run["captured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise HAAPIError("Review timestamp is invalid") from exc
    if (
        timestamp.tzinfo is None
        or captured.tzinfo is None
        or timestamp.astimezone(UTC) < captured.astimezone(UTC)
        or timestamp.astimezone(UTC) > datetime.now(UTC)
    ):
        raise HAAPIError("Review timestamp must be timezone-aware and current")
    return value["reviewed_by"].strip(), timestamp.isoformat(), value["claims"]


def _tier_review_file(
    path: Path, run: dict[str, Any], tier: str
) -> tuple[
    str, str, bool, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    """Validate the exact run-bound local audit created by the tier wizard."""
    value = _json(path)
    expected = {
        "schema_version",
        "tier",
        "archive_sha256",
        "run_id",
        "reviewed_by",
        "reviewed_at",
        "reviewer_independent_from_operator",
        "transitions",
        "scenarios",
    }
    if (
        frozenset(value)
        not in {frozenset(expected), frozenset(expected | {"absences"})}
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("tier") != tier
        or value.get("archive_sha256") != run.get("archive_sha256")
        or value.get("run_id") != run.get("run_id")
        or not isinstance(value.get("reviewed_by"), str)
        or re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", value["reviewed_by"].strip()) is None
        or not isinstance(value.get("reviewed_at"), str)
        or type(value.get("reviewer_independent_from_operator")) is not bool
        or not isinstance(value.get("transitions"), list)
        or len(value["transitions"]) > 32
        or not isinstance(value.get("scenarios"), list)
        or len(value["scenarios"]) > 8
        or not isinstance(value.get("absences", []), list)
        or len(value.get("absences", [])) > 7
    ):
        raise HAAPIError("Tier review file does not match this imported run")
    try:
        reviewed_at = datetime.fromisoformat(
            value["reviewed_at"].replace("Z", "+00:00")
        )
        captured_at = datetime.fromisoformat(run["captured_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError) as exc:
        raise HAAPIError("Tier review timestamp is invalid") from exc
    if (
        reviewed_at.tzinfo is None
        or captured_at.tzinfo is None
        or reviewed_at.astimezone(UTC) < captured_at.astimezone(UTC)
        or reviewed_at.astimezone(UTC) > datetime.now(UTC)
    ):
        raise HAAPIError("Tier review timestamp must be timezone-aware and current")
    transition_keys = {
        "step_id",
        "initial_state",
        "target_state",
        "change_frame_evidence",
        "restore_frame_evidence",
        "change_trace_sha256",
        "restore_trace_sha256",
        "final_restore_frame_evidence",
        "final_restore_trace_sha256",
        "final_restore_state",
        "final_restoration_confirmed",
        "physical_change_confirmed",
        "physical_restoration_confirmed",
        "production_authorization_confirmed",
    }
    reference_keys = {
        "trace_sha256",
        "frame_sha256",
        "frame_seq",
        "transaction_sha256",
        "session_generation",
        "source_action_index",
        "action_stage",
        "source_step_id",
        "requested_state",
    }
    for item in value["transitions"]:
        feature = (
            _TIER_STEP_FEATURES.get(item.get("step_id"))
            if isinstance(item, dict)
            else None
        )
        output_feature = feature in {"ac_output", "dc_output", "light_output"}
        initial_state = item.get("initial_state") if isinstance(item, dict) else None
        target_state = item.get("target_state") if isinstance(item, dict) else None
        final_restore_state = (
            item.get("final_restore_state") if isinstance(item, dict) else None
        )
        valid_final_state = (
            isinstance(final_restore_state, str)
            and final_restore_state in {"on", "off"}
            if output_feature
            else (
                type(final_restore_state) is bool
                if feature in {"eco_mode", "car_charger"}
                else (
                    type(final_restore_state) is int
                    and final_restore_state in {0, 1, 2}
                    if feature == "work_mode"
                    else (
                        type(final_restore_state) is int
                        and final_restore_state in {1, 2, 4, 6}
                        if feature == "eco_timeout"
                        else False
                    )
                )
            )
        )
        allowed_states = (
            isinstance(initial_state, str)
            and initial_state in {"on", "off"}
            and isinstance(target_state, str)
            and target_state in {"on", "off"}
            if output_feature
            else (
                type(initial_state) is bool and type(target_state) is bool
                if feature in {"eco_mode", "car_charger"}
                else (
                    type(initial_state) is int
                    and initial_state in {0, 1, 2}
                    and type(target_state) is int
                    and target_state in {0, 1, 2}
                    if feature == "work_mode"
                    else (
                        type(initial_state) is int
                        and initial_state in {1, 2, 4, 6}
                        and type(target_state) is int
                        and target_state in {1, 2, 4, 6}
                        if feature == "eco_timeout"
                        else False
                    )
                )
            )
        )
        if (
            not isinstance(item, dict)
            or set(item) != transition_keys
            or item.get("step_id") not in _TIER_STEP_FEATURES
            or not isinstance(item.get("change_frame_evidence"), dict)
            or not isinstance(item.get("restore_frame_evidence"), dict)
            or not isinstance(item.get("final_restore_frame_evidence"), dict)
            or not allowed_states
            or not valid_final_state
            or initial_state == target_state
            or any(
                set(item[key]) != reference_keys
                for key in (
                    "change_frame_evidence",
                    "restore_frame_evidence",
                    "final_restore_frame_evidence",
                )
            )
            or not isinstance(item.get("change_trace_sha256"), list)
            or not isinstance(item.get("restore_trace_sha256"), list)
            or not isinstance(item.get("final_restore_trace_sha256"), list)
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                for digest in (
                    item["change_trace_sha256"]
                    + item["restore_trace_sha256"]
                    + item["final_restore_trace_sha256"]
                )
            )
            or any(
                type(item.get(key)) is not bool
                for key in (
                    "physical_change_confirmed",
                    "physical_restoration_confirmed",
                    "final_restoration_confirmed",
                    "production_authorization_confirmed",
                )
            )
        ):
            raise HAAPIError("Tier review transition is malformed")
        for index, name in enumerate(
            (
                "change_frame_evidence",
                "restore_frame_evidence",
                "final_restore_frame_evidence",
            )
        ):
            reference = item[name]
            expected_state = (
                target_state
                if index == 0
                else initial_state
                if index == 1
                else final_restore_state
            )
            expected_machine_state = (
                expected_state == "on" if output_feature else expected_state
            )
            if (
                any(
                    not isinstance(reference.get(key), str)
                    or re.fullmatch(r"[a-f0-9]{64}", reference[key]) is None
                    for key in ("trace_sha256", "frame_sha256")
                )
                or type(reference.get("frame_seq")) is not int
                or reference["frame_seq"] < 1
                or type(reference.get("session_generation")) is not int
                or reference["session_generation"] < 1
                or type(reference.get("source_action_index")) is not int
                or reference.get("source_action_index")
                not in (
                    ({0, 1, 2, 3} if index < 2 else {3})
                    if output_feature
                    else {index if index < 2 else 1}
                )
                or reference.get("action_stage")
                != ("transition" if index == 0 else "restoration")
                or reference.get("source_step_id") != item["step_id"]
                or type(reference.get("requested_state"))
                is not type(expected_machine_state)
                or reference.get("requested_state") != expected_machine_state
                or reference["trace_sha256"]
                not in item[
                    "change_trace_sha256"
                    if index == 0
                    else "final_restore_trace_sha256"
                    if index == 2
                    else "restore_trace_sha256"
                ]
            ):
                raise HAAPIError("Tier review frame reference is invalid")
        if (
            item["change_frame_evidence"]["session_generation"]
            != item["restore_frame_evidence"]["session_generation"]
        ):
            raise HAAPIError("Tier review actions cross a GATT session generation")
        final_reference = item["final_restore_frame_evidence"]
        if final_reference["session_generation"] != item["change_frame_evidence"][
            "session_generation"
        ] or (not output_feature and final_reference != item["restore_frame_evidence"]):
            raise HAAPIError("Tier review final restoration reference is invalid")
    scenario_keys = {
        "scenario",
        "source_step_id",
        "trace_sha256",
        "frame_sha256",
        "transaction_sha256",
        "frame_seq",
        "session_generation",
        "physical_action_confirmed",
        "physical_recovery_confirmed",
    }
    for item in value["scenarios"]:
        if (
            not isinstance(item, dict)
            or set(item) != scenario_keys
            or item.get("scenario") != "reconnect_failover"
            or item.get("source_step_id") != "basic_reconnect"
            or any(
                not isinstance(item.get(key), str)
                or re.fullmatch(r"[a-f0-9]{64}", item[key]) is None
                for key in (
                    "trace_sha256",
                    "frame_sha256",
                    "transaction_sha256",
                )
            )
            or type(item.get("frame_seq")) is not int
            or item["frame_seq"] < 1
            or type(item.get("session_generation")) is not int
            or item["session_generation"] < 1
            or type(item.get("physical_action_confirmed")) is not bool
            or type(item.get("physical_recovery_confirmed")) is not bool
        ):
            raise HAAPIError("Tier review scenario is malformed")
    absence_keys = {
        "feature",
        "source_step_id",
        "physical_absence_confirmed",
        "safe_disable_confirmed",
    }
    for item in value.get("absences", []):
        if (
            not isinstance(item, dict)
            or set(item) != absence_keys
            or item.get("feature") not in {"ac_output", "dc_output", "light_output"}
            or item.get("source_step_id")
            not in {"ac_output_cycle", "dc_output_cycle", "light_output_cycle"}
            or type(item.get("physical_absence_confirmed")) is not bool
            or type(item.get("safe_disable_confirmed")) is not bool
        ):
            raise HAAPIError("Tier review absence decision is malformed")
    if len({item["scenario"] for item in value["scenarios"]}) != len(
        value["scenarios"]
    ):
        raise HAAPIError("Tier review contains duplicate scenarios")
    return (
        value["reviewed_by"].strip(),
        reviewed_at.isoformat(),
        value["reviewer_independent_from_operator"],
        value["transitions"],
        value["scenarios"],
        value.get("absences", []),
    )


def _reconnect_review_candidate(
    run: dict[str, Any], execution_step: dict[str, Any], artifact_rows: list[Any]
) -> dict[str, Any] | None:
    """Find a complete, same-route reconnect trace eligible for physical review."""
    if (
        execution_step.get("step_id") != "basic_reconnect"
        or execution_step.get("outcome") != "pass"
        or run.get("route") not in validation.VERIFIED_ROUTES
        or run.get("capture_completeness") != "complete"
        or run.get("build_verification") != "verified"
    ):
        return None
    by_id = {
        item.get("artifact_id"): item.get("content", {})
        for item in artifact_rows
        if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
    }
    by_source = {
        item.get("source_sha256"): item.get("content", {})
        for item in artifact_rows
        if isinstance(item, dict) and isinstance(item.get("source_sha256"), str)
    }
    references = execution_step.get("machine_evidence", [])
    if not isinstance(references, list):
        return None
    checkpoint = None
    traces = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        content = by_id.get(reference.get("artifact_id"), {})
        if not isinstance(content, dict):
            continue
        if (
            content.get("kind") == "scenario_checkpoint"
            and content.get("scenario_id") == "recovery_reconnect"
        ):
            checkpoint = content
        elif content.get("kind") == "gatt_trace_metadata":
            digest = reference.get("source_sha256")
            if isinstance(digest, str) and by_source.get(digest) is content:
                traces.append((digest, content))
    if not isinstance(checkpoint, dict):
        return None
    rows = checkpoint.get("steps", [])
    actions = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("step_id") == "operator_action"
    ]
    recoveries = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("step_id") == "recovered_session"
    ]
    if len(actions) != 1 or not recoveries:
        return None
    action = actions[0]
    recovery = recoveries[-1]
    action_observation = action.get("observation", {})
    observation = recovery.get("observation", {})
    if isinstance(action_observation, dict) and isinstance(
        action_observation.get("observation"), dict
    ):
        action_observation = action_observation["observation"]
    if isinstance(observation, dict) and isinstance(
        observation.get("observation"), dict
    ):
        observation = observation["observation"]
    generation = observation.get("session_generation_after")
    if not (
        action.get("outcome") == "pass"
        and action_observation.get("operator_action")
        == "route interruption and restoration"
        and action_observation.get("operator_confirmed") is True
        and recovery.get("outcome") == "pass"
        and observation.get("disconnect_observed") is True
        and observation.get("connected") is True
        and observation.get("status_fresh") is True
        and observation.get("fresh_status_telemetry") is True
        and observation.get("route_verified") is True
        and observation.get("trace_complete") is True
        and observation.get("physical_outcome") == "yes"
        and observation.get("route") == run.get("route")
        and type(observation.get("session_generation_before")) is int
        and type(generation) is int
        and generation != observation.get("session_generation_before")
    ):
        return None
    for digest, trace in traces:
        if (
            trace.get("complete") is not True
            or trace.get("dropped_events") != 0
            or trace.get("dropped_frames") != 0
            or trace.get("observed_route") != run.get("route")
            or not isinstance(trace.get("events"), list)
            or not isinstance(trace.get("frames"), list)
        ):
            continue
        decoded = any(
            isinstance(event, dict)
            and event.get("event") == "notification_decoded"
            and event.get("session_generation") == generation
            and event.get("observed_route") == run.get("route")
            and "StatusData" in event.get("decoded_packet_types", [])
            for event in trace["events"]
        )
        if not decoded:
            continue
        for frame in trace["frames"]:
            if not (
                isinstance(frame, dict)
                and frame.get("event") == "write_attempt"
                and frame.get("direction") == "tx"
                and frame.get("command_type") == "status_request"
                and frame.get("frame_validation") == "validated_tx"
                and frame.get("session_generation") == generation
                and frame.get("frame_sha256")
                == hashlib.sha256(bytes.fromhex(STATUS_REQUEST_HEX)).hexdigest()
                and isinstance(frame.get("transaction_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", frame["transaction_sha256"])
            ):
                continue
            write_ok = any(
                isinstance(event, dict)
                and event.get("event") == "write_ok"
                and event.get("seq", 0) > frame.get("seq", 0)
                and event.get("session_generation") == generation
                and event.get("observed_route") == run.get("route")
                and event.get("transaction_sha256") == frame.get("transaction_sha256")
                for event in trace["events"]
            )
            if write_ok:
                return {
                    "scenario": "reconnect_failover",
                    "source_step_id": "basic_reconnect",
                    "trace_sha256": digest,
                    "frame_sha256": frame["frame_sha256"],
                    "transaction_sha256": frame["transaction_sha256"],
                    "frame_seq": frame["seq"],
                    "session_generation": generation,
                }
    return None


_TIER_STEP_FEATURES = {
    "ac_output_cycle": "ac_output",
    "dc_output_cycle": "dc_output",
    "light_output_cycle": "light_output",
    "eco_mode_reversal": "eco_mode",
    "work_mode_reversal": "work_mode",
    "eco_timeout_reversal": "eco_timeout",
    "car_charger_reversal": "car_charger",
}


def _validate_review_matrix(
    matrix: dict[str, Any],
    *,
    root: Path,
    files: dict[Path, bytes],
) -> list[str]:
    """Validate a dry-run proposal against temporary copies of public evidence."""
    with tempfile.TemporaryDirectory(prefix="hil-tier-review-") as temporary:
        validation_root = Path(temporary)
        for relative in _matrix_evidence_paths(matrix):
            source = root / relative
            _reject_symlink_components(source, root)
            try:
                source.resolve(strict=True).relative_to(root.resolve())
                content = source.read_bytes()
            except OSError, ValueError:
                continue
            target = validation_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for source, content in files.items():
            relative = source.relative_to(root)
            target = validation_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        candidate = validation_root / "qualification_matrix.json"
        candidate.write_text(json.dumps(matrix), encoding="utf-8")
        previous_root = validation.ROOT
        validation.ROOT = validation_root
        try:
            errors, _ = validation.validate_matrix(
                matrix,
                matrix_path=candidate,
                require_pass=False,
                max_age_days=None,
            )
        finally:
            validation.ROOT = previous_root
    return errors


def _matrix_evidence_paths(matrix: dict[str, Any]) -> set[str]:
    result: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and value.startswith("tests/hil/fixtures/"):
            result.add(value)

    collect(matrix)
    return result


def _rows(content: bytes) -> list[dict[str, Any]]:
    result = []
    for line in content.splitlines():
        if not line:
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, ValueError) as exc:
            raise HAAPIError("Imported GATT evidence is invalid") from exc
        if not isinstance(value, dict):
            raise HAAPIError("Imported GATT evidence is invalid")
        result.append(value)
    return result


def _claim_support(
    claim: str,
    revision: dict[str, Any],
    run: dict[str, Any],
    members: dict[str, bytes],
) -> tuple[bool, list[str], dict[str, Any]]:
    refs = run.get("evidence", [])
    bronze = json.loads(members.get("tier-bronze-read-only.json", b"{}"))
    session = json.loads(members.get("session.json", b"{}"))
    session = session.get("data", {})
    device = session.get("device", {})
    runtime = session.get("runtime_capabilities", {})
    manifest = json.loads(members["manifest.json"])
    build = manifest.get("build_provenance", {})
    scope_checks = {
        "verified_observed_route": run.get("route") in validation.VERIFIED_ROUTES,
        "complete_capture": run.get("capture_completeness") == "complete",
        "verified_build": run.get("build_verification") == "verified",
        "installed_checkout_match": run.get("installed_fingerprint")
        == run.get("checkout_fingerprint"),
        "valid_build_fingerprint": isinstance(run.get("build_fingerprint"), str)
        and re.fullmatch(r"[a-f0-9]{64}", run["build_fingerprint"]) is not None,
        "session_matches_capture": run.get("captured_at") == session.get("started_at"),
        "session_id_present": run.get("session_id") not in {None, "", "unknown"},
        "read_only_session": session.get("mode") == "read_only_preflight",
        "runtime_available": isinstance(runtime, dict)
        and runtime.get("available") is True,
        "runtime_connected": isinstance(runtime, dict)
        and runtime.get("connected") is True,
        "status_fresh": isinstance(runtime, dict)
        and runtime.get("status_fresh") is True,
        "no_pending_transaction": isinstance(runtime, dict)
        and runtime.get("pending_transaction") is False,
        "manifest_build_verified": build.get("verification") == "verified",
    }
    supported_scope = all(scope_checks.values())
    identity = bool(
        isinstance(device, dict)
        and device.get("model", {}).get("verified") is True
        and device.get("model", {}).get("value")
        == revision.get("model", "").removeprefix("ALLPOWERS ")
        and device.get("hardware_version", {}).get("verified") is True
        and device.get("hardware_version", {}).get("value")
        == revision.get("hardware_version")
        and device.get("firmware_version", {}).get("verified") is True
        and device.get("firmware_version", {}).get("value")
        == run.get("firmware_version")
    )
    samples = bronze.get("samples", [])
    telemetry = bool(
        bronze.get("fresh_verified_samples") is True
        and bronze.get("physical_observation") == "plausible"
        and _bronze_samples_fresh(samples)
    )
    protocol = False
    protocol_evidence: list[str] = []
    capture_id = bronze.get("protocol_capture_id")
    generation = bronze.get("protocol_session_generation")
    status_seq = bronze.get("protocol_status_request_seq")
    write_ok_seq = bronze.get("protocol_status_write_ok_seq")
    decoded_seq = bronze.get("protocol_decoded_response_seq")
    if (
        bronze.get("protocol_probe") == "verified"
        and bronze.get("protocol_status_request_hex") == STATUS_REQUEST_HEX
        and bronze.get("protocol_route") == run.get("route")
        and bronze.get("qualification_status") == "inconclusive"
        and isinstance(capture_id, str)
        and type(generation) is int
        and generation > 0
        and type(status_seq) is int
        and type(write_ok_seq) is int
        and type(decoded_seq) is int
    ):
        captures = manifest.get("captures", {}).get("by_capture", [])
        capture = next(
            (item for item in captures if item.get("capture_id") == capture_id), None
        )
        events = _rows(members.get("events.jsonl", b""))
        frames = _rows(members.get("frames.jsonl", b""))
        status = next(
            (
                row
                for row in events
                if row.get("capture_id") == capture_id
                and row.get("seq") == status_seq
                and row.get("event") == "write_attempt"
            ),
            None,
        )
        write_ok = next(
            (
                row
                for row in events
                if row.get("capture_id") == capture_id
                and row.get("seq") == write_ok_seq
                and row.get("event") == "write_ok"
            ),
            None,
        )
        decoded = next(
            (
                row
                for row in events
                if row.get("capture_id") == capture_id
                and row.get("seq") == decoded_seq
                and row.get("event") == "status_data_decoded"
            ),
            None,
        )
        frame = next(
            (
                row
                for row in frames
                if row.get("capture_id") == capture_id and row.get("seq") == status_seq
            ),
            None,
        )
        protocol = bool(
            supported_scope
            and capture
            and capture.get("complete") is True
            and capture.get("route") == run.get("route")
            and status
            and write_ok
            and decoded
            and status_seq < write_ok_seq
            and status_seq < decoded_seq
            and frame
            and frame.get("direction") == "tx"
            and frame.get("command_type") == "status_request"
            and frame.get("frame_validation") == "validated_tx"
            and status.get("session_generation") == decoded.get("session_generation")
            and status.get("session_generation") == write_ok.get("session_generation")
            and status.get("observed_route") == run.get("route")
            and write_ok.get("observed_route") == run.get("route")
            and decoded.get("observed_route") == run.get("route")
        )
        if protocol:
            protocol_evidence = [
                item
                for item in refs
                if isinstance(item, str)
                and Path(item).name in {"events.jsonl", "frames.jsonl"}
            ]
    support = supported_scope and (
        identity
        if claim == "identity"
        else protocol
        if claim == "protocol_probe"
        else telemetry
    )
    evidence = [
        item
        for item in refs
        if isinstance(item, str) and Path(item).name == "tier-bronze-read-only.json"
    ]
    if claim == "protocol_probe":
        evidence.extend(protocol_evidence)
    return (
        support,
        evidence,
        {
            "identity": identity,
            "protocol_probe": protocol,
            "fresh_telemetry": telemetry,
            "scope": scope_checks,
        },
    )


def _evidence_digests(
    paths: list[str], files: dict[Path, bytes], root: Path
) -> dict[str, str]:
    result = {}
    for relative in paths:
        path = root / relative
        content = files.get(path)
        if content is None:
            content = path.read_bytes()
        result[relative] = hashlib.sha256(content).hexdigest()
    return result


def review_imported_run(
    matrix_path: Path,
    *,
    run_id: str,
    review_file: Path,
    apply: bool = False,
    repo_root: Path = ROOT,
) -> int:
    """Preview/apply digest-bound Bronze claims for a previously imported archive."""
    root = repo_root.absolute()
    matrix_path = matrix_path.absolute()
    _reject_symlink_components(matrix_path, root)
    try:
        matrix_path.relative_to(root)
        before = matrix_path.read_bytes()
        matrix = json.loads(before)
    except (ValueError, OSError, UnicodeError) as exc:
        raise HAAPIError("Unable to read in-repository qualification matrix") from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != 3:
        raise HAAPIError("Qualification matrix must use schema v3")
    errors, _ = validation.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    if errors:
        raise HAAPIError("Qualification matrix validation failed: " + "; ".join(errors))
    found = next(
        (
            (revision, run)
            for revision in matrix.get("revisions", [])
            if isinstance(revision, dict)
            for run in revision.get("runs", [])
            if isinstance(run, dict) and run.get("run_id") == run_id
        ),
        None,
    )
    if found is None or not run_id.startswith("archive-"):
        raise HAAPIError("Imported archive run was not found")
    revision, run = found
    reviewer, reviewed_at, decisions = _review_file(review_file, run)
    members = _verified_members(run, root)
    files: dict[Path, bytes] = {}
    updated = json.loads(json.dumps(run))
    updated["authorization_context"] = "read_only"
    updated["authorization_context_source"] = "verified_read_only_session"
    updated["qualification_status"] = "pending"
    updated["bronze_evidence"] = {}
    run_rel = Path("tests/hil/fixtures") / revision["id"] / "runs" / run_id
    for claim in CLAIMS:
        supported, evidence, _checks = _claim_support(claim, revision, run, members)
        passed = decisions[claim] == "pass" and supported
        item: dict[str, Any] = {"status": "inconclusive", "evidence": []}
        if passed:
            if claim == "protocol_probe":
                capture_rel = (run_rel / "bronze-protocol-capture.jsonl").as_posix()
                bronze = json.loads(members["tier-bronze-read-only.json"])
                capture_id = bronze["protocol_capture_id"]
                status_seq = bronze["protocol_status_request_seq"]
                write_ok_seq = bronze["protocol_status_write_ok_seq"]
                decoded_seq = bronze["protocol_decoded_response_seq"]
                generation = bronze["protocol_session_generation"]
                scope = {
                    "revision_id": revision["id"],
                    "model": revision["model"],
                    "hardware_revision": revision["hardware_version"],
                    "firmware_version": updated["firmware_version"],
                    "route": updated["route"],
                    "run_id": updated["run_id"],
                    "captured_at": updated["captured_at"],
                    "session_id": updated["session_id"],
                    "build_fingerprint": updated["build_fingerprint"],
                }
                capture_rows = [
                    {
                        **scope,
                        "capture_id": capture_id,
                        "session_generation": generation,
                        "seq": status_seq,
                        "scenario": "read_only_snapshot",
                        "operation_id": "read_only_snapshot",
                        "event": "write_attempt",
                        "direction": "write",
                        "frame_hex": STATUS_REQUEST_HEX,
                        "verdict": "ok",
                    },
                    {
                        **scope,
                        "capture_id": capture_id,
                        "session_generation": generation,
                        "seq": write_ok_seq,
                        "scenario": "read_only_snapshot",
                        "operation_id": "read_only_snapshot",
                        "event": "write_ok",
                        "direction": "write",
                        "verdict": "ok",
                    },
                    {
                        **scope,
                        "capture_id": capture_id,
                        "session_generation": generation,
                        "seq": decoded_seq,
                        "scenario": "read_only_snapshot",
                        "operation_id": "read_only_snapshot",
                        "event": "status_data_decoded",
                        "direction": "notify",
                        "verdict": "ok",
                    },
                ]
                capture_rows.sort(key=lambda row: row["seq"])
                files[root / capture_rel] = (
                    "".join(
                        json.dumps(row, sort_keys=True) + "\n" for row in capture_rows
                    )
                ).encode()
                evidence.append(capture_rel)
            artifact_rel = (run_rel / f"bronze-{claim}-claim.json").as_posix()
            artifact: dict[str, Any] = {
                "artifact_type": "bronze_evidence",
                "claim": claim,
                "revision_id": revision["id"],
                "model": revision["model"],
                "hardware_revision": revision["hardware_version"],
                "firmware_version": updated["firmware_version"],
                "route": updated["route"],
                "run_id": updated["run_id"],
                "session_id": updated["session_id"],
                "build_fingerprint": updated["build_fingerprint"],
                "captured_at": updated["captured_at"],
            }
            if claim == "identity":
                artifact["identity_confirmed"] = True
            elif claim == "protocol_probe":
                artifact["capture_evidence"] = capture_rel
            else:
                samples = json.loads(members["tier-bronze-read-only.json"])["samples"]
                ages = [
                    record["sample_age_seconds"]
                    for sample in samples
                    for key, record in sample["telemetry"].items()
                    if key
                    in {"battery", "input_power", "output_power", "remaining_time"}
                    and record.get("verified") is True
                    and type(record.get("sample_age_seconds")) is int
                ]
                artifact.update(
                    fresh=True, plausible=True, sample_age_seconds=max(ages)
                )
            files[root / artifact_rel] = (
                json.dumps(artifact, sort_keys=True, indent=2) + "\n"
            ).encode()
            evidence.append(artifact_rel)
            item = {
                "status": "pass",
                "evidence": evidence,
                "claim_artifact": artifact_rel,
                "physical_review": {
                    "outcome": "confirmed_pass",
                    "reviewed_by": reviewer,
                    "reviewed_at": reviewed_at,
                },
            }
            if claim == "protocol_probe":
                item["capture_evidence"] = capture_rel
        else:
            item["rationale"] = (
                "Review declined or imported machine evidence did not satisfy the Bronze claim contract."
            )
        updated["bronze_evidence"][claim] = item
        if passed:
            for relative in evidence:
                if relative not in updated["evidence"]:
                    updated["evidence"].append(relative)
    for item in updated["bronze_evidence"].values():
        if item.get("status") == "pass":
            item["physical_review"]["evidence_sha256"] = _evidence_digests(
                item["evidence"], files, root
            )
    new_revision = json.loads(json.dumps(revision))
    new_revision["runs"] = [
        updated if item.get("run_id") == run_id else item for item in revision["runs"]
    ]
    proposed = dict(matrix)
    proposed["generated_at"] = datetime.now(UTC).date().isoformat()
    proposed["revisions"] = [
        new_revision if item is revision else item for item in matrix["revisions"]
    ]
    statuses = {
        claim: item["status"] for claim, item in updated["bronze_evidence"].items()
    }
    from .tiers import evaluate_tiers

    summary = {
        "run_id": run_id,
        "reviewed_by": reviewer,
        "bronze_claims": statuses,
        "applied": apply,
    }
    if not apply:
        current_tier_result = evaluate_tiers(matrix)
        summary["current_attained_tier"] = _attained_tier(
            current_tier_result, revision, run
        )
        print(json.dumps(summary, sort_keys=True, indent=2))
        return 0
    created: list[Path] = []
    try:
        for path, content in files.items():
            _reject_symlink_components(path, root)
            if path.exists():
                if path.is_symlink() or path.read_bytes() != content:
                    raise HAAPIError("Review evidence path conflicts")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content)
            created.append(path)
        errors, _ = validation.validate_matrix(
            proposed, matrix_path=matrix_path, require_pass=False, max_age_days=None
        )
        if errors:
            raise HAAPIError("Reviewed matrix failed validation: " + "; ".join(errors))
        tier_result = evaluate_tiers(proposed)
        summary["attained_tier"] = _attained_tier(tier_result, revision, run)
        if matrix_path.is_symlink() or matrix_path.read_bytes() != before:
            raise HAAPIError("Qualification matrix changed during review")
        _atomic_write(
            matrix_path,
            (json.dumps(proposed, indent=2, ensure_ascii=False) + "\n").encode(),
        )
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    print(json.dumps(summary, sort_keys=True, indent=2))
    print(json.dumps(tier_result, sort_keys=True, indent=2))
    return 0


def review_imported_tier_run(
    matrix_path: Path,
    *,
    run_id: str,
    review_file: Path,
    tier: str,
    apply: bool = False,
    repo_root: Path = ROOT,
) -> int:
    """Dry-run or atomically apply trace-bound Silver/Gold physical review."""
    if tier not in {"silver", "gold"}:
        raise HAAPIError("Offline tier application supports Silver or Gold only")
    root = repo_root.absolute()
    matrix_path = matrix_path.absolute()
    _reject_symlink_components(matrix_path, root)
    try:
        matrix_path.relative_to(root)
        before = matrix_path.read_bytes()
        matrix = json.loads(before)
    except (ValueError, OSError, UnicodeError) as exc:
        raise HAAPIError("Unable to read in-repository qualification matrix") from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != 3:
        raise HAAPIError("Qualification matrix must use schema v3")
    errors, _ = validation.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    if errors:
        raise HAAPIError("Qualification matrix validation failed: " + "; ".join(errors))
    found = next(
        (
            (revision, run)
            for revision in matrix.get("revisions", [])
            if isinstance(revision, dict)
            for run in revision.get("runs", [])
            if isinstance(run, dict) and run.get("run_id") == run_id
        ),
        None,
    )
    if found is None or not run_id.startswith("archive-"):
        raise HAAPIError("Imported archive run was not found")
    revision, run = found
    review_file = review_file.absolute()
    _reject_symlink_components(review_file, root)
    try:
        review_file.relative_to(root)
        if not stat.S_ISREG(review_file.lstat().st_mode):
            raise HAAPIError("Tier review file must be a regular file")
    except ValueError as exc:
        raise HAAPIError("Tier review file must be inside the repository") from exc
    (
        reviewer,
        reviewed_at,
        reviewer_independent_from_operator,
        decisions,
        scenario_decisions,
        absence_decisions,
    ) = _tier_review_file(review_file, run, tier)
    if not reviewer_independent_from_operator:
        print(
            "The reviewer did not confirm independence from the HIL operator; "
            "no qualification result can be applied."
        )
        return 2
    members = _verified_members(run, root)
    execution = json.loads(members.get("tier-execution.json", b"{}"))
    artifacts = json.loads(members.get("tier-artifacts.json", b"{}"))
    if not isinstance(execution, dict) or not isinstance(artifacts, dict):
        raise HAAPIError("Imported guided tier proof is unavailable")
    execution_steps = {
        item.get("step_id"): item
        for item in execution.get("steps", [])
        if isinstance(item, dict) and isinstance(item.get("step_id"), str)
    }
    trace_artifacts = {
        item.get("source_sha256"): item.get("content", {})
        for item in artifacts.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("source_sha256"), str)
    }
    artifact_rows = artifacts.get("artifacts", [])
    reviewed_scenarios: list[dict[str, Any]] = []
    reconnect_step = execution_steps.get("basic_reconnect", {})
    reconnect_candidate = _reconnect_review_candidate(
        run, reconnect_step, artifact_rows
    )
    for decision in scenario_decisions:
        if (
            reconnect_candidate is not None
            and all(
                decision.get(key) == reconnect_candidate.get(key)
                for key in (
                    "scenario",
                    "source_step_id",
                    "trace_sha256",
                    "frame_sha256",
                    "transaction_sha256",
                    "frame_seq",
                    "session_generation",
                )
            )
            and decision.get("physical_action_confirmed") is True
            and decision.get("physical_recovery_confirmed") is True
        ):
            reviewed_scenarios.append(reconnect_candidate)
    absence_plan: dict[str, str] = {}
    try:
        session = json.loads(members["session.json"])
        session_data = session.get("data", {})
        test_plan = session_data.get("test_plan", {})
        session_device = session_data.get("device", {})
        session_model = _canonical_model(session_device.get("model", {}).get("value"))
        revision_model = _canonical_model(revision.get("model"))
        plan_model = _canonical_model(test_plan.get("device", {}).get("model"))
        if (
            session_model is not None
            and session_model == revision_model == plan_model
            and session_device.get("hardware_version", {}).get("value")
            == revision.get("hardware_version")
            and session_device.get("firmware_version", {}).get("value")
            == run.get("firmware_version")
        ):
            absence_plan = {
                step.get("feature"): step.get("id")
                for step in test_plan.get("steps", [])
                if isinstance(step, dict)
                and step.get("feature") in {"ac_output", "dc_output", "light_output"}
                and step.get("physical_presence") == "absent"
                and step.get("eligibility") == "not_applicable"
                and "runtime profile substantiates physical absence"
                in step.get("reasons", [])
            }
    except KeyError, TypeError, ValueError, UnicodeError:
        absence_plan = {}
    reviewed_absences = [
        decision
        for decision in absence_decisions
        if absence_plan.get(decision.get("feature")) == decision.get("source_step_id")
        and decision.get("physical_absence_confirmed") is True
        and decision.get("safe_disable_confirmed") is True
    ]
    updated = json.loads(json.dumps(run))
    files: dict[Path, bytes] = {}
    run_rel = Path("tests/hil/fixtures") / revision["id"] / "runs" / run_id
    source_bundle = next(
        (item for item in run["evidence"] if Path(item).name == "tier-artifacts.json"),
        None,
    )
    source_execution = next(
        (item for item in run["evidence"] if Path(item).name == "tier-execution.json"),
        None,
    )
    if source_bundle is None or source_execution is None:
        raise HAAPIError("Imported guided tier artifact graph is incomplete")
    common_evidence = [source_bundle, source_execution]
    session_evidence = next(
        (item for item in run.get("evidence", []) if Path(item).name == "session.json"),
        None,
    )
    accepted_cycles: dict[str, list[dict[str, Any]]] = {}
    presence_evidence: dict[str, list[str]] = {}
    scope = {
        "revision_id": revision["id"],
        "model": revision["model"],
        "hardware_revision": revision["hardware_version"],
        "firmware_version": run["firmware_version"],
        "route": run["route"],
        "run_id": run["run_id"],
        "captured_at": run["captured_at"],
        "session_id": run["session_id"],
        "build_fingerprint": run["build_fingerprint"],
    }

    for decision in decisions:
        feature = _TIER_STEP_FEATURES[decision["step_id"]]
        if tier == "silver" and feature not in {
            "ac_output",
            "dc_output",
            "light_output",
        }:
            continue
        source_step = execution_steps.get(decision["step_id"], {})
        if not (
            isinstance(source_step, dict)
            and source_step.get("authorization_context") == "production"
            and source_step.get("authorization_context_source")
            == "runtime_production_authorized"
            and decision["production_authorization_confirmed"] is True
        ):
            continue
        feature_artifact = next(
            (
                item.get("content")
                for item in artifacts.get("artifacts", [])
                if isinstance(item, dict)
                and isinstance(item.get("content"), dict)
                and item["content"].get("kind") == "guided_step_evidence"
                and item["content"].get("step_id") == decision["step_id"]
            ),
            None,
        )
        if not isinstance(feature_artifact, dict):
            continue
        if feature in {"ac_output", "dc_output", "light_output"}:
            actions = feature_artifact.get("actions")
            final_state = feature_artifact.get("initial_state")
            if (
                not isinstance(actions, list)
                or len(actions) != 4
                or feature_artifact.get("final_restore_action_index") != 3
                or decision.get("final_restore_state") != final_state
                or final_state not in {"on", "off"}
            ):
                continue
            from .review_wizard import _trace_operation_reference

            expected_final_reference = _trace_operation_reference(
                actions[3],
                trace_artifacts,
                feature=feature,
                state=final_state,
                route=run.get("route"),
                action_index=3,
                action_stage="restoration",
                step_id=decision["step_id"],
            )
            if expected_final_reference != decision.get("final_restore_frame_evidence"):
                continue
        physical_pass = (
            decision["physical_change_confirmed"] is True
            and decision["physical_restoration_confirmed"] is True
            and (
                feature not in {"ac_output", "dc_output", "light_output"}
                or (
                    decision["final_restoration_confirmed"] is True
                    and decision["final_restore_frame_evidence"]["trace_sha256"]
                    in trace_artifacts
                )
            )
            and feature_artifact.get("outcome") == "pass"
            and feature_artifact.get("physical_change_outcome") == "correct"
            and feature_artifact.get("physical_restore_outcome") == "correct"
        )
        if not physical_pass:
            continue
        change_reference = decision["change_frame_evidence"]
        restore_reference = decision["restore_frame_evidence"]
        if not (
            change_reference.get("trace_sha256") in trace_artifacts
            and restore_reference.get("trace_sha256") in trace_artifacts
        ):
            continue
        accepted_cycles.setdefault(feature, []).append(decision)
        presence_evidence.setdefault(feature, []).extend(
            reference
            for reference in common_evidence
            if reference not in presence_evidence.get(feature, [])
        )

    if not accepted_cycles and not reviewed_scenarios and not reviewed_absences:
        print(
            "No supported production-authorized physical result was independently "
            "confirmed; the qualification matrix remains unchanged."
        )
        return 2

    from .tiers import evaluate_tiers

    updated["feature_presence"] = json.loads(json.dumps(run["feature_presence"]))
    updated["write_capabilities"] = json.loads(json.dumps(run["write_capabilities"]))
    if not isinstance(updated.get("bronze_evidence"), dict):
        updated["bronze_evidence"] = {
            claim: {"status": "inconclusive", "evidence": []} for claim in CLAIMS
        }
    updated["qualification_status"] = "pending"
    applied_capabilities: list[str] = []
    applied_scenarios: list[str] = []
    review_tag = Path(review_file).name.removesuffix(".json")

    def capture_path(feature: str, index: int) -> str:
        return (run_rel / f"{review_tag}-{feature}-{index:02d}.jsonl").as_posix()

    def add_capture(path: str, references: list[dict[str, Any]], feature: str) -> None:
        rows = []
        for reference in references:
            rows.append(
                {
                    **scope,
                    "capability": feature,
                    "operation_id": feature,
                    "requested_state": reference["requested_state"],
                    "direction": "write",
                    "action_stage": reference["action_stage"],
                    "verdict": "ok",
                    "physical_outcome": "pass",
                    "session_generation": reference["session_generation"],
                    "source_trace_sha256": reference["trace_sha256"],
                    "source_frame_sha256": reference["frame_sha256"],
                    "source_frame_seq": reference["frame_seq"],
                    "source_transaction_sha256": reference["transaction_sha256"],
                    "source_step_id": reference["source_step_id"],
                    "source_action_index": reference["source_action_index"],
                }
            )
        rows.sort(key=lambda row: (row["session_generation"], row["source_frame_seq"]))
        files[root / path] = "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in rows
        ).encode()

    for feature, cycles in accepted_cycles.items():
        updated["feature_presence"][feature] = {
            "status": "present",
            "evidence": presence_evidence.get(feature, common_evidence),
        }
        if tier == "gold" and feature not in {"ac_output", "dc_output", "light_output"}:
            if len(cycles) != 1:
                continue
            cycle = cycles[0]
            path = (run_rel / f"{review_tag}-{feature}-restoration.json").as_posix()
            restoration_capture = capture_path(feature, 1)
            add_capture(
                restoration_capture,
                [cycle["change_frame_evidence"], cycle["restore_frame_evidence"]],
                feature,
            )
            artifact = {
                "artifact_type": "feature_restoration",
                **scope,
                "feature": feature,
                "restored": True,
                "pre_state": cycle["initial_state"],
                "restored_state": cycle["initial_state"],
            }
            files[root / path] = (
                json.dumps(artifact, sort_keys=True, indent=2) + "\n"
            ).encode()
            evidence = [*common_evidence, restoration_capture, path]
            updated["write_capabilities"][feature] = {
                "status": "pass",
                "evidence": evidence,
                "physical_review": {
                    "outcome": "confirmed_pass",
                    "reviewed_by": reviewer,
                    "reviewed_at": reviewed_at,
                    "restoration_evidence": path,
                    "restoration_capture": restoration_capture,
                },
            }
            updated["write_capabilities"][feature]["physical_review"][
                "evidence_sha256"
            ] = _evidence_digests(evidence, files, root)
            applied_capabilities.append(feature)
            continue
        if feature not in {"ac_output", "dc_output", "light_output"}:
            continue
        by_state: dict[str, dict[str, Any]] = {}
        for cycle in cycles:
            target = cycle["target_state"]
            if target in {"on", "off"} and target not in by_state:
                by_state[target] = cycle
        if set(by_state) != {"on", "off"}:
            continue
        final_state = cycles[0]["final_restore_state"]
        final_reference = cycles[0]["final_restore_frame_evidence"]
        if (
            final_state not in {"on", "off"}
            or final_reference.get("source_action_index") != 3
            or final_reference.get("action_stage") != "restoration"
            or final_reference.get("requested_state") != (final_state == "on")
            or any(
                cycle["final_restore_state"] != final_state
                or cycle["final_restore_frame_evidence"] != final_reference
                for cycle in cycles
            )
        ):
            continue
        final_capture = capture_path(feature, 3)
        add_capture(final_capture, [final_reference], feature)
        transitions = []
        evidence = [*common_evidence, final_capture]
        for state_index, state in enumerate(("on", "off"), start=1):
            cycle = by_state[state]
            expected_initial = "off" if state == "on" else "on"
            if cycle["initial_state"] != expected_initial:
                continue
            capture = capture_path(feature, state_index)
            add_capture(
                capture,
                [
                    cycle["change_frame_evidence"],
                    cycle["restore_frame_evidence"],
                ],
                feature,
            )
            artifact_path = (
                run_rel / f"{review_tag}-{feature}-{state}-transition.json"
            ).as_posix()
            artifact = {
                "artifact_type": "output_transition",
                **scope,
                "feature": feature,
                "requested_state": state,
                "observed_state": state,
                "initial_state": expected_initial,
                "restored": True,
                "restored_state": expected_initial,
            }
            files[root / artifact_path] = (
                json.dumps(artifact, sort_keys=True, indent=2) + "\n"
            ).encode()
            evidence.extend((capture, artifact_path))
            transitions.append(
                {
                    "state": state,
                    "evidence": artifact_path,
                    "capture_evidence": capture,
                    "restoration_capture": capture,
                }
            )
        if len(transitions) != 2:
            continue
        updated["write_capabilities"][feature] = {
            "status": "pass",
            "evidence": evidence,
            "physical_review": {
                "outcome": "confirmed_pass",
                "reviewed_by": reviewer,
                "reviewed_at": reviewed_at,
                "reviewer_independent_from_operator": True,
                "output_transitions": transitions,
                "final_restoration": {
                    "state": final_state,
                    "capture_evidence": final_capture,
                    "frame_evidence": final_reference,
                    "confirmed": True,
                    "reviewer_independent_from_operator": True,
                },
            },
        }
        updated["write_capabilities"][feature]["physical_review"]["evidence_sha256"] = (
            _evidence_digests(evidence, files, root)
        )
        applied_capabilities.append(feature)

    for decision in reviewed_scenarios:
        capture = (run_rel / "reconnect-failover-review.jsonl").as_posix()
        row = {
            **scope,
            "scenario": "reconnect_failover",
            "operation_id": "reconnect_failover",
            "direction": "write",
            "frame_hex": STATUS_REQUEST_HEX,
            "verdict": "ok",
            "physical_outcome": "pass",
            "source_trace_sha256": decision["trace_sha256"],
            "source_frame_sha256": decision["frame_sha256"],
            "source_transaction_sha256": decision["transaction_sha256"],
            "source_frame_seq": decision["frame_seq"],
            "source_step_id": decision["source_step_id"],
            "session_generation": decision["session_generation"],
        }
        files[root / capture] = (json.dumps(row, sort_keys=True) + "\n").encode()
        evidence = [*common_evidence, capture]
        result = {
            "status": "pass",
            "evidence": evidence,
            "physical_review": {
                "outcome": "confirmed_pass",
                "reviewed_by": reviewer,
                "reviewed_at": reviewed_at,
            },
        }
        result["physical_review"]["evidence_sha256"] = _evidence_digests(
            evidence, files, root
        )
        updated["scenarios"]["reconnect_failover"] = result
        applied_scenarios.append("reconnect_failover")

    for decision in reviewed_absences:
        feature = decision["feature"]
        path = (run_rel / f"{review_tag}-{feature}-physical-absence.json").as_posix()
        artifact = {
            "artifact_type": "physical_absence",
            **scope,
            "feature": feature,
            "source_step_id": decision["source_step_id"],
            "physical_absence_confirmed": True,
            "safe_disable_confirmed": True,
            "reviewed_by": reviewer,
            "reviewed_at": reviewed_at,
        }
        files[root / path] = (
            json.dumps(artifact, sort_keys=True, indent=2) + "\n"
        ).encode()
        evidence = [
            *common_evidence,
            *([session_evidence] if session_evidence else []),
            path,
        ]
        updated["feature_presence"][feature] = {
            "status": "absent",
            "evidence": evidence,
            "reviewed_by": reviewer,
            "reviewed_at": reviewed_at,
            "evidence_sha256": _evidence_digests(evidence, files, root),
        }
        updated["write_capabilities"][feature] = {
            "status": "not_applicable",
            "evidence": evidence,
            "rationale": "independently reviewed physical absence and safe disable",
        }
        applied_capabilities.append(f"{feature}: absent")

    if applied_capabilities:
        updated["authorization_context"] = "production"
        updated["authorization_context_source"] = "reviewed_production_session"
    for path in (source_bundle, source_execution):
        if path not in updated["evidence"]:
            updated["evidence"].append(path)
    for items in updated["write_capabilities"].values():
        if items.get("status") == "pass":
            for path in items.get("evidence", []):
                if path not in updated["evidence"]:
                    updated["evidence"].append(path)
    for item in updated["scenarios"].values():
        if item.get("status") == "pass":
            for path in item.get("evidence", []):
                if path not in updated["evidence"]:
                    updated["evidence"].append(path)
    new_revision = json.loads(json.dumps(revision))
    new_revision["runs"] = [
        updated if item.get("run_id") == run_id else item for item in revision["runs"]
    ]
    proposed = dict(matrix)
    proposed["generated_at"] = datetime.now(UTC).date().isoformat()
    proposed["revisions"] = [
        new_revision if item is revision else item for item in matrix["revisions"]
    ]
    validation_errors = _validate_review_matrix(
        proposed,
        root=root,
        files=files,
    )
    if validation_errors:
        raise HAAPIError(
            "Tier review failed schema-v3 validation: " + "; ".join(validation_errors)
        )
    tier_result = evaluate_tiers(proposed)
    summary = {
        "run_id": run_id,
        "tier": tier,
        "reviewed_by": reviewer,
        "reviewed_capabilities": applied_capabilities,
        "reviewed_scenarios": applied_scenarios,
        "attained_tier": _attained_tier(tier_result, revision, updated),
        "applied": apply,
    }
    if not apply:
        print(json.dumps(summary, sort_keys=True, indent=2))
        print(json.dumps(tier_result, sort_keys=True, indent=2))
        return 0
    created: list[Path] = []
    try:
        for path, content in files.items():
            _reject_symlink_components(path, root)
            if path.exists():
                if path.is_symlink() or path.read_bytes() != content:
                    raise HAAPIError("Tier review evidence path conflicts")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content)
            created.append(path)
        errors, _ = validation.validate_matrix(
            proposed, matrix_path=matrix_path, require_pass=False, max_age_days=None
        )
        if errors:
            raise HAAPIError(
                "Tier reviewed matrix failed validation: " + "; ".join(errors)
            )
        if matrix_path.is_symlink() or matrix_path.read_bytes() != before:
            raise HAAPIError("Qualification matrix changed during tier review")
        _atomic_write(
            matrix_path,
            (json.dumps(proposed, indent=2, ensure_ascii=False) + "\n").encode(),
        )
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    print(json.dumps(summary, sort_keys=True, indent=2))
    print(json.dumps(tier_result, sort_keys=True, indent=2))
    return 0


def _attained_tier(
    tier_result: dict[str, Any], revision: dict[str, Any], run: dict[str, Any]
) -> str:
    return next(
        (
            item["tier"]
            for item in tier_result.get("tiers", [])
            if item.get("scope", {}).get("revision_id") == revision.get("id")
            and item.get("scope", {}).get("firmware") == run.get("firmware_version")
            and item.get("scope", {}).get("observed_route") == run.get("route")
        ),
        "unqualified",
    )
