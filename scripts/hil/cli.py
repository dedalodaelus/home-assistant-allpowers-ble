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
    commands.add_parser(
        "doctor", parents=[common], help="Check read-only prerequisites"
    )
    run_parser = commands.add_parser(
        "run",
        parents=[common],
        help="Capture a read-only snapshot or guided scenario suite",
    )
    run_parser.add_argument("--suite", choices=(*SUITES, "all"))
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
    if args.command == "import":
        return import_archive(args.archive, matrix_path=args.matrix, apply=args.apply)
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
    if args.command == "run" and args.suite == "read-only":
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
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    if resume_state is not None:
        if resume_dir is None:
            raise HAAPIError("Missing validated recovery directory")
        output_dir = resume_dir
    else:
        output_dir = write_run(
            args.output_root, f"{timestamp}_{run_id}", environment, session
        )
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
