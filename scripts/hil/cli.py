"""HIL CLI setup, device selection, common preflight, and dispatch."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import hashlib
import hmac
import json  # noqa: F401 - shared module object used by trace.py and CLI tests
import math
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
from time import monotonic, sleep
from typing import Any, Callable

from .api import HAAPIError, HAClient
from .capabilities import feature_write_authorized
from .evidence import export_archive, write_report
from .importer import DEFAULT_MATRIX, import_archive
from .discovery import Device, choose_device, find_devices
from .environment import (
    build_environment,
    build_session,
    check_ha_version,
    evaluate_preflight,
    new_run_id,
    read_only_sample,
    unknown,
)
from .onboard import assess_run_gate
from .storage import (
    load_scenario_checkpoint,
    run_root,
    write_run,
    write_scenario_progress,
)
from .runner import (
    correlate_transaction,
    plan as plan_scenarios,
    plan_as_dict,
    recovery_verdict,
    route_matches_request,
    soak_verdict,
    short_stability_verdict,
    verdict,
)
from .scenarios import SUITES, load_scenarios
from .suite_runtime import SuiteServices
from .trace import (
    _save_private_trace,
    _validate_trace_result,
)

_SWITCH_CONTROLS = {"ac_output", "dc_output", "light", "eco_mode", "car_charger"}


def _shell_quote(value: str | Path, *, powershell: bool | None = None) -> str:
    """Quote one argument for the current shell without expanding its contents."""
    use_powershell = os.name == "nt" if powershell is None else powershell
    rendered = str(value)
    if use_powershell:
        return "'" + rendered.replace("'", "''") + "'"
    import shlex

    return shlex.quote(rendered)


def _shell_join(values: list[str | Path], *, powershell: bool | None = None) -> str:
    return " ".join(_shell_quote(value, powershell=powershell) for value in values)


def _control_value_available(key: str, state: dict[str, Any], expected: str) -> bool:
    """Validate switch values or a select's server-advertised options."""
    current = state.get("state")
    if key in _SWITCH_CONTROLS:
        return current in {"on", "off"} and expected in {"on", "off"}
    options = state.get("options")
    return isinstance(options, list) and current in options and expected in options


def make_parser() -> argparse.ArgumentParser:
    """Declare a small CLI; never accept tokens on the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.hil",
        description="Non-destructive ALLPOWERS HIL preflight and read-only collector",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--ha-url",
        default=None,
        help="HA origin (or set HA_HIL_URL); never include tokens",
    )
    common.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Explicitly allow plaintext HTTP to a remote HA server (unsafe)",
    )
    common.add_argument(
        "--device", help="Device index (1-based) or unambiguous model; not an entity ID"
    )
    common.add_argument(
        "--output-root",
        type=Path,
        default=Path("hil-results"),
        help="Local run directory",
    )
    common.add_argument(
        "--non-interactive", action="store_true", help="Never prompt for input"
    )
    common.add_argument(
        "--route",
        choices=("auto", "local_adapter", "active_proxy"),
        default="auto",
        help="Requested BLE route; never treated as proof of the actual route",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    report_parser = commands.add_parser(
        "report", help="Build a privacy-safe report offline"
    )
    report_parser.add_argument("run_directory", type=Path)
    export_parser = commands.add_parser(
        "export", help="Preview and export public HIL evidence offline"
    )
    export_parser.add_argument("run_directory", type=Path)
    export_parser.add_argument("--non-interactive", action="store_true")
    export_parser.add_argument("--confirm-export", action="store_true")
    import_parser = commands.add_parser(
        "import", help="Review and import a verified public HIL evidence ZIP"
    )
    import_parser.add_argument("archive", type=Path)
    import_parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    import_modes = import_parser.add_mutually_exclusive_group()
    import_modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing (default)",
    )
    import_modes.add_argument(
        "--apply",
        action="store_true",
        help="Write reviewed evidence and matrix changes",
    )
    tiers_parser = commands.add_parser(
        "tiers", help="Calculate evidence-backed hardware support tiers as JSON"
    )
    tiers_parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    review_parser = commands.add_parser(
        "review", help="Attest physical observations for an imported HIL archive"
    )
    review_parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    review_parser.add_argument("--run-id", required=True)
    review_parser.add_argument(
        "--tier",
        choices=("bronze", "silver", "gold", "platinum"),
        default="bronze",
        help="Support tier to review (Bronze remains the default)",
    )
    review_mode_input = review_parser.add_mutually_exclusive_group(required=True)
    review_mode_input.add_argument("--review-file", type=Path)
    review_mode_input.add_argument(
        "--interactive",
        action="store_true",
        help="Interactively review an imported run offline",
    )
    review_mode = review_parser.add_mutually_exclusive_group()
    review_mode.add_argument("--dry-run", action="store_true")
    review_mode.add_argument("--apply", action="store_true")
    commands.add_parser(
        "doctor", parents=[common], help="Check read-only prerequisites"
    )
    commands.add_parser(
        "onboard",
        parents=[common],
        help="Interactively prepare, run, and review a contributor HIL session",
    )
    run_parser = commands.add_parser(
        "run",
        parents=[common],
        help="Capture a read-only snapshot or guided scenario suite",
    )
    run_parser.add_argument("--suite", choices=(*SUITES, "all"))
    run_parser.add_argument(
        "--tier",
        choices=("bronze", "silver", "gold"),
        help="Build and persist a capability-aware guided tier plan",
    )
    run_parser.add_argument(
        "--allow-keepalive-disabled",
        action="store_true",
        help="Allow soak-24h to start while settings keepalive is disabled",
    )
    run_parser.add_argument(
        "--scenario-id",
        help="Select one declared scenario for outputs or settings",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print scenario eligibility without contacting Home Assistant",
    )
    run_parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run the offline transaction-safety rehearsal; no HA or hardware access",
    )
    run_parser.add_argument(
        "--list-suites", action="store_true", help="List available scenario suites"
    )
    run_parser.add_argument(
        "--resume", type=Path, help="Resume a validated scenario run directory"
    )
    trace_parser = commands.add_parser(
        "trace",
        parents=[common],
        help="Private admin-only bounded runtime GATT capture",
    )
    trace_parser.add_argument("--duration", type=int, default=15)
    trace_parser.add_argument("--scenario-id", default="manual-runtime")
    probe_parser = commands.add_parser(
        "probe-trace",
        parents=[common],
        help="Admin-only capture of HA's existing config-flow GATT probe",
    )
    probe_parser.add_argument("--flow-id")
    probe_parser.add_argument("--candidate")
    probe_parser.add_argument("--scenario-id", default="manual-probe")
    return parser


def _credential(args: argparse.Namespace) -> tuple[str, str]:
    url = args.ha_url or os.environ.get("HA_HIL_URL", "")
    if not url and getattr(args, "onboarding", False):
        url = input(
            "Home Assistant origin (for example https://home-assistant.local:8123): "
        ).strip()
    if not url:
        raise HAAPIError("Set --ha-url or HA_HIL_URL to your Home Assistant origin")
    token = os.environ.get("HA_HIL_TOKEN", "")
    if not token:
        if args.non_interactive:
            raise HAAPIError("Set HA_HIL_TOKEN for non-interactive runs")
        token = getpass.getpass("Home Assistant long-lived access token: ")
    return url, token


def _discovery(
    api: HAClient,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[Device]]:
    config = api.config()
    check_ha_version(config.get("version"))
    entities, devices, manifest, entries = api.discovery()
    return config, manifest, find_devices(entities, devices, entries)


def _candidate_output_state(
    capabilities: dict[str, Any] | None, feature: str
) -> dict[str, Any]:
    """Read one output state from the fresh, entry-scoped HIL snapshot."""
    snapshot = capabilities if isinstance(capabilities, dict) else {}
    output_states = snapshot.get("output_states")
    features = snapshot.get("features", {})
    facts = features.get(feature, {}) if isinstance(features, dict) else {}
    state = output_states.get(feature) if isinstance(output_states, dict) else None
    ready = bool(
        snapshot.get("schema_version") == 2
        and snapshot.get("verified") is True
        and snapshot.get("identity_conflicted") is False
        and snapshot.get("connected") is True
        and snapshot.get("status_fresh") is True
        and snapshot.get("pending_transaction") is False
        and type(snapshot.get("session_generation")) is int
        and snapshot["session_generation"] >= 1
        and isinstance(facts, dict)
        and facts.get("runtime_ready") is True
        and type(state) is bool
    )
    if not ready:
        return {"available": False, "state": None, "options": []}
    return {
        "available": True,
        "state": "on" if state else "off",
        "options": ["off", "on"],
    }


def _scenario_controls(
    api: HAClient,
    device: Device,
    capabilities: dict[str, Any] | None,
) -> dict[str, Any]:
    """Record only safe control values and fail-closed eligibility decisions."""
    settings_controls = {"eco_mode", "car_charger", "work_mode", "eco_timeout"}
    result: dict[str, Any] = {}
    for key in (
        "ac_output",
        "dc_output",
        "light",
        "eco_mode",
        "car_charger",
        "work_mode",
        "eco_timeout",
    ):
        entity_id = device.control_entities.get(key)
        if entity_id is None:
            observed = {"available": False, "state": None, "options": []}
        else:
            try:
                observed = api.control_state(key, entity_id)
            except HAAPIError:
                observed = {"available": False, "state": None, "options": []}
        facts = capabilities or {}
        capability_flags = facts.get("capabilities", {})
        feature_facts = facts.get("features", {})
        settings_key = key in settings_controls
        feature_key = "light_output" if key == "light" else key
        feature = feature_facts.get(feature_key)
        candidate_observed = (
            _candidate_output_state(facts, feature_key)
            if not settings_key
            else {"available": False, "state": None, "options": []}
        )
        plan_observed = (
            candidate_observed if candidate_observed["available"] else observed
        )
        control_capable = feature_write_authorized(
            facts,
            key,
            "write_settings_controls" if settings_key else "write_output_controls",
        )
        freshness = facts.get("settings_fresh" if settings_key else "status_fresh")
        runtime_ready = (
            feature.get("runtime_ready") is True if isinstance(feature, dict) else True
        )
        if key == "car_charger":
            control_capable = control_capable and capability_flags.get(
                "car_charger_enabled", False
            )
        result[key] = {
            **plan_observed,
            "eligible": bool(
                facts.get("connected") is True
                and facts.get("pending_transaction") is False
                and freshness is True
                and runtime_ready
                and control_capable is True
                and observed["available"] is True
            ),
            "candidate_eligible": bool(
                not settings_key
                and facts.get("schema_version") == 2
                and facts.get("verified") is True
                and facts.get("identity_conflicted") is False
                and facts.get("connected") is True
                and facts.get("status_fresh") is True
                and facts.get("pending_transaction") is False
                and type(facts.get("session_generation")) is int
                and facts["session_generation"] >= 1
                and isinstance(feature, dict)
                and feature.get("physical_presence") == "present"
                and feature.get("candidate_hil_eligible") is True
                and feature.get("production_authorized") is False
                and feature.get("runtime_ready") is True
                and candidate_observed["available"] is True
                and candidate_observed.get("state") in {"on", "off"}
            ),
        }
    return result


def _pending_plan_status(step: dict[str, Any]) -> tuple[str, str]:
    """Explain checks that still need evidence separately from hard blockers."""
    if step.get("eligibility") != "blocked":
        return str(step.get("eligibility", "blocked")), "; ".join(
            step.get("reasons", [])
        )
    reasons = set(step.get("reasons", []))
    pending = {
        "connected GATT route has not been verified",
        "candidate needs a fresh, single-use backend HIL grant",
    }
    if reasons and reasons <= pending:
        return "pending verification/consent", "; ".join(sorted(reasons))
    return "blocked", "; ".join(sorted(reasons))


def _candidate_step_needs_authorization(
    step: dict[str, Any],
    capabilities: dict[str, Any] | None,
    controls: dict[str, Any],
    observed_route: str | None,
) -> bool:
    """Recognize an otherwise eligible output candidate awaiting its backend grant."""
    feature = step.get("feature")
    if feature not in {"ac_output", "dc_output", "light_output"}:
        return False
    snapshot = capabilities if isinstance(capabilities, dict) else {}
    features = snapshot.get("features", {})
    facts = features.get(feature, {}) if isinstance(features, dict) else {}
    control_key = "light" if feature == "light_output" else feature
    control = controls.get(control_key, {})
    if not isinstance(facts, dict) or not isinstance(control, dict):
        return False
    reasons = set(step.get("reasons", []))
    reasons.discard("candidate needs a fresh, single-use backend HIL grant")
    return bool(
        facts.get("physical_presence") == "present"
        and facts.get("candidate_hil_eligible") is True
        and facts.get("production_authorized") is not True
        and control.get("candidate_eligible") is True
        and observed_route in {"local_adapter", "active_proxy"}
        and not reasons
    )


def _device_fingerprint(
    run_id: str, device: Device, ha_origin: str, requested_route: str = "auto"
) -> str:
    """Create a run-scoped device/server fingerprint without persisting IDs."""
    material = f"{run_id}\0{ha_origin}\0{requested_route}\0{device.config_entry_id or ''}\0{device.device_id}".encode()
    return hashlib.sha256(material).hexdigest()


def _has_verified_telemetry(snapshot: Any) -> bool:
    """Require at least one finite numeric physical measurement per sample."""
    measurement_keys = {"battery", "input_power", "output_power", "remaining_time"}
    return isinstance(snapshot, dict) and any(
        isinstance(record, dict)
        and record.get("verified") is True
        and record.get("source") == "ha_rest_entity_state"
        and type(record.get("value")) in (int, float)
        and math.isfinite(record["value"])
        for key, record in snapshot.items()
        if key in measurement_keys
    )


def _device_list(devices: list[Device]) -> None:
    print(f"ALLPOWERS devices: {len(devices)}")
    for index, device in enumerate(devices, start=1):
        print(
            f"  [{index}] {device.model} "
            f"(hardware {device.hardware or 'unknown'}, "
            f"firmware {device.firmware or 'unknown'}, "
            f"integration {device.entry_state or 'unverified'})"
        )


def _select_probe(listed: Any, args: argparse.Namespace) -> tuple[str, str]:
    """Select only server-issued opaque tokens, never a BLE address."""
    if not isinstance(listed, dict) or not isinstance(listed.get("flows"), list):
        raise HAAPIError("Invalid probe list response")
    flows = listed["flows"]
    if len(flows) > 64:
        raise HAAPIError("Too many eligible probe flows")
    available: list[tuple[str, str, str]] = []
    for flow in flows:
        if (
            not isinstance(flow, dict)
            or not isinstance(flow.get("flow_id"), str)
            or not isinstance(flow.get("candidates"), list)
            or len(flow["candidates"]) > 32
        ):
            raise HAAPIError("Invalid eligible probe flow")
        for candidate in flow["candidates"]:
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("token"), str)
                or not isinstance(candidate.get("label"), str)
            ):
                raise HAAPIError("Invalid opaque probe candidate")
            available.append((flow["flow_id"], candidate["token"], candidate["label"]))
    if args.flow_id is not None:
        available = [item for item in available if item[0] == args.flow_id]
    if args.candidate is not None:
        if args.candidate.isdecimal():
            index = int(args.candidate)
            available = [available[index - 1]] if 1 <= index <= len(available) else []
        else:
            available = [item for item in available if item[1] == args.candidate]
    if not available:
        raise HAAPIError("No eligible config-flow probe; open a HA form first")
    if len(available) == 1:
        return available[0][:2]
    if args.non_interactive:
        raise HAAPIError("Multiple probe candidates; select a flow and candidate")
    for index, (_, _, label) in enumerate(available, 1):
        print(f"  [{index}] {label}")
    choice = input("Select probe candidate number: ").strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(available):
        raise HAAPIError("Invalid probe selection")
    return available[int(choice) - 1][:2]


def _execute_probe_trace(
    api: HAClient, args: argparse.Namespace, environment: dict[str, Any]
) -> int:
    """Arm the real HA config-flow probe, never configure or connect via CLI."""
    if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", args.scenario_id) is None:
        raise HAAPIError("Scenario ID must be 1-80 safe characters")
    listed = api.websocket([{"type": "allpowers_ble/hil/probe/list"}])[0]
    flow_id, token = _select_probe(listed, args)
    run_id = new_run_id()
    start = api.websocket(
        [
            {
                "type": "allpowers_ble/hil/probe/arm",
                "flow_id": flow_id,
                "candidate_token": token,
                "run_id": run_id,
                "scenario_id": args.scenario_id,
                "requested_route": args.route,
            }
        ]
    )[0]
    if (
        not isinstance(start, dict)
        or start.get("schema_version") != 2
        or not isinstance(start.get("capture_id"), str)
        or not isinstance(start.get("ttl_seconds"), int)
    ):
        raise HAAPIError("Invalid private probe arming response")
    capture_id = start["capture_id"]
    session = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario_id": args.scenario_id,
        "route": {
            "requested": args.route,
            "observed": {"verified": False, "value": None},
        },
        "device": {"unavailable_reason": "config_flow_entry_not_created"},
        "integration_build": environment["integration"],
        "qualification_status": "inconclusive",
        "reason": "private probe GATT observations are not physical HIL proof",
    }
    stop = {
        "type": "allpowers_ble/hil/probe/stop",
        "flow_id": flow_id,
        "capture_id": capture_id,
    }
    try:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        output_dir = write_run(
            args.output_root, f"{timestamp}_{run_id}", environment, session
        )
    except HAAPIError:
        api.websocket([stop])
        raise
    print("Probe armed. Submit the ordinary matching Home Assistant form.")
    fetch = {
        "type": "allpowers_ble/hil/probe/fetch",
        "flow_id": flow_id,
        "capture_id": capture_id,
    }
    result: Any = None
    try:
        for _ in range(min(start["ttl_seconds"], 120) + 1):
            result = api.websocket([fetch])[0]
            if isinstance(result, dict) and result.get("closed") is True:
                break
            sleep(1)
        else:
            result = api.websocket([stop])[0]
    except KeyboardInterrupt, EOFError:
        result = api.websocket([stop])[0]
    if not isinstance(result, dict):
        raise HAAPIError("Invalid final probe trace")
    _validate_trace_result(
        result,
        run_id=run_id,
        scenario_id=args.scenario_id,
        capture_id=capture_id,
        requested_route=args.route,
        expected_version=2,
        expected_scope="config_probe",
    )

    if result["flow_id"] != flow_id:
        raise HAAPIError("Probe evidence belongs to another flow")
    _save_private_trace(output_dir, result)
    print("Private config-flow probe saved to:", run_root(output_dir))
    print("Probe outcome:", result["probe_outcome"])
    print("Observed connection route:", result["observed_route"])
    print("Physical HIL qualification: not permitted")
    events = {row["event"] for row in result["events"]}
    required = {
        "probe_started",
        "connect_attempt",
        "write_attempt",
        "write_ok",
        "notification_received",
        "notification_decoded",
        "probe_status_received",
        "probe_succeeded",
    }
    return (
        0
        if (
            result["probe_outcome"] == "success"
            and result["capture_complete"]
            and result["frame_count"] >= 2
            and required.issubset(events)
        )
        else 2
    )


def _bronze_status_exchange(
    events: list[dict[str, Any]], frames: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Find a correlated status write, its completion, and a later decoded reply."""
    events = [event for event in events if isinstance(event, dict)]
    frames = [frame for frame in frames if isinstance(frame, dict)]
    status_tx = next(
        (
            frame
            for frame in frames
            if frame.get("direction") == "tx"
            and frame.get("command_type") == "status_request"
            and frame.get("frame_validation") == "validated_tx"
            and isinstance(frame.get("transaction_id"), str)
            and frame["transaction_id"]
        ),
        None,
    )
    status_write_ok = next(
        (
            event
            for event in events
            if event.get("event") == "write_ok"
            and status_tx is not None
            and event.get("session_generation") == status_tx.get("session_generation")
            and event.get("transaction_id") == status_tx.get("transaction_id")
            and event.get("seq", 0) > status_tx.get("seq", 0)
        ),
        None,
    )
    decoded = next(
        (
            event
            for event in events
            if event.get("event") == "notification_decoded"
            and isinstance(event.get("decoded_packet_types"), list)
            and "StatusData" in event["decoded_packet_types"]
            and status_tx is not None
            and event.get("session_generation") == status_tx.get("session_generation")
            and event.get("seq", 0) > status_tx.get("seq", 0)
        ),
        None,
    )
    write_failed = bool(
        status_tx is not None
        and any(
            event.get("event") == "write_error"
            and event.get("session_generation") == status_tx.get("session_generation")
            and event.get("transaction_id") == status_tx.get("transaction_id")
            and event.get("seq", 0) > status_tx.get("seq", 0)
            for event in events
        )
    )
    return status_tx, status_write_ok, decoded, write_failed


