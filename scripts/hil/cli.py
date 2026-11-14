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
from time import monotonic, sleep
from pathlib import Path
import sys
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
    verdict,
)
from .scenarios import SUITES, load_scenarios
from .suite_runtime import SuiteServices
from .trace import (
    _save_private_trace,
    _validate_trace_result,
)

_SWITCH_CONTROLS = {"ac_output", "dc_output", "light", "eco_mode", "car_charger"}


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
        "review", help="Review an imported Bronze archive with independent claims"
    )
    review_parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    review_parser.add_argument("--run-id", required=True)
    review_parser.add_argument("--review-file", type=Path, required=True)
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
            **observed,
            "eligible": bool(
                facts.get("connected") is True
                and facts.get("pending_transaction") is False
                and freshness is True
                and runtime_ready
                and control_capable is True
                and observed["available"] is True
            ),
        }
    return result


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
    for planned in plan["steps"]:
        step_id = planned["id"]
        if stop_reason is not None:
            execution["steps"].append(
                {
                    "step_id": step_id,
                    "outcome": "blocked",
                    "reason": f"not attempted because an earlier step stopped execution: {stop_reason}",
                }
            )
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
            first_sampled_at = services.datetime.now(UTC).isoformat()
            try:
                first_sample = services.read_only_sample(api, device)
            except HAAPIError:
                first_sample = {}
            second_sampled_at = services.datetime.now(UTC).isoformat()
            second_sample: dict[str, Any] = {}
            protocol_probe: dict[str, Any] = {"outcome": "inconclusive"}
            if (
                device.entry_state == "loaded"
                and device.config_entry_id is not None
                and device.refresh_entity is not None
            ):
                capture_id = None
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
                        api.request_read_only_refresh(device.refresh_entity)
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
                        second_sampled_at = services.datetime.now(UTC).isoformat()
                        second_sample = services.read_only_sample(api, device)
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
                        frames = trace.get("frames", [])
                        status_tx, status_write_ok, decoded, write_failed = (
                            _bronze_status_exchange(events, frames)
                        )
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
                    else:
                        second_sampled_at = services.datetime.now(UTC).isoformat()
                        second_sample = services.read_only_sample(api, device)
                except HAAPIError, OSError, TypeError, ValueError, KeyError:
                    second_sample = {}
                    protocol_probe = {"outcome": "inconclusive"}
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
            else:
                services.sleep(1)
                try:
                    second_sampled_at = services.datetime.now(UTC).isoformat()
                    second_sample = services.read_only_sample(api, device)
                except HAAPIError:
                    second_sampled_at = services.datetime.now(UTC).isoformat()
                    second_sample = {}
            samples_valid = services.has_verified_telemetry(
                first_sample
            ) and services.has_verified_telemetry(second_sample)
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
            if (
                not isinstance(facts, dict)
                or facts.get("physical_presence") != "present"
            ):
                execution["steps"].append(
                    {
                        "step_id": step_id,
                        "outcome": "blocked",
                        "reason": "physical presence is unknown or unreviewed",
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
        execution["steps"].append(
            {
                "step_id": step_id,
                "scenario_id": scenario_id,
                "outcome": "pass" if passed else "inconclusive",
                **({} if passed else {"reason": step_reason}),
                "observed_routes": sorted(verified_routes),
                "human_review": "pending",
                "machine_evidence": [
                    {
                        "path": f"../../tier_{child_run_id}/private/{item}",
                        "sha256": hashlib.sha256(
                            (child_root / item).read_bytes()
                        ).hexdigest(),
                    }
                    for item in evidence_paths
                ],
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

    entity_key = {"light_output": "light"}.get(feature, feature)
    entry_id = device.config_entry_id
    if entry_id is None or args.non_interactive or device.entry_state != "loaded":
        return {
            "step_id": step_id,
            "outcome": "blocked",
            "reason": "interactive loaded entry required",
        }
    try:
        before_caps = api.hil_capabilities(entry_id)
        before_state = api.control_state(
            entity_key, device.control_entities.get(entity_key)
        )
    except HAAPIError:
        return {
            "step_id": step_id,
            "outcome": "inconclusive",
            "reason": "fresh candidate preflight unavailable",
        }
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
        current_state = api.control_state(
            entity_key, device.control_entities.get(entity_key)
        )
    except HAAPIError:
        return {
            "step_id": step_id,
            "outcome": "inconclusive",
            "reason": "pre-write state unavailable; no BLE action sent",
        }
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
            after_state = api.control_state(
                entity_key, device.control_entities.get(entity_key)
            )
        except HAAPIError:
            after_caps, after_state = {}, {"available": False}
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
                    restore_state = api.control_state(
                        entity_key, device.control_entities.get(entity_key)
                    )
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
                    final_state = api.control_state(
                        entity_key, device.control_entities.get(entity_key)
                    )
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
        read_only_sample=read_only_sample,
        monotonic=monotonic,
        sleep=sleep,
        prompt=input,
        datetime=datetime,
    )


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
            args.suite not in {"recovery", "soak-24h"}
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
        print(
            "Choose desired tier: 1=Bronze (read-only), 2=Silver, 3=Gold, 4=Platinum (24-hour soak)"
        )
        tier_choice = input("Desired tier: ").strip()
        tiers = {"1": "bronze", "2": "silver", "3": "gold", "4": "platinum"}
        if tier_choice not in tiers:
            raise HAAPIError("Invalid tier selection")
        requested_tier = tiers[tier_choice]
        print(f"Requested tier: {requested_tier.title()}")
        if requested_tier == "platinum":
            args.suite = "soak-24h"
            print("Applicable plan: the existing Platinum 24-hour soak suite.")
            print(
                "Expected elapsed time: up to 24 hours; stay present for setup and checks."
            )
            if input("Type RUN to start this plan: ").strip() != "RUN":
                print("Onboarding cancelled before tests started.")
                return 2
        else:
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
                reasons = (
                    "; ".join(step["reasons"]) or "ready for the existing guided suite"
                )
                print(
                    f"  {step['eligibility']}: {step['id']} ({step['feature']}) — {reasons}"
                )
            print("Restoration:", test_plan["restoration_plan"])
            if getattr(args, "onboarding", False):
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
            return _onboard_export(output_dir, result)
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
            return _onboard_export(output_dir, result)
        return result
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


def _onboard_export(output_dir: Path, run_result: int) -> int:
    """Offer a local privacy-reviewed export after the existing HIL run ends."""
    execution_path = output_dir / "tier-execution.json"
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
    print("Attained evidence-backed tier: unqualified until independent review.")
    if run_result != 0:
        print(
            "Run did not complete successfully; private checkpoint remains available."
        )
        return run_result
    try:
        export_choice = (
            input("Review and create a privacy-safe evidence ZIP now? [y/N]: ")
            .strip()
            .casefold()
        )
    except EOFError, KeyboardInterrupt:
        return 2
    if export_choice not in {"y", "yes"}:
        print("No archive created. The private run remains on this machine.")
        return run_result
    try:
        export_archive(output_dir, confirmed=False)
    except HAAPIError as ex:
        if str(ex) != "Export requires explicit confirmation":
            raise
    if (
        input(
            "Type EXPORT after reviewing the ZIP contents to create it locally: "
        ).strip()
        != "EXPORT"
    ):
        print("Export cancelled; no archive created.")
        return 2
    export_archive(output_dir, confirmed=True)
    print("Privacy-safe HIL evidence archive written locally; nothing was uploaded.")
    return run_result


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
