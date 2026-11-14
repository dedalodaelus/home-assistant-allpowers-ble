#!/usr/bin/env python3
"""Validate schema-v3 HIL qualification records and privacy safeguards."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX_SCHEMA_VERSION = 3
DEFAULT_MATRIX = ROOT / "tests" / "hil" / "qualification_matrix.json"
STATUSES = {"pass", "fail", "pending", "not_applicable", "inconclusive"}
STRICT_SCENARIO_STATUSES = {"pass"}
STRICT_CAPABILITY_STATUSES = {"pass", "not_applicable"}
VERIFIED_ROUTES = {"local_adapter", "active_proxy"}
REQUIRED_ROUTES = VERIFIED_ROUTES  # compatibility for callers using the old constant
RUN_ROUTES = VERIFIED_ROUTES | {"unverified", "mixed", "auto"}
CAPTURE_COMPLETENESS = {"complete", "incomplete", "unknown"}
BUILD_VERIFICATION = {"verified", "unverified", "mismatch"}
REQUIRED_SCENARIOS = (
    "golden_vector_replay",
    "reconnect_failover",
    "rapid_consecutive_writes",
    "stale_timeout_recovery",
    "soak_24h",
    "upgrade_reload",
    "vendor_app_contention",
)
REQUIRED_WRITE_CAPABILITIES = (
    "ac_output",
    "dc_output",
    "light_output",
    "eco_mode",
    "work_mode",
    "eco_timeout",
    "car_charger",
)
MAC_ADDRESS = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
PROTOCOL_HEADER = bytes((0xA5, 0x65))
STATUS_REQUEST = bytes.fromhex("A5 65 B1 00 01 06 01 00 00 00 00 00")
VALID_ECO_TIMEOUTS = frozenset((1, 2, 4, 6))
SUPPORTED_PROTOCOL_PROFILES = {
    ("ALLPOWERS R600", "r600-hw03", "0.3", "0x03"),
    ("VOLIX P1800", "volix-p1800-hw03", "0.3", "0x03"),
}


def _valid_protocol_frame(
    frame_hex: str,
    direction: Any,
    operation: str,
    requested_state: Any = None,
) -> bool:
    """Validate documented R600 notification and outbound GATT frame forms."""
    try:
        frame = bytes.fromhex(frame_hex)
    except ValueError:
        return False
    if len(frame) < 8 or frame[:2] != PROTOCOL_HEADER:
        return False

    if direction == "write" and frame == STATUS_REQUEST:
        return operation in {"stale_timeout_recovery", "reconnect_failover"}
    if _xor(frame) != 0:
        return False

    if direction == "notify":
        if frame[5] > 128 or len(frame) != 8 + frame[5]:
            return False
        command, payload = frame[6], frame[7:-1]
        if command == 0x01:
            valid = len(payload) >= 8 and payload[1] <= 100
        elif command == 0x03:
            valid = len(payload) >= 6
        elif command == 0x35:
            try:
                payload.rstrip(b"\x00").decode("utf-8")
                valid = True
            except UnicodeDecodeError:
                valid = False
        else:
            return False
        return valid and operation in {
            "golden_vector_replay",
            "reconnect_failover",
            "stale_timeout_recovery",
            "soak_24h",
            "upgrade_reload",
            "vendor_app_contention",
        }

    if direction != "write":
        return False
    # The observed status request is a special outbound envelope, distinct
    # from notification frames and the normal command-write envelope.
    if frame[2:5] != bytes((0x00, 0xB1, 0x01)):
        return False
    command, payload_length, payload = frame[5], frame[6], frame[7:-1]
    if command == 0x01:
        # Combined output control has an empty encoded payload length and one
        # flags byte, as documented by encode_output_control().
        if not (
            len(frame) == 9
            and payload_length == 0
            and len(payload) == 1
            and payload[0] & ~0x23 == 0
        ):
            return False
        if operation not in {
            "rapid_consecutive_writes",
            "ac_output",
            "dc_output",
            "light_output",
        }:
            return False
        if operation == "rapid_consecutive_writes":
            return True
        mask = {"ac_output": 0x02, "dc_output": 0x01, "light_output": 0x20}[operation]
        return (
            type(requested_state) is bool and bool(payload[0] & mask) is requested_state
        )
    if command == 0x02:
        if not (
            len(frame) == 8 + payload_length
            and payload_length == 2
            and len(payload) == 2
            and ((payload[0] & 0x06) >> 1) != 0x03
            and payload[1] in VALID_ECO_TIMEOUTS
        ):
            return False
        if operation == "rapid_consecutive_writes":
            return True
        if operation == "eco_mode":
            return (
                type(requested_state) is bool
                and bool(payload[0] & 0x01) is requested_state
            )
        if operation == "car_charger":
            return (
                type(requested_state) is bool
                and bool(payload[0] & 0x10) is requested_state
            )
        if operation == "work_mode":
            return (
                type(requested_state) is int
                and requested_state in {0, 1, 2}
                and ((payload[0] & 0x06) >> 1) == requested_state
            )
        if operation == "eco_timeout":
            return (
                type(requested_state) is int
                and requested_state in VALID_ECO_TIMEOUTS
                and payload[1] == requested_state
            )
    return False


def _xor(frame: bytes) -> int:
    checksum = 0
    for byte in frame:
        checksum ^= byte
    return checksum


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Require reviewed eligible scoped passes; evidence does not expire by age",
    )
    parser.add_argument("--route", choices=sorted(VERIFIED_ROUTES))
    parser.add_argument(
        "--revision", help="Limit strict qualification to one revision ID"
    )
    parser.add_argument(
        "--firmware", help="Limit strict qualification to one firmware version"
    )
    parser.add_argument("--feature", choices=REQUIRED_WRITE_CAPABILITIES)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Opt-in evidence freshness audit; support qualification does not expire by age",
    )
    args = parser.parse_args()
    if args.max_age_days is not None and args.max_age_days < 0:
        parser.error("--max-age-days must be a non-negative integer")
    scope_values = (args.revision, args.firmware, args.route, args.feature)
    if any(scope_values) and not all(scope_values):
        parser.error(
            "--revision, --firmware, --route, and --feature must be supplied together"
        )
    if any(scope_values) and not args.require_pass:
        parser.error("scoped qualification requires --require-pass")
    return args


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("matrix root must be a JSON object")
    return payload


def _error(errors: list[str], message: str) -> None:
    errors.append(message)


def _strings(value: Any):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _check_date(value: Any, context: str, errors: list[str]) -> date | None:
    if not isinstance(value, str):
        _error(errors, f"{context} must be a YYYY-MM-DD date")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _error(errors, f"{context} must be a YYYY-MM-DD date")
        return None


def _check_evidence(
    values: Any, context: str, errors: list[str], *, required: bool = False
) -> None:
    if not isinstance(values, list) or (required and not values):
        _error(
            errors,
            f"{context} evidence must be {'a non-empty' if required else 'a'} list",
        )
        return
    for index, value in enumerate(values, 1):
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            _error(
                errors,
                f"{context} evidence[{index}] must be a safe repository-relative path",
            )
            continue
        path = (ROOT / value).resolve()
        try:
            path.relative_to(ROOT)
        except ValueError:
            _error(errors, f"{context} evidence[{index}] escapes the repository")
            continue
        if not path.is_file():
            _error(errors, f"{context} evidence file not found: {value}")
            continue
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            _error(errors, f"{context} evidence file unreadable {value}: {exc}")
            continue
        if MAC_ADDRESS.search(contents):
            _error(errors, f"{value} contains a Bluetooth MAC address")


def _capture_rows(values: Any) -> list[dict[str, Any]] | None:
    """Load scoped JSONL rows, returning None if any capture is malformed."""
    if not isinstance(values, list):
        return []
    rows: list[dict[str, Any]] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value.endswith(".jsonl")
            or ".sample." in Path(value).name
        ):
            continue
        path = (ROOT / value).resolve()
        try:
            path.relative_to(ROOT)
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeError, ValueError:
            return None
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError, TypeError:
                return None
            if not isinstance(row, dict):
                return None
            rows.append(row)
    return rows


def _evidence_digests(references: Any) -> dict[str, str] | None:
    """Return a stable path-to-SHA256 manifest for every referenced file."""
    if not isinstance(references, list) or not references:
        return None
    digests: dict[str, str] = {}
    for value in references:
        if not isinstance(value, str) or not value:
            return None
        path = (ROOT / value).resolve()
        try:
            path.relative_to(ROOT)
            digests[value] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError, ValueError:
            return None
    return dict(sorted(digests.items()))


def _has_bound_capture(
    values: Any,
    *,
    route: Any,
    revision_id: str,
    firmware_version: Any,
    run_id: Any,
    captured_at: Any,
    session_id: Any,
    build_fingerprint: Any,
    result_key: str,
    capability: bool,
    expected_verdict: str,
) -> bool:
    rows = _capture_rows(values)
    if rows is None:
        return False
    for row in rows:
        frame_hex = row.get("frame_hex")
        row_captured_at = row.get("captured_at")
        try:
            capture_time = datetime.fromisoformat(
                row_captured_at.replace("Z", "+00:00")
            )
            run_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except AttributeError, ValueError:
            continue
        if (
            row.get("route") != route
            or row.get("revision_id") != revision_id
            or row.get("firmware_version") != firmware_version
            or row.get("run_id") != run_id
            or row.get("session_id") != session_id
            or row.get("build_fingerprint") != build_fingerprint
            or capture_time.tzinfo is None
            or run_time.tzinfo is None
            or capture_time.astimezone(UTC) != run_time.astimezone(UTC)
            or row.get("verdict") != expected_verdict
            or row.get("physical_outcome")
            != ("not_applicable" if expected_verdict == "not_applicable" else "pass")
            or not isinstance(frame_hex, str)
            or re.fullmatch(r"(?:[0-9A-Fa-f]{2}){1,256}", frame_hex) is None
            or not _valid_protocol_frame(
                frame_hex,
                row.get("direction"),
                row.get("capability") if capability else row.get("scenario"),
                row.get("requested_state") if capability else None,
            )
        ):
            continue
        if capability:
            if (
                row.get("capability") == result_key
                and row.get("operation_id") == result_key
                and row.get("direction")
                == ("write" if expected_verdict == "ok" else "observe")
            ):
                return True
        elif (
            row.get("scenario") == result_key
            and row.get("operation_id") == result_key
            and row.get("direction") in {"write", "notify"}
        ):
            return True
    return False


def _has_bound_bronze_probe(
    values: Any,
    *,
    route: Any,
    revision_id: str,
    firmware_version: Any,
    run_id: Any,
    captured_at: Any,
    session_id: Any,
    build_fingerprint: Any,
) -> bool:
    """Require a scoped status write, successful GATT completion, and response."""
    rows = _capture_rows(values)
    if rows is None:
        return False
    scope = {
        "route": route,
        "revision_id": revision_id,
        "firmware_version": firmware_version,
        "run_id": run_id,
        "captured_at": captured_at,
        "session_id": session_id,
        "build_fingerprint": build_fingerprint,
    }
    requests = [
        row
        for row in rows
        if all(row.get(key) == value for key, value in scope.items())
        and row.get("event") == "write_attempt"
        and row.get("direction") == "write"
        and isinstance(row.get("frame_hex"), str)
        and row["frame_hex"].lower() == STATUS_REQUEST.hex()
        and row.get("verdict") == "ok"
        and isinstance(row.get("capture_id"), str)
        and type(row.get("session_generation")) is int
        and type(row.get("seq")) is int
    ]
    for request in requests:
        write_ok = next(
            (
                row
                for row in rows
                if all(row.get(key) == value for key, value in scope.items())
                and row.get("capture_id") == request["capture_id"]
                and row.get("session_generation") == request["session_generation"]
                and type(row.get("seq")) is int
                and row["seq"] > request["seq"]
                and row.get("event") == "write_ok"
                and row.get("direction") == "write"
                and row.get("verdict") == "ok"
            ),
            None,
        )
        if write_ok is None:
            continue
        if any(
            all(row.get(key) == value for key, value in scope.items())
            and row.get("capture_id") == request["capture_id"]
            and row.get("session_generation") == request["session_generation"]
            and row.get("seq", 0) > write_ok["seq"]
            and row.get("event") == "status_data_decoded"
            and row.get("direction") == "notify"
            and row.get("verdict") == "ok"
            for row in rows
        ):
            return True
    return False


def _check_physical_review(
    item: dict[str, Any], status: str, context: str, errors: list[str]
) -> None:
    review = item.get("physical_review")
    if not isinstance(review, dict):
        _error(errors, f"{context} requires an independent physical_review record")
        return
    expected = "confirmed_pass" if status == "pass" else "confirmed_fail"
    reviewed_at = review.get("reviewed_at")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except AttributeError, ValueError:
        parsed = None
    digest_manifest = _evidence_digests(item.get("evidence"))
    if (
        review.get("outcome") != expected
        or not isinstance(review.get("reviewed_by"), str)
        or not review["reviewed_by"].strip()
        or parsed is None
        or parsed.tzinfo is None
        or digest_manifest is None
        or review.get("evidence_sha256") != digest_manifest
    ):
        _error(
            errors,
            f"{context} physical_review must independently confirm the result and bind its evidence digest",
        )


def _valid_reviewed_absence(item: dict[str, Any]) -> bool:
    reviewer = item.get("reviewed_by")
    reviewed_at = item.get("reviewed_at")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except AttributeError, ValueError:
        return False
    if not isinstance(reviewer, str) or not reviewer.strip() or parsed.tzinfo is None:
        return False
    digest_manifest = _evidence_digests(item.get("evidence"))
    return (
        digest_manifest is not None and item.get("evidence_sha256") == digest_manifest
    )


def _check_result_map(
    results: Any,
    expected: tuple[str, ...],
    context: str,
    errors: list[str],
    *,
    require_pass: bool,
    route: Any,
    revision_id: str,
    firmware_version: Any,
    run_id: Any,
    captured_at: Any,
    session_id: Any,
    build_fingerprint: Any,
    capability: bool = False,
    strict_keys: set[str] | None = None,
    tier_evidence: bool = False,
) -> None:
    if not isinstance(results, dict):
        _error(errors, f"{context} must be an object")
        return
    for key in expected:
        item = results.get(key)
        item_context = f"{context} {key}"
        if not isinstance(item, dict):
            _error(errors, f"{item_context} must be an object")
            continue
        status = item.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            _error(errors, f"{item_context} status must be one of {sorted(STATUSES)}")
        _check_evidence(
            item.get("evidence"),
            item_context,
            errors,
            required=status in {"pass", "fail"}
            or (require_pass and capability and status == "not_applicable"),
        )
        if status in {"pass", "fail"}:
            _check_physical_review(item, status, item_context, errors)
        if status == "not_applicable" and not (
            isinstance(item.get("rationale"), str) and item["rationale"].strip()
        ):
            _error(errors, f"{item_context} not_applicable status requires a rationale")
        if tier_evidence and status == "pass":
            item_scope = {
                "route": route,
                "revision_id": revision_id,
                "firmware_version": firmware_version,
                "run_id": run_id,
                "captured_at": captured_at,
                "session_id": session_id,
                "build_fingerprint": build_fingerprint,
            }
            if capability and key in {"ac_output", "dc_output", "light_output"}:
                _check_output_transitions(
                    item, item_context, key, errors, scope=item_scope
                )
            elif capability:
                _check_feature_restoration(
                    item, item_context, key, errors, scope=item_scope
                )
            elif key == "soak_24h" and not _valid_tier_soak(item, item_scope):
                _error(
                    errors,
                    f"{item_context} PASS requires a complete uninterrupted 24-hour soak artifact",
                )
        optional_vendor_app = not capability and key == "vendor_app_contention"
        key_is_strict = strict_keys is None or key in strict_keys
        if (
            require_pass
            and key_is_strict
            and (not optional_vendor_app or status == "pass")
        ):
            acceptable = (
                STRICT_CAPABILITY_STATUSES if capability else STRICT_SCENARIO_STATUSES
            )
            if status not in acceptable:
                expected = "pass or justified not_applicable" if capability else "pass"
                _error(
                    errors, f"{item_context} must be {expected} in require-pass mode"
                )
            elif status == "not_applicable" and capability:
                # Physical absence is reviewed through feature_presence evidence,
                # not a fabricated GATT operation for a control that does not exist.
                pass
            elif not _has_bound_capture(
                item.get("evidence"),
                route=route,
                revision_id=revision_id,
                firmware_version=firmware_version,
                run_id=run_id,
                captured_at=captured_at,
                session_id=session_id,
                build_fingerprint=build_fingerprint,
                result_key=key,
                capability=capability,
                expected_verdict="not_applicable"
                if status == "not_applicable"
                else "ok",
            ):
                _error(
                    errors,
                    f"{item_context} evidence must contain a route- and operation-bound capture row",
                )


def _validate_run(
    run: Any,
    context: str,
    errors: list[str],
    *,
    require_pass: bool,
    max_age_days: int | None,
    revision_id: str,
    revision_model: str,
    hardware_revision: str,
    protocol_profile_supported: bool,
    feature_scope: str | None = None,
) -> str | None:
    if not isinstance(run, dict):
        _error(errors, f"{context} must be an object")
        return None
    if require_pass and not protocol_profile_supported:
        _error(
            errors,
            f"{context} uses an unsupported protocol profile; strict PASS supports only documented R600/VOLIX P1800 HW 0.3 profiles",
        )
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id
    ):
        _error(errors, f"{context} run_id must be a stable opaque identifier")
    route = run.get("route")
    if not isinstance(route, str) or route not in RUN_ROUTES:
        _error(errors, f"{context} route must be one of {sorted(RUN_ROUTES)}")
    firmware = run.get("firmware_version")
    if firmware is not None and (not isinstance(firmware, str) or not firmware.strip()):
        _error(errors, f"{context} firmware_version must be a non-empty string or null")
    if (
        isinstance(route, str)
        and route in VERIFIED_ROUTES
        and firmware is None
        and require_pass
    ):
        _error(errors, f"{context} cannot qualify without a firmware_version")
    captured_at = run.get("captured_at")
    captured_date: date | None = None
    parsed_capture: datetime | None = None
    if captured_at is None:
        pass
    elif not isinstance(captured_at, str):
        _error(errors, f"{context} captured_at must be an ISO-8601 timestamp")
    else:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            parsed_capture = parsed.astimezone(UTC)
            captured_date = parsed_capture.date()
        except ValueError:
            _error(errors, f"{context} captured_at must include a timezone")
    if parsed_capture is not None and parsed_capture > datetime.now(UTC):
        _error(errors, f"{context} captured_at is in the future")
    session_id = run.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        _error(errors, f"{context} session_id must be an explicit non-empty value")
    build_fingerprint = run.get("build_fingerprint")
    if not isinstance(build_fingerprint, str) or not build_fingerprint.strip():
        _error(
            errors, f"{context} build_fingerprint must be an explicit non-empty value"
        )
    for field in ("tool_version", "integration_version"):
        value = run.get(field)
        if not isinstance(value, str) or not value.strip():
            _error(
                errors,
                f"{context} {field} must be a non-empty string (use 'unknown' if unavailable)",
            )
    build_verification = run.get("build_verification")
    if (
        not isinstance(build_verification, str)
        or build_verification not in BUILD_VERIFICATION
    ):
        _error(
            errors,
            f"{context} build_verification must be one of {sorted(BUILD_VERIFICATION)}",
        )
    completeness = run.get("capture_completeness")
    if not isinstance(completeness, str) or completeness not in CAPTURE_COMPLETENESS:
        _error(
            errors,
            f"{context} capture_completeness must be one of {sorted(CAPTURE_COMPLETENESS)}",
        )
    run_evidence = run.get("evidence")
    _check_evidence(run_evidence, context, errors)
    qualification = run.get("qualification_status")
    if not isinstance(qualification, str) or qualification not in STATUSES:
        _error(
            errors, f"{context} qualification_status must be one of {sorted(STATUSES)}"
        )
    if qualification == "not_applicable" and not (
        isinstance(run.get("rationale"), str) and run["rationale"].strip()
    ):
        _error(errors, f"{context} not_applicable status requires a rationale")
    if require_pass:
        if (
            (feature_scope is None and qualification != "pass")
            or route not in VERIFIED_ROUTES
            or firmware is None
            or completeness != "complete"
            or build_verification != "verified"
            or not run_evidence
            or run.get("authorization_context") != "production"
            or run.get("authorization_context_source") != "reviewed_production_session"
            or not isinstance(session_id, str)
            or session_id == "unknown"
            or not isinstance(build_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", build_fingerprint) is None
            or run.get("installed_fingerprint") != build_fingerprint
            or run.get("checkout_fingerprint") != build_fingerprint
        ):
            _error(
                errors,
                f"{context} lacks complete, attributed evidence for require-pass mode",
            )
        if captured_date is not None:
            age = (datetime.now(UTC).date() - captured_date).days
            if max_age_days is not None and age > max_age_days:
                _error(
                    errors, f"{context} evidence is stale ({age}d > {max_age_days}d)"
                )
    _check_result_map(
        run.get("scenarios"),
        REQUIRED_SCENARIOS,
        f"{context} scenario",
        errors,
        require_pass=require_pass,
        route=route,
        revision_id=revision_id,
        firmware_version=firmware,
        run_id=run_id,
        captured_at=captured_at,
        session_id=session_id,
        build_fingerprint=build_fingerprint,
        strict_keys=set() if feature_scope is not None else None,
        tier_evidence=isinstance(run.get("bronze_evidence"), dict),
    )
    _check_result_map(
        run.get("write_capabilities"),
        REQUIRED_WRITE_CAPABILITIES,
        f"{context} write capability",
        errors,
        require_pass=require_pass,
        route=route,
        revision_id=revision_id,
        firmware_version=firmware,
        run_id=run_id,
        captured_at=captured_at,
        session_id=session_id,
        build_fingerprint=build_fingerprint,
        capability=True,
        strict_keys={feature_scope} if feature_scope is not None else None,
        tier_evidence=isinstance(run.get("bronze_evidence"), dict),
    )
    if qualification == "pass":
        result_maps = (run.get("scenarios"), run.get("write_capabilities"))
        has_reviewable_pass = any(
            isinstance(results, dict)
            and any(
                isinstance(item, dict) and item.get("status") == "pass"
                for item in results.values()
            )
            for results in result_maps
        )
        if not has_reviewable_pass:
            _error(
                errors,
                f"{context} qualification_status pass requires at least one reviewed scenario or capability PASS",
            )
    presence = run.get("feature_presence")
    if not isinstance(presence, dict):
        _error(errors, f"{context} feature_presence must be an object")
    else:
        for feature in REQUIRED_WRITE_CAPABILITIES:
            item = presence.get(feature)
            if not isinstance(item, dict) or item.get("status") not in {
                "present",
                "absent",
                "unknown",
            }:
                _error(
                    errors,
                    f"{context} feature_presence {feature} status must be present, absent, or unknown",
                )
            elif item.get("status") == "absent":
                _check_evidence(
                    item.get("evidence"),
                    f"{context} feature_presence {feature}",
                    errors,
                    required=True,
                )
                if not _valid_reviewed_absence(item):
                    _error(
                        errors,
                        f"{context} absent feature {feature} requires reviewed evidence and reviewer",
                    )
                outcome = run.get("write_capabilities", {}).get(feature, {})
                if (
                    isinstance(outcome, dict)
                    and outcome.get("status") == "not_applicable"
                ):
                    presence_evidence = item.get("evidence", [])
                    result_evidence = outcome.get("evidence", [])
                    if not isinstance(result_evidence, list) or not set(
                        result_evidence
                    ).issubset(set(presence_evidence)):
                        _error(
                            errors,
                            f"{context} not_applicable {feature} must cite its reviewed absence evidence",
                        )
                if isinstance(outcome, dict) and outcome.get("status") == "pass":
                    _error(
                        errors,
                        f"{context} absent feature {feature} cannot have a pass outcome",
                    )
            elif isinstance(item, dict) and item.get("status") in {
                "present",
                "unknown",
            }:
                outcome = run.get("write_capabilities", {}).get(feature, {})
                if (
                    isinstance(outcome, dict)
                    and outcome.get("status") == "not_applicable"
                ):
                    _error(
                        errors,
                        f"{context} feature {feature} cannot be not_applicable unless reviewed absent",
                    )
                if (
                    item.get("status") == "unknown"
                    and isinstance(outcome, dict)
                    and outcome.get("status") == "pass"
                ):
                    _error(
                        errors,
                        f"{context} unknown feature {feature} cannot have a pass outcome",
                    )
    authorization_context = run.get("authorization_context")
    authorization_context_source = run.get("authorization_context_source")
    if authorization_context not in {
        "unknown",
        "read_only",
        "production",
        "experimental_hil",
    }:
        _error(
            errors,
            f"{context} authorization_context must be unknown, read_only, production, or experimental_hil",
        )
    if authorization_context == "experimental_hil" and (
        run.get("qualification_status") == "pass"
    ):
        _error(
            errors,
            f"{context} experimental_hil run cannot claim production qualification pass",
        )
    if require_pass and authorization_context != "production":
        _error(
            errors,
            f"{context} qualification requires explicit production authorization_context",
        )
    expected_context_source = {
        "read_only": "verified_read_only_session",
        "production": "reviewed_production_session",
        "experimental_hil": "experimental_hil_grant",
        "unknown": "archive_unverified",
    }.get(authorization_context)
    if authorization_context_source != expected_context_source:
        _error(
            errors,
            f"{context} authorization_context_source does not verify its authorization_context",
        )
    if authorization_context == "read_only" and any(
        isinstance(item, dict) and item.get("status") in {"pass", "fail"}
        for item in (
            run.get("write_capabilities", {}).values()
            if isinstance(run.get("write_capabilities"), dict)
            else []
        )
    ):
        _error(
            errors,
            f"{context} read_only authorization cannot contain write-capability verdicts",
        )
    if qualification == "pass" and (
        route not in VERIFIED_ROUTES
        or firmware is None
        or completeness != "complete"
        or build_verification != "verified"
        or not run_evidence
        or captured_date is None
    ):
        _error(
            errors,
            f"{context} cannot claim pass without complete, dated, firmware- and route-attributed evidence",
        )
    _check_bronze_evidence(
        run, context, revision_id, revision_model, hardware_revision, errors
    )
    return run_id if isinstance(run_id, str) else None


def _check_bronze_evidence(
    run: dict[str, Any],
    context: str,
    revision_id: str,
    revision_model: str,
    hardware_revision: str,
    errors: list[str],
) -> None:
    records = run.get("bronze_evidence")
    if records is None:
        return
    required = {"identity", "protocol_probe", "fresh_telemetry"}
    if not isinstance(records, dict) or set(records) != required:
        _error(
            errors,
            f"{context} bronze_evidence must define identity, protocol_probe, and fresh_telemetry",
        )
        return
    scope = {
        "revision_id": revision_id,
        "model": revision_model,
        "hardware_revision": hardware_revision,
        "firmware_version": run.get("firmware_version"),
        "route": run.get("route"),
        "run_id": run.get("run_id"),
        "session_id": run.get("session_id"),
        "build_fingerprint": run.get("build_fingerprint"),
        "captured_at": run.get("captured_at"),
    }
    for claim, item in records.items():
        claim_context = f"{context} bronze_evidence {claim}"
        if not isinstance(item, dict):
            _error(errors, f"{claim_context} must be an object")
            continue
        status = item.get("status")
        if status != "pass":
            if status not in STATUSES:
                _error(
                    errors, f"{claim_context} status must be one of {sorted(STATUSES)}"
                )
            continue
        _check_evidence(item.get("evidence"), claim_context, errors, required=True)
        _check_physical_review(item, status, claim_context, errors)
        paths = item.get("evidence")
        artifact_path = item.get("claim_artifact")
        if not isinstance(paths, list) or artifact_path not in paths:
            _error(
                errors,
                f"{claim_context} must cite its scoped claim_artifact in evidence",
            )
            continue
        try:
            artifact = json.loads((ROOT / artifact_path).read_text(encoding="utf-8"))
        except OSError, UnicodeError, ValueError, TypeError:
            _error(errors, f"{claim_context} claim_artifact must be valid JSON")
            continue
        if (
            not isinstance(artifact, dict)
            or artifact.get("artifact_type") != "bronze_evidence"
            or artifact.get("claim") != claim
            or any(artifact.get(key) != value for key, value in scope.items())
        ):
            _error(
                errors,
                f"{claim_context} claim_artifact must bind claim and exact run scope",
            )
            continue
        if claim == "identity" and artifact.get("identity_confirmed") is not True:
            _error(errors, f"{claim_context} must confirm model and revision identity")
        elif claim == "fresh_telemetry" and not (
            artifact.get("fresh") is True
            and artifact.get("plausible") is True
            and type(artifact.get("sample_age_seconds")) is int
            and 0 <= artifact["sample_age_seconds"] <= 60
        ):
            _error(errors, f"{claim_context} must establish fresh plausible telemetry")
        elif claim == "protocol_probe":
            capture_path = item.get("capture_evidence")
            if capture_path not in paths or not _has_bound_bronze_probe(
                [capture_path],
                route=run.get("route"),
                revision_id=revision_id,
                firmware_version=run.get("firmware_version"),
                run_id=run.get("run_id"),
                captured_at=run.get("captured_at"),
                session_id=run.get("session_id"),
                build_fingerprint=run.get("build_fingerprint"),
            ):
                _error(
                    errors,
                    f"{claim_context} requires a bound active protocol probe capture",
                )


def _check_feature_contract(
    revision: dict[str, Any], context: str, errors: list[str]
) -> None:
    contracts = revision.get("feature_contract")
    if contracts is None:
        return
    advanced = set(REQUIRED_WRITE_CAPABILITIES) - {
        "ac_output",
        "dc_output",
        "light_output",
    }
    if not isinstance(contracts, dict) or not set(contracts).issubset(advanced):
        _error(
            errors,
            f"{context} feature_contract may only exclude optional advanced features",
        )
        return
    for feature, item in contracts.items():
        item_context = f"{context} feature_contract {feature}"
        if not isinstance(item, dict) or (
            item.get("status") != "excluded"
            or item.get("safely_disabled") is not True
            or not isinstance(item.get("rationale"), str)
            or not item["rationale"].strip()
        ):
            _error(
                errors,
                f"{item_context} requires reviewed unavailable and safely-disabled declaration",
            )
            continue
        _check_evidence(item.get("evidence"), item_context, errors, required=True)
        review_subject = {
            "status": "pass",
            "evidence": item.get("evidence"),
            "physical_review": item.get("physical_review"),
        }
        _check_physical_review(review_subject, "pass", item_context, errors)
        paths = item.get("evidence")
        if not isinstance(paths, list) or len(paths) != 1:
            _error(errors, f"{item_context} must cite one contract artifact")
            continue
        try:
            artifact = json.loads((ROOT / paths[0]).read_text(encoding="utf-8"))
        except OSError, UnicodeError, ValueError, TypeError:
            _error(errors, f"{item_context} artifact must be valid JSON")
            continue
        if not isinstance(artifact, dict) or any(
            artifact.get(key) != value
            for key, value in {
                "artifact_type": "feature_contract",
                "revision_id": revision.get("id"),
                "model": revision.get("model"),
                "hardware_revision": revision.get("hardware_version"),
                "feature": feature,
                "safely_disabled": True,
                "rationale": item["rationale"],
            }.items()
        ):
            _error(
                errors,
                f"{item_context} artifact does not verify the declared exclusion",
            )


def _tier_artifact(
    path: Any, evidence: Any, artifact_type: str, scope: dict[str, Any]
) -> dict[str, Any] | None:
    if (
        not isinstance(path, str)
        or not isinstance(evidence, list)
        or path not in evidence
    ):
        return None
    target = (ROOT / path).resolve()
    try:
        target.relative_to(ROOT)
        value = json.loads(target.read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValueError:
        return None
    if not isinstance(value, dict) or value.get("artifact_type") != artifact_type:
        return None
    return (
        value
        if all(value.get(key) == expected for key, expected in scope.items())
        else None
    )


def _valid_tier_capture_row(
    row: Any,
    scope: dict[str, Any],
    feature: str,
    requested_state: Any,
    action_stage: str,
) -> bool:
    """Validate one physical write row, including its encoded target state."""
    if not isinstance(row, dict):
        return False
    return bool(
        all(row.get(key) == value for key, value in scope.items())
        and row.get("capability") == feature
        and row.get("operation_id") == feature
        and row.get("action_stage") == action_stage
        and type(row.get("requested_state")) is type(requested_state)
        and row.get("requested_state") == requested_state
        and row.get("direction") == "write"
        and row.get("verdict") == "ok"
        and row.get("physical_outcome") == "pass"
        and isinstance(row.get("frame_hex"), str)
        and _valid_protocol_frame(row["frame_hex"], "write", feature, requested_state)
    )


def _check_output_transitions(
    item: dict[str, Any],
    context: str,
    feature: str,
    errors: list[str],
    *,
    scope: dict[str, Any],
) -> None:
    review = item.get("physical_review")
    transitions = review.get("output_transitions") if isinstance(review, dict) else None
    if not isinstance(transitions, list) or len(transitions) != 2:
        _error(errors, f"{context} requires reviewed on and off transition evidence")
        return
    states: set[str] = set()
    for transition in transitions:
        if not isinstance(transition, dict) or transition.get("state") not in {
            "on",
            "off",
        }:
            _error(errors, f"{context} has an invalid output transition record")
            continue
        state = transition["state"]
        artifact = _tier_artifact(
            transition.get("evidence"), item.get("evidence"), "output_transition", scope
        )
        capture = transition.get("capture_evidence")
        rows = _capture_rows([capture]) if isinstance(capture, str) else None
        expected_request = state == "on"
        bound_capture = bool(
            isinstance(capture, str)
            and capture in item.get("evidence", [])
            and rows
            and _has_bound_capture(
                [capture],
                route=scope["route"],
                revision_id=scope["revision_id"],
                firmware_version=scope["firmware_version"],
                run_id=scope["run_id"],
                captured_at=scope["captured_at"],
                session_id=scope["session_id"],
                build_fingerprint=scope["build_fingerprint"],
                result_key=feature,
                capability=True,
                expected_verdict="ok",
            )
            and any(
                _valid_tier_capture_row(
                    row, scope, feature, expected_request, "transition"
                )
                for row in rows
            )
        )
        initial = "off" if state == "on" else "on"
        if (
            state in states
            or artifact is None
            or artifact.get("feature") != feature
            or artifact.get("requested_state") != state
            or artifact.get("observed_state") != state
            or artifact.get("initial_state") != initial
            or artifact.get("restored") is not True
            or artifact.get("restored_state") != initial
            or not bound_capture
            or not _valid_restoration_capture(
                transition.get("restoration_capture"),
                item,
                scope,
                feature,
                initial == "on",
            )
        ):
            _error(
                errors,
                f"{context} {state} transition lacks bound observation and restoration",
            )
        states.add(state)
    if states != {"on", "off"}:
        _error(errors, f"{context} requires both on and off transition evidence")


def _check_feature_restoration(
    item: dict[str, Any],
    context: str,
    feature: str,
    errors: list[str],
    *,
    scope: dict[str, Any],
) -> None:
    review = item.get("physical_review")
    path = review.get("restoration_evidence") if isinstance(review, dict) else None
    artifact = _tier_artifact(path, item.get("evidence"), "feature_restoration", scope)
    if not artifact or (
        artifact.get("feature") != feature
        or artifact.get("restored") is not True
        or artifact.get("pre_state") is None
        or artifact.get("restored_state") != artifact.get("pre_state")
        or not _valid_restoration_capture(
            review.get("restoration_capture") if isinstance(review, dict) else None,
            item,
            scope,
            feature,
            artifact.get("pre_state"),
        )
    ):
        _error(errors, f"{context} requires scope-bound safe-restoration evidence")


def _valid_restoration_capture(
    capture: Any,
    item: dict[str, Any],
    scope: dict[str, Any],
    feature: str,
    restored_state: Any,
) -> bool:
    if not isinstance(capture, str) or capture not in item.get("evidence", []):
        return False
    rows = _capture_rows([capture])
    return bool(
        rows
        and _has_bound_capture(
            [capture],
            route=scope["route"],
            revision_id=scope["revision_id"],
            firmware_version=scope["firmware_version"],
            run_id=scope["run_id"],
            captured_at=scope["captured_at"],
            session_id=scope["session_id"],
            build_fingerprint=scope["build_fingerprint"],
            result_key=feature,
            capability=True,
            expected_verdict="ok",
        )
        and any(
            _valid_tier_capture_row(row, scope, feature, restored_state, "restoration")
            for row in rows
        )
    )


def _valid_tier_soak(item: dict[str, Any], scope: dict[str, Any]) -> bool:
    review = item.get("physical_review")
    path = review.get("soak_artifact") if isinstance(review, dict) else None
    if not isinstance(path, str) or path not in item.get("evidence", []):
        return False
    target = (ROOT / path).resolve()
    try:
        target.relative_to(ROOT)
        rows = [
            json.loads(line)
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError, UnicodeError, ValueError, TypeError:
        return False
    if not rows or any(not isinstance(row, dict) for row in rows):
        return False
    summaries = [row for row in rows if row.get("event") == "segment_summary"]
    if len(summaries) != 1:
        return False
    summary = summaries[0]
    if not isinstance(summary, dict) or any(
        summary.get(key) != value for key, value in scope.items()
    ):
        return False
    samples = [row for row in rows if row.get("event") == "sample"]
    traces = [row for row in rows if row.get("event") == "trace_window"]
    try:
        started = datetime.fromisoformat(
            summary["segment_started_utc"].replace("Z", "+00:00")
        )
        ended = datetime.fromisoformat(
            summary["segment_ended_utc"].replace("Z", "+00:00")
        )
    except KeyError, AttributeError, ValueError:
        return False
    elapsed = [
        row.get("elapsed_seconds")
        for row in samples
        if type(row.get("elapsed_seconds")) in (int, float)
        and math.isfinite(row.get("elapsed_seconds"))
    ]
    elapsed.sort()
    gap = summary.get("allowed_gap_seconds")
    max_gap = summary.get("maximum_gap_seconds")
    measured = summary.get("measured_seconds")
    trace_times: list[datetime] = []
    for row in traces:
        try:
            trace_time = datetime.fromisoformat(
                row.get("utc", "").replace("Z", "+00:00")
            )
        except AttributeError, ValueError:
            return False
        if trace_time.tzinfo is None:
            return False
        trace_times.append(trace_time.astimezone(UTC))
    trace_times.sort()
    return bool(
        started.tzinfo is not None
        and ended.tzinfo is not None
        and type(gap) in (int, float)
        and 0 < gap <= 600
        and type(max_gap) in (int, float)
        and math.isfinite(max_gap)
        and max_gap <= gap
        and type(measured) in (int, float)
        and math.isfinite(measured)
        and measured >= 86400
        and len(samples) >= 289
        and type(summary.get("sample_count")) is int
        and len(samples) == summary.get("sample_count")
        and len(elapsed) == len(samples)
        and elapsed[0] <= 1
        and elapsed[-1] >= 86400
        and all(b - a <= gap for a, b in zip(elapsed, elapsed[1:], strict=False))
        and all(
            row.get("status_fresh") is True
            and isinstance(row.get("telemetry"), dict)
            and any(
                isinstance(value, dict) and value.get("verified") is True
                for value in row["telemetry"].values()
            )
            for row in samples
        )
        and len(traces) >= 24
        and type(summary.get("trace_count")) is int
        and len(traces) == summary.get("trace_count")
        and type(summary.get("minimum_samples")) is int
        and summary["minimum_samples"] >= 289
        and type(summary.get("minimum_traces")) is int
        and summary["minimum_traces"] >= 24
        and all(
            row.get("saved") is True
            and row.get("complete") is True
            and row.get("capture_id_present") is True
            and row.get("observed_route") == scope["route"]
            for row in traces
        )
        and len(trace_times) == len(traces)
        and trace_times[0] >= started.astimezone(UTC)
        and trace_times[-1] <= ended.astimezone(UTC)
        and trace_times[0] - started.astimezone(UTC) <= timedelta(seconds=4500)
        and ended.astimezone(UTC) - trace_times[-1] <= timedelta(seconds=4500)
        and all(
            b - a <= timedelta(seconds=4500)
            for a, b in zip(trace_times, trace_times[1:], strict=False)
        )
        and summary.get("outcome") == "pass"
        and summary.get("resumed") is False
        and summary.get("interrupted") is False
        and summary.get("uninterrupted") is True
        and ended.astimezone(UTC) - started.astimezone(UTC) >= timedelta(seconds=86400)
        and ended.astimezone(UTC) - started.astimezone(UTC) >= timedelta(seconds=86400)
        and type(summary.get("dropped_traces")) is int
        and summary["dropped_traces"] == 0
        and type(summary.get("clock_discontinuities")) is int
        and summary["clock_discontinuities"] == 0
        and not any(
            row.get("event")
            in {"sample_gap", "clock_discontinuity", "session_generation_change"}
            for row in rows
        )
    )


def _strict_candidate(run: Any) -> tuple[str, str, datetime] | None:
    """Return only complete, build-verified physical outcomes eligible for strict gating."""
    if not isinstance(run, dict):
        return None
    firmware = run.get("firmware_version")
    route = run.get("route")
    captured_at = run.get("captured_at")
    if (
        not isinstance(firmware, str)
        or not firmware.strip()
        or route not in VERIFIED_ROUTES
        or run.get("capture_completeness") != "complete"
        or run.get("build_verification") != "verified"
        or run.get("authorization_context") != "production"
        or run.get("authorization_context_source") != "reviewed_production_session"
        or not isinstance(run.get("evidence"), list)
        or not run["evidence"]
        or not isinstance(run.get("session_id"), str)
        or not run["session_id"].strip()
        or run["session_id"] == "unknown"
        or not isinstance(run.get("build_fingerprint"), str)
        or re.fullmatch(r"[0-9a-f]{64}", run["build_fingerprint"]) is None
        or run.get("installed_fingerprint") != run.get("build_fingerprint")
        or run.get("checkout_fingerprint") != run.get("build_fingerprint")
        or not isinstance(captured_at, str)
    ):
        return None
    try:
        parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return firmware, route, parsed.astimezone(UTC)


def _supports_documented_protocol(revision: dict[str, Any]) -> bool:
    return (
        revision.get("model"),
        revision.get("id"),
        revision.get("hardware_version"),
        revision.get("raw_hardware_version"),
    ) in SUPPORTED_PROTOCOL_PROFILES


def validate_matrix(
    payload: dict[str, Any],
    *,
    matrix_path: Path,
    require_pass: bool,
    max_age_days: int | None,
    route_scope: str | None = None,
    revision_scope: str | None = None,
    firmware_scope: str | None = None,
    feature_scope: str | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    summaries: list[str] = []
    if payload.get("schema_version") != MATRIX_SCHEMA_VERSION:
        _error(errors, f"schema_version must be {MATRIX_SCHEMA_VERSION}")
    _check_date(payload.get("generated_at"), "generated_at", errors)
    if MAC_ADDRESS.search(matrix_path.read_text(encoding="utf-8")):
        _error(errors, "matrix contains a Bluetooth MAC address")
    revisions = payload.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        _error(errors, "revisions must be a non-empty list")
        return errors, summaries
    seen_revisions: set[str] = set()
    seen_runs: set[str] = set()
    for index, revision in enumerate(revisions, 1):
        if not isinstance(revision, dict):
            _error(errors, f"revisions[{index}] must be an object")
            continue
        revision_id = revision.get("id")
        if not isinstance(revision_id, str) or not revision_id:
            _error(errors, f"revisions[{index}] id must be a non-empty string")
            continue
        if revision_id in seen_revisions:
            _error(errors, f"duplicate revision id: {revision_id}")
        seen_revisions.add(revision_id)
        protocol_profile_supported = _supports_documented_protocol(revision)
        for field in ("model", "hardware_version", "raw_hardware_version"):
            if not isinstance(revision.get(field), str) or not revision[field].strip():
                _error(
                    errors, f"revision {revision_id} {field} must be a non-empty string"
                )
        if revision.get("privacy_review") != "pass":
            _error(errors, f"revision {revision_id} privacy_review must be pass")
        _check_feature_contract(revision, f"revision {revision_id}", errors)
        runs = revision.get("runs")
        if not isinstance(runs, list):
            _error(errors, f"revision {revision_id} runs must be a list")
            continue
        legacy_v2_runs = revision.get("legacy_v2_runs", [])
        if not isinstance(legacy_v2_runs, list):
            _error(errors, f"revision {revision_id} legacy_v2_runs must be a list")
            legacy_v2_runs = []
        for legacy_run in legacy_v2_runs:
            for value in _strings(legacy_run):
                if MAC_ADDRESS.search(value):
                    _error(
                        errors,
                        f"revision {revision_id} legacy v2 history contains a Bluetooth MAC address",
                    )
        strict_candidates: dict[tuple[str, str], list[tuple[datetime, Any]]] = {}
        reported_firmwares: set[str] = set()
        for run_index, run in enumerate(runs, 1):
            run_id = _validate_run(
                run,
                f"revision {revision_id} run[{run_index}]",
                errors,
                require_pass=False,
                max_age_days=max_age_days,
                revision_id=revision_id,
                revision_model=revision.get("model", ""),
                hardware_revision=revision.get("hardware_version", ""),
                protocol_profile_supported=protocol_profile_supported,
            )
            if run_id is not None:
                if run_id in seen_runs:
                    _error(errors, f"duplicate run_id: {run_id}")
                seen_runs.add(run_id)
            # Any explicitly reported firmware is in the qualification scope,
            # even when its run is incomplete or inconclusive. Eligibility for
            # a route remains a separate decision below.
            if isinstance(run, dict):
                firmware = run.get("firmware_version")
                if isinstance(firmware, str) and firmware.strip():
                    reported_firmwares.add(firmware)
            candidate = _strict_candidate(run)
            if candidate is not None:
                firmware, route, captured_at = candidate
                strict_candidates.setdefault((firmware, route), []).append(
                    (captured_at, run)
                )
        legacy = revision.get("legacy_results")
        if not isinstance(legacy, list):
            _error(errors, f"revision {revision_id} legacy_results must be a list")
            continue
        for legacy_index, item in enumerate(legacy, 1):
            context = f"revision {revision_id} legacy_results[{legacy_index}]"
            if (
                not isinstance(item, dict)
                or item.get("scope") != "unscoped_legacy"
                or type(item.get("source_schema_version")) is not int
                or item.get("source_schema_version") != 1
                or not isinstance(item.get("source"), dict)
            ):
                _error(
                    errors, f"{context} must preserve a v1 source as unscoped_legacy"
                )
                continue
            source = item["source"]
            if source.get("id") != revision_id:
                _error(errors, f"{context} source revision id does not match")
            normalized = item.get("normalized_outcomes")
            if not isinstance(normalized, dict) or set(normalized) != {
                "routes",
                "scenarios",
                "write_capabilities",
            }:
                _error(
                    errors,
                    f"{context} normalized_outcomes must preserve v1 result groups",
                )
            else:
                for result_group in normalized.values():
                    if not isinstance(result_group, dict):
                        _error(
                            errors,
                            f"{context} normalized result groups must be objects",
                        )
                        continue
                    for status in result_group.values():
                        if not isinstance(status, str) or status not in STATUSES:
                            _error(
                                errors,
                                f"{context} has an invalid normalized legacy status",
                            )
            for value in _strings(source):
                if MAC_ADDRESS.search(value):
                    _error(errors, f"{context} contains a Bluetooth MAC address")
        if require_pass and revision_scope in {None, revision_id}:
            if route_scope is not None and route_scope not in VERIFIED_ROUTES:
                _error(errors, f"invalid strict route scope: {route_scope}")
            if feature_scope is not None:
                if feature_scope not in REQUIRED_WRITE_CAPABILITIES:
                    _error(errors, f"invalid feature scope: {feature_scope}")
                if firmware_scope not in reported_firmwares:
                    _error(
                        errors,
                        f"revision {revision_id} has no run for firmware {firmware_scope}",
                    )
                    scoped_firmwares: list[str] = []
                else:
                    scoped_firmwares = [firmware_scope]
                for firmware in scoped_firmwares:
                    scoped_routes = (
                        {route_scope}
                        if route_scope
                        else {
                            route
                            for candidate_firmware, route in strict_candidates
                            if candidate_firmware == firmware
                        }
                    )
                    if not scoped_routes:
                        _error(
                            errors,
                            f"revision {revision_id} firmware {firmware} has no eligible route for {feature_scope}",
                        )
                    for route in sorted(scoped_routes):
                        candidates = [
                            (captured_at, run)
                            for captured_at, run in strict_candidates.get(
                                (firmware, route), []
                            )
                            if isinstance(run.get("write_capabilities"), dict)
                            and isinstance(
                                run["write_capabilities"].get(feature_scope), dict
                            )
                            and run["write_capabilities"][feature_scope].get("status")
                            in {"pass", "fail", "not_applicable"}
                        ]
                        if not candidates:
                            _error(
                                errors,
                                f"revision {revision_id} firmware {firmware} route {route} has no reviewed {feature_scope} outcome",
                            )
                            continue
                        newest_at = max(captured_at for captured_at, _ in candidates)
                        newest = [
                            run
                            for captured_at, run in candidates
                            if captured_at == newest_at
                        ]
                        if len(newest) != 1:
                            _error(
                                errors,
                                f"revision {revision_id} firmware {firmware} route {route} has ambiguous latest {feature_scope} outcomes",
                            )
                            continue
                        _validate_run(
                            newest[0],
                            f"revision {revision_id} firmware {firmware} route {route} feature {feature_scope}",
                            errors,
                            require_pass=True,
                            max_age_days=max_age_days,
                            revision_id=revision_id,
                            revision_model=revision.get("model", ""),
                            hardware_revision=revision.get("hardware_version", ""),
                            protocol_profile_supported=protocol_profile_supported,
                            feature_scope=feature_scope,
                        )
            else:
                if not strict_candidates:
                    _error(
                        errors,
                        f"revision {revision_id} has no eligible scoped runs for require-pass mode",
                    )
                for firmware in sorted(reported_firmwares):
                    scoped_routes = (
                        {route_scope}
                        if route_scope
                        else {
                            route
                            for candidate_firmware, route in strict_candidates
                            if candidate_firmware == firmware
                        }
                    )
                    if not scoped_routes:
                        _error(
                            errors,
                            f"revision {revision_id} firmware {firmware} has no eligible observed route",
                        )
                    for route in sorted(scoped_routes):
                        candidates = [
                            (captured_at, run)
                            for captured_at, run in strict_candidates.get(
                                (firmware, route), []
                            )
                            if run.get("qualification_status") in {"pass", "fail"}
                        ]
                        if not candidates:
                            _error(
                                errors,
                                f"revision {revision_id} firmware {firmware} missing strict route: {route}",
                            )
                            continue
                        newest_at = max(captured_at for captured_at, _ in candidates)
                        newest = [
                            run
                            for captured_at, run in candidates
                            if captured_at == newest_at
                        ]
                        if len(newest) != 1:
                            _error(
                                errors,
                                f"revision {revision_id} firmware {firmware} route {route} has ambiguous latest runs at {newest_at.isoformat()}",
                            )
                            continue
                        run = newest[0]
                        run_id = run.get("run_id", "unknown")
                        _validate_run(
                            run,
                            f"revision {revision_id} current run {run_id}",
                            errors,
                            require_pass=True,
                            max_age_days=max_age_days,
                            revision_id=revision_id,
                            revision_model=revision.get("model", ""),
                            hardware_revision=revision.get("hardware_version", ""),
                            protocol_profile_supported=protocol_profile_supported,
                        )
        summaries.append(
            f"{revision_id}: {len(runs)} scoped run(s), "
            f"{len(legacy)} v1 and {len(legacy_v2_runs)} v2 unqualified history record(s)"
        )
    migration = payload.get("migration")
    if revision_scope is not None and revision_scope not in seen_revisions:
        _error(errors, f"strict revision scope not found: {revision_scope}")
    if migration is not None and (
        not isinstance(migration, dict)
        or migration.get("from_schema_version") not in {1, 2}
    ):
        _error(
            errors,
            "migration metadata, when present, must identify a v1 or v2 source matrix",
        )
    return errors, summaries


def main() -> int:
    args = parse_args()
    matrix_path = Path(args.matrix).resolve()
    if not matrix_path.is_file():
        print(f"Matrix file not found: {matrix_path}", file=sys.stderr)
        return 1
    try:
        payload = read_json(matrix_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load matrix file: {exc}", file=sys.stderr)
        return 1
    errors, summaries = validate_matrix(
        payload,
        matrix_path=matrix_path,
        require_pass=args.require_pass,
        max_age_days=args.max_age_days,
        route_scope=args.route,
        revision_scope=args.revision,
        firmware_scope=args.firmware,
        feature_scope=args.feature,
    )
    if errors:
        print("HIL qualification validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("HIL qualification validation passed")
    for summary in summaries:
        print(f"- {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