def _bronze_status_marker(api: HAClient, entry_id: str) -> dict[str, Any]:
    """Read the allowlisted current-session freshness marker for Bronze proof."""
    read_capabilities = getattr(api, "hil_capabilities", None)
    if not callable(read_capabilities):
        return {}
    try:
        snapshot = read_capabilities(entry_id)
    except HAAPIError:
        return {}
    if not isinstance(snapshot, dict):
        return {}
    return {
        "connected": snapshot.get("connected"),
        "status_fresh": snapshot.get("status_fresh"),
        "session_generation": snapshot.get("session_generation"),
        "status_notification_count": snapshot.get("status_notification_count"),
    }


def _bronze_status_freshness(
    before: dict[str, Any],
    after: dict[str, Any],
    decoded: dict[str, Any] | None,
    events: list[dict[str, Any]],
    *,
    capture_id: str,
) -> dict[str, Any] | None:
    """Bind a new same-session status notification to the sample window."""
    if (
        decoded is None
        or before.get("connected") is not True
        or after.get("connected") is not True
        or after.get("status_fresh") is not True
    ):
        return None
    generation = before.get("session_generation")
    count_before = before.get("status_notification_count")
    count_after = after.get("status_notification_count")
    measurement = decoded.get("status_measurement")
    started = [event for event in events if event.get("event") == "capture_started"]
    stopped = [event for event in events if event.get("event") == "capture_stopped"]
    if (
        type(generation) is not int
        or generation < 1
        or after.get("session_generation") != generation
        or decoded.get("session_generation") != generation
        or type(count_before) is not int
        or type(count_after) is not int
        or not 0 <= count_before < count_after <= 1_000_000_000
        or not isinstance(decoded.get("seq"), int)
        or not isinstance(decoded.get("utc"), str)
        or not isinstance(measurement, dict)
        or len(started) != 1
        or len(stopped) != 1
        or type(started[0].get("seq")) is not int
        or type(stopped[0].get("seq")) is not int
        or not started[0]["seq"] < decoded["seq"] < stopped[0]["seq"]
        or started[0].get("session_generation") != generation
    ):
        return None
    return {
        "capture_id": capture_id,
        "session_generation": generation,
        "status_notification_count_before": count_before,
        "status_notification_count_after": count_after,
        "decoded_response_seq": decoded["seq"],
        "decoded_response_at_utc": decoded["utc"],
        "capture_started_seq": started[0]["seq"],
        "capture_stopped_seq": stopped[0]["seq"],
        "status_measurement": measurement,
    }


