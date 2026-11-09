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
from .discovery import KNOWN_MODELS, safe_model, safe_version
from .trace import _validate_trace_result

PUBLIC_SCHEMA = 2
MAX_FILE = 2 * 1024 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_MEMBERS = 8
PUBLIC_NAMES = {
    "environment.json",
    "session.json",
    "scenarios.json",
    "events.jsonl",
    "frames.jsonl",
    "manifest.json",
    "report.md",
}
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


def _public_observation(scenario_id: str, step: dict[str, Any]) -> dict[str, Any]:
    """Project only typed values and declaration-backed expectations."""
    declaration = DECLARATION_BY_ID.get(scenario_id)
    step_id = step.get("step_id")
    declared_step_id = (
        "periodic_observation"
        if scenario_id == "soak_24h_observation" and step_id == "soak_segment"
        else step_id
    )
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
    action = step.get("action")
    if action in {
        "route interruption and restoration",
        "vendor-app contention",
        "Home Assistant or integration reload/upgrade",
    }:
        observation["operator_action"] = action

    target = declared_step.get("target") if declared_step is not None else None
    expected = declared_step.get("expected") if declared_step is not None else None
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
            actual = "reconnected"
        elif step.get("trace_complete") is False:
            actual = "observation_incomplete"
    if actual is None and step.get("step_id") == "soak_segment":
        actual = "completed" if step.get("outcome") == "pass" else "inconclusive"
    reason = step.get("reason")
    limitation = None
    if isinstance(reason, str):
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


def _valid_public_observation(value: Any) -> bool:
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
        "measured_seconds",
        "maximum_gap_seconds",
        "route",
        "operator_action",
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
    }
    if set(value) - allowed:
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
    if "operator_action" in value and value["operator_action"] not in {
        "route interruption and restoration",
        "vendor-app contention",
        "Home Assistant or integration reload/upgrade",
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
        return value in {"mismatch", "unverified"}
    if key in {"version", "installed_manifest_version", "python_version", "kernel"}:
        return re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,31}", value) is not None
    if key == "runner_checkout_commit":
        return re.fullmatch(r"[a-f0-9]{40}", value) is not None
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
                            }
                            else "unknown",
                            "verified": item.get("verified") is True,
                        }
                elif (
                    key == "build_match"
                    and isinstance(item, str)
                    and item in {"mismatch", "unverified"}
                ):
                    allowed[key] = {
                        "value": item,
                        "source": "runner_repository_manifest",
                        "verified": item == "mismatch",
                    }
            result["data"][group_name] = allowed
    else:
        for key in (
            "run_id",
            "scenario_id",
            "qualification_status",
            "route",
            "preflight",
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
            elif key == "route" and isinstance(value, dict):
                observed = value.get("observed")
                if (
                    isinstance(observed, dict)
                    and isinstance(observed.get("value"), str)
                    and observed.get("value")
                    in {"local_adapter", "active_proxy", "unverified"}
                ):
                    result["data"][key] = {
                        "observed": {
                            "value": observed["value"],
                            "verified": observed.get("verified") is True,
                        }
                    }
            elif key == "preflight" and isinstance(value, dict):
                result["data"][key] = {
                    "overall": value.get("overall")
                    if isinstance(value.get("overall"), str)
                    and value.get("overall") in {"ready", "incomplete", "inconclusive"}
                    else "inconclusive"
                }
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
    files: dict[str, bytes] = {
        "environment.json": (
            json.dumps(
                _safe_versioned(env, "environment.json"), sort_keys=True, indent=2
            )
            + "\n"
        ).encode(),
        "session.json": (
            json.dumps(
                _safe_versioned(session, "session.json"), sort_keys=True, indent=2
            )
            + "\n"
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
                        event,
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
    model = device.get("model", {}).get("value", "unavailable")
    firmware = device.get("firmware_version", {}).get("value", "unavailable")
    hardware = device.get("hardware_version", {}).get("value", "unavailable")
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
        + f"- Device: {model}; hardware {hardware}; firmware {firmware}\n"
        + f"- Observed GATT route: {route_summary}\n\n"
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
    }
    sources = {
        "runner_platform",
        "runner_runtime",
        "ha_rest_config",
        "ha_ws_manifest_get",
        "runner_repository_manifest",
        "runner_git_checkout",
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


def verify_public_archive(path: Path) -> dict[str, Any]:
    """Validate archive member set and manifest digests before consumers import it."""
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
        manifest = json.loads(data["manifest.json"])
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
    if (
        not isinstance(build, dict)
        or set(build)
        != {"installed_manifest_version", "verification", "runner_checkout_commit"}
        or not isinstance(build.get("verification"), str)
        or build["verification"] not in {"unverified", "mismatch"}
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
    ):
        raise HAAPIError("Invalid HIL archive build provenance")
    try:
        environment_data = json.loads(data["environment.json"]).get("data", {})
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
    }
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
                value = json.loads(data[name])
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
                        "route",
                        "preflight",
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
                            or set(route) != {"observed"}
                            or not isinstance(observed, dict)
                            or set(observed) != {"value", "verified"}
                            or not isinstance(observed.get("value"), str)
                            or observed.get("value")
                            not in {"local_adapter", "active_proxy", "unverified"}
                            or type(observed.get("verified")) is not bool
                        ):
                            raise HAAPIError("Invalid public route metadata")
                    if "preflight" in public_session:
                        preflight = public_session["preflight"]
                        if (
                            not isinstance(preflight, dict)
                            or set(preflight) != {"overall"}
                            or not isinstance(preflight.get("overall"), str)
                            or preflight.get("overall")
                            not in {"ready", "incomplete", "inconclusive"}
                        ):
                            raise HAAPIError("Invalid public preflight metadata")
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
                            "observation_incomplete",
                            "completed",
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
        elif name.endswith(".jsonl"):
            try:
                for line in data[name].splitlines():
                    event = json.loads(line)
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
                        or event.get("event") not in EVENT_NAMES
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
    ):
        raise HAAPIError("Required public archive members are missing")
    event_rows = [
        json.loads(line) for line in data.get("events.jsonl", b"").splitlines()
    ]
    frame_rows = [
        json.loads(line) for line in data.get("frames.jsonl", b"").splitlines()
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
                {"step_id": step["step_id"]},
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
