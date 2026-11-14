"""Fail-closed public projection and verification for private HIL runs."""

from __future__ import annotations

from datetime import datetime
import hashlib
import ipaddress
import math
import json
import os
from pathlib import Path
import re
import secrets
import stat
import zipfile
from typing import Any

from .api import HAAPIError
from .scenarios import load_scenarios
from .storage import load_scenario_checkpoint
from .discovery import BINARY_KEYS, KNOWN_MODELS, SENSOR_KEYS, safe_model, safe_version
from .trace import _validate_trace_result

PUBLIC_SCHEMA = 2
MAX_FILE = 2 * 1024 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_MEMBERS = 16
BRONZE_STATUS_REQUEST = "a565b1000106010000000000"
PUBLIC_NAMES = {
    "environment.json",
    "session.json",
    "scenarios.json",
    "events.jsonl",
    "frames.jsonl",
    "manifest.json",
    "report.md",
    "tier-execution.json",
    "tier-artifacts.json",
    "tier-bronze-read-only.json",
}


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON object key: {key}")
        value[key] = item
    return value


def _load_public_json(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)


OUTCOMES = {"pass", "fail", "inconclusive", "skipped", "not_applicable"}
EVENT_NAMES = {
    "capture_started",
    "connect_attempt",
    "connect_succeeded",
    "connect_failed",
    "disconnect",
    "notification_received",
    "notification_decoded",
    "write_attempt",
    "write_ok",
    "write_failed",
    "route_observed",
    "capture_stopped",
    "probe_started",
    "probe_succeeded",
    "probe_failed",
    "status_request",
    "connected",
    "disconnected",
    "transaction_inherited",
    "transaction_accepted",
    "transaction_submitted",
    "transaction_failed",
    "transaction_confirmed",
    "transaction_rejected",
    "transaction_timed_out",
    "transaction_abandoned",
    "control_rejected",
    "model_profile_selected",
    "probe_armed",
    "probe_status_received",
    "probe_settings_received",
    "probe_gatt_rejected",
    "probe_gatt_validated",
    "probe_notify_failed",
    "probe_notify_started",
    "write_error",
    "write_rejected",
    "write_unexpected_error",
    "probe_status_timeout",
    "probe_notify_stopped",
    "probe_notify_stop_failed",
    "probe_disconnect_failed",
    "client_stopped",
}
PUBLIC_EVENT_NAMES = EVENT_NAMES | {"status_data_decoded"}
DECLARATIONS = load_scenarios()
SCENARIO_IDS = {scenario.id for scenario in DECLARATIONS}
DECLARATION_BY_ID = {scenario.id: scenario for scenario in DECLARATIONS}
STEP_IDS = {step["id"] for scenario in DECLARATIONS for step in scenario.steps}
STEP_IDS.update(
    {
        "settings_preflight",
        "output_preflight",
        "restore_original",
        "baseline",
        "operator_action",
        "recovered_session",
        "resume",
        "interrupted",
        "soak_segment",
    }
)
TIER_STEP_IDS = {
    "read_only_probe",
    "physical_plausibility_observation",
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
}
REQUESTED_ROUTES = {"auto", "local_adapter", "active_proxy"}
PREFLIGHT_CHECKS = {"entry_loaded", "connected", "telemetry_fresh"}
PREFLIGHT_RESULTS = {"ready", "incomplete", "unknown"}
RUNTIME_CAPABILITIES = {
    "read_telemetry",
    "write_output_controls",
    "write_settings_controls",
    "write_settings_keepalive",
    "car_charger_enabled",
}


def _safe_read_only_value(key: str, value: Any) -> int | float | str | None:
    if value is None:
        return None
    if key in BINARY_KEYS:
        return value if isinstance(value, str) and value in {"on", "off"} else None
    if key in {"hardware_version", "firmware_version"}:
        return safe_version(value)
    if key in SENSOR_KEYS and type(value) in (int, float):
        if (
            math.isfinite(value)
            and abs(value) <= 99_999_999
            and re.fullmatch(r"-?\d{1,8}(?:\.\d{1,5})?", str(value))
        ):
            return value
    return None