def _execute_tier_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    environment: dict[str, Any],
    output_dir: Path,
    run_id: str,
    plan: dict[str, Any],
    capabilities: dict[str, Any] | None,
) -> int:
    """Dispatch applicable guided steps through the existing scenario runners."""
    import copy
    import json
    import secrets

    from .outputs import _execute_output_suite
    from .recovery import _execute_recovery_suite
    from .settings import _execute_settings_suite

    services = _suite_services()
    features = (
        capabilities.get("features", {}) if isinstance(capabilities, dict) else {}
    )
    declarations = {item.id: item for item in load_scenarios()}
    step_scenarios = {
        "ac_output_cycle": ("outputs", "output_ac_cycle", "ac_output"),
        "dc_output_cycle": ("outputs", "output_dc_cycle", "dc_output"),
        "light_output_cycle": ("outputs", "output_light_cycle", "light_output"),
        "eco_mode_reversal": ("settings", "settings_eco_mode", "eco_mode"),
        "work_mode_reversal": ("settings", "settings_work_mode", "work_mode"),
        "eco_timeout_reversal": ("settings", "settings_eco_timeout", "eco_timeout"),
        "car_charger_reversal": ("outputs", "output_car_charger_cycle", "car_charger"),
    }
    execution: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "requested_tier": plan["requested_tier"],
        "execution_tier": plan["requested_tier"],
        "requested_route": args.route,
        "device": {
            "model": device.model,
            "hardware_version": device.hardware,
            "firmware_version": device.firmware,
        },
        "build_provenance": plan.get("device", {}).get("build_provenance", {})
        if isinstance(plan.get("device"), dict)
        else {},
        "review_state": "pending",
        "steps": [],
    }
    overall = 0
    recovery_completed = False
    recovery_evidence_run: str | None = None
    stop_reason: str | None = None
    plan_steps = plan["steps"]
    for planned in plan_steps:
        step_id = planned["id"]
        facts: dict[str, Any] = {}
        if stop_reason is not None:
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "outcome": "blocked",
                    "reason": f"not attempted because an earlier step stopped execution: {stop_reason}",
                }
            )
            continue
        if (
            getattr(args, "onboarding", False)
            and planned.get("action") in {"write", "manual"}
            and planned.get("eligibility")
            not in (
                {"eligible", "pending_authorization"}
                if planned.get("action") == "write"
                else {"eligible"}
            )
        ):
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "outcome": "blocked",
                    "reason": "; ".join(planned.get("reasons", []))
                    or "the refreshed plan does not authorize this step",
                }
            )
            overall = 2
            continue
        if step_id == "read_only_probe":
            if args.non_interactive:
                execution["steps"].append(
                    {
                        "step_id": step_id,
                        "outcome": "inconclusive",
                        "reason": "interactive observation is required",
                    }
                )
                overall = 2
                continue
            try:
                first_sample = services.read_only_sample(api, device)
            except HAAPIError:
                first_sample = {}
            first_sampled_at = services.datetime.now(UTC).isoformat()
            status_marker_before: dict[str, Any] = {}
            status_marker_after: dict[str, Any] = {}
            second_sampled_at = services.datetime.now(UTC).isoformat()
            second_sample: dict[str, Any] = {}
            protocol_probe: dict[str, Any] = {"outcome": "inconclusive"}
            decoded_status_event: dict[str, Any] | None = None
            capture_events: list[dict[str, Any]] = []
            if (
                device.entry_state == "loaded"
                and device.config_entry_id is not None
                and device.refresh_button is not None
            ):
                capture_id = None
                failure_stage = "capture_start"
                status_marker_before = _bronze_status_marker(
                    api, device.config_entry_id
                )
                try:
                    started = api.websocket(
                        [
                            {
                                "type": "allpowers_ble/hil/start",
                                "entry_id": device.config_entry_id,
                                "run_id": run_id,
                                "scenario_id": "read_only_snapshot",
                                "requested_route": args.route,
                            }
                        ]
                    )[0]
                    if (
                        isinstance(started, dict)
                        and started.get("schema_version") == 2
                        and isinstance(started.get("capture_id"), str)
                        and type(started.get("ttl_seconds")) is int
                        and started["ttl_seconds"] >= 2
                    ):
                        capture_id = started["capture_id"]
                        failure_stage = "refresh"
                        api.request_read_only_refresh(device.refresh_button)
                        failure_stage = "capture"
                        wait_budget = min(12.0, started["ttl_seconds"] - 1.0)
                        deadline = services.monotonic() + wait_budget
                        maximum_polls = min(25, math.ceil(wait_budget / 0.5) + 1)
                        for poll_index in range(maximum_polls):
                            trace_snapshot = api.websocket(
                                [
                                    {
                                        "type": "allpowers_ble/hil/fetch",
                                        "entry_id": device.config_entry_id,
                                        "capture_id": capture_id,
                                    }
                                ]
                            )[0]
                            if (
                                not isinstance(trace_snapshot, dict)
                                or trace_snapshot.get("capture_id") != capture_id
                                or not isinstance(trace_snapshot.get("events"), list)
                                or not isinstance(trace_snapshot.get("frames"), list)
                            ):
                                raise HAAPIError(
                                    "Invalid Bronze status capture snapshot"
                                )
                            (
                                poll_status_tx,
                                poll_write_ok,
                                poll_decoded,
                                write_failed,
                            ) = _bronze_status_exchange(
                                trace_snapshot["events"], trace_snapshot["frames"]
                            )
                            disconnected = bool(
                                poll_status_tx is not None
                                and any(
                                    event.get("event") == "disconnected"
                                    and event.get("session_generation")
                                    == poll_status_tx.get("session_generation")
                                    and event.get("seq", 0)
                                    > poll_status_tx.get("seq", 0)
                                    for event in trace_snapshot["events"]
                                )
                            )
                            if (
                                (
                                    poll_write_ok is not None
                                    and poll_decoded is not None
                                    and not write_failed
                                )
                                or write_failed
                                or disconnected
                            ):
                                break
                            remaining = deadline - services.monotonic()
                            if poll_index + 1 >= maximum_polls or remaining <= 0:
                                break
                            services.sleep(min(0.5, remaining))
                        stop_command = {
                            "type": "allpowers_ble/hil/stop",
                            "entry_id": device.config_entry_id,
                            "capture_id": capture_id,
                        }
                        try:
                            trace = api.websocket([stop_command])[0]
                        except HAAPIError:
                            trace = api.websocket(
                                [{**stop_command, "type": "allpowers_ble/hil/fetch"}]
                            )[0]
                        services.validate_trace_result(
                            trace,
                            run_id=run_id,
                            scenario_id="read_only_snapshot",
                            capture_id=capture_id,
                            requested_route=args.route,
                            expected_version=2,
                        )
                        events = trace.get("events", [])
                        capture_events = events
                        frames = trace.get("frames", [])
                        status_tx, status_write_ok, decoded, write_failed = (
                            _bronze_status_exchange(events, frames)
                        )
                        decoded_status_event = decoded
                        status_marker_after = _bronze_status_marker(
                            api, device.config_entry_id
                        )
                        # The sealed capture is causally between the two REST
                        # reads. This avoids comparing Home Assistant's UTC
                        # clock with the HIL runner's clock.
                        second_sample = services.read_only_sample(api, device)
                        second_sampled_at = services.datetime.now(UTC).isoformat()
                        route = trace.get("observed_route")
                        protocol_verified = bool(
                            trace.get("complete") is True
                            and trace.get("dropped_events", 0) == 0
                            and trace.get("dropped_frames", 0) == 0
                            and route in {"local_adapter", "active_proxy"}
                            and services.route_matches_request(args.route, route)
                            and status_tx is not None
                            and status_write_ok is not None
                            and decoded is not None
                            and not write_failed
                            and not any(
                                event.get("event") == "disconnected"
                                and event.get("session_generation")
                                == status_tx.get("session_generation")
                                and event.get("seq", 0) > status_tx.get("seq", 0)
                                for event in events
                            )
                        )
                        trace_dir = output_dir / "traces" / "bronze_read_only"
                        trace_dir.parent.mkdir(mode=0o700, exist_ok=True)
                        trace_dir.mkdir(mode=0o700, exist_ok=False)
                        services.save_private_trace(trace_dir, trace)
                        protocol_probe = {
                            "outcome": "pass" if protocol_verified else "inconclusive",
                            "capture_id": trace.get("capture_id"),
                            "trace": str(trace_dir.relative_to(output_dir)),
                            "trace_sha256": hashlib.sha256(
                                (trace_dir / "trace.json").read_bytes()
                            ).hexdigest(),
                            "route": route
                            if route in {"local_adapter", "active_proxy"}
                            else "unverified",
                            "session_generation": status_tx.get("session_generation")
                            if status_tx
                            else None,
                            "status_request_seq": status_tx.get("seq")
                            if status_tx
                            else None,
                            "status_write_ok_seq": status_write_ok.get("seq")
                            if status_write_ok
                            else None,
                            "decoded_response_seq": decoded.get("seq")
                            if decoded
                            else None,
                            "status_request_hex": status_tx.get("hex")
                            if status_tx
                            else None,
                        }
                        failure_stage = "complete"
                    else:
                        second_sample = services.read_only_sample(api, device)
                        second_sampled_at = services.datetime.now(UTC).isoformat()
                        status_marker_after = _bronze_status_marker(
                            api, device.config_entry_id
                        )
                except HAAPIError, OSError, TypeError, ValueError, KeyError:
                    second_sample = {}
                    protocol_probe = {
                        "outcome": "inconclusive",
                        "stage": failure_stage,
                        "reason": (
                            "refresh_failed"
                            if failure_stage == "refresh"
                            else "capture_incomplete"
                        ),
                    }
                    if capture_id is not None:
                        try:
                            api.websocket(
                                [
                                    {
                                        "type": "allpowers_ble/hil/stop",
                                        "entry_id": device.config_entry_id,
                                        "capture_id": capture_id,
                                    }
                                ]
                            )
                        except HAAPIError:
                            pass
            elif (
                device.entry_state == "loaded"
                and device.config_entry_id is not None
                and device.refresh_button is None
            ):
                protocol_probe = {
                    "outcome": "inconclusive",
                    "stage": "refresh",
                    "reason": "refresh_unavailable",
                }
            else:
                services.sleep(1)
                try:
                    second_sample = services.read_only_sample(api, device)
                    second_sampled_at = services.datetime.now(UTC).isoformat()
                    if device.config_entry_id is not None:
                        status_marker_after = _bronze_status_marker(
                            api, device.config_entry_id
                        )
                except HAAPIError:
                    second_sampled_at = services.datetime.now(UTC).isoformat()
                    second_sample = {}
            from .evidence import _bronze_samples_fresh

            status_freshness = (
                _bronze_status_freshness(
                    status_marker_before,
                    status_marker_after,
                    decoded_status_event,
                    capture_events,
                    capture_id=protocol_probe["capture_id"],
                )
                if protocol_probe.get("outcome") == "pass"
                and isinstance(protocol_probe.get("capture_id"), str)
                else None
            )
            samples = [
                {"sampled_at_utc": first_sampled_at, "telemetry": first_sample},
                {"sampled_at_utc": second_sampled_at, "telemetry": second_sample},
            ]
            samples_valid = _bronze_samples_fresh(
                samples,
                status_evidence=status_freshness,
                status_event=decoded_status_event,
            )
            try:
                physical = services.prompt(
                    "Physical plausibility: 1=telemetry matches the attended station, 2=does not match, 3=unable to observe: "
                ).strip()
            except EOFError, KeyboardInterrupt:
                physical = "3"
            sample_hashes = [
                hashlib.sha256(
                    json.dumps(item, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                for item in (first_sample, second_sample)
            ]
            bronze_evidence = {
                "schema_version": 1,
                "step_id": step_id,
                "sample_sha256": sample_hashes,
                "samples": [
                    {
                        "sampled_at_utc": first_sampled_at,
                        "telemetry": first_sample,
                    },
                    {
                        "sampled_at_utc": second_sampled_at,
                        "telemetry": second_sample,
                    },
                ],
                "fresh_verified_samples": bool(samples_valid),
                "status_freshness": status_freshness,
                "protocol_probe": protocol_probe,
                "physical_observation": {
                    "1": "plausible",
                    "2": "implausible",
                    "3": "unable_to_observe",
                }.get(physical, "unable_to_observe"),
            }
            bronze_path = output_dir / "tier-bronze-read-only.json"
            bronze_path.write_text(
                json.dumps(bronze_evidence, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            bronze_path.chmod(0o600)
            bronze_pass = (
                samples_valid
                and physical == "1"
                and protocol_probe.get("outcome") == "pass"
            )
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "outcome": "pass" if bronze_pass else "inconclusive",
                    **(
                        {
                            "reason": "The read-only refresh button could not be safely "
                            "resolved or invoked. Check that exactly one enabled "
                            "ALLPOWERS Refresh status button is linked to the selected "
                            "device and loaded entry.",
                            "failure_category": protocol_probe["reason"],
                        }
                        if protocol_probe.get("reason")
                        in {"refresh_failed", "refresh_unavailable"}
                        else {}
                    ),
                    "observed_routes": [
                        protocol_probe["route"]
                        if protocol_probe.get("outcome") == "pass"
                        else None
                    ],
                    "machine_evidence": [
                        {
                            "path": bronze_path.name,
                            "sha256": hashlib.sha256(
                                bronze_path.read_bytes()
                            ).hexdigest(),
                        }
                    ],
                    "human_review": "pending",
                }
            )
            execution["steps"].append(
                {
                    "step_id": "physical_plausibility_observation",
                    "outcome": "pass" if bronze_pass else "inconclusive",
                    "observed_routes": [
                        protocol_probe["route"]
                        if protocol_probe.get("outcome") == "pass"
                        else None
                    ],
                    "machine_evidence": [
                        {
                            "path": bronze_path.name,
                            "sha256": hashlib.sha256(
                                bronze_path.read_bytes()
                            ).hexdigest(),
                        }
                    ],
                    "human_review": "pending",
                }
            )
            if plan.get("requested_tier") in {"silver", "gold"}:
                from .guided import build_tier_plan

                observed_route = (
                    protocol_probe.get("route")
                    if protocol_probe.get("outcome") == "pass"
                    else None
                )
                refreshed_capabilities = None
                if device.config_entry_id is not None:
                    try:
                        refreshed_capabilities = api.hil_capabilities(
                            device.config_entry_id
                        )
                    except HAAPIError:
                        pass
                refreshed_controls = _scenario_controls(
                    api, device, refreshed_capabilities
                )
                requested_tier = plan["requested_tier"]
                build_provenance = plan.get("device", {}).get("build_provenance", {})
                inventory_preflight = (
                    {}
                    if args.non_interactive
                    else _collect_physical_inventory_preflight(
                        refreshed_capabilities,
                        refreshed_controls,
                        prompt=services.prompt,
                    )
                )
                refreshed_plan = build_tier_plan(
                    requested_tier,
                    refreshed_capabilities,
                    refreshed_controls,
                    observed_route=observed_route,
                    build_provenance=build_provenance,
                    inventory_preflight=inventory_preflight,
                )
                for refreshed_step in refreshed_plan["steps"]:
                    if _candidate_step_needs_authorization(
                        refreshed_step,
                        refreshed_capabilities,
                        refreshed_controls,
                        observed_route,
                    ):
                        refreshed_step["eligibility"] = "pending_authorization"
                        refreshed_step["reasons"] = [
                            "candidate needs a fresh, single-use backend HIL grant"
                        ]
                print(
                    "Updated plan after the Bronze connected-route and capability check:"
                )
                for refreshed_step in refreshed_plan["steps"]:
                    status, reasons = _pending_plan_status(refreshed_step)
                    print(
                        f"  {status}: {refreshed_step['id']} "
                        f"({refreshed_step['feature']}) — "
                        f"{reasons or 'ready for the existing guided suite'}"
                    )
                output_steps = [
                    step
                    for step in refreshed_plan["steps"]
                    if step.get("action") == "write"
                    and step.get("feature")
                    in {"ac_output", "dc_output", "light_output"}
                    and step.get("eligibility") in {"eligible", "pending_authorization"}
                ]
                if not bronze_pass:
                    output_steps = []
                if not output_steps:
                    print(
                        f"No Silver/Gold output step is currently achievable for "
                        f"{requested_tier.title()}; output qualification cannot be claimed."
                    )
                    try:
                        continue_bronze = (
                            not args.non_interactive
                            and services.prompt(
                                "Type BRONZE to keep only the read-only diagnostics, or press Enter to stop: "
                            ).strip()
                            == "BRONZE"
                        )
                    except EOFError, KeyboardInterrupt:
                        continue_bronze = False
                    if continue_bronze:
                        refreshed_plan = build_tier_plan(
                            "bronze",
                            refreshed_capabilities,
                            refreshed_controls,
                            observed_route=observed_route,
                            build_provenance=build_provenance,
                            inventory_preflight=inventory_preflight,
                        )
                        execution["execution_tier"] = "bronze"
                        print(
                            f"Continuing as Bronze diagnostics; requested tier "
                            f"remains {requested_tier.title()}. No Silver/Gold evidence is claimed."
                        )
                    else:
                        refreshed_plan = build_tier_plan(
                            "bronze",
                            refreshed_capabilities,
                            refreshed_controls,
                            observed_route=observed_route,
                            build_provenance=build_provenance,
                            inventory_preflight=inventory_preflight,
                        )
                        execution["execution_tier"] = "bronze"
                        overall = 2
                        print(
                            "Stopping after the Bronze diagnostics already collected; "
                            "no Silver/Gold steps will run."
                        )
                else:
                    try:
                        proceed = (
                            not args.non_interactive
                            and services.prompt(
                                "Type RUN after reviewing the refreshed plan to authorize proceeding to individually consented steps: "
                            ).strip()
                            == "RUN"
                        )
                    except EOFError, KeyboardInterrupt:
                        proceed = False
                    if not proceed:
                        refreshed_plan = build_tier_plan(
                            "bronze",
                            refreshed_capabilities,
                            refreshed_controls,
                            observed_route=observed_route,
                            build_provenance=build_provenance,
                            inventory_preflight=inventory_preflight,
                        )
                        execution["execution_tier"] = "bronze"
                        overall = 2
                        print("Plan not confirmed; stopping after Bronze diagnostics.")
                    else:
                        print(
                            "A pending authorization still requires the backend grant "
                            "and a separate consent for every action and restoration."
                        )
                refreshed_plan["requested_tier"] = requested_tier
                refreshed_plan["execution_tier"] = execution["execution_tier"]
                session_path = output_dir / "session.json"
                try:
                    saved_session = json.loads(session_path.read_text(encoding="utf-8"))
                    if isinstance(saved_session, dict):
                        saved_session["test_plan"] = refreshed_plan
                        session_path.write_text(
                            json.dumps(saved_session, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8",
                        )
                        session_path.chmod(0o600)
                except OSError, json.JSONDecodeError:
                    pass
                features = (
                    refreshed_capabilities.get("features", {})
                    if isinstance(refreshed_capabilities, dict)
                    else {}
                )
                plan_steps[:] = refreshed_plan["steps"]
            if not bronze_pass:
                overall = 2
            continue
        if step_id == "physical_plausibility_observation":
            continue
        if step_id == "minimum_output_requirement":
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "outcome": "pending_review",
                    "reason": "minimum output qualification is computed after evidence import and independent review",
                    "human_review": "pending",
                }
            )
            continue
        if step_id == "guarded_recovery" and recovery_completed:
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "scenario_id": "recovery_reconnect",
                    "outcome": "shared_evidence",
                    "evidence_run": recovery_evidence_run,
                }
            )
            continue
        if step_id in {"basic_reconnect", "guarded_recovery"}:
            scenario_id = "recovery_reconnect"
            suite = "recovery"
            feature = "telemetry"
        else:
            mapping = step_scenarios.get(step_id)
            if mapping is None:
                continue
            suite, scenario_id, feature = mapping
            facts = features.get(feature, {}) if isinstance(features, dict) else {}
            if isinstance(facts, dict) and facts.get("physical_presence") == "absent":
                execution["steps"].append(
                    {
                        "step_id": step_id,
                        "outcome": "not_applicable",
                        "reason": "reviewed profile substantiates physical absence",
                    }
                )
                continue
            if not isinstance(facts, dict) or not (
                facts.get("physical_presence") == "present"
                or (
                    facts.get("physical_presence") == "unknown"
                    and facts.get("production_authorized") is True
                    and planned.get("inventory_preflight")
                    == "operator_inspected_present"
                    and isinstance(plan.get("physical_inventory_preflight"), dict)
                    and plan["physical_inventory_preflight"].get("source")
                    == "operator_physical_inspection"
                )
            ):
                execution["steps"].append(
                    {
                        "step_id": step_id,
                        "outcome": "blocked",
                        "reason": "physical presence is unknown and no valid production-authorized inventory preflight is recorded",
                    }
                )
                overall = 2
                continue
            if facts.get("production_authorized") is not True:
                if (
                    feature in {"ac_output", "dc_output", "light_output"}
                    and facts.get("candidate_hil_eligible") is True
                ):
                    candidate_result = _execute_experimental_output_step(
                        api,
                        args,
                        device,
                        output_dir,
                        run_id,
                        step_id,
                        feature,
                        services,
                    )
                    candidate_result["authorization_context"] = "experimental_hil"
                    candidate_result["authorization_context_source"] = (
                        "experimental_hil_grant"
                    )
                    execution["steps"].append(candidate_result)
                    if candidate_result.get("outcome") != "pass":
                        overall = 2
                    if (
                        candidate_result.get("outcome") != "pass"
                        or candidate_result.get("state_safe_to_continue") is False
                    ):
                        reason = candidate_result.get("reason")
                        stop_reason = (
                            reason.strip()
                            if isinstance(reason, str) and reason.strip()
                            else "candidate was denied or did not complete with confirmed restoration"
                        )
                    continue
                execution["steps"].append(
                    {
                        "step_id": step_id,
                        "outcome": "blocked",
                        "reason": "production capability absent; experimental grant execution unavailable for this control",
                    }
                )
                overall = 2
                continue
        declaration = declarations.get(scenario_id)
        if declaration is None:
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "outcome": "blocked",
                    "reason": "reviewed scenario declaration missing",
                }
            )
            overall = 2
            continue
        # Checkpoints and trace validators require the same 16-hex run IDs
        # used by standalone scenario runs.
        child_run_id = secrets.token_hex(8)
        child_session = {
            "run_id": child_run_id,
            "parent_tier_run_id": run_id,
            "tier_step_id": step_id,
            "requested_route": args.route,
            "device": {
                "model": device.model,
                "hardware_version": device.hardware,
                "firmware_version": device.firmware,
            },
        }
        child_root = write_run(
            args.output_root,
            f"tier_{child_run_id}",
            environment,
            child_session,
        )
        child_args = copy.copy(args)
        child_args.suite = suite
        child_args.scenario_id = scenario_id if suite != "recovery" else None
        child_args.resume = None
        child_args.guided_reconnect_required = step_id == "basic_reconnect"
        if suite == "outputs":
            status = _execute_output_suite(
                api, child_args, device, environment, child_root, child_run_id, services
            )
        elif suite == "settings":
            status = _execute_settings_suite(
                api, child_args, device, child_root, child_run_id, services
            )
        else:
            status = _execute_recovery_suite(
                api, child_args, device, child_root, child_run_id, services, None
            )
            recovery_completed = True
        try:
            checkpoint = json.loads(
                (child_root / "checkpoint.json").read_text(encoding="utf-8")
            )
            rows = checkpoint.get("steps", [])
        except OSError, ValueError:
            rows = []
        evidence_paths = sorted(
            {
                str(path.relative_to(child_root))
                for path in child_root.rglob("*")
                if path.is_file()
                and (path.name == "trace.json" or path.name == "checkpoint.json")
            }
        )
        verified_routes: set[str] = set()
        for evidence_path in evidence_paths:
            if Path(evidence_path).name != "trace.json":
                continue
            try:
                trace_metadata = json.loads(
                    (child_root / evidence_path).read_text(encoding="utf-8")
                )
            except OSError, UnicodeError, ValueError:
                continue
            route = trace_metadata.get("observed_route")
            if (
                trace_metadata.get("complete") is True
                and route in {"local_adapter", "active_proxy"}
                and services.route_matches_request(args.route, route)
            ):
                verified_routes.add(route)
        passed = (
            status == 0
            and bool(rows)
            and any(Path(item).name == "trace.json" for item in evidence_paths)
            and all(row.get("outcome") == "pass" for row in rows)
        )
        review_evidence_path: Path | None = None
        if (
            passed
            and isinstance(facts, dict)
            and facts.get("production_authorized") is True
        ):
            review_artifact = _reviewable_tier_step_artifact(
                parent_private=output_dir,
                child_private=child_root,
                step_id=step_id,
                feature=feature,
                checkpoint_rows=rows,
            )
            if review_artifact is not None:
                review_evidence_path = output_dir / f"tier-{step_id}.json"
                review_evidence_path.write_text(
                    json.dumps(review_artifact, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                review_evidence_path.chmod(0o600)
        reconnect_reason = None
        if passed and step_id == "basic_reconnect":
            action_row = next(
                (row for row in rows if row.get("step_id") == "operator_action"),
                {},
            )
            reconnect_row = next(
                (row for row in rows if row.get("step_id") == "recovered_session"),
                {},
            )
            before_generation = reconnect_row.get("session_generation_before")
            after_generation = reconnect_row.get("session_generation_after")
            reconnect_confirmed = bool(
                action_row.get("action") == "route interruption and restoration"
                and action_row.get("operator_confirmed") is True
                and type(before_generation) is int
                and type(after_generation) is int
                and after_generation != before_generation
                and reconnect_row.get("connected") is True
                and reconnect_row.get("status_fresh") is True
                and reconnect_row.get("fresh_status_telemetry") is True
                and reconnect_row.get("route_verified") is True
                and reconnect_row.get("trace_complete") is True
                and reconnect_row.get("physical_outcome") == "yes"
                and reconnect_row.get("route") in verified_routes
            )
            if not reconnect_confirmed:
                passed = False
                reconnect_reason = (
                    "mandatory reconnect requires a confirmed route interruption, "
                    "changed session generation, fresh status, "
                    "verified route, and positive physical confirmation"
                )
        failed = next((row for row in rows if row.get("outcome") != "pass"), None)
        failed_reason = failed.get("reason") if isinstance(failed, dict) else None
        step_reason = reconnect_reason or (
            failed_reason.strip()
            if isinstance(failed_reason, str) and failed_reason.strip()
            else "guided scenario did not complete with confirmed evidence"
        )
        machine_evidence = [
            {
                "path": f"../../tier_{child_run_id}/private/{item}",
                "sha256": hashlib.sha256((child_root / item).read_bytes()).hexdigest(),
            }
            for item in evidence_paths
        ]
        if review_evidence_path is not None:
            machine_evidence.append(
                {
                    "path": review_evidence_path.name,
                    "sha256": hashlib.sha256(
                        review_evidence_path.read_bytes()
                    ).hexdigest(),
                }
            )
        execution["steps"].append(
            {
                "step_id": step_id,
                "scenario_id": scenario_id,
                "outcome": "pass" if passed else "inconclusive",
                **({} if passed else {"reason": step_reason}),
                "observed_routes": sorted(verified_routes),
                "human_review": "pending",
                **(
                    {
                        "authorization_context": "production",
                        "authorization_context_source": "runtime_production_authorized",
                    }
                    if isinstance(facts, dict)
                    and facts.get("production_authorized") is True
                    else {}
                ),
                "machine_evidence": machine_evidence,
                "evidence_run": f"../../tier_{child_run_id}/private",
            }
        )
        if suite == "recovery":
            recovery_evidence_run = f"../../tier_{child_run_id}/private"
        if not passed:
            overall = 2
            stop_reason = step_reason
    target = output_dir / "tier-execution.json"
    execution["verified_routes"] = sorted(
        {
            route
            for step in execution["steps"]
            for route in step.get("observed_routes", [])
            if route in {"local_adapter", "active_proxy"}
        }
    )
    target.write_text(
        json.dumps(execution, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    target.chmod(0o600)
    print("Guided execution saved to:", target)
    print(
        "Requested tier is not an attained qualification; independent review remains pending."
    )
    return overall


def _collect_physical_inventory_preflight(
    capabilities: dict[str, Any] | None,
    controls: dict[str, Any],
    *,
    prompt: Callable[[str], str],
) -> dict[str, dict[str, str]]:
    """Collect an operator inspection gate, separate from independent review."""
    features = (
        capabilities.get("features", {}) if isinstance(capabilities, dict) else {}
    )
    if not isinstance(features, dict):
        return {}
    control_keys = {
        "ac_output": "ac_output",
        "dc_output": "dc_output",
        "light_output": "light",
        "eco_mode": "eco_mode",
        "work_mode": "work_mode",
        "eco_timeout": "eco_timeout",
        "car_charger": "car_charger",
    }
    result: dict[str, dict[str, str]] = {}
    identity = " ".join(
        str(value)
        for value in (
            capabilities.get("model"),
            capabilities.get("hardware_revision"),
            capabilities.get("firmware_revision"),
        )
        if isinstance(value, str) and value
    )
    for feature, control_key in control_keys.items():
        facts = features.get(feature)
        control = controls.get(control_key)
        if (
            not isinstance(facts, dict)
            or facts.get("physical_presence") != "unknown"
            or facts.get("production_authorized") is not True
            or not isinstance(control, dict)
            or control.get("available") is not True
        ):
            continue
        try:
            answer = prompt(
                f"Physically inspect {identity}: confirm {feature} is present and belongs to this exact unit. Type PRESENT only after inspection (anything else leaves it blocked): "
            ).strip()
        except EOFError, KeyboardInterrupt:
            answer = ""
        if answer == "PRESENT":
            result[feature] = {
                "outcome": "present",
                "basis": "operator_physical_inspection",
            }
    return result


def _execute_experimental_output_step(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    step_id: str,
    feature: str,
    services: SuiteServices,
) -> dict[str, Any]:
    """Execute one candidate output and its separately authorized restoration."""
    import json

    entry_id = device.config_entry_id
    if entry_id is None or args.non_interactive or device.entry_state != "loaded":
        return {
            "step_id": step_id,
            "outcome": "blocked",
            "reason": "interactive loaded entry required",
        }
    try:
        before_caps = api.hil_capabilities(entry_id)
    except HAAPIError:
        return {
            "step_id": step_id,
            "outcome": "inconclusive",
            "reason": "fresh candidate preflight unavailable",
        }
    before_state = _candidate_output_state(before_caps, feature)
    facts = before_caps.get("features", {}).get(feature, {})
    generation = before_caps.get("session_generation")
    initial = before_state.get("state")
    if (
        before_caps.get("verified") is not True
        or before_caps.get("identity_conflicted") is not False
        or before_caps.get("connected") is not True
        or before_caps.get("status_fresh") is not True
        or before_caps.get("pending_transaction") is not False
        or type(generation) is not int
        or not isinstance(facts, dict)
        or facts.get("physical_presence") != "present"
        or facts.get("candidate_hil_eligible") is not True
        or facts.get("production_authorized") is True
        or before_state.get("available") is not True
        or initial not in {"on", "off"}
    ):
        return {
            "step_id": step_id,
            "outcome": "blocked",
            "reason": "candidate capability or state is not eligible",
        }
    target = initial != "on"
    target_name = "on" if target else "off"
    print(
        f"Experimental HIL only: verify a noncritical load is safe; "
        f"{feature} will change to {target_name} and then restore to {initial}."
    )
    try:
        if (
            services.prompt(
                "Type 1 to authorize this single experimental action: "
            ).strip()
            != "1"
        ):
            return {
                "step_id": step_id,
                "outcome": "skipped",
                "reason": "experimental action denied",
            }
        if (
            services.prompt(
                "Type 1 again to request and execute the single-use backend grant: "
            ).strip()
            != "1"
        ):
            return {
                "step_id": step_id,
                "outcome": "skipped",
                "reason": "grant execution denied",
            }
    except EOFError, KeyboardInterrupt:
        return {
            "step_id": step_id,
            "outcome": "skipped",
            "reason": "experimental action consent interrupted",
        }
    try:
        current_caps = api.hil_capabilities(entry_id)
    except HAAPIError:
        return {
            "step_id": step_id,
            "outcome": "inconclusive",
            "reason": "pre-write state unavailable; no BLE action sent",
        }
    current_state = _candidate_output_state(current_caps, feature)
    current_facts = current_caps.get("features", {}).get(feature, {})
    if (
        current_caps.get("verified") is not True
        or current_caps.get("identity_conflicted") is not False
        or current_caps.get("connected") is not True
        or current_caps.get("status_fresh") is not True
        or current_caps.get("pending_transaction") is not False
        or current_caps.get("session_generation") != generation
        or not isinstance(current_facts, dict)
        or current_facts.get("candidate_hil_eligible") is not True
        or current_state.get("available") is not True
        or current_state.get("state") != initial
    ):
        return {
            "step_id": step_id,
            "outcome": "inconclusive",
            "reason": "same-session pre-write recheck failed; no BLE action sent",
        }

    machine_actions: list[dict[str, Any]] = []

    def execute(target_state: bool) -> dict[str, Any]:
        phase = "change" if not machine_actions else "restore"
        scenario_id = {
            "ac_output": "output_ac_cycle",
            "dc_output": "output_dc_cycle",
            "light_output": "output_light_cycle",
        }[feature]
        try:
            started = api.websocket(
                [
                    {
                        "type": "allpowers_ble/hil/start",
                        "entry_id": entry_id,
                        "run_id": run_id,
                        "scenario_id": scenario_id,
                        "requested_route": args.route,
                    }
                ]
            )[0]
        except HAAPIError:
            machine_actions.append(
                {
                    "outcome": "inconclusive",
                    "confirmed": False,
                    "trace_error": "capture_start_failed",
                    "trace_evidence": [],
                }
            )
            return {
                "outcome": "inconclusive",
                "confirmed": False,
                "trace_error": "capture_start_failed",
            }
        capture_id = started.get("capture_id") if isinstance(started, dict) else None
        if not isinstance(capture_id, str) or started.get("schema_version") != 2:
            machine_actions.append(
                {
                    "outcome": "inconclusive",
                    "confirmed": False,
                    "trace_error": "capture_start_invalid",
                    "trace_evidence": [],
                }
            )
            return {
                "outcome": "inconclusive",
                "confirmed": False,
                "trace_error": "capture_start_invalid",
            }
        backend_result: dict[str, Any] | None = None
        backend_failed = False
        try:
            with api.websocket_session() as connection:
                backend_result = connection.experimental_output(
                    entry_id,
                    feature,
                    target_state,
                    authorization_confirmed=True,
                    execution_confirmed=True,
                )
        except HAAPIError:
            backend_failed = True
        try:
            trace = api.websocket(
                [
                    {
                        "type": "allpowers_ble/hil/stop",
                        "entry_id": entry_id,
                        "capture_id": capture_id,
                    }
                ]
            )[0]
        except HAAPIError:
            try:
                trace = api.websocket(
                    [
                        {
                            "type": "allpowers_ble/hil/fetch",
                            "entry_id": entry_id,
                            "capture_id": capture_id,
                        }
                    ]
                )[0]
            except HAAPIError:
                trace = None
        transaction = None
        trace_path = None
        trace_valid = False
        if isinstance(trace, dict):
            try:
                services.validate_trace_result(
                    trace,
                    run_id=run_id,
                    scenario_id=scenario_id,
                    capture_id=capture_id,
                    requested_route=args.route,
                    expected_version=2,
                )
                trace_valid = True
                transaction = services.correlate_transaction(
                    trace,
                    expected_generation=generation,
                    kind="output",
                )
                trace_dir = output_dir / "traces" / f"{step_id}_{phase}"
                trace_dir.parent.mkdir(mode=0o700, exist_ok=True)
                trace_dir.mkdir(mode=0o700, exist_ok=False)
                services.save_private_trace(trace_dir, trace)
                trace_path = str(trace_dir.relative_to(output_dir))
            except HAAPIError, OSError, TypeError, ValueError:
                trace_valid = False
                transaction = None
        trace_evidence = []
        if trace_path is not None:
            for filename in ("trace.json", "events.jsonl", "frames.jsonl"):
                path = output_dir / trace_path / filename
                if path.is_file():
                    trace_evidence.append(
                        {
                            "path": f"{trace_path}/{filename}",
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
        action_result = backend_result or {
            "outcome": "inconclusive",
            "confirmed": False,
        }
        action_result = {
            **action_result,
            "trace_valid": trace_valid,
            "trace_complete": trace_valid and trace.get("complete") is True,
            "trace_route": trace.get("observed_route") if trace_valid else None,
            "trace_path": trace_path,
            "trace_evidence": trace_evidence,
            "transaction": transaction,
            "backend_failed": backend_failed,
        }
        machine_actions.append(
            {
                "outcome": action_result["outcome"],
                "confirmed": action_result["confirmed"],
                "route": action_result.get("route"),
                "session_generation": action_result.get("session_generation"),
                "trace_route": action_result.get("trace_route"),
                "trace_complete": action_result.get("trace_complete"),
                "trace_valid": action_result.get("trace_valid"),
                "trace_path": action_result.get("trace_path"),
                "trace_evidence": action_result.get("trace_evidence", []),
                "transaction": action_result.get("transaction"),
                "backend_failed": backend_failed,
            }
        )
        return action_result

    try:
        changed = execute(target)
    except HAAPIError:
        evidence = {
            "schema_version": 1,
            "step_id": step_id,
            "actions": machine_actions,
            "outcome": "inconclusive",
        }
        outcome = "inconclusive"
        reason = "backend grant denied, expired, or execution unconfirmed; no retry"
    else:
        try:
            after_caps = api.hil_capabilities(entry_id)
        except HAAPIError:
            after_caps, after_state = {}, {"available": False}
        else:
            after_state = _candidate_output_state(after_caps, feature)
        change_readback_confirmed = (
            changed.get("outcome") == "confirmed"
            and changed.get("confirmed") is True
            and changed.get("session_generation") == generation
            and changed.get("route") in {"local_adapter", "active_proxy"}
            and after_caps.get("verified") is True
            and after_caps.get("identity_conflicted") is False
            and after_caps.get("connected") is True
            and after_caps.get("status_fresh") is True
            and after_caps.get("pending_transaction") is False
            and after_caps.get("session_generation") == generation
            and after_caps.get("features", {})
            .get(feature, {})
            .get("candidate_hil_eligible")
            is True
            and after_state.get("available") is True
            and after_state.get("state") == target_name
        )
        change_evidence_complete = (
            changed.get("trace_valid") is True
            and changed.get("trace_complete") is True
            and changed.get("trace_route") == changed.get("route")
            and services.route_matches_request(args.route, changed.get("trace_route"))
            and isinstance(changed.get("transaction"), dict)
            and changed["transaction"].get("session_generation") == generation
        )
        if not change_readback_confirmed:
            evidence = {
                "schema_version": 1,
                "step_id": step_id,
                "initial_state": initial,
                "target_state": target_name,
                "actions": machine_actions,
                "readback_state": after_state.get("state"),
                "outcome": "inconclusive",
            }
            outcome = "inconclusive"
            if changed.get("backend_failed"):
                reason = (
                    "backend grant denied, expired, or execution unconfirmed; no retry"
                )
            elif changed.get("trace_error"):
                reason = "GATT trace could not be armed; no write was sent"
            else:
                reason = (
                    "write/readback is ambiguous; no automatic restoration or retry"
                )
        else:
            try:
                physical_change = services.prompt(
                    f"Physical result for {feature}={target_name}: 1=correct, 2=incorrect, 3=unable to observe: "
                ).strip()
            except EOFError, KeyboardInterrupt:
                physical_change = "3"
            try:
                restore_consent = (
                    services.prompt(
                        f"Fresh readback confirms {target_name}. Type 1 to authorize a separate restoration to {initial}: "
                    ).strip()
                    == "1"
                )
            except EOFError, KeyboardInterrupt:
                restore_consent = False
            if not restore_consent:
                evidence = {
                    "schema_version": 1,
                    "step_id": step_id,
                    "initial_state": initial,
                    "target_state": target_name,
                    "actions": machine_actions,
                    "physical_change_outcome": {
                        "1": "correct",
                        "2": "incorrect",
                        "3": "unable_to_observe",
                    }.get(physical_change, "unable_to_observe"),
                    "outcome": "inconclusive",
                }
                outcome = "inconclusive"
                reason = "restoration was not authorized; manual inspection required"
            else:
                try:
                    restore_caps = api.hil_capabilities(entry_id)
                    restore_state = _candidate_output_state(restore_caps, feature)
                    if (
                        restore_caps.get("verified") is not True
                        or restore_caps.get("identity_conflicted") is not False
                        or restore_caps.get("connected") is not True
                        or restore_caps.get("status_fresh") is not True
                        or restore_caps.get("pending_transaction") is not False
                        or restore_caps.get("session_generation") != generation
                        or restore_caps.get("features", {})
                        .get(feature, {})
                        .get("candidate_hil_eligible")
                        is not True
                        or restore_state.get("available") is not True
                        or restore_state.get("state") != target_name
                    ):
                        raise HAAPIError("Restoration preflight changed")
                    restored = execute(initial == "on")
                    final_caps = api.hil_capabilities(entry_id)
                    final_state = _candidate_output_state(final_caps, feature)
                    restoration_readback_confirmed = (
                        restored.get("outcome") == "confirmed"
                        and restored.get("confirmed") is True
                        and restored.get("session_generation") == generation
                        and restored.get("route") in {"local_adapter", "active_proxy"}
                        and final_caps.get("verified") is True
                        and final_caps.get("identity_conflicted") is False
                        and final_caps.get("connected") is True
                        and final_caps.get("status_fresh") is True
                        and final_caps.get("pending_transaction") is False
                        and final_caps.get("session_generation") == generation
                        and final_caps.get("features", {})
                        .get(feature, {})
                        .get("candidate_hil_eligible")
                        is True
                        and final_state.get("available") is True
                        and final_state.get("state") == initial
                    )
                    restoration_evidence_complete = (
                        restored.get("trace_valid") is True
                        and restored.get("trace_complete") is True
                        and restored.get("trace_route") == restored.get("route")
                        and services.route_matches_request(
                            args.route, restored.get("trace_route")
                        )
                        and isinstance(restored.get("transaction"), dict)
                        and restored["transaction"].get("session_generation")
                        == generation
                    )
                except HAAPIError:
                    restoration_readback_confirmed = False
                    restoration_evidence_complete = False
                    final_state = {"state": None}
                evidence = {
                    "schema_version": 1,
                    "step_id": step_id,
                    "initial_state": initial,
                    "target_state": target_name,
                    "actions": machine_actions,
                    "physical_change_outcome": {
                        "1": "correct",
                        "2": "incorrect",
                        "3": "unable_to_observe",
                    }.get(physical_change, "unable_to_observe"),
                    "restored_state": final_state.get("state"),
                    "outcome": "inconclusive",
                }
                outcome = "inconclusive"
                reason = "restoration lacks confirmed same-session readback"
                if restoration_readback_confirmed:
                    try:
                        physical_restore = services.prompt(
                            "Physical restoration result: 1=original state restored, 2=incorrect, 3=unable to observe: "
                        ).strip()
                    except EOFError, KeyboardInterrupt:
                        physical_restore = "3"
                    evidence["physical_restore_outcome"] = {
                        "1": "correct",
                        "2": "incorrect",
                        "3": "unable_to_observe",
                    }.get(physical_restore, "unable_to_observe")
                    if (
                        evidence["physical_change_outcome"] == "correct"
                        and evidence["physical_restore_outcome"] == "correct"
                    ):
                        if change_evidence_complete and restoration_evidence_complete:
                            evidence["outcome"] = "pass"
                            outcome = "pass"
                            reason = None
                        else:
                            reason = (
                                "physical change and restoration were observed, but "
                                "complete trace-bound transaction evidence is missing"
                            )
    evidence_path = output_dir / f"tier-{step_id}.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    evidence_path.chmod(0o600)
    result = {
        "step_id": step_id,
        "outcome": outcome,
        "observed_routes": sorted(
            {
                action["trace_route"]
                for action in machine_actions
                if action.get("trace_valid") is True
                and action.get("trace_complete") is True
                and action.get("trace_route") == action.get("route")
                and action.get("trace_route") in {"local_adapter", "active_proxy"}
                and services.route_matches_request(args.route, action.get("route"))
            }
        ),
        "state_safe_to_continue": (
            not machine_actions
            or (
                len(machine_actions) >= 2
                and evidence.get("restored_state") == initial
                and evidence.get("physical_restore_outcome") == "correct"
            )
        ),
        "machine_evidence": [
            {
                "path": evidence_path.name,
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            }
        ],
        "human_review": "pending",
    }
    if reason is not None:
        result["reason"] = reason
    return result


def _suite_services() -> SuiteServices:
    """Build the injected runtime dependencies for suite executors."""
    return SuiteServices(
        device_fingerprint=_device_fingerprint,
        has_verified_telemetry=_has_verified_telemetry,
        save_private_trace=_save_private_trace,
        validate_trace_result=_validate_trace_result,
        control_value_available=_control_value_available,
        write_scenario_progress=write_scenario_progress,
        load_scenarios=load_scenarios,
        correlate_transaction=correlate_transaction,
        route_matches_request=route_matches_request,
        verdict=verdict,
        recovery_verdict=recovery_verdict,
        soak_verdict=soak_verdict,
        short_stability_verdict=short_stability_verdict,
        read_only_sample=read_only_sample,
        monotonic=monotonic,
        sleep=sleep,
        prompt=input,
        datetime=datetime,
    )


def _reviewable_tier_step_artifact(
    *,
    parent_private: Path,
    child_private: Path,
    step_id: str,
    feature: str,
    checkpoint_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Bind standard production-suite actions for offline physical review."""
    actions = []
    setting_states = {
        "eco_mode": {"on": True, "off": False},
        "car_charger": {"on": True, "off": False},
        "work_mode": {"mute": 0, "standard": 1, "fast": 2},
        "eco_timeout": {
            "one_hour": 1,
            "two_hours": 2,
            "four_hours": 4,
            "six_hours": 6,
        },
    }

    def protocol_state(value: Any) -> Any:
        if feature in setting_states and isinstance(value, str):
            return setting_states[feature].get(value)
        return value

    for row in checkpoint_rows:
        if row.get("target") != feature or row.get("outcome") != "pass":
            continue
        trace_name = row.get("trace")
        if not isinstance(trace_name, str):
            continue
        trace_dir = (child_private / trace_name).resolve()
        try:
            trace_dir.relative_to(child_private.resolve())
            trace = json.loads((trace_dir / "trace.json").read_text(encoding="utf-8"))
            trace["events"] = [
                json.loads(line)
                for line in (trace_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            trace["frames"] = [
                json.loads(line)
                for line in (trace_dir / "frames.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
        except OSError, ValueError, UnicodeError:
            continue
        evidence = []
        for filename in ("trace.json", "events.jsonl", "frames.jsonl"):
            source = trace_dir / filename
            if not source.is_file():
                continue
            relative = Path(os.path.relpath(source, parent_private)).as_posix()
            evidence.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
        transaction = row.get("transaction")
        generation = row.get("session_generation")
        route = row.get("route")
        kind = (
            "output"
            if feature in {"ac_output", "dc_output", "light_output"}
            else "settings"
        )
        correlated = (
            correlate_transaction(trace, expected_generation=generation, kind=kind)
            if type(generation) is int
            else None
        )
        trace_valid = bool(
            evidence
            and trace.get("complete") is True
            and trace.get("dropped_events") == 0
            and trace.get("dropped_frames") == 0
            and route in {"local_adapter", "active_proxy"}
            and trace.get("observed_route") == route
            and type(generation) is int
            and isinstance(transaction, dict)
            and transaction.get("session_generation") == generation
            and correlated == transaction
        )
        actions.append(
            {
                "outcome": "confirmed" if trace_valid else "inconclusive",
                "confirmed": trace_valid,
                "trace_valid": trace_valid,
                "trace_complete": trace_valid,
                "route": route,
                "trace_route": trace.get("observed_route"),
                "session_generation": generation,
                "transaction": transaction,
                "trace_evidence": evidence,
            }
        )
    rows = [
        row
        for row in checkpoint_rows
        if row.get("target") == feature and row.get("trace")
    ]
    physical = {"yes": "correct", "no": "incorrect"}
    output_feature = feature in {"ac_output", "dc_output", "light_output"}
    if not output_feature:
        if len(actions) != 2 or len(rows) != 2:
            return None
        initial = protocol_state(rows[0].get("pre_state"))
        target = protocol_state(rows[0].get("expected"))
        restored = protocol_state(rows[-1].get("post_state"))
        physical_change = physical.get(
            rows[0].get("physical_outcome"), "unable_to_observe"
        )
        physical_restore = physical.get(
            rows[-1].get("physical_outcome"), "unable_to_observe"
        )
        complete = (
            all(action.get("confirmed") is True for action in actions)
            and initial != target
            and restored == initial
            and physical_change == "correct"
            and physical_restore == "correct"
        )
        return {
            "schema_version": 1,
            "step_id": step_id,
            "initial_state": initial,
            "target_state": target,
            "restored_state": restored,
            "physical_change_outcome": physical_change,
            "physical_restore_outcome": physical_restore,
            "outcome": "pass" if complete else "inconclusive",
            "actions": actions,
        }
    if len(actions) != 4 or len(rows) != 4:
        return None
    output_cycles = [
        {
            "initial_state": protocol_state(rows[0].get("pre_state")),
            "target_state": protocol_state(rows[0].get("expected")),
            "change_action_index": 0,
            "restore_action_index": 1,
            "physical_change_outcome": physical.get(
                rows[0].get("physical_outcome"), "unable_to_observe"
            ),
            "physical_restore_outcome": physical.get(
                rows[1].get("physical_outcome"), "unable_to_observe"
            ),
        },
        {
            "initial_state": protocol_state(rows[1].get("pre_state")),
            "target_state": protocol_state(rows[1].get("expected")),
            "change_action_index": 1,
            "restore_action_index": 2,
            "physical_change_outcome": physical.get(
                rows[1].get("physical_outcome"), "unable_to_observe"
            ),
            "physical_restore_outcome": physical.get(
                rows[2].get("physical_outcome"), "unable_to_observe"
            ),
        },
    ]
    complete = (
        all(item.get("confirmed") is True for item in actions)
        and [row.get("expected") for row in rows] == ["on", "off", "on", "off"]
        and rows[0].get("pre_state") == "off"
        and rows[-1].get("post_state") == "off"
        and all(row.get("physical_outcome") == "yes" for row in rows)
    )
    return {
        "schema_version": 1,
        "step_id": step_id,
        "initial_state": protocol_state(rows[0].get("pre_state")),
        "target_state": protocol_state(rows[0].get("expected")),
        "restored_state": protocol_state(rows[-1].get("post_state")),
        "physical_change_outcome": physical.get(
            rows[0].get("physical_outcome"), "unable_to_observe"
        ),
        "physical_restore_outcome": physical.get(
            rows[-1].get("physical_outcome"), "unable_to_observe"
        ),
        "output_cycles": output_cycles,
        "final_restore_action_index": 3,
        "final_restore_outcome": physical.get(
            rows[-1].get("physical_outcome"), "unable_to_observe"
        ),
        "outcome": "pass" if complete else "inconclusive",
        "actions": actions,
    }


def _execute_output_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    environment: dict[str, Any],
    output_dir: Path,
    run_id: str,
) -> int:
    from .outputs import _execute_output_suite as execute_suite

    return execute_suite(
        api, args, device, environment, output_dir, run_id, _suite_services()
    )


def _execute_settings_write(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    scenario: Any,
    key: str,
    entity_id: str,
    target: str,
    expected_pre_state: str,
    generation: int,
    trace_name: str,
    on_update: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    from .settings import _execute_settings_write as execute_write

    return execute_write(
        api,
        args,
        device,
        output_dir,
        run_id,
        scenario,
        key,
        entity_id,
        target,
        expected_pre_state,
        generation,
        trace_name,
        on_update,
        _suite_services(),
    )


def _execute_settings_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
) -> int:
    from .settings import _execute_settings_suite as execute_suite

    return execute_suite(api, args, device, output_dir, run_id, _suite_services())


def _execute_recovery_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    resume_state: dict[str, Any] | None = None,
) -> int:
    from .recovery import _execute_recovery_suite as execute_suite

    return execute_suite(
        api, args, device, output_dir, run_id, _suite_services(), resume_state
    )


def _execute_soak_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    resume_state: dict[str, Any] | None = None,
) -> int:
    from .soak import _execute_soak_suite as execute_suite

    return execute_suite(
        api, args, device, output_dir, run_id, _suite_services(), resume_state
    )


def execute(args: argparse.Namespace) -> int:
    """Execute a validated command; credentials stay in memory for live commands."""
    if args.command == "onboard":
        if args.non_interactive or not sys.stdin.isatty():
            raise HAAPIError("Onboarding requires an interactive terminal")
        args.onboarding = True
        args.command = "run"
        args.suite = "read-only"
        args.tier = None
        args.list_suites = False
        args.resume = None
        args.simulate = False
        args.dry_run = False
        args.scenario_id = None
        args.allow_keepalive_disabled = False
    if args.command == "import":
        return import_archive(args.archive, matrix_path=args.matrix, apply=args.apply)
    if args.command == "review":
        if args.interactive:
            if args.dry_run or args.apply:
                raise HAAPIError(
                    "--interactive cannot be combined with --dry-run or --apply; "
                    "interactive review has its own dry-run and confirmation"
                )
            from .review_wizard import interactive_review

            if args.tier == "bronze":
                return interactive_review(args.matrix, run_id=args.run_id)
            return interactive_review(args.matrix, run_id=args.run_id, tier=args.tier)
        if args.tier != "bronze":
            raise HAAPIError("--tier applies only to --interactive review")
        from .review import review_imported_run

        return review_imported_run(
            args.matrix,
            run_id=args.run_id,
            review_file=args.review_file,
            apply=args.apply,
        )
    if args.command == "tiers":
        from .tiers import evaluate_tiers
        from scripts.validate_hil_qualification import validate_matrix

        try:
            payload = json.loads(args.matrix.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            raise HAAPIError("Unable to read qualification matrix") from ex
        errors, _ = validate_matrix(
            payload,
            matrix_path=args.matrix,
            require_pass=False,
            max_age_days=None,
        )
        if errors:
            raise HAAPIError(
                "Qualification matrix validation failed: " + "; ".join(errors)
            )
        print(json.dumps(evaluate_tiers(payload), indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        write_report(args.run_directory)
        print("HIL report written to: public/report.md")
        return 0
    if args.command == "export":
        confirmed = args.confirm_export
        if not confirmed and not args.non_interactive and sys.stdin.isatty():
            try:
                export_archive(args.run_directory, confirmed=False)
            except HAAPIError as ex:
                if str(ex) != "Export requires explicit confirmation":
                    raise
            confirmed = (
                input("Type EXPORT to create the local public evidence ZIP: ").strip()
                == "EXPORT"
            )
            if not confirmed:
                return 2
        if args.non_interactive and not confirmed:
            print(
                "Export preview follows; rerun with --confirm-export to create the ZIP."
            )
            try:
                export_archive(args.run_directory, confirmed=False)
            except HAAPIError as ex:
                if str(ex) == "Export requires explicit confirmation":
                    return 2
                raise
            return 2
        export_archive(args.run_directory, confirmed=confirmed)
        print("HIL evidence archive written to: hil-evidence.zip")
        return 0
    if args.command == "run":
        if args.tier is not None and args.suite not in (None, "read-only"):
            raise HAAPIError(
                "--tier currently accompanies the read-only evidence capture"
            )
        if args.list_suites:
            print("Available HIL suites:")
            for suite in SUITES:
                print(f"  {suite}")
            return 0
        if args.resume is not None and (
            args.suite not in {"recovery", "soak-24h", "stability-short"}
            or args.dry_run
            or args.list_suites
        ):
            raise HAAPIError("This scenario cannot be resumed")
        if args.simulate:
            if args.suite != "transaction-safety" or args.dry_run:
                raise HAAPIError(
                    "--simulate is supported only for run --suite transaction-safety"
                )
            from .transaction_safety import execute_transaction_safety_simulation

            return execute_transaction_safety_simulation(args.output_root)
        if args.suite == "transaction-safety":
            raise HAAPIError(
                "Transaction-safety live execution is not enabled; use --simulate "
                "for the offline rehearsal"
            )
        if args.suite == "settings" and args.scenario_id is None:
            raise HAAPIError("Select a settings scenario with --scenario-id")
        if args.scenario_id is not None:
            if args.suite not in {"outputs", "settings"}:
                raise HAAPIError(
                    "--scenario-id is currently supported only for outputs and settings"
                )
            if not any(
                item.id == args.scenario_id and item.suite == args.suite
                for item in load_scenarios()
            ):
                raise HAAPIError("Unknown selected scenario")
        if args.dry_run:
            suite = args.suite or "read-only"
            planned = plan_scenarios(suite, non_interactive=args.non_interactive)
            if args.scenario_id is not None:
                planned = tuple(
                    item for item in planned if item.scenario.id == args.scenario_id
                )
            if not planned:
                print(
                    f"blocked: {suite} — no runnable scenario declarations are available yet"
                )
            for item in planned:
                entry = plan_as_dict(item)
                status = "eligible" if entry["eligible"] else "blocked"
                print(f"{status}: {entry['id']} [{entry['suite']}] — {entry['reason']}")
                print("  requested actions:", ", ".join(entry["requested_effects"]))
            return 0
        if args.suite not in (
            None,
            "read-only",
            "outputs",
            "settings",
            "recovery",
            "soak-24h",
            "stability-short",
        ):
            raise HAAPIError(
                "This suite is declarative but execution is not implemented yet; use --dry-run"
            )
        if (
            getattr(args, "allow_keepalive_disabled", False)
            and args.suite != "soak-24h"
        ):
            raise HAAPIError("--allow-keepalive-disabled applies only to soak-24h")
        if args.suite in {"outputs", "settings", "recovery"} and args.non_interactive:
            raise HAAPIError(f"The {args.suite} suite requires interactive operation")
    resume_state: dict[str, Any] | None = None
    resume_dir: Path | None = None
    if args.command == "run" and args.resume is not None:
        resume_dir, resume_state = load_scenario_checkpoint(args.resume)
        expected_scenarios = {
            "recovery": "recovery_reconnect",
            "soak-24h": "soak_24h_observation",
            "stability-short": "short_stability_observation",
        }
        if resume_state["scenario_id"] != expected_scenarios[args.suite]:
            raise HAAPIError("Checkpoint does not belong to the selected scenario")
    url, token = _credential(args)
    api = HAClient(url, token, allow_insecure_http=args.allow_insecure_http)
    config, manifest, devices = _discovery(api)
    # Optional admin-only HA Bluetooth scanner inventory. Its raw scanner
    # identifiers and BLE addresses never leave HAClient; only counts do.
    get_scanners = getattr(api, "scanner_candidates", None)
    candidates = get_scanners() if callable(get_scanners) else None
    get_fingerprint = getattr(api, "integration_build_fingerprint", None)
    remote_fingerprint = get_fingerprint() if callable(get_fingerprint) else None
    environment = build_environment(config, manifest, candidates, remote_fingerprint)
    print("Home Assistant:", environment["home_assistant"]["version"]["value"])
    installed = environment["integration"]["installed_manifest_version"]["value"]
    print("ALLPOWERS manifest version:", installed or "unknown")
    integration = environment["integration"]
    print("Installed integration build:", integration["build_match"])
    installed_commit = integration["installed_commit"]["value"]
    print(
        "Installed integration commit:",
        installed_commit or integration["verification_reason"],
    )
    _device_list(devices)
    print(
        "Bluetooth route: not verified by preflight; "
        "use 'trace' for connected-backend attribution"
    )
    print(
        "HA host / adapter / proxy details: unavailable without an authorized collector"
    )
    if args.command == "probe-trace":
        return _execute_probe_trace(api, args, environment)
    if args.command == "doctor":
        if not devices:
            print(
                "No integration-owned devices found; verify setup and "
                "registry permissions"
            )
            return 2
        inspected = (
            [choose_device(devices, args.device, non_interactive=args.non_interactive)]
            if args.device
            else devices
        )
        assessments = []
        for device in inspected:
            # Read actual binary sensor states; a loaded config entry alone
            # cannot prove a station is connected or telemetry is fresh.
            assessment = evaluate_preflight(device, read_only_sample(api, device))
            assessments.append(assessment)
            checks = assessment["checks"]
            print(
                "Readiness: integration={entry_loaded}; BLE={connected}; "
                "telemetry={telemetry_fresh}; overall={overall}".format(
                    **checks, overall=assessment["overall"]
                )
            )
        if all(item["overall"] == "ready" for item in assessments):
            print(
                "Preflight ready at the observed HA snapshot; "
                "no HIL qualification granted"
            )
            return 0
        print(
            "Preflight inconclusive or incomplete: check integration setup, "
            "BLE connectivity, and diagnostic telemetry_available (disabled "
            "by default). Read-only run remains available; "
            "no HIL qualification granted."
        )
        return 2

    selected = choose_device(devices, args.device, non_interactive=args.non_interactive)
    if getattr(args, "onboarding", False):
        print(
            f"Selected {selected.model} (hardware {selected.hardware or 'unknown'}, "
            f"firmware {selected.firmware or 'unknown'})."
        )
        print("Installed integration build:", integration["build_match"])
        print("Connected BLE route: unknown until a connected-backend trace proves it.")
        scanners = environment["home_assistant"]["bluetooth_scanner_candidates"]
        scanner_counts = scanners.get("value") if isinstance(scanners, dict) else None
        if isinstance(scanner_counts, dict):
            inventory = (
                ", ".join(
                    f"{kind}: {count}"
                    for kind, count in sorted(scanner_counts.items())
                    if isinstance(kind, str) and type(count) is int
                )
                or "none reported"
            )
            print("HA scanner candidates (not connected-route proof):", inventory)
        else:
            print("HA scanner candidate inventory: unknown or unavailable")
        print("Runner-host Bluetooth hardware does not identify the remote HA adapter.")
        print("Choose desired tier: 1=Bronze, 2=Silver, 3=Gold, 4=Platinum (blocked).")
        tier_choice = input("Desired tier: ").strip()
        tiers = {"1": "bronze", "2": "silver", "3": "gold", "4": "platinum"}
        if tier_choice not in tiers:
            raise HAAPIError("Invalid tier selection")
        requested_tier = tiers[tier_choice]
        print(f"Requested tier: {requested_tier.title()}")
        if requested_tier == "platinum":
            print(
                "Platinum remains blocked: the public evidence contract cannot "
                "verify a scoped, continuous 24-hour soak. No HIL run was started."
            )
            return 2
        repo_root = Path(__file__).resolve().parents[2]
        if _current_feature_branch(repo_root) is None:
            print(
                "Cannot start the qualification workflow: select a clean "
                "feature branch before running HIL. No tests were started."
            )
            print("  git fetch origin")
            print("  git switch -c feat/hil-qualification origin/devel")
            print("  git status --short")
            return 2
        args.tier = requested_tier
    if resume_state is not None:
        fingerprint = _device_fingerprint(
            resume_state["run_id"], selected, api.origin, args.route
        )
        if not hmac.compare_digest(fingerprint, resume_state["device_fingerprint"]):
            raise HAAPIError("Selected device does not match the checkpoint")
    entry_id: str | None = None
    if args.command == "trace":
        if not 1 <= args.duration <= 30:
            raise HAAPIError(
                "Trace duration must be 1-30 seconds; split long scenarios"
            )
        if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", args.scenario_id) is None:
            raise HAAPIError("Scenario ID must be 1-80 safe characters")
        _, registry_devices, _, entries = api.discovery()
        if entries is None:
            raise HAAPIError("Admin config entry registry is required for HIL trace")
        matched = [
            row
            for row in registry_devices
            if isinstance(row, dict) and row.get("id") == selected.device_id
        ]
        if len(matched) != 1:
            raise HAAPIError("Selected HA device registry record is ambiguous")
        allowed = {
            row["entry_id"]
            for row in entries
            if isinstance(row, dict)
            and row.get("domain") == "allpowers_ble"
            and isinstance(row.get("entry_id"), str)
            and row.get("state") == "loaded"
        }
        candidates = set()
        value = matched[0].get("config_entry_id")
        if isinstance(value, str) and value in allowed:
            candidates.add(value)
        other = matched[0].get("config_entries")
        if isinstance(other, list):
            candidates.update(v for v in other if isinstance(v, str) and v in allowed)
        if len(candidates) != 1:
            raise HAAPIError("Cannot determine one loaded ALLPOWERS config entry")
        entry_id = next(iter(candidates))
    run_id = resume_state["run_id"] if resume_state is not None else new_run_id()
    sample = read_only_sample(api, selected)
    session = build_session(
        run_id=run_id,
        requested_route=args.route,
        device=selected,
        environment=environment,
        sample=sample,
    )
    tier_plan: dict[str, Any] | None = None
    tier_capabilities: dict[str, Any] | None = None
    if args.command == "run" and (args.suite == "read-only" or args.tier is not None):
        capabilities: dict[str, Any] | None = None
        unavailable_reason = "no unique loaded integration entry found"
        if selected.config_entry_id is not None:
            try:
                capabilities = api.hil_capabilities(selected.config_entry_id)
            except HAAPIError:
                unavailable_reason = "admin capability snapshot unavailable"
        session["runtime_capabilities"] = (
            capabilities if capabilities is not None else unknown(unavailable_reason)
        )
        session["controls"] = _scenario_controls(api, selected, capabilities)
        if args.tier is not None:
            from .guided import build_tier_plan

            # The requested route is not route evidence. The plan remains
            # blocked until a connected backend has been attributed by trace.
            test_plan = build_tier_plan(
                args.tier,
                capabilities,
                session["controls"],
                observed_route=None,
                build_provenance={
                    "checkout_commit": integration.get(
                        "runner_checkout_commit", {}
                    ).get("value"),
                    "installed_commit": integration.get("installed_commit", {}).get(
                        "value"
                    ),
                    "build_match": integration.get("build_match"),
                },
            )
            tier_plan = test_plan
            tier_capabilities = capabilities
            session["test_plan"] = test_plan
            print(
                f"Guided {args.tier.title()} plan for {test_plan['device']['model']} "
                f"(hardware {test_plan['device']['hardware_revision'] or 'unknown'}, "
                f"firmware {test_plan['device']['firmware_revision'] or 'unknown'})"
            )
            print("Verified route:", test_plan["device"]["verified_route"] or "unknown")
            print("Estimated operator actions:", test_plan["operator_actions_estimate"])
            print(
                "Expected time: varies by applicable steps; physical observations are manual."
            )
            print(
                "Safety: disconnect critical loads, remain present, and authorize "
                "each state change and restoration separately."
            )
            for step in test_plan["steps"]:
                status, reasons = _pending_plan_status(step)
                reasons = reasons or "ready for the existing guided suite"
                print(f"  {status}: {step['id']} ({step['feature']}) — {reasons}")
            print("Restoration:", test_plan["restoration_plan"])
            if getattr(args, "onboarding", False):
                print(
                    "Acquisition is not qualification; reviewed assurance and tier are "
                    "computed only after explicit offline review."
                )
                if (
                    input("Type RUN after reviewing the plan to continue: ").strip()
                    != "RUN"
                ):
                    print("Onboarding cancelled before tests started.")
                    return 2
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    if resume_state is not None:
        if resume_dir is None:
            raise HAAPIError("Missing validated recovery directory")
        output_dir = resume_dir
    else:
        output_dir = write_run(
            args.output_root, f"{timestamp}_{run_id}", environment, session
        )
    if args.command == "run" and tier_plan is not None:
        result = _execute_tier_suite(
            api,
            args,
            selected,
            environment,
            output_dir,
            run_id,
            tier_plan,
            tier_capabilities,
        )
        if getattr(args, "onboarding", False):
            return _onboard_export(
                output_dir,
                result,
                requested_tier=tier_plan["requested_tier"],
                device=selected.model,
                route=args.route,
                ha_url=getattr(api, "origin", args.ha_url or ""),
                output_root=args.output_root,
            )
        return result
    if args.command == "run" and args.suite == "outputs":
        return _execute_output_suite(
            api, args, selected, environment, output_dir, run_id
        )
    if args.command == "run" and args.suite == "settings":
        return _execute_settings_suite(api, args, selected, output_dir, run_id)
    if args.command == "run" and args.suite == "recovery":
        return _execute_recovery_suite(
            api, args, selected, output_dir, run_id, resume_state
        )
    if args.command == "run" and args.suite == "soak-24h":
        result = _execute_soak_suite(
            api, args, selected, output_dir, run_id, resume_state
        )
        if getattr(args, "onboarding", False):
            resume_command = (
                "python -m scripts.hil run --suite soak-24h --resume "
                f"{_shell_quote(output_dir)} --ha-url {_shell_quote(api.origin)}"
            )
            return _onboard_export(
                output_dir,
                result,
                requested_tier="platinum",
                device=selected.model,
                route=args.route,
                ha_url=getattr(api, "origin", args.ha_url or ""),
                output_root=args.output_root,
                resume_command=resume_command,
            )
        return result
    if args.command == "run" and args.suite == "stability-short":
        return _execute_soak_suite(
            api, args, selected, output_dir, run_id, resume_state
        )
    if args.command == "trace":
        if entry_id is None:
            raise HAAPIError("Missing HIL config entry")
        start = api.websocket(
            [
                {
                    "type": "allpowers_ble/hil/start",
                    "entry_id": entry_id,
                    "run_id": run_id,
                    "scenario_id": args.scenario_id,
                    "requested_route": args.route,
                }
            ]
        )[0]
        if (
            not isinstance(start, dict)
            or start.get("schema_version") not in (1, 2)
            or not isinstance(start.get("capture_id"), str)
        ):
            raise HAAPIError("Unexpected HIL capture start response")
        capture_id = start["capture_id"]
        print("Private GATT capture active; no new BLE connection is opened")
        try:
            sleep(args.duration)
        finally:
            stop_command = {
                "type": "allpowers_ble/hil/stop",
                "entry_id": entry_id,
                "capture_id": capture_id,
            }
            try:
                result = api.websocket([stop_command])[0]
            except HAAPIError:
                # A lost stop response must not strand sealed evidence in HA.
                result = api.websocket(
                    [
                        {
                            "type": "allpowers_ble/hil/fetch",
                            "entry_id": entry_id,
                            "capture_id": capture_id,
                        }
                    ]
                )[0]
        _validate_trace_result(
            result,
            run_id=run_id,
            scenario_id=args.scenario_id,
            capture_id=capture_id,
            requested_route=args.route,
            expected_version=start["schema_version"],
        )
        _save_private_trace(output_dir, result)
        print("Private HIL trace saved to:", run_root(output_dir))
        print(
            "Capture completeness:",
            "complete" if result.get("complete") else "incomplete",
        )
        print(f"GATT frames recorded: {result['frame_count']}")
        print(
            f"Dropped events / frames: {result.get('dropped_events')} / "
            f"{result.get('dropped_frames')}"
        )
        print("Capture termination:", result["termination_reason"])
        print(
            "Observed connection route:",
            result["observed_route"],
            "(GATT backend only; physical HIL PASS not permitted)",
        )
        return 0 if result["complete"] and result["frame_count"] else 2
    print("Read-only preflight saved to:", run_root(output_dir))
    print("Read-only readiness:", session["preflight"]["overall"])
    print(
        "Qualification: inconclusive (no GATT trace, physical proof or verified route)"
    )
    return 0


def _onboard_export(
    output_dir: Path,
    run_result: int,
    *,
    requested_tier: str = "bronze",
    device: str | None = None,
    route: str = "auto",
    ha_url: str = "",
    output_root: Path | None = None,
    resume_command: str | None = None,
) -> int:
    """Gate the run before offering a local privacy-reviewed export."""
    execution_path = output_dir / "tier-execution.json"
    blockers = assess_run_gate(
        output_dir,
        run_result=run_result,
        requested_tier=requested_tier,
        status_exchange=_bronze_status_exchange,
    )
    if execution_path.is_file():
        try:
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            execution = {}
        routes = (
            execution.get("verified_routes", []) if isinstance(execution, dict) else []
        )
        if isinstance(routes, list):
            observed = sorted(
                route for route in routes if route in {"local_adapter", "active_proxy"}
            )
            print(
                "Observed connected BLE route:",
                ", ".join(observed) if observed else "unknown / not proven",
            )
    if blockers:
        print("\nTEST GATE: FAILED — onboarding stopped before export.")
        for blocker in blockers:
            print(
                f"  {blocker['step_id']}: {blocker['outcome']} — "
                f"{blocker['reason']}\n    Evidence: {blocker['evidence']}"
            )
        print(
            "No ZIP export prompt, import, review, or PR guidance was started. "
            "Private evidence remains local."
        )
        if resume_command is not None and requested_tier == "platinum":
            try:
                _, checkpoint = load_scenario_checkpoint(output_dir)
            except HAAPIError:
                checkpoint = None
            resumable = bool(
                isinstance(checkpoint, dict)
                and checkpoint.get("scenario_id") == "soak_24h_observation"
                and isinstance(checkpoint.get("steps"), list)
                and checkpoint["steps"]
                and not any(
                    isinstance(step, dict) and step.get("outcome") == "pass"
                    for step in checkpoint["steps"]
                )
            )
            if resumable:
                print("To resume the existing Platinum soak, run:")
                print(resume_command)
        rerun = ["python", "-m", "scripts.hil", "onboard"]
        if device:
            rerun.extend(("--device", device))
        rerun.extend(("--route", route))
        if ha_url:
            rerun.extend(("--ha-url", ha_url))
        if output_root is not None:
            rerun.extend(("--output-root", str(output_root.resolve())))
        doctor = ["python", "-m", "scripts.hil", "doctor"]
        if device:
            doctor.extend(("--device", device))
        doctor.extend(("--route", route))
        if ha_url:
            doctor.extend(("--ha-url", ha_url))
        print("\nTo diagnose and rerun after addressing the reported requirement:")
        print("  " + _shell_join(doctor))
        print("  " + _shell_join(rerun))
        print("  python -m scripts.hil report " + _shell_quote(output_dir.resolve()))
        print("  python -m scripts.hil export " + _shell_quote(output_dir.resolve()))
        return 2

    print(
        "TEST GATE: PASS (eligible for explicit operator attestation or separate "
        "independent evidence review; not a hardware qualification)."
    )
    report_path = write_report(output_dir)
    report_path = report_path.resolve()
    print("Report:", report_path)
    try:
        report_text = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as ex:
        print("Unable to display the generated public report:", ex)
        return 2
    print("Report excerpt (generated from public/report.md):")
    excerpt = report_text[:12000]
    print(excerpt)
    if len(report_text) > len(excerpt):
        print("[report excerpt truncated]")
    print(
        "The verified ZIP is an acquisition snapshot, not a qualification. "
        "Review and tier evaluation happen after explicit import."
    )
    if not (output_dir / "session.json").is_file():
        print("No valid private run is available for export.")
        return 2
    try:
        export_choice = (
            input("Review and create a privacy-safe evidence ZIP now? [y/N]: ")
            .strip()
            .casefold()
        )
    except EOFError, KeyboardInterrupt:
        return run_result or 2
    if export_choice not in {"y", "yes"}:
        print("No archive created. The private run remains on this machine.")
        return 2
    try:
        export_archive(output_dir, confirmed=False)
    except HAAPIError as ex:
        if str(ex) != "Export requires explicit confirmation":
            print("The available run could not be exported:", ex)
            return run_result or 2
    try:
        confirmed = (
            input(
                "Type EXPORT after reviewing the ZIP contents to create it locally: "
            ).strip()
            == "EXPORT"
        )
    except EOFError, KeyboardInterrupt:
        return run_result or 2
    if not confirmed:
        print("Export cancelled; no archive created.")
        return 2
    try:
        archive_path = export_archive(output_dir, confirmed=True)
        from .evidence import verify_public_archive

        verify_public_archive(archive_path)
    except HAAPIError as ex:
        print("The reviewed archive could not be created or verified:", ex)
        return 2
    print("Privacy-safe HIL evidence archive written:", archive_path.resolve())
    if _onboard_import_review(archive_path, output_dir, requested_tier):
        return 0
    return 2


def _current_feature_branch(repo_root: Path) -> str | None:
    """Return a current safe branch name from a clean checkout, if available."""
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    if branch in {"", "devel", "main"} or status.strip():
        return None
    return branch


def _review_resume_command(matrix_path: Path, run_id: str, tier: str = "bronze") -> str:
    command = (
        "python -m scripts.hil review --matrix "
        f"{_shell_quote(matrix_path)} --run-id {_shell_quote(run_id)} --interactive"
    )
    if tier != "bronze":
        command += " --tier " + _shell_quote(tier)
    return command


def _verified_review_stage_paths(
    matrix: dict[str, Any],
    matrix_path: Path,
    repo_root: Path,
    *,
    revision_id: str,
    run_id: str,
    tier: str = "bronze",
) -> list[str] | None:
    """Return only existing, digest-verified public files for one reviewed run."""
    from .importer import _reject_symlink_components
    from .review import CLAIMS

    if (
        re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", revision_id) is None
        or re.fullmatch(r"archive-[a-f0-9]{64}", run_id) is None
    ):
        return None
    matches = [
        run
        for revision in matrix.get("revisions", [])
        if isinstance(revision, dict) and revision.get("id") == revision_id
        for run in revision.get("runs", [])
        if isinstance(run, dict) and run.get("run_id") == run_id
    ]
    if len(matches) != 1:
        return None
    run = matches[0]
    # Include every matrix artifact so the printed git add command can produce
    # a checkout in which validate_matrix can verify all included runs.
    stage_references: set[str] = set()
    digests: dict[str, str] = {}
    try:
        for matrix_revision in matrix.get("revisions", []):
            if not isinstance(matrix_revision, dict):
                return None
            matrix_revision_id = matrix_revision.get("id")
            if (
                not isinstance(matrix_revision_id, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", matrix_revision_id)
                is None
            ):
                return None
            for matrix_run in matrix_revision.get("runs", []):
                if not isinstance(matrix_run, dict):
                    return None
                matrix_run_id = matrix_run.get("run_id")
                if (
                    not isinstance(matrix_run_id, str)
                    or re.fullmatch(r"archive-[a-f0-9]{64}", matrix_run_id) is None
                ):
                    return None
                references = matrix_run.get("evidence")
                if (
                    not isinstance(references, list)
                    or not references
                    or any(not isinstance(item, str) for item in references)
                    or len(set(references)) != len(references)
                ):
                    return None
                run_references = set(references)
                for section in (
                    "bronze_evidence",
                    "write_capabilities",
                    "scenarios",
                    "feature_presence",
                ):
                    rows = matrix_run.get(section)
                    if not isinstance(rows, dict):
                        continue
                    for item in rows.values():
                        evidence_refs = (
                            item.get("evidence") if isinstance(item, dict) else None
                        )
                        if isinstance(evidence_refs, list):
                            if any(
                                not isinstance(reference, str)
                                for reference in evidence_refs
                            ):
                                return None
                            run_references.update(evidence_refs)
                expected_run_dir = (
                    PurePosixPath("tests/hil/fixtures")
                    / matrix_revision_id
                    / "runs"
                    / matrix_run_id
                )
                for reference in run_references:
                    relative = PurePosixPath(reference)
                    if (
                        "\\" in reference
                        or relative.is_absolute()
                        or any(part in {"", ".", ".."} for part in relative.parts)
                        or not relative.is_relative_to(expected_run_dir)
                    ):
                        return None
                    target = repo_root.joinpath(*relative.parts)
                    _reject_symlink_components(target, repo_root)
                    metadata = target.lstat()
                    if not stat.S_ISREG(metadata.st_mode):
                        return None
                    if matrix_run_id == run_id:
                        digests[reference] = hashlib.sha256(
                            target.read_bytes()
                        ).hexdigest()
                stage_references.update(run_references)
    except HAAPIError, OSError, ValueError:
        return None

    if tier == "bronze":
        bronze = run.get("bronze_evidence")
        if not isinstance(bronze, dict) or set(bronze) != set(CLAIMS):
            return None
        for claim in CLAIMS:
            item = bronze.get(claim)
            review = item.get("physical_review") if isinstance(item, dict) else None
            claim_refs = item.get("evidence") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or item.get("status") != "pass"
                or not isinstance(review, dict)
                or review.get("outcome") != "confirmed_pass"
                or not isinstance(review.get("reviewed_by"), str)
                or not review["reviewed_by"].strip()
                or not isinstance(claim_refs, list)
                or not claim_refs
                or any(
                    not isinstance(reference, str) or reference not in digests
                    for reference in claim_refs
                )
                or review.get("evidence_sha256")
                != {reference: digests[reference] for reference in sorted(claim_refs)}
                or item.get("claim_artifact") not in claim_refs
            ):
                return None
    elif tier in {"silver", "gold"}:
        controls = run.get("write_capabilities")
        scenarios = run.get("scenarios")
        if not isinstance(controls, dict) or not isinstance(scenarios, dict):
            return None
        reviewed_controls = [
            item
            for item in controls.values()
            if isinstance(item, dict)
            and item.get("status") == "pass"
            and isinstance(item.get("physical_review"), dict)
        ]
        reviewed_scenarios = [
            item
            for item in scenarios.values()
            if isinstance(item, dict)
            and item.get("status") == "pass"
            and isinstance(item.get("physical_review"), dict)
        ]
        if not reviewed_controls and not reviewed_scenarios:
            return None
    else:
        return None
    try:
        matrix_relative = matrix_path.relative_to(repo_root).as_posix()
        _reject_symlink_components(matrix_path, repo_root)
    except HAAPIError, OSError, ValueError:
        return None
    return [matrix_relative, *sorted(stage_references)]


def _isolated_run_tier(
    matrix: dict[str, Any], *, revision_id: str, run_id: str, firmware: str, route: str
) -> str:
    """Evaluate one reviewed run without letting older scope evidence fill gaps."""
    item = _isolated_run_summary(
        matrix,
        revision_id=revision_id,
        run_id=run_id,
        firmware=firmware,
        route=route,
    )
    return item.get("tier", "unqualified") if item is not None else "unqualified"


def _isolated_run_summary(
    matrix: dict[str, Any], *, revision_id: str, run_id: str, firmware: str, route: str
) -> dict[str, Any] | None:
    """Return the evaluated exact scope with every other run removed."""
    isolated = json.loads(json.dumps(matrix))
    for revision in isolated.get("revisions", []):
        if isinstance(revision, dict):
            revision["runs"] = [
                item
                for item in revision.get("runs", [])
                if isinstance(item, dict) and item.get("run_id") == run_id
            ]
    from .tiers import evaluate_tiers

    result = evaluate_tiers(isolated)
    return next(
        (
            item
            for item in result.get("tiers", [])
            if item.get("scope", {}).get("revision_id") == revision_id
            and item.get("scope", {}).get("firmware") == firmware
            and item.get("scope", {}).get("observed_route") == route
        ),
        None,
    )


def _onboard_import_review(
    archive_path: Path,
    output_dir: Path,
    requested_tier: str,
    *,
    repo_root: Path | None = None,
    matrix_path: Path | None = None,
) -> bool:
    """Import reviewed public evidence, invoke the requested review, and hand off."""
    if requested_tier == "platinum":
        print(
            "Platinum review remains blocked by the continuous-soak evidence contract."
        )
        return False
    from . import importer

    repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    matrix_path = matrix_path or (repo_root / "tests/hil/qualification_matrix.json")
    try:
        proposal = importer._build_proposal(
            archive_path, matrix_path, repo_root=repo_root
        )
    except HAAPIError as ex:
        print("Import preview failed; no repository files were changed:", ex)
        return False
    importer._print_preview(proposal)
    try:
        accepted = input(
            "Import this reviewed evidence? [y/N]: "
        ).strip().casefold() in {"y", "yes"}
        if not accepted:
            print("Import declined; no repository files were changed.")
            return False
        branch = _current_feature_branch(repo_root)
        if branch is None:
            print("Cannot import safely: select a clean feature branch based on devel.")
            print("  git status --short")
            print("  git switch -c feat/hil-evidence origin/devel")
            return False
        if (
            input("Type APPLY IMPORT to write these exact files: ").strip()
            != "APPLY IMPORT"
        ):
            print("Import cancelled; no repository files were changed.")
            return False
    except EOFError, KeyboardInterrupt:
        print("Import cancelled; no repository files were changed.")
        return False
    try:
        importer._apply_proposal(proposal)
    except HAAPIError as ex:
        print("Import failed safely:", ex)
        return False

    run_id = proposal["run_id"]
    try:
        print("Choose how this imported run will be reviewed:")
        print("  [1] Operator attestation by the person who performed the run")
        print("  [2] Independent review by a different person")
        choice = input("Review route (default: leave inconclusive): ").strip()
        expected_assurance = {
            "1": "operator_attested",
            "2": "independently_reviewed",
        }.get(choice)
    except EOFError, KeyboardInterrupt:
        expected_assurance = None
    if expected_assurance is None:
        print(
            "Evidence remains imported and unqualified; no PR commands were generated."
        )
        print("Later, run: " + _review_resume_command(matrix_path, run_id))
        return False
    if expected_assurance == "independently_reviewed":
        try:
            reviewer_available = (
                input(
                    "Hand the terminal to a different reviewer, then type REVIEWER READY: "
                ).strip()
                == "REVIEWER READY"
            )
        except EOFError, KeyboardInterrupt:
            reviewer_available = False
        if not reviewer_available:
            print(
                "Evidence remains imported and unqualified; no PR commands were generated."
            )
            print("Later, run: " + _review_resume_command(matrix_path, run_id))
            return False
    try:
        from .review_wizard import interactive_review

        review_tiers = ["bronze"]
        if requested_tier in {"silver", "gold"}:
            review_tiers.append(requested_tier)
        for review_tier in review_tiers:
            if (
                interactive_review(
                    matrix_path,
                    run_id=run_id,
                    repo_root=repo_root,
                    expected_assurance=expected_assurance,
                    **({} if review_tier == "bronze" else {"tier": review_tier}),
                )
                != 0
            ):
                print("To continue the offline review later, run:")
                print("  " + _review_resume_command(matrix_path, run_id, review_tier))
                return False
    except (HAAPIError, EOFError, KeyboardInterrupt) as ex:
        print("Explicit review did not complete:", ex)
        print("To continue the offline review later, run:")
        print("  " + _review_resume_command(matrix_path, run_id, requested_tier))
        return False

    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        print("Qualification matrix could not be reloaded; no PR commands generated.")
        return False
    if not isinstance(matrix, dict):
        print("Qualification matrix is malformed; no PR commands generated.")
        return False
    from scripts import validate_hil_qualification as validation

    errors, _ = validation.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    if errors:
        print("Schema-v3 validation failed:", "; ".join(errors))
        return False
    from .tiers import evaluate_tiers

    tier_result = evaluate_tiers(matrix)
    selected = next(
        (
            item
            for item in tier_result["tiers"]
            if item.get("scope", {}).get("revision_id") == proposal["revision_id"]
            and item.get("scope", {}).get("firmware") == proposal["firmware_version"]
            and item.get("scope", {}).get("observed_route") == proposal["route"]
        ),
        None,
    )
    tier_rank = {"unqualified": 0, "bronze": 1, "silver": 2, "gold": 3, "platinum": 4}
    isolated_summary = _isolated_run_summary(
        matrix,
        revision_id=proposal["revision_id"],
        firmware=proposal["firmware_version"],
        route=proposal["route"],
        run_id=run_id,
    )
    assurance_by_tier = (
        isolated_summary.get("assurance", {}) if isolated_summary else {}
    )
    attained_tier = (
        isolated_summary.get("tier", "unqualified")
        if isolated_summary
        else "unqualified"
    )
    attained_assurance = (
        assurance_by_tier.get(attained_tier)
        if isinstance(assurance_by_tier, dict)
        else None
    )
    requested_assurance = (
        assurance_by_tier.get(requested_tier)
        if isinstance(assurance_by_tier, dict)
        else None
    )
    print(
        "POST-REVIEW QUALIFICATION: "
        f"attained {attained_tier}; "
        f"assurance {attained_assurance or 'unknown'}; "
        f"scope {proposal['revision_id']} / {proposal['firmware_version']} / {proposal['route']}"
    )
    print(
        "  Review basis: schema-v3 matrix evaluation isolated to imported run "
        f"{run_id}; requested {requested_tier} assurance "
        f"{requested_assurance or 'unknown'}."
    )
    if isolated_summary is not None:
        for reason in isolated_summary.get("unmet_requirements", []):
            print("  Missing condition:", reason)
    if (
        selected is None
        or isolated_summary is None
        or requested_assurance != expected_assurance
        or tier_rank.get(isolated_summary.get("tier"), 0)
        < tier_rank.get(requested_tier, 99)
    ):
        print(
            f"Requested {requested_tier.title()} with {expected_assurance} assurance was not attained by this run; no PR commands generated."
        )
        if isolated_summary is not None:
            for reason in isolated_summary.get("unmet_requirements", []):
                print("  -", reason)
        print("After addressing the missing evidence, review this imported run with:")
        print("  " + _review_resume_command(matrix_path, run_id, requested_tier))
        return False
    # The scope evaluator can retain results from older runs. A new onboarding
    # handoff must qualify from this exact acquisition and its own review.
    isolated_tier = isolated_summary.get("tier", "unqualified")
    if tier_rank.get(isolated_tier, 0) < tier_rank.get(requested_tier, 99):
        print(
            f"This imported run alone reaches {isolated_tier.title()} with "
            f"{attained_assurance or 'unknown'} assurance, below the requested "
            f"{requested_tier.title()} / {expected_assurance} result; earlier scope evidence "
            "cannot complete it. No PR commands generated."
        )
        return False
    stage_paths = _verified_review_stage_paths(
        matrix,
        matrix_path,
        repo_root,
        revision_id=proposal["revision_id"],
        run_id=run_id,
        tier=requested_tier,
    )
    if stage_paths is None:
        print(
            f"This imported run lacks complete, applied {requested_tier.title()} "
            "review evidence or verified public artifacts; no PR commands were generated."
        )
        print("To resume the offline review, run:")
        print("  " + _review_resume_command(matrix_path, run_id, requested_tier))
        return False
    from scripts.pr_body import render_pr_body

    model = proposal["model"]
    hardware = proposal["hardware_version"]
    firmware = proposal["firmware_version"]
    route = proposal["route"]
    try:
        _, public_members, _ = importer._load_archive(archive_path)
        bronze_evidence = json.loads(public_members["tier-bronze-read-only.json"])
        sampled = [
            datetime.fromisoformat(item["sampled_at_utc"].replace("Z", "+00:00"))
            for item in bronze_evidence["samples"]
            if isinstance(item, dict) and isinstance(item.get("sampled_at_utc"), str)
        ]
        sample_interval = (
            f"{(max(sampled) - min(sampled)).total_seconds():g} seconds"
            if len(sampled) >= 2
            else "not explicitly recorded"
        )
        physical_observation = bronze_evidence.get("physical_observation", "unknown")
        capture_id = bronze_evidence.get("protocol_capture_id")
        request_seq = bronze_evidence.get("protocol_status_request_seq")
        write_ok_seq = bronze_evidence.get("protocol_status_write_ok_seq")
        decoded_seq = bronze_evidence.get("protocol_decoded_response_seq")
        session_generation = bronze_evidence.get("protocol_session_generation")
        public_events = [
            json.loads(line)
            for line in public_members["events.jsonl"].decode("utf-8").splitlines()
            if line
        ]
        public_frames = [
            json.loads(line)
            for line in public_members["frames.jsonl"].decode("utf-8").splitlines()
            if line
        ]
        status_request = any(
            frame.get("capture_id") == capture_id
            and frame.get("direction") == "tx"
            and frame.get("command_type") == "status_request"
            and frame.get("frame_validation") == "validated_tx"
            and frame.get("seq") == request_seq
            and frame.get("session_generation") == session_generation
            for frame in public_frames
        )
        write_ack = any(
            event.get("capture_id") == capture_id
            and event.get("event") == "write_ok"
            and event.get("seq") == write_ok_seq
            and event.get("session_generation") == session_generation
            for event in public_events
        )
        decoded_status = any(
            event.get("capture_id") == capture_id
            and event.get("event") == "status_data_decoded"
            and event.get("seq") == decoded_seq
            and event.get("session_generation") == session_generation
            for event in public_events
        )
        protocol_behavior_verified = (
            bronze_evidence.get("protocol_probe") == "verified"
            and isinstance(capture_id, str)
            and bool(capture_id)
            and type(session_generation) is int
            and status_request
            and write_ack
            and decoded_status
        )
        from .evidence import _bronze_samples_fresh

        freshness = bronze_evidence.get("status_freshness")
        freshness_event = next(
            (
                event
                for event in public_events
                if isinstance(freshness, dict)
                and event.get("capture_id") == freshness.get("capture_id")
                and event.get("seq") == freshness.get("decoded_response_seq")
            ),
            None,
        )

        fresh_samples_verified = _bronze_samples_fresh(
            bronze_evidence["samples"],
            status_evidence=freshness,
            status_event=freshness_event,
            allow_missing_source=True,
        )
    except (
        HAAPIError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        sample_interval = "not available in the verified public archive"
        physical_observation = "unknown"
        protocol_behavior_verified = False
        fresh_samples_verified = False
    selected_run = next(
        (
            run
            for revision in matrix.get("revisions", [])
            if isinstance(revision, dict)
            for run in revision.get("runs", [])
            if isinstance(run, dict) and run.get("run_id") == run_id
        ),
        None,
    )
    reviewed_claims = (
        selected_run.get("bronze_evidence", {})
        if isinstance(selected_run, dict)
        else {}
    )
    reviewed_claims_match_assurance = all(
        isinstance(reviewed_claims.get(claim), dict)
        and reviewed_claims[claim].get("status") == "pass"
        and isinstance(reviewed_claims[claim].get("physical_review"), dict)
        and reviewed_claims[claim].get("physical_review", {}).get("outcome")
        == "confirmed_pass"
        and reviewed_claims[claim].get("physical_review", {}).get("assurance")
        == expected_assurance
        for claim in ("identity", "protocol_probe", "fresh_telemetry")
    )
    if not (
        protocol_behavior_verified
        and fresh_samples_verified
        and physical_observation == "plausible"
        and reviewed_claims_match_assurance
    ):
        print(
            "Verified public Bronze behavior or applied review claims are incomplete; "
            "no PR body was generated."
        )
        return False

    # Scope evaluation can be higher because of older runs. State only the
    # highest tier attained by this acquisition under the selected assurance.
    attained_tier = min(
        (isolated_tier, requested_tier),
        key=lambda tier: tier_rank.get(tier, 0),
    ).title()
    assurance_label = (
        "operator-attested"
        if expected_assurance == "operator_attested"
        else "independently reviewed"
    )
    title = f"test(hil): add {assurance_label} {model} {attained_tier} evidence"
    current_run_prefix = (
        PurePosixPath("tests/hil/fixtures") / proposal["revision_id"] / "runs" / run_id
    ).as_posix() + "/"
    # The git handoff includes every run needed to validate the complete matrix,
    # while this PR's hardware claims must cite only this acquisition's evidence.
    evidence_paths = [
        path for path in stage_paths[1:] if path.startswith(current_run_prefix)
    ]
    controls = (
        selected_run.get("write_capabilities", {})
        if isinstance(selected_run, dict)
        else {}
    )
    scenarios = (
        selected_run.get("scenarios", {}) if isinstance(selected_run, dict) else {}
    )
    reviewed_controls = (
        sorted(
            name
            for name, item in controls.items()
            if isinstance(item, dict)
            and item.get("status") == "pass"
            and isinstance(item.get("physical_review"), dict)
        )
        if isinstance(controls, dict)
        else []
    )
    reviewed_scenarios = (
        sorted(
            name
            for name, item in scenarios.items()
            if isinstance(item, dict)
            and item.get("status") == "pass"
            and isinstance(item.get("physical_review"), dict)
        )
        if isinstance(scenarios, dict)
        else []
    )
    presence = (
        selected_run.get("feature_presence", {})
        if isinstance(selected_run, dict)
        else {}
    )
    reviewed_absences = (
        sorted(
            feature
            for feature, item in presence.items()
            if isinstance(item, dict)
            and item.get("status") == "absent"
            and isinstance(item.get("reviewed_by"), str)
            and bool(item.get("evidence"))
            and isinstance(item.get("evidence_sha256"), dict)
        )
        if isinstance(presence, dict)
        else []
    )
    validation_text = (
        f"The imported public archive passed integrity and schema checks. The exact "
        f"run's Bronze identity, protocol, and freshness claims and {attained_tier} "
        f"review evidence were {assurance_label} and applied. The resulting "
        "matrix passed schema-v3 validation and the tier evaluator both in scope "
        "and with this run isolated, so earlier evidence cannot complete this run."
    )
    safety_text = (
        "This PR adds privacy-filtered qualification evidence only. It changes no "
        "protocol implementation, writable flags, BLE timing, or connection behavior. "
        + (
            f"The acquisition performed {assurance_label} control transitions "
            f"({', '.join(reviewed_controls) or 'none'}) and scenarios "
            f"({', '.join(reviewed_scenarios) or 'none'}); reviewed evidence includes "
            f"restoration records and physical absences ({', '.join(reviewed_absences) or 'none'})."
            if requested_tier in {"silver", "gold"}
            else "The evidence contains a read-only status request; no output controls were changed."
        )
        + " Unknown protocol bits and current-session freshness safeguards in the integration are unaffected."
    )
    hardware_text = (
        f"For the exact {model}, hardware {hardware}, firmware {firmware}, and "
        f"observed {route} route, the reviewed public artifacts at "
        f"{', '.join(evidence_paths)} record a validated status-request TX, a write "
        "acknowledgement, and decoded StatusData in the same complete capture and "
        "session generation. The trace validates capture/session identity and the "
        "recorded event sequence; it does not establish that the decoded notification "
        "was causally caused by that request. Two fresh telemetry samples and a "
        "plausible operator observation are present, and the identity, protocol, and "
        f"freshness claims were {assurance_label} and applied. Telemetry sample "
        f"interval: {sample_interval}. "
        + (
            f"{assurance_label.title()} controls: {', '.join(reviewed_controls) or 'none'}. "
            f"{assurance_label.title()} scenarios: {', '.join(reviewed_scenarios) or 'none'}. "
            f"{assurance_label.title()} physical absences: {', '.join(reviewed_absences) or 'none'}. "
            f"The validated handoff stages all {len(stage_paths) - 1} exact public "
            "and review evidence files needed to validate the complete matrix."
            if requested_tier in {"silver", "gold"}
            else "This evidence does not qualify output writes or other routes, firmware, or hardware revisions."
        )
    )
    checked = {
        "For normal changes, the base branch is `devel` (`main` is release-only). If this PR comes from a fork, confirm the base repository and branch before submitting; GitHub may default to `main`.": True,
        "The PR title uses Conventional Commits syntax, for example `docs: clarify contributor workflow` or `test: cover status freshness timeout boundary`.": True,
        "The related issue, if applicable, is linked, and existing issues and PRs were checked to avoid duplicate work.": False,
        "No protocol write behavior changes.": True,
        "Unknown bits remain preserved.": True,
        "Writes still require fresh state from the current GATT session.": True,
        "Hardware model/revision evidence is attached or linked when required.": True,
        "Documentation and changelog impact addressed.": True,
        "Logs, diagnostics, addresses, serial numbers, and packet captures are sanitized.": True,
        "For non-hotfix work, base branch is `devel`.": True,
        "This change does not require bypassing branch protections on `devel` or `main`.": True,
    }
    body_data = {
        "title": title,
        "issue": (
            "#350 (solo-operator onboarding and reports); #349 (assurance contract); "
            "#227 (qualification plan); #328 (Bronze reviewer); #329 "
            "(higher-tier reviewer); #330 (onboard orchestration)"
        ),
        "summary": (
            f"Add {assurance_label} {attained_tier} qualification evidence for run "
            f"{run_id} on {model}, "
            f"hardware {hardware}, firmware {firmware}, on the observed {route} "
            f"route with `{expected_assurance}` assurance. The linked public evidence paths substantiate the hardware and "
            "revision checklist item for this exact scope. The related qualification "
            "and reviewer issues are linked; check for duplicate issues and PRs before "
            "submitting this generated hardware-evidence PR. "
            "No new production write authorization is requested or granted; "
            f"scope-specific reviewed control evidence is included for {', '.join(reviewed_controls) or 'no controls'}. "
            "Release tooling owns "
            "CHANGELOG.md; no changelog entry is included. No user-facing strings "
            "changed, so translations are not applicable. The main-source-branch "
            "checklist item applies only to main-target PRs."
        ),
        "safety_impact": safety_text,
        "validation": validation_text,
        "hardware_results": hardware_text,
        "checked": checked,
    }
    body_path = output_dir / "pr.md"
    try:
        body_path.write_text(
            render_pr_body(
                (repo_root / ".github/PULL_REQUEST_TEMPLATE.md").read_text(
                    encoding="utf-8"
                ),
                body_data,
            ),
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError) as ex:
        print("Unable to generate the complete PR body:", ex)
        return False
    print(
        f"REVIEWED TIER: {attained_tier}; ASSURANCE: {expected_assurance} "
        f"({model} / {hardware} / {firmware} / {route})"
    )
    print("READY FOR QUALIFICATION PR. Inspect the staged diff and body first.")
    print("Local PR body:", body_path)
    gh_executable = shutil.which("gh")
    if gh_executable is None:
        print("GitHub CLI is not installed; install gh before opening the PR.")
    else:
        try:
            auth_status = subprocess.run(
                [gh_executable, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            auth_status = None
        if auth_status is None or auth_status.returncode != 0:
            print(
                "GitHub CLI authentication is unavailable; run `gh auth login` "
                "before opening the PR."
            )
    print("Exact allowlisted Git/gh commands:")
    print("  git status --short")
    print("  python scripts/validate_hil_qualification.py")
    print("  python -m scripts.hil tiers --matrix tests/hil/qualification_matrix.json")
    print("  pre-commit run --all-files")
    print("  git add " + _shell_join(stage_paths))
    print("  git diff --cached --check")
    print("  git diff --cached")
    print("  git diff --cached --stat")
    print("  git commit -S -m " + _shell_quote(title))
    print("  git verify-commit HEAD")
    print("  git push -u origin " + _shell_quote(branch))
    print(
        "  gh pr create --repo dedalodaelus/home-assistant-allpowers-ble "
        "--base devel --head "
        + _shell_quote(branch)
        + " --title "
        + _shell_quote(title)
        + " --body-file "
        + _shell_quote(body_path)
    )
    print("Do not stage private HIL results, ZIP archives, tokens, or raw traces.")
    print("After pushing, confirm GitHub shows the commit signature as Verified.")
    return True


def main(argv: list[str] | None = None) -> int:
    """Parse and execute, with transport details never printed on failure."""
    args = make_parser().parse_args(argv)
    try:
        return execute(args)
    except (HAAPIError, KeyboardInterrupt, EOFError) as ex:
        if isinstance(ex, HAAPIError):
            print(f"HIL preflight failed: {ex}", file=sys.stderr)
        else:
            print("HIL preflight interrupted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