def _public_read_only_telemetry(source: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(source, dict):
        return {}
    result = {}
    for key in SENSOR_KEYS + BINARY_KEYS:
        record = source.get(key)
        if not isinstance(record, dict):
            continue
        value = _safe_read_only_value(key, record.get("value"))
        verified = record.get("verified") is True and value is not None
        result[key] = {
            "value": value if verified else None,
            "verified": verified,
        }
    return result


def _public_observation(scenario_id: str, step: dict[str, Any]) -> dict[str, Any]:
    """Project only typed values and declaration-backed expectations."""
    declaration = DECLARATION_BY_ID.get(scenario_id)
    step_id = step.get("step_id")
    declared_step_id = step_id
    if step_id == "soak_segment":
        if scenario_id == "soak_24h_observation":
            declared_step_id = "periodic_observation"
        elif scenario_id == "short_stability_observation":
            declared_step_id = "short_observation"
    declared_step = (
        next(
            (item for item in declaration.steps if item.get("id") == declared_step_id),
            None,
        )
        if declaration is not None
        else None
    )
    allowed_values = {
        "on",
        "off",
        "two_hours",
        "standard",
        "mute",
        "performance",
        "sleep",
        "auto",
        "unknown",
        "unavailable",
        "yes",
        "no",
        "unable_to_observe",
        "could_not_connect",
        "unable_or_unwilling",
        "connected",
        "ha_stable",
        "ha_recovered",
        "inconclusive",
    }
    if declared_step is not None and isinstance(declared_step.get("expected"), str):
        expected = declared_step["expected"]
        if re.fullmatch(r"[a-z][a-z0-9_ -]{0,80}", expected):
            allowed_values.add(expected)

    def state_value(name: str) -> str | None:
        value = step.get(name)
        return value if isinstance(value, str) and value in allowed_values else None

    observation: dict[str, Any] = {}
    for name in ("pre_state", "post_state", "cleanup_post_state"):
        value = state_value(name)
        if value is not None:
            observation[name] = value
    for name in (
        "connected",
        "status_fresh",
        "disconnect_observed",
        "operator_confirmed",
        "route_verified",
        "trace_complete",
        "fresh_status_telemetry",
        "station_cutoff_observed",
        "restart_discontinuity",
        "data_limit_hit",
    ):
        if type(step.get(name)) is bool:
            observation[name] = step[name]
    for name in (
        "session_generation",
        "session_generation_before",
        "session_generation_after",
        "segment",
        "sample_count",
        "trace_count",
        "dropped_traces",
        "clock_discontinuities",
        "fresh_status_sample_count",
    ):
        value = step.get(name)
        if type(value) is int and 0 <= value <= 1_000_000:
            observation[name] = value
    for name in ("measured_seconds", "maximum_gap_seconds"):
        value = step.get(name)
        if (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0 <= value <= 86_400
        ):
            observation[name] = value
    route = step.get("route")
    if route in {"local_adapter", "active_proxy", "unverified", "mixed"}:
        observation["route"] = route
    for source, target in (
        ("station_model", "station_model"),
        ("station_hardware_revision", "station_hardware_revision"),
        ("station_firmware_revision", "station_firmware_revision"),
    ):
        value = step.get(source)
        if source == "station_model":
            safe = safe_model(value)
        else:
            safe = safe_version(value)
        if safe is not None:
            observation[target] = safe
    operator_mode = step.get("operator_mode")
    if operator_mode in {"interactive", "non_interactive"}:
        observation["operator_mode"] = operator_mode
    action = step.get("action")
    if action in {
        "route interruption and restoration",
        "vendor-app contention",
        "Home Assistant or integration reload/upgrade",
    }:
        observation["operator_action"] = action
    app_result = state_value("operator_app_result")
    if app_result is not None:
        observation["operator_app_result"] = app_result
    ha_observation = state_value("ha_observation")
    if ha_observation is not None:
        observation["ha_observation"] = ha_observation

    target = declared_step.get("target") if declared_step is not None else None
    expected = declared_step.get("expected") if declared_step is not None else None
    executed_pre_state = step.get("pre_state")
    if executed_pre_state is None and isinstance(step.get("observation"), dict):
        executed_pre_state = step["observation"].get("pre_state")
    if (
        scenario_id == "settings_eco_mode"
        and step_id == "eco_mode_on"
        and expected == "on"
        and step.get("expected") == "off"
        and executed_pre_state == "on"
    ):
        # The only alternate ECO action is the declared, reversible off edge
        # when the original value was already on. Never publish arbitrary
        # private requested values as public expectations.
        expected = "off"
    if not isinstance(target, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", target):
        target = None
    if not isinstance(expected, str) or not 0 < len(expected) <= 128:
        expected = None
    actual = state_value("post_state")
    if actual is None and step.get("step_id") == "recovered_session":
        if (
            step.get("connected") is True
            and type(step.get("session_generation_after")) is int
        ):
            actual = state_value("ha_observation") or "reconnected"
        elif step.get("trace_complete") is False:
            actual = "observation_incomplete"
    if actual is None and step.get("step_id") == "soak_segment":
        outcome = step.get("outcome")
        if scenario_id == "soak_24h_observation":
            actual = "completed" if outcome == "pass" else "inconclusive"
        else:
            actual = outcome if outcome in {"pass", "fail"} else "inconclusive"
    reason = step.get("reason")
    limitation = None
    if isinstance(reason, str) and step.get("outcome") not in {"pass", "fail"}:
        lowered = reason.lower()
        if "restart" in lowered or "unobserved interval" in lowered:
            limitation = "restart_discontinuity"
        elif "interrupt" in lowered or "declined" in lowered or "confirm" in lowered:
            limitation = "operator_interruption_or_decline"
        elif "storage" in lowered or "byte cap" in lowered:
            limitation = "storage_limit"
        elif "trace" in lowered or "capture" in lowered:
            limitation = "trace_unavailable"
        elif "fresh" in lowered or "status" in lowered or "telemetry" in lowered:
            limitation = "observation_unavailable"
        else:
            limitation = "precondition_or_observation_incomplete"
    return {
        "target": target,
        "expected": expected,
        "actual": actual,
        "observation": observation,
        "limitation_reason": limitation,
    }


def _valid_public_observation(value: Any, *, nested: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    allowed = {
        "pre_state",
        "post_state",
        "cleanup_post_state",
        "connected",
        "status_fresh",
        "disconnect_observed",
        "operator_confirmed",
        "route_verified",
        "trace_complete",
        "fresh_status_telemetry",
        "station_cutoff_observed",
        "restart_discontinuity",
        "data_limit_hit",
        "session_generation",
        "session_generation_before",
        "session_generation_after",
        "segment",
        "sample_count",
        "trace_count",
        "dropped_traces",
        "clock_discontinuities",
        "fresh_status_sample_count",
        "measured_seconds",
        "maximum_gap_seconds",
        "route",
        "station_model",
        "station_hardware_revision",
        "station_firmware_revision",
        "operator_mode",
        "operator_action",
        "target",
        "expected",
        "actual",
        "observation",
        "limitation_reason",
        "operator_app_result",
        "ha_observation",
    }
    state_values = {
        "on",
        "off",
        "two_hours",
        "standard",
        "mute",
        "performance",
        "sleep",
        "auto",
        "unknown",
        "unavailable",
        "yes",
        "no",
        "unable_to_observe",
        "could_not_connect",
        "connected",
        "unable_or_unwilling",
        "ha_stable",
        "ha_recovered",
    }
    envelope_fields = {
        "target",
        "expected",
        "actual",
        "observation",
        "limitation_reason",
    }
    if set(value) - (allowed - envelope_fields if nested else allowed):
        return False
    if (
        "target" in value
        and value["target"] is not None
        and (
            not isinstance(value["target"], str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value["target"]) is None
        )
    ):
        return False
    if (
        "expected" in value
        and value["expected"] is not None
        and (
            not isinstance(value["expected"], str)
            or re.fullmatch(r"[a-z][a-z0-9_ -]{0,127}", value["expected"]) is None
        )
    ):
        return False
    if "actual" in value and value["actual"] not in state_values | {
        None,
        "reconnected",
        "observation_incomplete",
        "completed",
        "inconclusive",
    }:
        return False
    if "limitation_reason" in value and value["limitation_reason"] not in {
        None,
        "restart_discontinuity",
        "operator_interruption_or_decline",
        "storage_limit",
        "trace_unavailable",
        "observation_unavailable",
        "precondition_or_observation_incomplete",
    }:
        return False
    if "observation" in value and not _valid_public_observation(
        value["observation"], nested=True
    ):
        return False
    for key in {"pre_state", "post_state", "cleanup_post_state"} & set(value):
        if value[key] not in state_values:
            return False
    for key in {
        "connected",
        "status_fresh",
        "disconnect_observed",
        "operator_confirmed",
        "route_verified",
        "trace_complete",
        "fresh_status_telemetry",
        "restart_discontinuity",
        "data_limit_hit",
    } & set(value):
        if type(value[key]) is not bool:
            return False
    for key in {
        "session_generation",
        "session_generation_before",
        "session_generation_after",
        "segment",
        "sample_count",
        "trace_count",
        "dropped_traces",
        "clock_discontinuities",
    } & set(value):
        if type(value[key]) is not int or not 0 <= value[key] <= 1_000_000:
            return False
    for key in {"measured_seconds", "maximum_gap_seconds"} & set(value):
        if (
            type(value[key]) not in (int, float)
            or not math.isfinite(value[key])
            or not 0 <= value[key] <= 86_400
        ):
            return False
    if "route" in value and value["route"] not in {
        "local_adapter",
        "active_proxy",
        "unverified",
        "mixed",
    }:
        return False
    for key in {"route_verified", "trace_complete"} & set(value):
        if type(value[key]) is not bool:
            return False
    if (
        "station_model" in value
        and safe_model(value["station_model"]) != value["station_model"]
    ):
        return False
    for key in {"station_hardware_revision", "station_firmware_revision"} & set(value):
        if safe_version(value[key]) != value[key]:
            return False
    if "operator_mode" in value and value["operator_mode"] not in {
        "interactive",
        "non_interactive",
    }:
        return False
    if "operator_action" in value and value["operator_action"] not in {
        "route interruption and restoration",
        "vendor-app contention",
        "Home Assistant or integration reload/upgrade",
    }:
        return False
    if "operator_app_result" in value and value["operator_app_result"] not in {
        "could_not_connect",
        "connected",
        "unable_or_unwilling",
    }:
        return False
    if "ha_observation" in value and value["ha_observation"] not in {
        "ha_stable",
        "ha_recovered",
        "inconclusive",
    }:
        return False
    return True


def _safe_fact_value(key: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if re.search(
        r"(?i)(?:token|secret|password|credential|bearer|ssid|hostname)", value
    ):
        return False
    if re.search(r"(?i)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", value):
        return False
    if re.search(r"(?i)(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])", value):
        return False
    if re.search(
        r"(?i)(?<![a-z0-9_-])(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+[a-z]{2,63}(?![a-z0-9_-])",
        value,
    ):
        return False
    for match in re.finditer(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", value):
        try:
            ipaddress.ip_address(match.group())
        except ValueError:
            continue
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return False
    if key == "cpu":
        return (
            re.fullmatch(r"(?:Intel|AMD|Apple|ARM)[A-Za-z0-9_.+ -]{0,50}", value)
            is not None
        )
    if key == "architecture":
        return value in {
            "x86_64",
            "AMD64",
            "aarch64",
            "arm64",
            "armv7l",
            "i386",
            "i686",
        }
    if key == "os":
        return value in {"Linux", "Darwin", "Windows", "FreeBSD"}
    if key == "installation_type":
        return value in {
            "Home Assistant OS",
            "Home Assistant Container",
            "Home Assistant Supervised",
            "Home Assistant Core",
        }
    if key == "build_match":
        return value in {"verified", "mismatch", "unverified"}
    if key in {"version", "installed_manifest_version", "python_version", "kernel"}:
        return re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,31}", value) is not None
    if key in {"runner_checkout_commit", "installed_commit"}:
        return re.fullmatch(r"[a-f0-9]{40}", value) is not None
    if key in {"installed_fingerprint", "checkout_fingerprint"}:
        return re.fullmatch(r"[a-f0-9]{64}", value) is not None
    if key == "verification_reason":
        return value in {
            "checkout has local modifications",
            "checkout subtree is dirty or Git attribution is unavailable",
            "installed integration files differ from checkout",
            "installed fingerprint endpoint unavailable or invalid",
            "checkout integration files could not be scanned",
            "checkout commit unavailable",
            "checkout Git commit unavailable or invalid",
            "installed integration fingerprint could not be compared",
            "installed and checkout manifest versions differ",
        }
    return False


def _regular(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise HAAPIError("Unsafe HIL evidence path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise HAAPIError("HIL evidence file is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HAAPIError("HIL evidence file is not regular")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_FILE + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE:
        raise HAAPIError("HIL evidence file exceeds size limit")
    return data


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_regular(path))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise HAAPIError("Malformed HIL JSON evidence") from exc
    if not isinstance(value, dict):
        raise HAAPIError("Malformed HIL JSON evidence")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = _regular(path)
    rows = []
    try:
        for line in raw.splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError
            rows.append(row)
            if len(rows) > 10_000:
                raise ValueError
    except (UnicodeError, ValueError, TypeError) as exc:
        raise HAAPIError("Malformed private HIL trace records") from exc
    return rows


def _safe_utc(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        return None
    return parsed.isoformat(timespec="microseconds")


def _verified_transaction_references(
    transaction: Any,
    events: dict[int, dict[str, Any]],
    frames: dict[int, dict[str, Any]],
) -> list[int]:
    """Return event sequences proving a validated, command-matched TX."""
    if not isinstance(transaction, dict):
        return []
    kind = transaction.get("kind")
    expected_kind = (
        "output" if transaction.get("command_type") == "output_control" else "settings"
    )
    if kind != expected_kind or transaction.get("command_type") not in {
        "output_control",
        "settings_control",
    }:
        return []
    transaction_id = transaction.get("transaction_id")
    generation = transaction.get("session_generation")
    write_seq, submitted_seq, confirmed_seq = (
        transaction.get("write_seq"),
        transaction.get("submitted_seq"),
        transaction.get("confirmed_seq"),
    )
    if (
        not isinstance(transaction_id, str)
        or not transaction_id
        or type(generation) is not int
        or any(
            type(seq) is not int or seq < 1
            for seq in (write_seq, submitted_seq, confirmed_seq)
        )
        or not write_seq < submitted_seq < confirmed_seq
    ):
        return []
    write, submitted, confirmed = (
        events.get(seq) for seq in (write_seq, submitted_seq, confirmed_seq)
    )
    if not all(isinstance(row, dict) for row in (write, submitted, confirmed)):
        return []
    if not (
        write.get("event") == "write_attempt"
        and submitted.get("event") == "transaction_submitted"
        and confirmed.get("event") == "transaction_confirmed"
        and all(
            row.get("transaction_id") == transaction_id
            for row in (write, submitted, confirmed)
        )
        and all(
            row.get("session_generation") == generation
            for row in (write, submitted, confirmed)
        )
        and submitted.get("transaction_kind") == kind
        and confirmed.get("reason") == "matching_validated_telemetry"
    ):
        return []
    frame = frames.get(write_seq)
    if not isinstance(frame, dict) or not (
        frame.get("direction") == "tx"
        and frame.get("frame_validation") == "validated_tx"
        and frame.get("command_type") == transaction["command_type"]
        and frame.get("session_generation") == generation
        and frame.get("transaction_id") == transaction_id
        and frame.get("event") == "write_attempt"
    ):
        return []
    return [write_seq, submitted_seq, confirmed_seq]


def _safe_directory(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise HAAPIError("HIL run directory is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HAAPIError("Unsafe HIL run directory")
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise HAAPIError("HIL run path is not a directory")


def _safe_versioned(source: dict[str, Any], name: str) -> dict[str, Any]:
    """Expose only conservative, non-identifying facts from environment data."""
    keys = {
        "schema_version",
        "collected_at",
        "home_assistant",
        "runner_host",
        "integration",
    }
    result: dict[str, Any] = {
        "schema_version": PUBLIC_SCHEMA,
        "source_schema_version": source.get("schema_version")
        if type(source.get("schema_version")) is int
        else None,
        "data": {},
    }
    if set(source) - keys:
        result["omitted"] = ["unknown_source_fields"]
    if name == "environment.json":
        for group_name in ("home_assistant", "runner_host", "integration"):
            group = source.get(group_name)
            if not isinstance(group, dict):
                continue
            allowed: dict[str, Any] = {}
            for key in (
                "version",
                "installation_type",
                "os",
                "architecture",
                "kernel",
                "cpu",
                "python_version",
                "installed_manifest_version",
                "build_match",
                "runner_checkout_commit",
                "installed_commit",
                "installed_fingerprint",
                "checkout_fingerprint",
                "verification_reason",
            ):
                item = group.get(key)
                if isinstance(item, dict):
                    value = item.get("value")
                    if _safe_fact_value(key, value):
                        allowed[key] = {
                            "value": value,
                            "source": item.get("source")
                            if item.get("source")
                            in {
                                "runner_platform",
                                "runner_runtime",
                                "ha_rest_config",
                                "ha_ws_manifest_get",
                                "runner_repository_manifest",
                                "runner_git_checkout",
                                "ha_ws_hil_build",
                                "runner_integration_files",
                            }
                            else "unknown",
                            "verified": item.get("verified") is True,
                        }
                elif (
                    key == "build_match"
                    and isinstance(item, str)
                    and item in {"verified", "mismatch", "unverified"}
                ):
                    allowed[key] = {
                        "value": item,
                        "source": "runner_build_verification",
                        "verified": item == "verified",
                    }
                elif key == "verification_reason" and _safe_fact_value(
                    key, group.get(key)
                ):
                    allowed[key] = {
                        "value": group[key],
                        "source": "runner_build_verification",
                        "verified": False,
                    }
            result["data"][group_name] = allowed
    else:
        for key in (
            "run_id",
            "scenario_id",
            "qualification_status",
            "mode",
            "route",
            "preflight",
            "telemetry",
            "runtime_capabilities",
            "test_plan",
            "started_at",
        ):
            value = source.get(key)
            if (
                key == "run_id"
                and isinstance(value, str)
                and re.fullmatch(r"[a-f0-9]{16}", value)
            ):
                result["data"][key] = value
            elif (
                key == "scenario_id"
                and isinstance(value, str)
                and value in SCENARIO_IDS
            ):
                result["data"][key] = value
            elif key == "qualification_status":
                result["data"][key] = "inconclusive"
            elif key == "mode" and value == "read_only_preflight":
                result["data"][key] = value
            elif key == "route" and isinstance(value, dict):
                observed = value.get("observed")
                route = {}
                if (
                    isinstance(observed, dict)
                    and isinstance(observed.get("value"), str)
                    and observed.get("value")
                    in {"local_adapter", "active_proxy", "unverified"}
                ):
                    route["observed"] = {
                        "value": observed["value"],
                        "verified": observed.get("verified") is True,
                    }
                requested = value.get("requested")
                if isinstance(requested, str) and requested in REQUESTED_ROUTES:
                    route["requested"] = requested
                if route:
                    result["data"][key] = route
            elif key == "preflight" and isinstance(value, dict):
                preflight = {
                    "overall": value.get("overall")
                    if isinstance(value.get("overall"), str)
                    and value.get("overall") in {"ready", "incomplete", "inconclusive"}
                    else "inconclusive"
                }
                checks = value.get("checks")
                if isinstance(checks, dict):
                    preflight["checks"] = {
                        name: status
                        for name, status in checks.items()
                        if name in PREFLIGHT_CHECKS
                        and isinstance(status, str)
                        and status in PREFLIGHT_RESULTS
                    }
                result["data"][key] = preflight
            elif key == "telemetry":
                telemetry = _public_read_only_telemetry(value)
                if telemetry:
                    result["data"][key] = telemetry
            elif key == "runtime_capabilities" and isinstance(value, dict):
                source_capabilities = value.get("capabilities")
                available = (
                    isinstance(source_capabilities, dict)
                    and value.get("verified") is True
                )
                capabilities = (
                    {
                        name: flag
                        for name, flag in source_capabilities.items()
                        if name in RUNTIME_CAPABILITIES and type(flag) is bool
                    }
                    if available
                    else {}
                )
                result["data"][key] = {
                    "available": available,
                    "connected": value.get("connected")
                    if available and type(value.get("connected")) is bool
                    else None,
                    "status_fresh": value.get("status_fresh")
                    if available and type(value.get("status_fresh")) is bool
                    else None,
                    "settings_fresh": value.get("settings_fresh")
                    if available and type(value.get("settings_fresh")) is bool
                    else None,
                    "pending_transaction": value.get("pending_transaction")
                    if available and type(value.get("pending_transaction")) is bool
                    else None,
                    "capabilities": capabilities,
                }
            elif key == "test_plan" and isinstance(value, dict):
                projected = _public_tier_plan(value)
                if projected is not None:
                    result["data"][key] = projected
            elif key == "started_at" and isinstance(value, str):
                try:
                    timestamp = datetime.fromisoformat(value)
                except ValueError:
                    continue
                if (
                    timestamp.utcoffset() is not None
                    and timestamp.utcoffset().total_seconds() == 0
                ):
                    result["data"][key] = value
        device = source.get("device")
        if isinstance(device, dict):
            projected_device = {}
            for key in ("model", "hardware_version", "firmware_version"):
                record = device.get(key)
                value = record.get("value") if isinstance(record, dict) else None
                if (
                    key == "model"
                    and safe_model(value) == value
                    and value in KNOWN_MODELS
                ):
                    projected_device[key] = {
                        "value": value,
                        "source": "ha_device_registry",
                        "verified": record.get("verified") is True,
                    }
                elif key != "model":
                    version = safe_version(value)
                    if version is not None:
                        projected_device[key] = {
                            "value": version,
                            "source": "ha_device_registry",
                            "verified": record.get("verified") is True,
                        }
            result["data"]["device"] = projected_device
        result["data"].setdefault("qualification_status", "inconclusive")
    return result


def _public_tier_plan(value: dict[str, Any]) -> dict[str, Any] | None:
    """Project guided plans through a narrow public evidence allowlist."""
    safe_reasons = {
        "runtime profile substantiates physical absence",
        "physical presence is unknown or unreviewed",
        "read-only telemetry capability is unavailable",
        "fresh connected telemetry preflight is not ready",
        "no production write authorization or reviewed HIL candidate",
        "candidate needs a fresh, single-use backend HIL grant",
        "control state is unavailable",
        "fresh same-session capability preflight is not ready",
        "connected GATT route has not been verified",
        "minimum output requirement awaits a reviewed output PASS",
        "tier requires a basic output PASS; all declared outputs are absent",
        "no present, production-authorized or reviewed candidate output is available",
    }
    safe_step_ids = {
        "read_only_probe",
        "physical_plausibility_observation",
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
    }
    if (
        value.get("schema_version") != 1
        or value.get("requested_tier") not in {"bronze", "silver", "gold"}
        or value.get("attained_tier") is not None
        or value.get("review_state") != "pending"
        or not isinstance(value.get("steps"), list)
        or len(value["steps"]) > 32
        or type(value.get("operator_actions_estimate")) is not int
        or not 0 <= value["operator_actions_estimate"] <= 128
    ):
        return None
    device = value.get("device")
    if not isinstance(device, dict):
        return None
    model = safe_model(device.get("model")) or "unknown"
    route = device.get("verified_route")
    if route not in {None, "local_adapter", "active_proxy"}:
        route = None
    steps = []
    for step in value["steps"]:
        if not isinstance(step, dict):
            return None
        if (
            not isinstance(step.get("id"), str)
            or step["id"] not in safe_step_ids
            or step.get("eligibility") not in {"eligible", "blocked", "not_applicable"}
            or step.get("outcome") != "pending"
            or step.get("human_review") != "pending"
            or not isinstance(step.get("reasons"), list)
            or not all(
                isinstance(reason, str) and reason in safe_reasons
                for reason in step["reasons"]
            )
        ):
            return None
        steps.append(
            {
                "id": step["id"],
                "feature": step.get("feature")
                if step.get("feature")
                in {
                    "telemetry",
                    "ac_output",
                    "dc_output",
                    "light_output",
                    "eco_mode",
                    "work_mode",
                    "eco_timeout",
                    "car_charger",
                    "output",
                }
                else "unknown",
                "eligibility": step["eligibility"],
                "outcome": step["outcome"],
                "reasons": step["reasons"],
                "human_review": step["human_review"],
            }
        )
    return {
        "schema_version": 1,
        "requested_tier": value["requested_tier"],
        "attained_tier": None,
        "review_state": "pending",
        "device": {
            "model": model,
            "hardware_revision": safe_version(device.get("hardware_revision")),
            "firmware_revision": safe_version(device.get("firmware_revision")),
            "verified_route": route,
        },
        "operator_actions_estimate": value["operator_actions_estimate"],
        "steps": steps,
    }


def _public_tier_artifact(name: str, content: bytes) -> dict[str, Any]:
    """Project one verified private guided artifact without paths or run IDs."""
    if name == "trace.json":
        value = _load_public_json(content)
        route = value.get("observed_route")
        requested_route = value.get("requested_route")
        return {
            "kind": "gatt_trace_metadata",
            "schema_version": value.get("schema_version")
            if type(value.get("schema_version")) is int
            else None,
            "scenario_id": value.get("scenario_id")
            if value.get("scenario_id") in SCENARIO_IDS
            else None,
            "requested_route": requested_route
            if requested_route in REQUESTED_ROUTES
            else "auto",
            "observed_route": route
            if route in {"local_adapter", "active_proxy", "unverified", "mixed"}
            else "unverified",
            "complete": value.get("complete") is True,
            "event_count": value.get("event_count")
            if type(value.get("event_count")) is int
            and 0 <= value["event_count"] <= 1_000_000
            else 0,
            "frame_count": value.get("frame_count")
            if type(value.get("frame_count")) is int
            and 0 <= value["frame_count"] <= 1_000_000
            else 0,
            "dropped_events": value.get("dropped_events")
            if type(value.get("dropped_events")) is int
            and 0 <= value["dropped_events"] <= 1_000_000
            else 0,
            "dropped_frames": value.get("dropped_frames")
            if type(value.get("dropped_frames")) is int
            and 0 <= value["dropped_frames"] <= 1_000_000
            else 0,
        }
    if name == "events.jsonl":
        rows = []
        for raw in content.splitlines():
            if not raw:
                continue
            row = _load_public_json(raw)
            event = row.get("event")
            if event not in EVENT_NAMES:
                continue
            projected = {
                "seq": row.get("seq"),
                "event": event,
                "utc": _safe_utc(row.get("utc")),
                "elapsed_ms": row.get("elapsed_ms")
                if type(row.get("elapsed_ms")) in (int, float)
                and math.isfinite(row["elapsed_ms"])
                and 0 <= row["elapsed_ms"] <= 86_400_000
                else None,
                "session_generation": row.get("session_generation")
                if type(row.get("session_generation")) is int
                and 1 <= row["session_generation"] <= 1_000_000
                else None,
                "observed_route": row.get("observed_route")
                if row.get("observed_route")
                in {"local_adapter", "active_proxy", "unverified"}
                else "unverified",
            }
            kind = row.get("transaction_kind")
            if kind in {"output", "settings"}:
                projected["transaction_kind"] = kind
            rows.append(projected)
        return {"kind": "gatt_events", "rows": rows}
    if name == "frames.jsonl":
        rows = []
        for raw in content.splitlines():
            if not raw:
                continue
            row = _load_public_json(raw)
            if row.get("event") not in EVENT_NAMES:
                continue
            rows.append(
                {
                    "seq": row.get("seq"),
                    "direction": row.get("direction")
                    if row.get("direction") in {"rx", "tx"}
                    else "rx",
                    "command_type": row.get("command_type")
                    if row.get("command_type")
                    in {
                        "status_request",
                        "output_control",
                        "settings_control",
                        "unknown",
                    }
                    else "unknown",
                    "frame_validation": row.get("frame_validation")
                    if row.get("frame_validation")
                    in {"validated_tx", "unvalidated_chunk"}
                    else "unvalidated_chunk",
                    "session_generation": row.get("session_generation")
                    if type(row.get("session_generation")) is int
                    and 1 <= row["session_generation"] <= 1_000_000
                    else None,
                    "payload_omitted": True,
                }
            )
        return {"kind": "gatt_frames", "rows": rows}
    if name == "checkpoint.json":
        value = _load_public_json(content)
        steps = value.get("steps")
        if not isinstance(steps, list) or len(steps) > 128:
            raise HAAPIError("Invalid guided child checkpoint")
        safe_steps = []
        for step in steps:
            if (
                not isinstance(step, dict)
                or step.get("step_id") not in STEP_IDS
                or step.get("outcome") not in OUTCOMES
            ):
                raise HAAPIError("Invalid guided child checkpoint step")
            safe_steps.append(
                {
                    "step_id": step["step_id"],
                    "outcome": step["outcome"],
                    "observation": _public_observation(
                        value.get("scenario_id", ""), step
                    ),
                }
            )
        scenario_id = value.get("scenario_id")
        return {
            "kind": "scenario_checkpoint",
            "scenario_id": scenario_id if scenario_id in SCENARIO_IDS else None,
            "qualification_status": "inconclusive",
            "steps": safe_steps,
        }

    if not name.startswith("tier-") or not name.endswith(".json"):
        raise HAAPIError("Unsupported guided evidence artifact")
    value = _load_public_json(content)
    step_id = value.get("step_id")
    if step_id not in TIER_STEP_IDS:
        raise HAAPIError("Invalid guided evidence step")
    safe: dict[str, Any] = {
        "kind": "guided_step_evidence",
        "step_id": step_id,
        "outcome": value.get("outcome")
        if value.get("outcome") in OUTCOMES | {"blocked"}
        else "inconclusive",
    }
    for key in ("initial_state", "target_state", "restored_state", "readback_state"):
        if value.get(key) in {"on", "off"}:
            safe[key] = value[key]
    for key in ("physical_change_outcome", "physical_restore_outcome"):
        if value.get(key) in {"correct", "incorrect", "unable_to_observe"}:
            safe[key] = value[key]
    actions = value.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 4:
        raise HAAPIError("Invalid guided step action evidence")
    safe_actions = []
    for action in actions:
        if not isinstance(action, dict):
            raise HAAPIError("Invalid guided step action evidence")
        projected = {
            key: action[key]
            for key in (
                "outcome",
                "confirmed",
                "trace_valid",
                "trace_complete",
                "backend_failed",
            )
            if (key == "outcome" and action.get(key) in {"confirmed", "inconclusive"})
            or (key != "outcome" and type(action.get(key)) is bool)
        }
        for key in ("route", "trace_route"):
            if action.get(key) in {"local_adapter", "active_proxy", "unverified"}:
                projected[key] = action[key]
        generation = action.get("session_generation")
        if type(generation) is int and 1 <= generation <= 1_000_000:
            projected["session_generation"] = generation
        transaction = action.get("transaction")
        if isinstance(transaction, dict):
            kind = transaction.get("kind")
            transaction_generation = transaction.get("session_generation")
            if kind in {"output", "settings"} and type(transaction_generation) is int:
                projected["transaction"] = {
                    "kind": kind,
                    "session_generation": transaction_generation,
                }
        trace_digests = []
        for trace_artifact in action.get("trace_evidence", []):
            if not isinstance(trace_artifact, dict):
                raise HAAPIError("Invalid guided trace evidence reference")
            digest = trace_artifact.get("sha256")
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            ):
                raise HAAPIError("Invalid guided trace evidence digest")
            trace_digests.append(digest)
        if trace_digests:
            projected["trace_evidence_sha256"] = sorted(set(trace_digests))
        safe_actions.append(projected)
    safe["actions"] = safe_actions
    return safe


def _valid_public_tier_artifact(value: dict[str, Any]) -> bool:
    """Enforce the public artifact allowlist again when importing a ZIP."""
    kind = value.get("kind")
    if kind == "gatt_trace_metadata":
        return bool(
            set(value)
            == {
                "kind",
                "schema_version",
                "scenario_id",
                "requested_route",
                "observed_route",
                "complete",
                "event_count",
                "frame_count",
                "dropped_events",
                "dropped_frames",
            }
            and value.get("schema_version") in {1, 2}
            and value.get("scenario_id") in SCENARIO_IDS | {None}
            and value.get("requested_route") in REQUESTED_ROUTES
            and value.get("observed_route")
            in {"local_adapter", "active_proxy", "unverified", "mixed"}
            and type(value.get("complete")) is bool
            and all(
                type(value.get(key)) is int and 0 <= value[key] <= 1_000_000
                for key in (
                    "event_count",
                    "frame_count",
                    "dropped_events",
                    "dropped_frames",
                )
            )
        )
    if kind == "gatt_events":
        rows = value.get("rows")
        if (
            set(value) != {"kind", "rows"}
            or not isinstance(rows, list)
            or len(rows) > 100_000
        ):
            return False
        for row in rows:
            if not isinstance(row, dict) or set(row) - {
                "seq",
                "event",
                "utc",
                "elapsed_ms",
                "session_generation",
                "observed_route",
                "transaction_kind",
            }:
                return False
            if (
                type(row.get("seq")) is not int
                or row["seq"] < 1
                or row.get("event") not in EVENT_NAMES
                or row.get("utc") is not None
                and _safe_utc(row["utc"]) != row["utc"]
                or row.get("elapsed_ms") is not None
                and (
                    type(row["elapsed_ms"]) not in (int, float)
                    or not math.isfinite(row["elapsed_ms"])
                    or not 0 <= row["elapsed_ms"] <= 86_400_000
                )
                or row.get("session_generation") is not None
                and (
                    type(row["session_generation"]) is not int
                    or not 1 <= row["session_generation"] <= 1_000_000
                )
                or row.get("observed_route")
                not in {"local_adapter", "active_proxy", "unverified"}
                or row.get("transaction_kind") not in {None, "output", "settings"}
            ):
                return False
        return True
    if kind == "gatt_frames":
        rows = value.get("rows")
        if (
            set(value) != {"kind", "rows"}
            or not isinstance(rows, list)
            or len(rows) > 100_000
        ):
            return False
        return all(
            isinstance(row, dict)
            and set(row)
            == {
                "seq",
                "direction",
                "command_type",
                "frame_validation",
                "session_generation",
                "payload_omitted",
            }
            and type(row.get("seq")) is int
            and row["seq"] >= 1
            and row.get("direction") in {"rx", "tx"}
            and row.get("command_type")
            in {"status_request", "output_control", "settings_control", "unknown"}
            and row.get("frame_validation") in {"validated_tx", "unvalidated_chunk"}
            and (
                row.get("session_generation") is None
                or type(row["session_generation"]) is int
                and 1 <= row["session_generation"] <= 1_000_000
            )
            and row.get("payload_omitted") is True
            for row in rows
        )
    if kind == "scenario_checkpoint":
        steps = value.get("steps")
        if (
            set(value) != {"kind", "scenario_id", "qualification_status", "steps"}
            or value.get("scenario_id") not in SCENARIO_IDS | {None}
            or value.get("qualification_status") != "inconclusive"
            or not isinstance(steps, list)
            or len(steps) > 128
        ):
            return False
        return all(
            isinstance(step, dict)
            and set(step) == {"step_id", "outcome", "observation"}
            and step.get("step_id") in STEP_IDS
            and step.get("outcome") in OUTCOMES
            and _valid_public_observation(step.get("observation"))
            for step in steps
        )
    if kind == "guided_step_evidence":
        allowed = {
            "kind",
            "step_id",
            "outcome",
            "initial_state",
            "target_state",
            "restored_state",
            "readback_state",
            "physical_change_outcome",
            "physical_restore_outcome",
            "actions",
        }
        actions = value.get("actions")
        if (
            set(value) - allowed
            or not {"kind", "step_id", "outcome", "actions"}.issubset(value)
            or value.get("step_id") not in TIER_STEP_IDS
            or value.get("outcome") not in OUTCOMES | {"blocked"}
            or any(
                value.get(key) not in {"on", "off"}
                for key in (
                    "initial_state",
                    "target_state",
                    "restored_state",
                    "readback_state",
                )
                if key in value
            )
            or any(
                value.get(key) not in {"correct", "incorrect", "unable_to_observe"}
                for key in ("physical_change_outcome", "physical_restore_outcome")
                if key in value
            )
            or not isinstance(actions, list)
            or len(actions) > 4
        ):
            return False
        action_fields = {
            "outcome",
            "confirmed",
            "trace_valid",
            "trace_complete",
            "backend_failed",
            "route",
            "trace_route",
            "session_generation",
            "transaction",
            "trace_evidence_sha256",
        }
        for action in actions:
            if (
                not isinstance(action, dict)
                or set(action) - action_fields
                or action.get("outcome") not in {"confirmed", "inconclusive"}
                or any(
                    type(action[key]) is not bool
                    for key in action
                    if key
                    in {"confirmed", "trace_valid", "trace_complete", "backend_failed"}
                )
                or any(
                    action.get(key)
                    not in {"local_adapter", "active_proxy", "unverified"}
                    for key in ("route", "trace_route")
                    if key in action
                )
                or action.get("session_generation") is not None
                and (
                    type(action["session_generation"]) is not int
                    or not 1 <= action["session_generation"] <= 1_000_000
                )
                or action.get("transaction") is not None
                and (
                    not isinstance(action["transaction"], dict)
                    or set(action["transaction"]) != {"kind", "session_generation"}
                    or action["transaction"].get("kind") not in {"output", "settings"}
                    or type(action["transaction"].get("session_generation")) is not int
                )
                or action.get("trace_evidence_sha256") is not None
                and (
                    not isinstance(action["trace_evidence_sha256"], list)
                    or len(action["trace_evidence_sha256"]) > 3
                    or any(
                        not isinstance(digest, str)
                        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                        for digest in action["trace_evidence_sha256"]
                    )
                )
            ):
                return False
        return True
    return False


def _bronze_samples_fresh(samples: list[Any]) -> bool:
    if len(samples) != 2:
        return False
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(
            sample.get("telemetry"), dict
        ):
            return False
        telemetry = sample["telemetry"]
        measurement = any(
            key in {"battery", "input_power", "output_power", "remaining_time"}
            and isinstance(record, dict)
            and record.get("verified") is True
            and type(record.get("sample_age_seconds")) is int
            and 0 <= record["sample_age_seconds"] <= 60
            for key, record in telemetry.items()
        )
        available = telemetry.get("telemetry_available")
        if (
            not measurement
            or not isinstance(available, dict)
            or available.get("verified") is not True
            or available.get("value") != "on"
        ):
            return False
    return True


def build_public(run_path: Path) -> tuple[Path, dict[str, bytes], dict[str, Any]]:
    """Project a legacy flat or private/ run without modifying source files."""
    run_path = run_path.expanduser()
    if run_path.name == "private" and run_path.is_dir():
        private = run_path
        run_dir = run_path.parent
    elif (run_path / "private").is_dir():
        run_dir, private = run_path, run_path / "private"
    else:
        run_dir = run_path
        private = run_path
    _safe_directory(run_dir)
    _safe_directory(private)
    missing_sources = []
    source_files = {}
    for filename in ("environment.json", "session.json"):
        try:
            source_files[filename] = _read_json(private / filename)
        except FileNotFoundError:
            missing_sources.append(filename)
            source_files[filename] = {"schema_version": 1}
    env = source_files["environment.json"]
    session = source_files["session.json"]
    if (
        type(env.get("schema_version")) is not int
        or env.get("schema_version") != 1
        or type(session.get("schema_version")) is not int
        or session.get("schema_version") != 1
    ):
        raise HAAPIError("Unsupported private HIL source schema")
    raw_run_id = session.get("run_id")
    if raw_run_id is not None and (
        not isinstance(raw_run_id, str)
        or re.fullmatch(r"[a-f0-9]{16}", raw_run_id) is None
    ):
        raise HAAPIError("Invalid private HIL run identity")
    public_session = _safe_versioned(session, "session.json")
    if isinstance(raw_run_id, str):
        public_session["data"]["run_id"] = hashlib.sha256(
            ("allpowers-hil-public-run:" + raw_run_id).encode("utf-8")
        ).hexdigest()[:16]
    files: dict[str, bytes] = {
        "environment.json": (
            json.dumps(
                _safe_versioned(env, "environment.json"), sort_keys=True, indent=2
            )
            + "\n"
        ).encode(),
        "session.json": (
            json.dumps(public_session, sort_keys=True, indent=2) + "\n"
        ).encode(),
    }
    scenario_path = private / "scenarios.json"
    if scenario_path.exists():
        scenario = _read_json(scenario_path)
        if (
            type(scenario.get("schema_version")) is not int
            or scenario.get("schema_version") != 2
        ):
            raise HAAPIError("Unsupported private HIL scenario schema")
        if (
            scenario.get("run_id") != raw_run_id
            or scenario.get("qualification_status") != "inconclusive"
            or not isinstance(scenario.get("scenario_id"), str)
            or scenario["scenario_id"] not in SCENARIO_IDS
            or not isinstance(scenario.get("steps"), list)
            or len(scenario["steps"]) > 128
        ):
            raise HAAPIError("Private scenario identity does not match its run")
        steps = scenario.get("steps", [])
        safe_steps = []
        if isinstance(steps, list):
            for step in steps[:128]:
                if not isinstance(step, dict):
                    continue
                sid, outcome = step.get("step_id"), step.get("outcome")
                if (
                    isinstance(sid, str)
                    and sid in STEP_IDS
                    and isinstance(outcome, str)
                    and outcome in OUTCOMES
                ):
                    safe_steps.append(
                        {
                            "step_id": sid,
                            "outcome": outcome,
                            "evidence": [],
                            "missing_evidence": True,
                        }
                    )
        payload = {
            "schema_version": PUBLIC_SCHEMA,
            "source_schema_version": scenario.get("schema_version")
            if type(scenario.get("schema_version")) is int
            else None,
            "qualification_status": "inconclusive",
            "steps": safe_steps,
        }
        files["scenarios.json"] = (
            json.dumps(payload, sort_keys=True, indent=2) + "\n"
        ).encode()
    tier_execution_path = private / "tier-execution.json"
    if tier_execution_path.exists():
        tier_execution = _read_json(tier_execution_path)
        if (
            tier_execution.get("schema_version") != 1
            or tier_execution.get("run_id") != raw_run_id
            or tier_execution.get("review_state") != "pending"
            or tier_execution.get("attained_tier") is not None
            or tier_execution.get("requested_tier") not in {"bronze", "silver", "gold"}
            or not isinstance(tier_execution.get("steps"), list)
            or len(tier_execution["steps"]) > 32
        ):
            raise HAAPIError("Invalid private guided tier execution")
        public_steps = []
        public_artifacts = []
        for step_index, step in enumerate(tier_execution["steps"]):
            if (
                not isinstance(step, dict)
                or not isinstance(step.get("step_id"), str)
                or step["step_id"] not in TIER_STEP_IDS
                or not isinstance(step.get("outcome"), str)
                or step.get("outcome")
                not in OUTCOMES | {"blocked", "pending_review", "shared_evidence"}
            ):
                raise HAAPIError("Invalid private guided tier step")
            evidence_hashes = []
            evidence_refs = []
            seen_sources: set[Path] = set()
            child_run_id: str | None = None
            child_private: Path | None = None
            evidence_run = step.get("evidence_run")
            if evidence_run is not None:
                if not isinstance(evidence_run, str):
                    raise HAAPIError("Invalid guided child run reference")
                try:
                    child_private = (private / evidence_run).resolve(strict=True)
                    child_private.relative_to(run_dir.parent.resolve())
                    child_session = _read_json(child_private / "session.json")
                    _, child_checkpoint = load_scenario_checkpoint(child_private)
                except (OSError, ValueError, HAAPIError) as exc:
                    raise HAAPIError(
                        "Guided child run identity is unavailable"
                    ) from exc
                directory_run_id = child_private.parent.name.removeprefix("tier_")
                expected_child_step_id = step["step_id"]
                shared_reconnect = False
                if (
                    step["outcome"] == "shared_evidence"
                    and step["step_id"] == "guarded_recovery"
                ):
                    shared_reconnect = any(
                        isinstance(previous, dict)
                        and previous.get("step_id") == "basic_reconnect"
                        and previous.get("scenario_id") == step.get("scenario_id")
                        and previous.get("outcome") == "pass"
                        and previous.get("evidence_run") == evidence_run
                        for previous in tier_execution["steps"][:step_index]
                    )
                    if shared_reconnect:
                        expected_child_step_id = "basic_reconnect"
                if (
                    (
                        step["outcome"] == "shared_evidence"
                        and step["step_id"] == "guarded_recovery"
                        and not shared_reconnect
                    )
                    or child_private.name != "private"
                    or re.fullmatch(r"[a-f0-9]{16}", directory_run_id) is None
                    or child_session.get("run_id") != directory_run_id
                    or child_session.get("parent_tier_run_id") != raw_run_id
                    or child_session.get("tier_step_id") != expected_child_step_id
                    or child_session.get("requested_route")
                    != tier_execution.get("requested_route")
                    or child_session.get("device") != tier_execution.get("device")
                    or child_checkpoint.get("schema_version") != 2
                    or child_checkpoint.get("run_id") != directory_run_id
                    or child_checkpoint.get("scenario_id") != step.get("scenario_id")
                    or child_checkpoint.get("qualification_status") != "inconclusive"
                    or not isinstance(child_checkpoint.get("steps"), list)
                    or not child_checkpoint["steps"]
                ):
                    raise HAAPIError("Guided child run identity failed validation")
                child_run_id = directory_run_id
            machine_evidence = step.get("machine_evidence", [])
            if not isinstance(machine_evidence, list) or len(machine_evidence) > 32:
                raise HAAPIError("Invalid guided tier evidence list")
            pending_sources: list[tuple[Path, str | None]] = []
            for item in machine_evidence:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise HAAPIError("Invalid guided tier evidence reference")
                try:
                    evidence_source = private / item["path"]
                    evidence_path = evidence_source.resolve(strict=True)
                    evidence_path.relative_to(run_dir.parent.resolve())
                except (OSError, ValueError) as exc:
                    raise HAAPIError("Guided tier evidence is unavailable") from exc
                expected_digest = item.get("sha256")
                if (
                    not isinstance(expected_digest, str)
                    or re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None
                ):
                    raise HAAPIError("Invalid guided tier evidence digest")
                pending_sources.append((evidence_path, expected_digest))
            cursor = 0
            while cursor < len(pending_sources):
                evidence_path, expected_digest = pending_sources[cursor]
                cursor += 1
                if evidence_path in seen_sources:
                    continue
                seen_sources.add(evidence_path)
                try:
                    evidence_bytes = _regular(evidence_path)
                except OSError as exc:
                    raise HAAPIError("Guided tier evidence is unavailable") from exc
                digest = hashlib.sha256(evidence_bytes).hexdigest()
                if digest != expected_digest:
                    raise HAAPIError("Guided tier evidence digest mismatch")
                evidence_hashes.append(digest)
                if evidence_path.name == "trace.json":
                    trace_meta = _read_json(evidence_path)
                    expected_scenario_id = step.get("scenario_id")
                    if child_run_id is None and expected_scenario_id is None:
                        expected_scenario_id = trace_meta.get("scenario_id")
                    expected_requested_route = (
                        child_session.get("requested_route")
                        if child_run_id is not None
                        else trace_meta.get("requested_route")
                    )
                    if (
                        trace_meta.get("scenario_id") != expected_scenario_id
                        or trace_meta.get("requested_route") != expected_requested_route
                    ):
                        raise HAAPIError("Guided child trace scope failed validation")
                    events_path = evidence_path.with_name("events.jsonl")
                    frames_path = evidence_path.with_name("frames.jsonl")
                    try:
                        events_bytes = _regular(events_path)
                        frames_bytes = _regular(frames_path)
                        trace = {
                            **trace_meta,
                            "events": [
                                _load_public_json(line)
                                for line in events_bytes.splitlines()
                                if line
                            ],
                            "frames": [
                                _load_public_json(line)
                                for line in frames_bytes.splitlines()
                                if line
                            ],
                        }
                        _validate_trace_result(
                            trace,
                            run_id=child_run_id or raw_run_id,
                            scenario_id=expected_scenario_id,
                            capture_id=trace_meta.get("capture_id"),
                            requested_route=expected_requested_route,
                            expected_version=trace_meta.get("schema_version"),
                            expected_scope=trace_meta.get(
                                "capture_scope", "runtime_entry"
                            ),
                        )
                    except (
                        OSError,
                        HAAPIError,
                        TypeError,
                        ValueError,
                        KeyError,
                    ) as exc:
                        raise HAAPIError(
                            "Guided child GATT trace failed validation"
                        ) from exc
                    for sibling_path, sibling_bytes in (
                        (events_path, events_bytes),
                        (frames_path, frames_bytes),
                    ):
                        sibling_digest = hashlib.sha256(sibling_bytes).hexdigest()
                        evidence_hashes.append(sibling_digest)
                        if sibling_path not in seen_sources:
                            pending_sources.append((sibling_path, sibling_digest))
                projected = _public_tier_artifact(evidence_path.name, evidence_bytes)
                artifact_id = (
                    f"step-{step_index + 1:02d}-evidence-{len(evidence_refs) + 1:02d}"
                )
                public_artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "source_sha256": digest,
                        "content": projected,
                    }
                )
                evidence_refs.append(
                    {
                        "artifact_id": artifact_id,
                        "source_sha256": digest,
                    }
                )
                if evidence_path.name.startswith(
                    "tier-"
                ) and evidence_path.name.endswith(".json"):
                    private_value = _load_public_json(evidence_bytes)
                    for action in private_value.get("actions", []):
                        if not isinstance(action, dict):
                            continue
                        for trace_item in action.get("trace_evidence", []):
                            if (
                                not isinstance(trace_item, dict)
                                or not isinstance(trace_item.get("path"), str)
                                or not isinstance(trace_item.get("sha256"), str)
                            ):
                                raise HAAPIError(
                                    "Invalid guided trace evidence reference"
                                )
                            try:
                                trace_source = (private / trace_item["path"]).resolve(
                                    strict=True
                                )
                                trace_source.relative_to(run_dir.parent.resolve())
                            except (OSError, ValueError) as exc:
                                raise HAAPIError(
                                    "Guided tier evidence is unavailable"
                                ) from exc
                            pending_sources.append((trace_source, trace_item["sha256"]))
            public_step = {
                "step_id": step["step_id"],
                "outcome": step["outcome"],
                "machine_evidence_sha256": sorted(set(evidence_hashes)),
                "machine_evidence": evidence_refs,
            }
            if step.get("human_review") in {"pending", "reviewed"}:
                public_step["human_review"] = step["human_review"]
            public_steps.append(public_step)
        files["tier-artifacts.json"] = (
            json.dumps(
                {"schema_version": PUBLIC_SCHEMA, "artifacts": public_artifacts},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()
        files["tier-execution.json"] = (
            json.dumps(
                {
                    "schema_version": PUBLIC_SCHEMA,
                    "requested_tier": tier_execution["requested_tier"],
                    "attained_tier": None,
                    "review_state": "pending",
                    "qualification_status": "inconclusive",
                    "steps": public_steps,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()
    bronze_path = private / "tier-bronze-read-only.json"
    if bronze_path.exists():
        bronze = _read_json(bronze_path)
        samples = bronze.get("sample_sha256")
        private_samples = bronze.get("samples", [])
        if (
            bronze.get("schema_version") != 1
            or bronze.get("step_id") != "read_only_probe"
            or not isinstance(samples, list)
            or len(samples) != 2
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in samples
            )
            or type(bronze.get("fresh_verified_samples")) is not bool
            or bronze.get("physical_observation")
            not in {"plausible", "implausible", "unable_to_observe"}
            or not isinstance(private_samples, list)
            or len(private_samples) not in {0, 2}
        ):
            raise HAAPIError("Invalid private Bronze read-only evidence")
        public_samples = []
        samples_fresh = len(private_samples) == 2
        for sample in private_samples:
            sampled_at = (
                _safe_utc(sample.get("sampled_at_utc"))
                if isinstance(sample, dict)
                else None
            )
            if (
                not isinstance(sample, dict)
                or set(sample) != {"sampled_at_utc", "telemetry"}
                or sampled_at is None
                or not isinstance(sample.get("telemetry"), dict)
            ):
                raise HAAPIError("Invalid private Bronze telemetry sample")
            public_telemetry = {}
            has_fresh_measurement = False
            telemetry = sample["telemetry"]
            for key in SENSOR_KEYS + BINARY_KEYS:
                record = telemetry.get(key)
                if not isinstance(record, dict):
                    continue
                value = _safe_read_only_value(key, record.get("value"))
                verified = record.get("verified") is True and value is not None
                age = record.get("sample_age_seconds")
                safe_record: dict[str, Any] = {
                    "value": value if verified else None,
                    "verified": verified,
                }
                if verified and type(age) is int and 0 <= age <= 86_400:
                    safe_record["sample_age_seconds"] = age
                    if (
                        key
                        in {"battery", "input_power", "output_power", "remaining_time"}
                        and age <= 60
                    ):
                        has_fresh_measurement = True
                public_telemetry[key] = safe_record
            sample_fresh = bool(
                has_fresh_measurement
                and public_telemetry.get("telemetry_available", {}).get("value") == "on"
                and public_telemetry.get("telemetry_available", {}).get("verified")
                is True
            )
            samples_fresh = samples_fresh and sample_fresh
            public_samples.append(
                {
                    "sampled_at_utc": sampled_at,
                    "telemetry": public_telemetry,
                }
            )
        protocol = bronze.get("protocol_probe", {})
        if not isinstance(protocol, dict):
            raise HAAPIError("Invalid private Bronze protocol evidence")
        protocol_outcome = protocol.get("outcome", "not_observed")
        if protocol_outcome not in {"pass", "inconclusive", "not_observed"}:
            raise HAAPIError("Invalid private Bronze protocol outcome")
        protocol_digest = protocol.get("trace_sha256")
        if protocol_digest is not None and (
            not isinstance(protocol_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", protocol_digest) is None
        ):
            raise HAAPIError("Invalid private Bronze trace digest")
        protocol_route = protocol.get("route")
        if protocol_route not in {None, "local_adapter", "active_proxy", "unverified"}:
            raise HAAPIError("Invalid private Bronze route")
        for name in (
            "session_generation",
            "status_request_seq",
            "decoded_response_seq",
        ):
            value = protocol.get(name)
            if value is not None and (type(value) is not int or value < 1):
                raise HAAPIError("Invalid private Bronze protocol reference")
        protocol_pass = False
        if protocol_outcome == "pass":
            if (
                protocol.get("trace") != "traces/bronze_read_only"
                or protocol_digest is None
            ):
                raise HAAPIError("Bronze protocol evidence has no safe trace link")
            trace_dir = private / "traces" / "bronze_read_only"
            trace_meta = _read_json(trace_dir / "trace.json")
            trace_events = _read_jsonl(trace_dir / "events.jsonl")
            trace_frames = _read_jsonl(trace_dir / "frames.jsonl")
            trace = {**trace_meta, "events": trace_events, "frames": trace_frames}
            try:
                _validate_trace_result(
                    trace,
                    run_id=raw_run_id,
                    scenario_id="read_only_snapshot",
                    capture_id=trace_meta.get("capture_id"),
                    requested_route=trace_meta.get("requested_route"),
                    expected_version=2,
                )
            except (HAAPIError, TypeError, ValueError, KeyError) as exc:
                raise HAAPIError("Bronze protocol trace failed validation") from exc
            status_tx = next(
                (
                    frame
                    for frame in trace_frames
                    if frame.get("direction") == "tx"
                    and frame.get("command_type") == "status_request"
                    and frame.get("frame_validation") == "validated_tx"
                    and isinstance(frame.get("transaction_id"), str)
                    and frame["transaction_id"]
                ),
                None,
            )
            decoded = next(
                (
                    event
                    for event in trace_events
                    if event.get("event") == "notification_decoded"
                    and isinstance(event.get("decoded_packet_types"), list)
                    and "StatusData" in event["decoded_packet_types"]
                    and status_tx is not None
                    and event.get("session_generation")
                    == status_tx.get("session_generation")
                    and event.get("seq", 0) > status_tx.get("seq", 0)
                ),
                None,
            )
            status_write_ok = next(
                (
                    event
                    for event in trace_events
                    if event.get("event") == "write_ok"
                    and status_tx is not None
                    and event.get("session_generation")
                    == status_tx.get("session_generation")
                    and event.get("transaction_id") == status_tx.get("transaction_id")
                    and event.get("seq", 0) > status_tx.get("seq", 0)
                    and decoded is not None
                ),
                None,
            )
            protocol_pass = bool(
                trace_meta.get("complete") is True
                and trace_meta.get("dropped_events", 0) == 0
                and trace_meta.get("dropped_frames", 0) == 0
                and trace_meta.get("observed_route")
                in {"local_adapter", "active_proxy"}
                and status_tx is not None
                and status_tx.get("hex", "").lower() == BRONZE_STATUS_REQUEST
                and protocol.get("status_request_hex", "").lower()
                == BRONZE_STATUS_REQUEST
                and decoded is not None
                and status_write_ok is not None
                and hashlib.sha256((trace_dir / "trace.json").read_bytes()).hexdigest()
                == protocol_digest
                and protocol.get("route") == trace_meta.get("observed_route")
                and protocol.get("session_generation")
                == status_tx.get("session_generation")
                and protocol.get("status_request_seq") == status_tx.get("seq")
                and protocol.get("status_write_ok_seq") == status_write_ok.get("seq")
                and protocol.get("decoded_response_seq") == decoded.get("seq")
            )
            if not protocol_pass:
                raise HAAPIError("Bronze protocol evidence binding failed")
        files["tier-bronze-read-only.json"] = (
            json.dumps(
                {
                    "schema_version": PUBLIC_SCHEMA,
                    "sample_sha256": samples,
                    "samples": public_samples,
                    "fresh_verified_samples": bool(
                        bronze["fresh_verified_samples"] and samples_fresh
                    ),
                    "physical_observation": bronze["physical_observation"],
                    "protocol_probe": "verified" if protocol_pass else protocol_outcome,
                    "protocol_trace_sha256": protocol_digest if protocol_pass else None,
                    "protocol_status_request_hex": BRONZE_STATUS_REQUEST
                    if protocol_pass
                    else None,
                    "protocol_route": protocol_route if protocol_pass else None,
                    "protocol_session_generation": protocol.get("session_generation")
                    if protocol_pass
                    else None,
                    "protocol_status_request_seq": protocol.get("status_request_seq")
                    if protocol_pass
                    else None,
                    "protocol_status_write_ok_seq": protocol.get("status_write_ok_seq")
                    if protocol_pass
                    else None,
                    "protocol_decoded_response_seq": protocol.get(
                        "decoded_response_seq"
                    )
                    if protocol_pass
                    else None,
                    "protocol_capture_id": None,
                    "protocol_evidence": [],
                    "qualification_status": "inconclusive",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()
    omissions = [
        "raw_frame_payloads",
        "private_identifiers",
        "unvalidated_trace_correlations",
    ]
    if missing_sources:
        omissions.append("missing_run_metadata")
    capture_index = 0
    capture_stats: list[dict[str, Any]] = []
    trace_references: dict[
        str,
        tuple[list[str], bool, dict[int, dict[str, Any]], dict[int, dict[str, Any]]],
    ] = {}
    transaction_kind_by_trace_seq: dict[tuple[str, int], str] = {}
    scenario_source = _read_json(scenario_path) if scenario_path.exists() else {}
    trace_root = private / "traces"
    trace_dirs = (
        sorted(p for p in trace_root.glob("*") if p.is_dir())
        if trace_root.exists()
        else []
    )
    if len(trace_dirs) > 128:
        raise HAAPIError("Too many private HIL trace captures")
    for base in [private, *trace_dirs]:
        event_path = base / "events.jsonl"
        if not event_path.exists():
            continue
        trace_path = base / "trace.json"
        if not trace_path.exists():
            omissions.append("trace_metadata_unavailable")
            continue
        trace_meta = _read_json(trace_path)
        trace_scenario = trace_meta.get("scenario_id")
        if (
            type(trace_meta.get("schema_version")) is not int
            or trace_meta.get("schema_version") not in {1, 2}
            or trace_meta.get("run_id") != raw_run_id
            or not isinstance(raw_run_id, str)
            or re.fullmatch(r"[a-f0-9]{16}", raw_run_id) is None
            or not isinstance(trace_scenario, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", trace_scenario) is None
        ):
            raise HAAPIError("HIL trace identity does not match its run")
        frames_path = base / "frames.jsonl"
        trace_result = {
            **trace_meta,
            "events": _read_jsonl(event_path),
            "frames": _read_jsonl(frames_path) if frames_path.exists() else [],
        }
        try:
            _validate_trace_result(
                trace_result,
                run_id=raw_run_id,
                scenario_id=trace_scenario,
                capture_id=trace_meta.get("capture_id"),
                requested_route=trace_meta.get("requested_route"),
                expected_version=trace_meta.get("schema_version"),
                expected_scope=trace_meta.get("capture_scope", "runtime_entry"),
            )
        except (HAAPIError, TypeError, ValueError, KeyError) as exc:
            raise HAAPIError("Private HIL trace validation failed") from exc
        source_events = trace_result["events"]
        source_by_seq = {item["seq"]: item for item in source_events}
        source_frames = trace_result["frames"]
        frames_by_seq = {item["seq"]: item for item in source_frames}
        trace_name = f"traces/{base.name}"
        for step in scenario_source.get("steps", []):
            if not isinstance(step, dict):
                continue
            for trace_key, transaction_key in (
                ("trace", "transaction"),
                ("cleanup_trace", "cleanup_transaction"),
            ):
                transaction = step.get(transaction_key)
                if step.get(trace_key) != trace_name or not isinstance(
                    transaction, dict
                ):
                    continue
                seqs = _verified_transaction_references(
                    transaction, source_by_seq, frames_by_seq
                )
                for seq in seqs:
                    transaction_kind_by_trace_seq[(trace_name, seq)] = transaction[
                        "kind"
                    ]
        total_rows = len(source_events)
        capture_rows = []
        for item in source_events:
            seq, event = item["seq"], item["event"]
            if event in EVENT_NAMES:
                capture_rows.append(
                    (
                        seq,
                        (
                            "status_data_decoded"
                            if event == "notification_decoded"
                            and isinstance(item.get("decoded_packet_types"), list)
                            and "StatusData" in item["decoded_packet_types"]
                            else event
                        ),
                        item["utc"],
                        item["elapsed_ms"],
                        item["session_generation"],
                        item["observed_route"],
                    )
                )
        if (
            type(trace_meta.get("event_count")) is not int
            or trace_meta["event_count"] != total_rows
        ):
            raise HAAPIError("Private HIL trace event count mismatch")
        if len(capture_rows) != total_rows:
            omissions.append("unknown_event_names_omitted")
        if capture_rows:
            capture_index += 1
            route = trace_meta.get("observed_route")
            if route not in {"local_adapter", "active_proxy", "unverified", "mixed"}:
                route = "unverified"
            frame_count = trace_meta.get("frame_count", 0)
            dropped_events = trace_meta.get("dropped_events", 0)
            dropped_frames = trace_meta.get("dropped_frames", 0)
            if any(
                type(value) is not int or not 0 <= value <= 1_000_000
                for value in (frame_count, dropped_events, dropped_frames)
            ):
                raise HAAPIError("Invalid private HIL trace counters")
            complete = (
                (
                    trace_meta.get("complete") is True
                    or trace_meta.get("capture_complete") is True
                )
                and dropped_events == 0
                and dropped_frames == 0
                and trace_meta.get("event_count") == total_rows
                and total_rows == len(capture_rows)
            )
            public_capture_id = f"capture-{capture_index:04d}"
            capture_stats.append(
                {
                    "capture_id": public_capture_id,
                    "events": len(capture_rows),
                    "frames": 0,
                    "dropped_events": dropped_events,
                    "dropped_frames": dropped_frames,
                    "complete": complete,
                    "route": route,
                }
            )
            if base != private:
                trace_references[f"traces/{base.name}"] = (
                    [f"{public_capture_id}:{seq}" for seq, *_ in capture_rows],
                    not complete,
                    {item["seq"]: item for item in source_events},
                    {item["seq"]: item for item in source_frames},
                )
            rows = [
                {
                    "schema_version": PUBLIC_SCHEMA,
                    "capture_id": public_capture_id,
                    "seq": seq,
                    "event": event,
                    "layer": "gatt_api",
                    "utc": item_utc,
                    "elapsed_ms": elapsed_ms,
                    "session_generation": generation,
                    "observed_route": event_route,
                    "transaction_kind": item.get("transaction_kind")
                    if item.get("transaction_kind") in {"output", "settings"}
                    else transaction_kind_by_trace_seq.get(
                        (f"traces/{base.name}", seq)
                    ),
                }
                for seq, event, item_utc, elapsed_ms, generation, event_route in capture_rows
            ]
            existing = files.get("events.jsonl", b"")
            files["events.jsonl"] = existing + b"".join(
                (json.dumps(row, sort_keys=True) + "\n").encode() for row in rows
            )
            public_frames = []
            for frame in trace_result["frames"]:
                if frame.get("event") not in EVENT_NAMES:
                    continue
                public_frames.append(
                    {
                        "schema_version": PUBLIC_SCHEMA,
                        "capture_id": public_capture_id,
                        "seq": frame["seq"],
                        "direction": frame["direction"],
                        "command_type": frame["command_type"],
                        "frame_validation": frame["frame_validation"],
                        "session_generation": frame["session_generation"],
                        "payload_omitted": True,
                    }
                )
            capture_stats[-1]["frames"] = len(public_frames)
            existing_frames = files.get("frames.jsonl", b"")
            files["frames.jsonl"] = existing_frames + b"".join(
                (json.dumps(row, sort_keys=True) + "\n").encode()
                for row in public_frames
            )
    if scenario_path.exists():
        scenario = _read_json(scenario_path)
        safe_steps = []
        for step in scenario["steps"]:
            if not isinstance(step, dict):
                continue
            step_id, outcome = step.get("step_id"), step.get("outcome")
            if not isinstance(step_id, str) or step_id not in STEP_IDS:
                continue
            if not isinstance(outcome, str) or outcome not in OUTCOMES:
                continue
            safe_fields = _public_observation(scenario["scenario_id"], step)
            evidence: list[str] = []
            cleanup_evidence: list[str] = []
            missing_evidence = True
            cleanup_missing_evidence = True
            for trace_key, transaction_key, destination in (
                ("trace", "transaction", "primary"),
                ("cleanup_trace", "cleanup_transaction", "cleanup"),
            ):
                entry = trace_references.get(step.get(trace_key))
                if not entry:
                    continue
                all_refs, capture_incomplete, source_events, source_frames = entry
                seqs = _verified_transaction_references(
                    step.get(transaction_key), source_events, source_frames
                )
                if seqs and all_refs:
                    capture_id = all_refs[0].split(":", 1)[0]
                    refs = [f"{capture_id}:{seq}" for seq in seqs]
                    if destination == "primary":
                        evidence = refs
                        missing_evidence = capture_incomplete
                    else:
                        cleanup_evidence = refs
                        cleanup_missing_evidence = capture_incomplete
            missing_evidence = missing_evidence or not evidence
            cleanup_missing_evidence = cleanup_missing_evidence or not cleanup_evidence
            observation_evidence: list[str] = []
            observation_incomplete = True
            observation_trace = step.get("trace") or step.get("last_trace")
            observation_entry = trace_references.get(observation_trace)
            if observation_entry:
                all_refs, capture_incomplete, source_events, _ = observation_entry
                capture_id = all_refs[0].split(":", 1)[0] if all_refs else None
                if capture_id:
                    observation_evidence = [
                        f"{capture_id}:{seq}"
                        for seq, event in source_events.items()
                        if event.get("event") == "route_observed"
                    ]
                observation_incomplete = (
                    capture_incomplete
                    or step.get("trace_complete") is False
                    or not observation_evidence
                )
            safe_steps.append(
                {
                    "step_id": step_id,
                    "outcome": outcome,
                    **safe_fields,
                    "evidence": evidence,
                    "missing_evidence": missing_evidence,
                    "cleanup_evidence": cleanup_evidence,
                    "cleanup_missing_evidence": cleanup_missing_evidence,
                    "observation_evidence": observation_evidence,
                    "missing_observation_evidence": observation_incomplete,
                    "physical_observation": step.get("physical_outcome")
                    if isinstance(step.get("physical_outcome"), str)
                    and step["physical_outcome"] in {"yes", "no", "unable_to_observe"}
                    else None,
                    "cleanup_physical_observation": step.get("cleanup_physical_outcome")
                    if isinstance(step.get("cleanup_physical_outcome"), str)
                    and step["cleanup_physical_outcome"]
                    in {"yes", "no", "unable_to_observe"}
                    else None,
                    "started_at_utc": _safe_utc(step.get("started_utc")),
                    "ended_at_utc": _safe_utc(step.get("ended_utc")),
                    "elapsed_seconds": step.get("elapsed_seconds")
                    if type(step.get("elapsed_seconds")) in (int, float)
                    and 0 <= step["elapsed_seconds"] <= 86_400
                    and math.isfinite(step["elapsed_seconds"])
                    else None,
                }
            )
        files["scenarios.json"] = (
            json.dumps(
                {
                    "schema_version": PUBLIC_SCHEMA,
                    "source_schema_version": 2,
                    "scenario_id": scenario["scenario_id"],
                    "qualification_status": "inconclusive",
                    "steps": safe_steps,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()
    if "tier-bronze-read-only.json" in files:
        bronze_public = _load_public_json(files["tier-bronze-read-only.json"])
        if bronze_public.get("protocol_probe") == "verified":
            protocol_trace = trace_references.get("traces/bronze_read_only")
            if protocol_trace is not None:
                all_refs, capture_incomplete, source_events, source_frames = (
                    protocol_trace
                )
                capture_id = all_refs[0].split(":", 1)[0] if all_refs else None
                status_seq = bronze_public["protocol_status_request_seq"]
                write_ok_seq = bronze_public["protocol_status_write_ok_seq"]
                decoded_seq = bronze_public["protocol_decoded_response_seq"]
                status_event = source_events.get(status_seq)
                write_ok_event = source_events.get(write_ok_seq)
                status_frame = source_frames.get(status_seq)
                decoded_event = source_events.get(decoded_seq)
                if (
                    not capture_incomplete
                    and capture_id is not None
                    and isinstance(status_event, dict)
                    and status_event.get("event") == "write_attempt"
                    and isinstance(write_ok_event, dict)
                    and write_ok_event.get("event") == "write_ok"
                    and status_seq < write_ok_seq
                    and status_seq < decoded_seq
                    and isinstance(status_frame, dict)
                    and status_frame.get("direction") == "tx"
                    and status_frame.get("command_type") == "status_request"
                    and status_frame.get("frame_validation") == "validated_tx"
                    and isinstance(decoded_event, dict)
                    and decoded_event.get("event") == "notification_decoded"
                    and isinstance(decoded_event.get("decoded_packet_types"), list)
                    and "StatusData" in decoded_event["decoded_packet_types"]
                    and decoded_event.get("session_generation")
                    == status_event.get("session_generation")
                    and write_ok_event.get("session_generation")
                    == status_event.get("session_generation")
                ):
                    bronze_public["protocol_capture_id"] = capture_id
                    bronze_public["protocol_evidence"] = [
                        f"{capture_id}:{status_seq}",
                        f"{capture_id}:{write_ok_seq}",
                        f"{capture_id}:{decoded_seq}",
                    ]
                else:
                    bronze_public["protocol_probe"] = "inconclusive"
                    for key in (
                        "protocol_trace_sha256",
                        "protocol_status_request_hex",
                        "protocol_route",
                        "protocol_session_generation",
                        "protocol_status_request_seq",
                        "protocol_status_write_ok_seq",
                        "protocol_decoded_response_seq",
                    ):
                        bronze_public[key] = None
            else:
                bronze_public["protocol_probe"] = "inconclusive"
        files["tier-bronze-read-only.json"] = (
            json.dumps(bronze_public, sort_keys=True, indent=2) + "\n"
        ).encode()
    public_dir = run_dir / "public"
    safe_run_id = json.loads(files["session.json"]).get("data", {}).get("run_id")
    return (
        run_dir,
        files,
        {
            "omissions": sorted(set(omissions)),
            "capture_count": capture_index,
            "capture_stats": capture_stats,
            "public_dir": public_dir,
            "run_id": safe_run_id if isinstance(safe_run_id, str) else "unavailable",
        },
    )


def _render_report(
    run_id: str,
    environment: dict[str, Any],
    session: dict[str, Any],
    scenarios: dict[str, Any],
    event_count: int,
    capture_count: int,
    route_summary: str,
) -> bytes:
    env_data = environment.get("data", {})
    ha_facts = env_data.get("home_assistant", {})
    integration_facts = env_data.get("integration", {})
    device = session.get("data", {}).get("device", {})
    session_data = session.get("data", {})
    model = device.get("model", {}).get("value", "unavailable")
    firmware = device.get("firmware_version", {}).get("value", "unavailable")
    hardware = device.get("hardware_version", {}).get("value", "unavailable")
    read_only_text = ""
    if session_data.get("mode") == "read_only_preflight":
        route = session_data.get("route", {})
        preflight = session_data.get("preflight", {})
        checks = preflight.get("checks", {})
        telemetry = session_data.get("telemetry", {})
        runtime = session_data.get("runtime_capabilities", {})
        check_text = (
            ", ".join(f"{name} `{checks[name]}`" for name in sorted(checks))
            or "unavailable"
        )
        telemetry_text = (
            ", ".join(
                f"{key} `{record['value'] if record['value'] is not None else 'unavailable'}`"
                for key, record in sorted(telemetry.items())
            )
            or "unavailable"
        )
        capability_text = (
            ", ".join(
                f"{name} `{value}`"
                for name, value in sorted(runtime.get("capabilities", {}).items())
            )
            or "unavailable"
        )
        read_only_text = (
            "## Initial read-only preflight snapshot\n\n"
            + f"- Snapshot type: `{session_data['mode']}`\n"
            + f"- Requested route: `{route.get('requested', 'unavailable')}`\n"
            + f"- Preflight: `{preflight.get('overall', 'inconclusive')}`; {check_text}\n"
            + f"- Telemetry: {telemetry_text}\n"
            + f"- Runtime capabilities available: `{runtime.get('available', False)}`; "
            + f"connected `{runtime.get('connected', 'unavailable')}`, "
            + f"status fresh `{runtime.get('status_fresh', 'unavailable')}`, "
            + f"settings fresh `{runtime.get('settings_fresh', 'unavailable')}`; "
            + f"capabilities {capability_text}\n\n"
        )
    step_lines = []
    for row in scenarios.get("steps", []):
        refs = ", ".join(f"`{ref}`" for ref in row["evidence"])
        evidence_text = refs if refs else "evidence unavailable"
        if row["missing_evidence"]:
            evidence_text += " (incomplete)"
        cleanup_refs = ", ".join(f"`{ref}`" for ref in row["cleanup_evidence"])
        if cleanup_refs or row["cleanup_missing_evidence"]:
            evidence_text += "; cleanup evidence " + (
                cleanup_refs if cleanup_refs else "unavailable"
            )
            if row["cleanup_missing_evidence"]:
                evidence_text += " (incomplete)"
        if row["observation_evidence"] or row["missing_observation_evidence"]:
            observation_refs = ", ".join(
                f"`{ref}`" for ref in row["observation_evidence"]
            )
            evidence_text += "; observation evidence " + (
                observation_refs if observation_refs else "unavailable"
            )
            if row["missing_observation_evidence"]:
                evidence_text += " (incomplete)"
        if row["expected"] is not None:
            evidence_text += f"; expected `{row['expected']}`"
        if row["actual"] is not None:
            evidence_text += f"; observed `{row['actual']}`"
        if row["target"] is not None:
            evidence_text += f"; target `{row['target']}`"
        if row["observation"]:
            summary = ", ".join(
                f"{key} `{value}`" for key, value in sorted(row["observation"].items())
            )
            evidence_text += f"; observations {summary}"
        if row["limitation_reason"] is not None:
            evidence_text += f"; limitation `{row['limitation_reason']}`"
        if row["physical_observation"] is not None:
            evidence_text += f"; physical observation `{row['physical_observation']}`"
        if row["cleanup_physical_observation"] is not None:
            evidence_text += (
                f"; cleanup observation `{row['cleanup_physical_observation']}`"
            )
        if row["started_at_utc"] is not None:
            evidence_text += f"; started `{row['started_at_utc']}`"
        if row["ended_at_utc"] is not None:
            evidence_text += f"; ended `{row['ended_at_utc']}`"
        if row["elapsed_seconds"] is not None:
            evidence_text += f"; elapsed {row['elapsed_seconds']} s"
        step_lines.append(
            f"- `{row['step_id']}`: **{row['outcome']}**; {evidence_text}"
        )
    steps_text = (
        "\n".join(step_lines)
        if step_lines
        else "No correlated scenario steps were recorded."
    )
    report = (
        "# HIL evidence report\n\n"
        + f"Run: `{run_id}`\n\n"
        + (
            f"Scenario: `{scenarios.get('scenario_id', 'unavailable')}`\n\n"
            if scenarios.get("scenario_id")
            else ""
        )
        + "Qualification: **inconclusive**. This report summarizes collected software evidence and does not qualify hardware or prove RF behavior.\n\n"
        + "## Environment\n\n"
        + f"- Home Assistant: {ha_facts.get('version', {}).get('value', 'unavailable')}\n"
        + f"- Integration manifest version: {integration_facts.get('installed_manifest_version', {}).get('value', 'unavailable')} (installed build identity {integration_facts.get('build_match', {}).get('value', 'unverified')})\n"
        + f"- Installed integration commit: {integration_facts.get('installed_commit', {}).get('value') or integration_facts.get('verification_reason', {}).get('value', 'unverified')}\n"
        + f"- Device: {model}; hardware {hardware}; firmware {firmware}\n"
        + f"- Observed GATT route: {route_summary}\n\n"
        + read_only_text
        + "## Scenario results\n\n"
        + steps_text
        + f"\n\nGATT evidence: {event_count} allowlisted events across {capture_count} captures.\n\n"
        + "## Limitations\n\n- Event payloads and frame bytes are omitted by policy.\n- Missing or uncorrelated observations remain inconclusive.\n- Review `hil-evidence.zip` locally before attaching it manually to an issue or PR.\n"
    )
    return report.encode()


def write_report(run_path: Path) -> Path:
    _, files, meta = build_public(run_path)
    for name, content in files.items():
        _atomic_write(meta["public_dir"] / name, content)
    scenarios = (
        json.loads(files["scenarios.json"])
        if "scenarios.json" in files
        else {"steps": []}
    )
    environment = json.loads(files["environment.json"])
    session = json.loads(files["session.json"])
    event_count = len(files.get("events.jsonl", b"").splitlines())
    routes = {row["route"] for row in meta["capture_stats"]}
    route_summary = (
        next(iter(routes)) if len(routes) == 1 else "mixed" if routes else "unverified"
    )
    report = _render_report(
        meta["run_id"],
        environment,
        session,
        scenarios,
        event_count,
        meta["capture_count"],
        route_summary,
    )
    _atomic_write(meta["public_dir"] / "report.md", report)
    all_files = {**files, "report.md": report}
    manifest = _write_manifest(meta["public_dir"], all_files, meta, exported=False)
    _atomic_write(
        meta["public_dir"] / "manifest.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    )
    return meta["public_dir"] / "report.md"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        path.parent.is_symlink()
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
        or path.is_symlink()
    ):
        raise HAAPIError("Unsafe HIL public output path")
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _write_manifest(
    public_dir: Path, files: dict[str, bytes], meta: dict[str, Any], *, exported: bool
) -> dict[str, Any]:
    environment = json.loads(files["environment.json"])
    integration = environment.get("data", {}).get("integration", {})

    def fact_value(name: str) -> str | None:
        fact = integration.get(name)
        value = fact.get("value") if isinstance(fact, dict) else None
        return value if isinstance(value, str) else None

    manifest = {
        "schema_version": PUBLIC_SCHEMA,
        "run_id": meta["run_id"],
        "toolkit_version": "unversioned",
        "toolkit_schema_version": PUBLIC_SCHEMA,
        "build_provenance": {
            "installed_manifest_version": fact_value("installed_manifest_version"),
            "verification": fact_value("build_match") or "unverified",
            "runner_checkout_commit": fact_value("runner_checkout_commit"),
            "installed_commit": fact_value("installed_commit"),
            "installed_fingerprint": fact_value("installed_fingerprint"),
            "checkout_fingerprint": fact_value("checkout_fingerprint"),
            "verification_reason": fact_value("verification_reason"),
        },
        "privacy_policy": "allowlist",
        "validation": "passed",
        "export_confirmed": exported,
        "qualification_status": "inconclusive",
        "omissions": meta["omissions"],
        "captures": {
            "count": meta["capture_count"],
            "events": sum(1 for _ in files.get("events.jsonl", b"").splitlines()),
            "frames": sum(row["frames"] for row in meta["capture_stats"]),
            "dropped_events": sum(
                row["dropped_events"] for row in meta["capture_stats"]
            ),
            "dropped_frames": sum(
                row["dropped_frames"] for row in meta["capture_stats"]
            ),
            "complete": bool(meta["capture_stats"])
            and all(row["complete"] for row in meta["capture_stats"]),
            "by_capture": meta["capture_stats"],
        },
        "files": [
            {"name": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            for n, b in sorted(files.items())
        ],
    }
    return manifest


def _valid_public_facts(groups: dict[str, Any]) -> bool:
    fact_names = {
        "version",
        "installation_type",
        "os",
        "architecture",
        "kernel",
        "cpu",
        "python_version",
        "installed_manifest_version",
        "build_match",
        "runner_checkout_commit",
        "installed_commit",
        "installed_fingerprint",
        "checkout_fingerprint",
        "verification_reason",
    }
    sources = {
        "runner_platform",
        "runner_runtime",
        "ha_rest_config",
        "ha_ws_manifest_get",
        "runner_repository_manifest",
        "runner_git_checkout",
        "ha_ws_hil_build",
        "runner_integration_files",
        "runner_build_verification",
        "unknown",
    }
    if set(groups) - {"home_assistant", "runner_host", "integration"}:
        return False
    for group in groups.values():
        if not isinstance(group, dict) or set(group) - fact_names:
            return False
        for fact_name, fact in group.items():
            if not isinstance(fact, dict) or set(fact) != {
                "value",
                "source",
                "verified",
            }:
                return False
            if (
                fact.get("source") not in sources
                or type(fact.get("verified")) is not bool
            ):
                return False
            if not _safe_fact_value(fact_name, fact["value"]):
                return False
    return True


def verify_public_archive(
    path: Path, *, include_contents: bool = False
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, bytes], str]:
    """Validate the public archive, optionally returning the verified member bytes."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise HAAPIError("Unsafe HIL archive path")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ARCHIVE:
        raise HAAPIError("Invalid HIL archive")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as archive_file:
            if not stat.S_ISREG(os.fstat(archive_file.fileno()).st_mode):
                raise HAAPIError("Invalid HIL archive file")
            archive_bytes = archive_file.read(MAX_ARCHIVE + 1)
            if len(archive_bytes) > MAX_ARCHIVE:
                raise HAAPIError("Invalid HIL archive")
            archive_digest = hashlib.sha256(archive_bytes).hexdigest()
            archive_file.seek(0)
            with zipfile.ZipFile(archive_file) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if (
                    len(infos) > MAX_MEMBERS
                    or len(set(names)) != len(names)
                    or any(
                        n.startswith("/")
                        or ".." in Path(n).parts
                        or "\\" in n
                        or n != "public/" + n.removeprefix("public/")
                        or n.removeprefix("public/") not in PUBLIC_NAMES
                        for n in names
                    )
                ):
                    raise HAAPIError("Unsafe HIL archive members")
                if sum(i.file_size for i in infos) > MAX_ARCHIVE or any(
                    i.file_size > MAX_FILE for i in infos
                ):
                    raise HAAPIError("HIL archive exceeds size limits")
                if any(stat.S_ISLNK(i.external_attr >> 16) for i in infos):
                    raise HAAPIError("Symbolic links are forbidden in HIL archives")
                data = {
                    i.filename.removeprefix("public/"): archive.read(i) for i in infos
                }
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise HAAPIError("Invalid HIL archive") from exc
    if "manifest.json" not in data:
        raise HAAPIError("HIL archive manifest missing")
    try:
        manifest = _load_public_json(data["manifest.json"])
    except (UnicodeError, ValueError) as exc:
        raise HAAPIError("Invalid HIL archive manifest") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema_version",
            "run_id",
            "toolkit_version",
            "toolkit_schema_version",
            "build_provenance",
            "privacy_policy",
            "validation",
            "export_confirmed",
            "qualification_status",
            "omissions",
            "captures",
            "files",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != PUBLIC_SCHEMA
        or type(manifest.get("toolkit_schema_version")) is not int
        or manifest.get("toolkit_schema_version") != PUBLIC_SCHEMA
        or manifest.get("toolkit_version") != "unversioned"
        or manifest.get("privacy_policy") != "allowlist"
        or manifest.get("validation") != "passed"
        or manifest.get("export_confirmed") is not True
        or manifest.get("qualification_status") != "inconclusive"
        or not isinstance(manifest.get("omissions"), list)
        or any(
            not isinstance(item, str)
            or item
            not in {
                "raw_frame_payloads",
                "private_identifiers",
                "unvalidated_trace_correlations",
                "missing_run_metadata",
                "trace_metadata_unavailable",
                "unknown_event_names_omitted",
                "unknown_source_fields",
            }
            for item in manifest["omissions"]
        )
    ):
        raise HAAPIError("Unsupported HIL archive manifest")
    if manifest.get("run_id") != "unavailable" and (
        not isinstance(manifest.get("run_id"), str)
        or re.fullmatch(r"[a-f0-9]{16}", manifest["run_id"]) is None
    ):
        raise HAAPIError("Invalid HIL archive run identity")
    build = manifest.get("build_provenance")
    legacy_build_keys = {
        "installed_manifest_version",
        "verification",
        "runner_checkout_commit",
    }
    current_build_keys = legacy_build_keys | {
        "installed_commit",
        "installed_fingerprint",
        "checkout_fingerprint",
        "verification_reason",
    }
    if (
        not isinstance(build, dict)
        or frozenset(build)
        not in {frozenset(legacy_build_keys), frozenset(current_build_keys)}
        or not isinstance(build.get("verification"), str)
        or build["verification"] not in {"verified", "unverified", "mismatch"}
        or (
            build["installed_manifest_version"] is not None
            and not _safe_fact_value(
                "installed_manifest_version", build["installed_manifest_version"]
            )
        )
        or (
            build["runner_checkout_commit"] is not None
            and not _safe_fact_value(
                "runner_checkout_commit", build["runner_checkout_commit"]
            )
        )
        or any(
            build[name] is not None and not _safe_fact_value(name, build[name])
            for name in current_build_keys - legacy_build_keys
            if name in build
        )
        or (
            "verification_reason" in build
            and build["verification_reason"] is not None
            and not _safe_fact_value(
                "verification_reason", build["verification_reason"]
            )
        )
    ):
        raise HAAPIError("Invalid HIL archive build provenance")
    try:
        environment_data = _load_public_json(data["environment.json"]).get("data", {})
        integration_facts = environment_data.get("integration", {})
    except (KeyError, AttributeError, UnicodeError, ValueError) as exc:
        raise HAAPIError("HIL archive build provenance has no environment") from exc
    if not isinstance(environment_data, dict) or not isinstance(
        integration_facts, dict
    ):
        raise HAAPIError("Invalid HIL archive integration facts")

    def environment_fact_value(name: str) -> str | None:
        fact = integration_facts.get(name)
        value = fact.get("value") if isinstance(fact, dict) else None
        return value if isinstance(value, str) else None

    expected_build = {
        "installed_manifest_version": environment_fact_value(
            "installed_manifest_version"
        ),
        "verification": environment_fact_value("build_match") or "unverified",
        "runner_checkout_commit": environment_fact_value("runner_checkout_commit"),
        "installed_commit": environment_fact_value("installed_commit"),
        "installed_fingerprint": environment_fact_value("installed_fingerprint"),
        "checkout_fingerprint": environment_fact_value("checkout_fingerprint"),
        "verification_reason": integration_facts.get("verification_reason", {}).get(
            "value"
        )
        if isinstance(integration_facts.get("verification_reason"), dict)
        else None,
    }
    if set(build) == legacy_build_keys:
        expected_build = {key: expected_build[key] for key in legacy_build_keys}
    if build != expected_build:
        raise HAAPIError("HIL archive build provenance disagrees with environment")
    captures = manifest.get("captures")
    capture_fields = {
        "count",
        "events",
        "frames",
        "dropped_events",
        "dropped_frames",
        "complete",
        "by_capture",
    }
    if (
        not isinstance(captures, dict)
        or set(captures) != capture_fields
        or any(
            type(captures.get(key)) is not int or captures[key] < 0
            for key in capture_fields - {"complete", "by_capture"}
        )
        or type(captures.get("complete")) is not bool
        or not isinstance(captures.get("by_capture"), list)
    ):
        raise HAAPIError("Invalid HIL archive counters")
    for capture in captures["by_capture"]:
        if (
            not isinstance(capture, dict)
            or set(capture)
            != {
                "capture_id",
                "events",
                "frames",
                "dropped_events",
                "dropped_frames",
                "complete",
                "route",
            }
            or not isinstance(capture.get("capture_id"), str)
            or re.fullmatch(r"capture-[0-9]{4}", capture["capture_id"]) is None
            or any(
                type(capture.get(key)) is not int or capture[key] < 0
                for key in ("events", "frames", "dropped_events", "dropped_frames")
            )
            or type(capture.get("complete")) is not bool
            or not isinstance(capture.get("route"), str)
            or capture.get("route")
            not in {"local_adapter", "active_proxy", "unverified", "mixed"}
        ):
            raise HAAPIError("Invalid HIL archive capture counters")
        if capture["complete"] and (
            capture["dropped_events"] > 0 or capture["dropped_frames"] > 0
        ):
            raise HAAPIError("Complete HIL capture declares dropped events or frames")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list) or any(
        not isinstance(item, dict) or set(item) != {"name", "size", "sha256"}
        for item in manifest_files
    ):
        raise HAAPIError("Invalid HIL archive file manifest")
    listed = {item["name"]: item for item in manifest_files}
    if len(listed) != len(manifest_files):
        raise HAAPIError("Duplicate HIL archive file manifest entry")
    if set(listed) != set(data) - {"manifest.json"}:
        raise HAAPIError("HIL archive file list mismatch")
    for name, item in listed.items():
        if (
            item.get("size") != len(data[name])
            or item.get("sha256") != hashlib.sha256(data[name]).hexdigest()
        ):
            raise HAAPIError("HIL archive digest mismatch")
        if name.endswith(".json"):
            try:
                value = _load_public_json(data[name])
            except (UnicodeError, ValueError) as exc:
                raise HAAPIError("Malformed JSON in HIL archive") from exc
            if (
                not isinstance(value, dict)
                or type(value.get("schema_version")) is not int
                or value["schema_version"] != PUBLIC_SCHEMA
            ):
                raise HAAPIError("Unsupported JSON schema in HIL archive")
            if name in {"environment.json", "session.json"}:
                expected = {"schema_version", "source_schema_version", "data"}
                if set(value) not in (expected, expected | {"omitted"}):
                    raise HAAPIError("Unexpected public JSON fields")
                source_schema = value.get("source_schema_version")
                if source_schema is not None and (
                    type(source_schema) is not int or source_schema != 1
                ):
                    raise HAAPIError("Unsupported public source schema")
                if "omitted" in value and value["omitted"] != ["unknown_source_fields"]:
                    raise HAAPIError("Invalid public omission marker")
                if not isinstance(value["data"], dict):
                    raise HAAPIError("Invalid public JSON data")
                allowed = (
                    {"home_assistant", "runner_host", "integration"}
                    if name == "environment.json"
                    else {
                        "run_id",
                        "scenario_id",
                        "qualification_status",
                        "mode",
                        "route",
                        "preflight",
                        "telemetry",
                        "runtime_capabilities",
                        "test_plan",
                        "started_at",
                        "device",
                    }
                )
                if set(value["data"]) - allowed:
                    raise HAAPIError("Unexpected public data fields")
                if name == "environment.json" and not _valid_public_facts(
                    value["data"]
                ):
                    raise HAAPIError("Unsafe environment values in archive")
                if (
                    name == "session.json"
                    and value["data"].get("qualification_status") != "inconclusive"
                ):
                    raise HAAPIError("Invalid public qualification status")
                if name == "session.json":
                    public_session = value["data"]
                    run_id = public_session.get("run_id")
                    if run_id is not None and (
                        not isinstance(run_id, str)
                        or re.fullmatch(r"[a-f0-9]{16}", run_id) is None
                    ):
                        raise HAAPIError("Invalid public run identity")
                    scenario_id = public_session.get("scenario_id")
                    if scenario_id is not None and (
                        not isinstance(scenario_id, str)
                        or scenario_id not in SCENARIO_IDS
                    ):
                        raise HAAPIError("Invalid public scenario identity")
                    if public_session.get("mode") not in (
                        None,
                        "read_only_preflight",
                    ):
                        raise HAAPIError("Invalid public execution mode")
                    if "device" in public_session and not isinstance(
                        public_session["device"], dict
                    ):
                        raise HAAPIError("Invalid public device metadata")
                    if (
                        "started_at" in public_session
                        and _safe_utc(public_session["started_at"]) is None
                    ):
                        raise HAAPIError("Invalid public session timestamp")
                    if "route" in public_session:
                        route = public_session["route"]
                        observed = (
                            route.get("observed") if isinstance(route, dict) else None
                        )
                        if (
                            not isinstance(route, dict)
                            or not route
                            or set(route) - {"observed", "requested"}
                            or (
                                "requested" in route
                                and (
                                    not isinstance(route["requested"], str)
                                    or route["requested"] not in REQUESTED_ROUTES
                                )
                            )
                            or (
                                "observed" in route
                                and (
                                    not isinstance(observed, dict)
                                    or set(observed) != {"value", "verified"}
                                    or not isinstance(observed.get("value"), str)
                                    or observed.get("value")
                                    not in {
                                        "local_adapter",
                                        "active_proxy",
                                        "unverified",
                                    }
                                    or type(observed.get("verified")) is not bool
                                )
                            )
                        ):
                            raise HAAPIError("Invalid public route metadata")
                    if "preflight" in public_session:
                        preflight = public_session["preflight"]
                        if (
                            not isinstance(preflight, dict)
                            or set(preflight) - {"overall", "checks"}
                            or "overall" not in preflight
                            or not isinstance(preflight.get("overall"), str)
                            or preflight.get("overall")
                            not in {"ready", "incomplete", "inconclusive"}
                            or (
                                "checks" in preflight
                                and (
                                    not isinstance(preflight["checks"], dict)
                                    or set(preflight["checks"]) - PREFLIGHT_CHECKS
                                    or any(
                                        not isinstance(status, str)
                                        or status not in PREFLIGHT_RESULTS
                                        for status in preflight["checks"].values()
                                    )
                                )
                            )
                        ):
                            raise HAAPIError("Invalid public preflight metadata")
                    if "telemetry" in public_session:
                        telemetry = public_session["telemetry"]
                        if not isinstance(telemetry, dict) or set(telemetry) - set(
                            SENSOR_KEYS + BINARY_KEYS
                        ):
                            raise HAAPIError("Invalid public telemetry metadata")
                        for key, fact in telemetry.items():
                            if (
                                not isinstance(fact, dict)
                                or set(fact) != {"value", "verified"}
                                or type(fact.get("verified")) is not bool
                                or _safe_read_only_value(key, fact.get("value"))
                                != fact.get("value")
                                or fact["verified"] != (fact["value"] is not None)
                            ):
                                raise HAAPIError("Invalid public telemetry value")
                    if "runtime_capabilities" in public_session:
                        runtime = public_session["runtime_capabilities"]
                        runtime_fields = {
                            "available",
                            "connected",
                            "status_fresh",
                            "settings_fresh",
                            "pending_transaction",
                            "capabilities",
                        }
                        optional_bools = {
                            "connected",
                            "status_fresh",
                            "settings_fresh",
                            "pending_transaction",
                        }
                        if (
                            not isinstance(runtime, dict)
                            or set(runtime) != runtime_fields
                            or type(runtime.get("available")) is not bool
                            or any(
                                runtime.get(name) is not None
                                and type(runtime[name]) is not bool
                                for name in optional_bools
                            )
                            or not isinstance(runtime.get("capabilities"), dict)
                            or set(runtime["capabilities"]) - RUNTIME_CAPABILITIES
                            or any(
                                type(flag) is not bool
                                for flag in runtime["capabilities"].values()
                            )
                            or (
                                not runtime["available"]
                                and (
                                    runtime["capabilities"]
                                    or any(
                                        runtime[name] is not None
                                        for name in optional_bools
                                    )
                                )
                            )
                        ):
                            raise HAAPIError("Invalid public runtime capabilities")
                    if (
                        "test_plan" in public_session
                        and _public_tier_plan(public_session["test_plan"])
                        != public_session["test_plan"]
                    ):
                        raise HAAPIError("Invalid public guided tier plan")
                    device = public_session.get("device", {})
                    if set(device) - {"model", "hardware_version", "firmware_version"}:
                        raise HAAPIError("Unexpected public device metadata")
                    for key, fact in device.items():
                        if (
                            not isinstance(fact, dict)
                            or set(fact) != {"value", "source", "verified"}
                            or fact.get("source") != "ha_device_registry"
                            or type(fact.get("verified")) is not bool
                            or (
                                key == "model"
                                and (
                                    not isinstance(fact.get("value"), str)
                                    or safe_model(fact["value"]) != fact["value"]
                                )
                            )
                            or (
                                key != "model"
                                and safe_version(fact.get("value")) != fact.get("value")
                            )
                        ):
                            raise HAAPIError("Unsafe public device values")
            elif name == "scenarios.json":
                if (
                    set(value)
                    != {
                        "schema_version",
                        "source_schema_version",
                        "scenario_id",
                        "qualification_status",
                        "steps",
                    }
                    or value["qualification_status"] != "inconclusive"
                    or not isinstance(value["steps"], list)
                    or value.get("source_schema_version") != 2
                    or value.get("scenario_id") not in SCENARIO_IDS
                ):
                    raise HAAPIError("Invalid public scenario schema")
                for row in value["steps"]:
                    if (
                        not isinstance(row, dict)
                        or set(row)
                        != {
                            "step_id",
                            "outcome",
                            "target",
                            "expected",
                            "actual",
                            "observation",
                            "limitation_reason",
                            "evidence",
                            "missing_evidence",
                            "cleanup_evidence",
                            "cleanup_missing_evidence",
                            "observation_evidence",
                            "missing_observation_evidence",
                            "physical_observation",
                            "cleanup_physical_observation",
                            "started_at_utc",
                            "ended_at_utc",
                            "elapsed_seconds",
                        }
                        or not isinstance(row.get("step_id"), str)
                        or row.get("step_id") not in STEP_IDS
                        or not isinstance(row.get("outcome"), str)
                        or row.get("outcome") not in OUTCOMES
                        or (
                            row.get("target") is not None
                            and (
                                not isinstance(row["target"], str)
                                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", row["target"])
                                is None
                            )
                        )
                        or (
                            row.get("expected") is not None
                            and (
                                not isinstance(row["expected"], str)
                                or len(row["expected"]) > 128
                            )
                        )
                        or row.get("actual")
                        not in {
                            None,
                            "on",
                            "off",
                            "two_hours",
                            "standard",
                            "mute",
                            "performance",
                            "sleep",
                            "auto",
                            "unknown",
                            "unavailable",
                            "yes",
                            "no",
                            "unable_to_observe",
                            "reconnected",
                            "ha_stable",
                            "ha_recovered",
                            "observation_incomplete",
                            "completed",
                            "pass",
                            "fail",
                            "inconclusive",
                        }
                        or not _valid_public_observation(row.get("observation"))
                        or row.get("limitation_reason")
                        not in {
                            None,
                            "restart_discontinuity",
                            "operator_interruption_or_decline",
                            "storage_limit",
                            "trace_unavailable",
                            "observation_unavailable",
                            "precondition_or_observation_incomplete",
                        }
                        or not isinstance(row.get("evidence"), list)
                        or any(
                            not isinstance(ref, str)
                            or re.fullmatch(r"capture-[0-9]{4}:[1-9][0-9]{0,6}", ref)
                            is None
                            for ref in row["evidence"]
                        )
                        or type(row.get("missing_evidence")) is not bool
                        or (not row["evidence"] and not row["missing_evidence"])
                        or not isinstance(row.get("cleanup_evidence"), list)
                        or any(
                            not isinstance(ref, str)
                            or re.fullmatch(r"capture-[0-9]{4}:[1-9][0-9]{0,6}", ref)
                            is None
                            for ref in row["cleanup_evidence"]
                        )
                        or type(row.get("cleanup_missing_evidence")) is not bool
                        or (
                            not row["cleanup_evidence"]
                            and not row["cleanup_missing_evidence"]
                        )
                        or not isinstance(row.get("observation_evidence"), list)
                        or any(
                            not isinstance(ref, str)
                            or re.fullmatch(r"capture-[0-9]{4}:[1-9][0-9]{0,6}", ref)
                            is None
                            for ref in row["observation_evidence"]
                        )
                        or type(row.get("missing_observation_evidence")) is not bool
                        or (
                            not row["observation_evidence"]
                            and not row["missing_observation_evidence"]
                        )
                        or (
                            row.get("physical_observation") is not None
                            and (
                                not isinstance(row["physical_observation"], str)
                                or row["physical_observation"]
                                not in {"yes", "no", "unable_to_observe"}
                            )
                        )
                        or (
                            row.get("cleanup_physical_observation") is not None
                            and (
                                not isinstance(row["cleanup_physical_observation"], str)
                                or row["cleanup_physical_observation"]
                                not in {"yes", "no", "unable_to_observe"}
                            )
                        )
                        or (
                            row.get("started_at_utc") is not None
                            and _safe_utc(row["started_at_utc"])
                            != row["started_at_utc"]
                        )
                        or (
                            row.get("ended_at_utc") is not None
                            and _safe_utc(row["ended_at_utc"]) != row["ended_at_utc"]
                        )
                        or (
                            row.get("elapsed_seconds") is not None
                            and (
                                type(row["elapsed_seconds"]) not in (int, float)
                                or not 0 <= row["elapsed_seconds"] <= 86_400
                                or not math.isfinite(row["elapsed_seconds"])
                            )
                        )
                    ):
                        raise HAAPIError("Invalid public scenario row")
            elif name == "tier-execution.json":
                if (
                    set(value)
                    != {
                        "schema_version",
                        "requested_tier",
                        "attained_tier",
                        "review_state",
                        "qualification_status",
                        "steps",
                    }
                    or value.get("requested_tier") not in {"bronze", "silver", "gold"}
                    or value.get("attained_tier") is not None
                    or value.get("review_state") != "pending"
                    or value.get("qualification_status") != "inconclusive"
                    or not isinstance(value.get("steps"), list)
                    or len(value["steps"]) > 32
                ):
                    raise HAAPIError("Invalid public guided tier execution")
                for row in value["steps"]:
                    hashes = (
                        row.get("machine_evidence_sha256")
                        if isinstance(row, dict)
                        else None
                    )
                    if (
                        not isinstance(row, dict)
                        or set(row)
                        - {
                            "step_id",
                            "outcome",
                            "human_review",
                            "machine_evidence_sha256",
                            "machine_evidence",
                        }
                        or row.get("step_id") not in TIER_STEP_IDS
                        or row.get("outcome")
                        not in OUTCOMES
                        | {"blocked", "pending_review", "shared_evidence"}
                        or row.get("human_review") not in {None, "pending", "reviewed"}
                        or not isinstance(hashes, list)
                        or not isinstance(row.get("machine_evidence"), list)
                        or len(row["machine_evidence"]) > 32
                        or len(hashes) > 32
                        or any(
                            not isinstance(digest, str)
                            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                            for digest in hashes
                        )
                    ):
                        raise HAAPIError("Invalid public guided tier step")
                    references = row["machine_evidence"]
                    if any(
                        not isinstance(reference, dict)
                        or set(reference) != {"artifact_id", "source_sha256"}
                        or not isinstance(reference.get("artifact_id"), str)
                        or re.fullmatch(
                            r"step-[0-9]{2}-evidence-[0-9]{2}",
                            reference["artifact_id"],
                        )
                        is None
                        or not isinstance(reference.get("source_sha256"), str)
                        or re.fullmatch(r"[a-f0-9]{64}", reference["source_sha256"])
                        is None
                        for reference in references
                    ) or sorted(
                        {item["source_sha256"] for item in references}
                    ) != sorted(set(hashes)):
                        raise HAAPIError("Invalid public guided evidence references")
            elif name == "tier-artifacts.json":
                if (
                    set(value) != {"schema_version", "artifacts"}
                    or not isinstance(value.get("artifacts"), list)
                    or len(value["artifacts"]) > 1024
                ):
                    raise HAAPIError("Invalid public guided artifacts")
                artifact_ids = []
                for artifact in value["artifacts"]:
                    try:
                        safe_content = (
                            isinstance(artifact, dict)
                            and isinstance(artifact.get("content"), dict)
                            and _valid_public_tier_artifact(artifact["content"])
                        )
                    except TypeError, ValueError:
                        safe_content = False
                    if (
                        not isinstance(artifact, dict)
                        or set(artifact) != {"artifact_id", "source_sha256", "content"}
                        or not isinstance(artifact.get("artifact_id"), str)
                        or re.fullmatch(
                            r"step-[0-9]{2}-evidence-[0-9]{2}",
                            artifact["artifact_id"],
                        )
                        is None
                        or not isinstance(artifact.get("source_sha256"), str)
                        or re.fullmatch(r"[a-f0-9]{64}", artifact["source_sha256"])
                        is None
                        or not isinstance(artifact.get("content"), dict)
                        or not safe_content
                        or artifact["content"].get("kind")
                        not in {
                            "gatt_trace_metadata",
                            "gatt_events",
                            "gatt_frames",
                            "scenario_checkpoint",
                            "guided_step_evidence",
                        }
                    ):
                        raise HAAPIError("Invalid public guided artifact entry")
                    artifact_ids.append(artifact["artifact_id"])
                if len(artifact_ids) != len(set(artifact_ids)):
                    raise HAAPIError("Duplicate public guided artifact reference")
            elif name == "tier-bronze-read-only.json":
                if (
                    set(value)
                    != {
                        "schema_version",
                        "sample_sha256",
                        "samples",
                        "fresh_verified_samples",
                        "physical_observation",
                        "protocol_probe",
                        "protocol_trace_sha256",
                        "protocol_status_request_hex",
                        "protocol_route",
                        "protocol_session_generation",
                        "protocol_status_request_seq",
                        "protocol_status_write_ok_seq",
                        "protocol_decoded_response_seq",
                        "protocol_capture_id",
                        "protocol_evidence",
                        "qualification_status",
                    }
                    or not isinstance(value.get("sample_sha256"), list)
                    or len(value["sample_sha256"]) != 2
                    or any(
                        not isinstance(digest, str)
                        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                        for digest in value["sample_sha256"]
                    )
                    or type(value.get("fresh_verified_samples")) is not bool
                    or not isinstance(value.get("samples"), list)
                    or len(value["samples"]) not in {0, 2}
                    or any(
                        not isinstance(sample, dict)
                        or set(sample) != {"sampled_at_utc", "telemetry"}
                        or _safe_utc(sample.get("sampled_at_utc"))
                        != sample.get("sampled_at_utc")
                        or not isinstance(sample.get("telemetry"), dict)
                        or set(sample["telemetry"]) - set(SENSOR_KEYS + BINARY_KEYS)
                        or any(
                            not isinstance(record, dict)
                            or set(record) - {"value", "verified", "sample_age_seconds"}
                            or type(record.get("verified")) is not bool
                            or _safe_read_only_value(key, record.get("value"))
                            != record.get("value")
                            or record["verified"] != (record["value"] is not None)
                            or (
                                "sample_age_seconds" in record
                                and (
                                    type(record["sample_age_seconds"]) is not int
                                    or not 0 <= record["sample_age_seconds"] <= 86_400
                                    or not record["verified"]
                                )
                            )
                            for key, record in sample["telemetry"].items()
                        )
                        for sample in value["samples"]
                    )
                    or value["fresh_verified_samples"]
                    != _bronze_samples_fresh(value["samples"])
                    or value.get("physical_observation")
                    not in {"plausible", "implausible", "unable_to_observe"}
                    or value.get("protocol_probe")
                    not in {"not_observed", "inconclusive", "verified"}
                    or value.get("qualification_status") != "inconclusive"
                    or (
                        value.get("protocol_probe") == "verified"
                        and (
                            not isinstance(value.get("protocol_trace_sha256"), str)
                            or re.fullmatch(
                                r"[a-f0-9]{64}", value["protocol_trace_sha256"]
                            )
                            is None
                            or value.get("protocol_route")
                            not in {"local_adapter", "active_proxy"}
                            or value.get("protocol_status_request_hex")
                            != BRONZE_STATUS_REQUEST
                            or type(value.get("protocol_session_generation")) is not int
                            or type(value.get("protocol_status_request_seq")) is not int
                            or type(value.get("protocol_status_write_ok_seq"))
                            is not int
                            or type(value.get("protocol_decoded_response_seq"))
                            is not int
                            or value["protocol_status_write_ok_seq"]
                            <= value["protocol_status_request_seq"]
                            or value["protocol_decoded_response_seq"]
                            <= value["protocol_status_request_seq"]
                            or not isinstance(value.get("protocol_capture_id"), str)
                            or re.fullmatch(
                                r"capture-[0-9]{4}", value["protocol_capture_id"]
                            )
                            is None
                            or value.get("protocol_evidence")
                            != [
                                f"{value['protocol_capture_id']}:{value['protocol_status_request_seq']}",
                                f"{value['protocol_capture_id']}:{value['protocol_status_write_ok_seq']}",
                                f"{value['protocol_capture_id']}:{value['protocol_decoded_response_seq']}",
                            ]
                        )
                    )
                    or (
                        value.get("protocol_probe") != "verified"
                        and any(
                            value.get(key) is not None
                            for key in (
                                "protocol_trace_sha256",
                                "protocol_status_request_hex",
                                "protocol_route",
                                "protocol_session_generation",
                                "protocol_status_request_seq",
                                "protocol_status_write_ok_seq",
                                "protocol_decoded_response_seq",
                                "protocol_capture_id",
                            )
                        )
                    )
                    or (
                        value.get("protocol_probe") != "verified"
                        and value.get("protocol_evidence") != []
                    )
                ):
                    raise HAAPIError("Invalid public Bronze evidence")
                if value.get("protocol_probe") == "verified":
                    try:
                        public_events = [
                            _load_public_json(line)
                            for line in data["events.jsonl"].splitlines()
                        ]
                        public_frames = [
                            _load_public_json(line)
                            for line in data["frames.jsonl"].splitlines()
                        ]
                    except (KeyError, UnicodeError, ValueError) as exc:
                        raise HAAPIError(
                            "Bronze protocol references have no trace"
                        ) from exc
                    capture_id = value["protocol_capture_id"]
                    status_seq = value["protocol_status_request_seq"]
                    write_ok_seq = value["protocol_status_write_ok_seq"]
                    decoded_seq = value["protocol_decoded_response_seq"]
                    status_event = next(
                        (
                            row
                            for row in public_events
                            if row.get("capture_id") == capture_id
                            and row.get("seq") == status_seq
                        ),
                        None,
                    )
                    decoded_event = next(
                        (
                            row
                            for row in public_events
                            if row.get("capture_id") == capture_id
                            and row.get("seq") == decoded_seq
                        ),
                        None,
                    )
                    write_ok_event = next(
                        (
                            row
                            for row in public_events
                            if row.get("capture_id") == capture_id
                            and row.get("seq") == write_ok_seq
                        ),
                        None,
                    )
                    status_frame = next(
                        (
                            row
                            for row in public_frames
                            if row.get("capture_id") == capture_id
                            and row.get("seq") == status_seq
                        ),
                        None,
                    )
                    capture = next(
                        (
                            row
                            for row in captures.get("by_capture", [])
                            if row.get("capture_id") == capture_id
                        ),
                        None,
                    )
                    if not (
                        isinstance(status_event, dict)
                        and status_event.get("event") == "write_attempt"
                        and isinstance(write_ok_event, dict)
                        and write_ok_event.get("event") == "write_ok"
                        and status_seq < write_ok_seq
                        and status_seq < decoded_seq
                        and isinstance(decoded_event, dict)
                        and decoded_event.get("event") == "status_data_decoded"
                        and status_event.get("session_generation")
                        == decoded_event.get("session_generation")
                        and write_ok_event.get("session_generation")
                        == status_event.get("session_generation")
                        and isinstance(status_frame, dict)
                        and status_frame.get("direction") == "tx"
                        and status_frame.get("command_type") == "status_request"
                        and status_frame.get("frame_validation") == "validated_tx"
                        and isinstance(capture, dict)
                        and capture.get("route") == value.get("protocol_route")
                        and capture.get("complete") is True
                    ):
                        raise HAAPIError(
                            "Bronze protocol evidence is not capture-bound"
                        )
        elif name.endswith(".jsonl"):
            try:
                for line in data[name].splitlines():
                    event = _load_public_json(line)
                    if name == "frames.jsonl":
                        if (
                            not isinstance(event, dict)
                            or set(event)
                            != {
                                "schema_version",
                                "capture_id",
                                "seq",
                                "direction",
                                "command_type",
                                "frame_validation",
                                "session_generation",
                                "payload_omitted",
                            }
                            or event.get("schema_version") != PUBLIC_SCHEMA
                            or event.get("direction") not in {"tx", "rx"}
                            or event.get("command_type")
                            not in {
                                "status_request",
                                "output_control",
                                "settings_control",
                                "unknown",
                            }
                            or event.get("frame_validation")
                            not in {"validated_tx", "unvalidated_chunk"}
                            or (
                                event.get("direction") == "tx"
                                and event.get("frame_validation") != "validated_tx"
                            )
                            or (
                                event.get("direction") == "rx"
                                and (
                                    event.get("command_type") != "unknown"
                                    or event.get("frame_validation")
                                    != "unvalidated_chunk"
                                )
                            )
                            or (
                                event.get("session_generation") is not None
                                and (
                                    type(event["session_generation"]) is not int
                                    or not 1 <= event["session_generation"] <= 1_000_000
                                )
                            )
                            or event.get("payload_omitted") is not True
                            or not isinstance(event.get("capture_id"), str)
                            or re.fullmatch(r"capture-[0-9]{4}", event["capture_id"])
                            is None
                            or type(event.get("seq")) is not int
                            or event["seq"] < 1
                        ):
                            raise ValueError
                        continue
                    if (
                        not isinstance(event, dict)
                        or set(event)
                        != {
                            "schema_version",
                            "capture_id",
                            "seq",
                            "event",
                            "layer",
                            "utc",
                            "elapsed_ms",
                            "session_generation",
                            "observed_route",
                            "transaction_kind",
                        }
                        or event.get("schema_version") != PUBLIC_SCHEMA
                        or not isinstance(event.get("capture_id"), str)
                        or re.fullmatch(r"capture-[0-9]{4}", event["capture_id"])
                        is None
                        or type(event.get("seq")) is not int
                        or event.get("seq") < 1
                        or not isinstance(event.get("event"), str)
                        or event.get("event") not in PUBLIC_EVENT_NAMES
                        or event.get("layer") != "gatt_api"
                        or not isinstance(event.get("utc"), str)
                        or datetime.fromisoformat(event["utc"]).utcoffset() is None
                        or datetime.fromisoformat(event["utc"])
                        .utcoffset()
                        .total_seconds()
                        != 0
                        or type(event.get("elapsed_ms")) not in (int, float)
                        or not math.isfinite(event["elapsed_ms"])
                        or event.get("elapsed_ms") < 0
                        or (
                            event.get("session_generation") is not None
                            and (
                                type(event["session_generation"]) is not int
                                or not 1 <= event["session_generation"] <= 1_000_000
                            )
                        )
                        or not isinstance(event.get("observed_route"), str)
                        or event.get("observed_route")
                        not in {"local_adapter", "active_proxy", "unverified", "mixed"}
                        or event.get("transaction_kind")
                        not in {None, "output", "settings"}
                    ):
                        raise ValueError
            except (UnicodeError, ValueError, TypeError) as exc:
                raise HAAPIError("Malformed JSONL in HIL archive") from exc
    if (
        "session.json" not in data
        or "report.md" not in data
        or (captures["count"] and "frames.jsonl" not in data)
        or ("tier-execution.json" in data) != ("tier-artifacts.json" in data)
    ):
        raise HAAPIError("Required public archive members are missing")
    if "tier-execution.json" in data:
        execution = _load_public_json(data["tier-execution.json"])
        artifact_bundle = _load_public_json(data["tier-artifacts.json"])
        artifacts = {item["artifact_id"]: item for item in artifact_bundle["artifacts"]}
        referenced: dict[str, str] = {}
        for step in execution["steps"]:
            for reference in step["machine_evidence"]:
                artifact_id = reference["artifact_id"]
                if artifact_id in referenced:
                    raise HAAPIError("Duplicate public guided evidence reference")
                referenced[artifact_id] = reference["source_sha256"]
        if set(artifacts) != set(referenced) or any(
            artifacts[artifact_id]["source_sha256"] != digest
            for artifact_id, digest in referenced.items()
        ):
            raise HAAPIError("Public guided evidence binding mismatch")
    event_rows = [
        _load_public_json(line) for line in data.get("events.jsonl", b"").splitlines()
    ]
    frame_rows = [
        _load_public_json(line) for line in data.get("frames.jsonl", b"").splitlines()
    ]
    capture_rows = captures["by_capture"]
    if (
        captures["count"] != len(capture_rows)
        or captures["events"] != len(event_rows)
        or captures["frames"] != len(frame_rows)
        or captures["dropped_events"]
        != sum(row["dropped_events"] for row in capture_rows)
        or captures["dropped_frames"]
        != sum(row["dropped_frames"] for row in capture_rows)
        or captures["complete"]
        != (bool(capture_rows) and all(row["complete"] for row in capture_rows))
    ):
        raise HAAPIError("HIL archive capture counters do not match evidence")
    per_capture_counts = {}
    capture_ids = [row["capture_id"] for row in capture_rows]
    if len(capture_ids) != len(set(capture_ids)):
        raise HAAPIError("Duplicate HIL capture identity")
    declared_captures = set(capture_ids)
    event_identity_rows = [(row["capture_id"], row["seq"]) for row in event_rows]
    if len(event_identity_rows) != len(set(event_identity_rows)):
        raise HAAPIError("Duplicate HIL event reference")
    if any(
        capture_id not in declared_captures for capture_id, _ in event_identity_rows
    ):
        raise HAAPIError("HIL event references undeclared capture")
    observed_routes_by_capture: dict[str, set[str]] = {}
    for row in event_rows:
        observed_route = row["observed_route"]
        if observed_route in {"local_adapter", "active_proxy"}:
            observed_routes_by_capture.setdefault(row["capture_id"], set()).add(
                observed_route
            )
    for capture in capture_rows:
        declared_route = capture["route"]
        observed_routes = observed_routes_by_capture.get(capture["capture_id"], set())
        if declared_route in {"local_adapter", "active_proxy"} and (
            observed_routes - {declared_route}
        ):
            raise HAAPIError("HIL capture route conflicts with route observations")
    event_references = set()
    for row in event_rows:
        per_capture_counts[row["capture_id"]] = (
            per_capture_counts.get(row["capture_id"], 0) + 1
        )
        event_references.add(f"{row['capture_id']}:{row['seq']}")
    if any(
        per_capture_counts.get(row["capture_id"], 0) != row["events"]
        for row in capture_rows
    ):
        raise HAAPIError("HIL archive event references do not match captures")
    frame_counts: dict[str, int] = {}
    frames_by_reference = {}
    frame_identity_rows = [(row["capture_id"], row["seq"]) for row in frame_rows]
    if len(frame_identity_rows) != len(set(frame_identity_rows)):
        raise HAAPIError("Duplicate HIL frame reference")
    if any(
        capture_id not in declared_captures for capture_id, _ in frame_identity_rows
    ):
        raise HAAPIError("HIL frame references undeclared capture")
    for frame in frame_rows:
        if f"{frame['capture_id']}:{frame['seq']}" not in event_references:
            raise HAAPIError("HIL frame metadata has no corresponding event")
        frame_counts[frame["capture_id"]] = frame_counts.get(frame["capture_id"], 0) + 1
        frames_by_reference[(frame["capture_id"], frame["seq"])] = frame
    if any(
        frame_counts.get(row["capture_id"], 0) != row["frames"] for row in capture_rows
    ):
        raise HAAPIError("HIL archive frame counters do not match evidence")
    if "scenarios.json" in data:
        scenario_steps = json.loads(data["scenarios.json"])["steps"]
        complete_captures = {
            row["capture_id"] for row in capture_rows if row["complete"]
        }

        def verify_step_channel(refs: list[str], missing: bool) -> None:
            if any(ref not in event_references for ref in refs):
                raise HAAPIError("HIL scenario references missing event evidence")
            if not refs:
                if not missing:
                    raise HAAPIError("HIL scenario marks absent evidence complete")
                return
            rows = [
                next(
                    row
                    for row in event_rows
                    if f"{row['capture_id']}:{row['seq']}" == ref
                )
                for ref in refs
            ]
            if (
                len(rows) != 3
                or [row["event"] for row in rows]
                != [
                    "write_attempt",
                    "transaction_submitted",
                    "transaction_confirmed",
                ]
                or len({row["capture_id"] for row in rows}) != 1
                or len({row["session_generation"] for row in rows}) != 1
                or rows[1]["transaction_kind"] not in {"output", "settings"}
            ):
                raise HAAPIError(
                    "HIL step evidence does not prove a confirmed transaction"
                )
            write_frame = frames_by_reference.get(
                (rows[0]["capture_id"], rows[0]["seq"])
            )
            expected_command = (
                "output_control"
                if rows[1]["transaction_kind"] == "output"
                else "settings_control"
            )
            if (
                not isinstance(write_frame, dict)
                or write_frame.get("direction") != "tx"
                or write_frame.get("frame_validation") != "validated_tx"
                or write_frame.get("command_type") != expected_command
                or write_frame.get("session_generation")
                != rows[0]["session_generation"]
            ):
                raise HAAPIError(
                    "HIL step evidence lacks a matching validated control frame"
                )
            if not missing and any(
                row["capture_id"] not in complete_captures for row in rows
            ):
                raise HAAPIError("HIL scenario relies on incomplete capture evidence")

        for step in scenario_steps:
            declared = _public_observation(
                json.loads(data["scenarios.json"])["scenario_id"],
                {
                    "step_id": step["step_id"],
                    "expected": step["expected"],
                    "pre_state": step["observation"].get("pre_state"),
                    "post_state": step["actual"],
                    "outcome": step["outcome"],
                },
            )
            if (
                step["target"] != declared["target"]
                or step["expected"] != declared["expected"]
            ):
                raise HAAPIError(
                    "HIL scenario target or expected value disagrees with declaration"
                )
            if any(ref not in event_references for ref in step["observation_evidence"]):
                raise HAAPIError("HIL observation references missing event evidence")
            for ref in step["observation_evidence"]:
                capture_id, seq = ref.split(":", 1)
                matching = next(
                    row
                    for row in event_rows
                    if row["capture_id"] == capture_id and row["seq"] == int(seq)
                )
                if matching["event"] != "route_observed":
                    raise HAAPIError(
                        "HIL observation reference is not a route observation"
                    )
            verify_step_channel(step["evidence"], step["missing_evidence"])
            verify_step_channel(
                step["cleanup_evidence"], step["cleanup_missing_evidence"]
            )
    session_data = json.loads(data["session.json"])["data"]
    scenario_data = (
        json.loads(data["scenarios.json"])
        if "scenarios.json" in data
        else {"steps": []}
    )
    expected_report = _render_report(
        session_data.get("run_id", "unavailable"),
        json.loads(data["environment.json"]),
        json.loads(data["session.json"]),
        scenario_data,
        captures["events"],
        captures["count"],
        (
            captures["by_capture"][0]["route"]
            if len({row["route"] for row in captures["by_capture"]}) == 1
            else "mixed"
            if captures["by_capture"]
            else "unverified"
        ),
    )
    if data["report.md"] != expected_report:
        raise HAAPIError("HIL report does not match public evidence")
    if manifest.get("run_id") != session_data.get("run_id", "unavailable"):
        raise HAAPIError("HIL archive run identity mismatch")
    if include_contents:
        return manifest, data, archive_digest
    return manifest


def export_archive(run_path: Path, *, confirmed: bool) -> Path:
    run_dir, files, meta = build_public(run_path)
    public_dir = meta["public_dir"]
    for name, content in files.items():
        _atomic_write(public_dir / name, content)
    report = write_report(run_dir)
    all_files = {**files, "report.md": _regular(report)}
    manifest = _write_manifest(public_dir, all_files, meta, exported=confirmed)
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    _atomic_write(public_dir / "manifest.json", manifest_bytes)
    preview = {
        "archive": "hil-evidence.zip",
        "files": sorted([*all_files, "manifest.json"]),
        "file_sizes": {
            name: len(content) for name, content in sorted(all_files.items())
        },
        "captures": meta["capture_stats"],
        "scenario_results": (
            json.loads(files["scenarios.json"])["steps"]
            if "scenarios.json" in files
            else []
        ),
        "omissions": meta["omissions"],
        "limitations": [
            "Frame payloads are omitted.",
            "No step is linked to a capture without verified correlation.",
            "This archive does not confer physical HIL qualification.",
        ],
        "qualification_status": "inconclusive",
    }
    print(json.dumps(preview, sort_keys=True))
    if not confirmed:
        raise HAAPIError("Export requires explicit confirmation")
    temp = run_dir / f".hil-evidence.zip.{secrets.token_hex(8)}.tmp"
    target = run_dir / "hil-evidence.zip"
    created = False
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        os.close(fd)
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(all_files):
                archive.writestr("public/" + name, all_files[name])
            archive.writestr("public/manifest.json", manifest_bytes)
        verify_public_archive(temp)
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        if created:
            temp.unlink(missing_ok=True)
        raise
    return target
