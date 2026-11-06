"""Command line preflight and read-only snapshot for HIL issue #228."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import json
import math
import os
import re
from time import sleep
from pathlib import Path
import sys
from typing import Any

from .api import HAAPIError, HAClient
from .discovery import Device, choose_device, find_devices
from .environment import (
    build_environment,
    build_session,
    check_ha_version,
    evaluate_preflight,
    new_run_id,
    read_only_sample,
)
from .storage import write_run
from .runner import plan as plan_scenarios, plan_as_dict
from .scenarios import SUITES


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
        "--dry-run",
        action="store_true",
        help="Print scenario eligibility without contacting Home Assistant",
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


def _device_list(devices: list[Device]) -> None:
    print(f"ALLPOWERS devices: {len(devices)}")
    for index, device in enumerate(devices, start=1):
        print(
            f"  [{index}] {device.model} "
            f"(hardware {device.hardware or 'unknown'}, "
            f"firmware {device.firmware or 'unknown'}, "
            f"integration {device.entry_state or 'unverified'})"
        )


def _natural_int(value: Any) -> bool:
    """Exclude booleans from integer fields and forbid negative counters."""
    return type(value) is int and value >= 0


def _check_trace_rows(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    scenario_id: str,
    frame: bool,
    schema_version: int = 1,
) -> dict[int, dict[str, Any]]:
    """Validate ordered v1/v2 event/frame rows without decoding private payloads."""
    by_seq: dict[int, dict[str, Any]] = {}
    previous = 0
    previous_elapsed = 0.0
    for row in rows:
        seq = row.get("seq")
        if (
            not _natural_int(seq)
            or seq <= previous
            or row.get("schema_version") != schema_version
            or row.get("run_id") != run_id
            or row.get("scenario_id") != scenario_id
            or row.get("layer") != "gatt_api"
            or row.get("observed_route")
            not in (
                ("unverified",)
                if schema_version == 1
                else ("local_adapter", "active_proxy", "unverified")
            )
            or not isinstance(row.get("event"), str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", row["event"]) is None
        ):
            raise HAAPIError("Malformed HIL trace event/frame identity or order")
        utc_value = row.get("utc")
        try:
            when = datetime.fromisoformat(utc_value)
        except (TypeError, ValueError) as ex:
            raise HAAPIError("Malformed HIL trace timestamp") from ex
        if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
            raise HAAPIError("HIL trace timestamp must be UTC")
        elapsed = row.get("elapsed_ms")
        generation = row.get("session_generation")
        if (
            type(elapsed) not in (float, int)
            or not math.isfinite(elapsed)
            or elapsed < previous_elapsed
            or (
                generation is not None
                and (not _natural_int(generation) or generation < 1)
            )
        ):
            raise HAAPIError("Malformed HIL trace timing or session generation")
        if frame:
            direction = row.get("direction")
            opcode = row.get("command_type")
            validation = row.get("frame_validation")
            encoded = row.get("hex")
            if (
                direction not in ("rx", "tx")
                or opcode
                not in (
                    "status_request",
                    "output_control",
                    "settings_control",
                    "unknown",
                )
                or validation not in ("validated_tx", "unvalidated_chunk")
                or not isinstance(encoded, str)
                or len(encoded) > 512
                or len(encoded) % 2
                or re.fullmatch(r"[0-9a-f]*", encoded) is None
                or (
                    direction == "rx"
                    and (opcode != "unknown" or validation != "unvalidated_chunk")
                )
                or (
                    validation == "validated_tx"
                    and (direction != "tx" or opcode == "unknown")
                )
                or (
                    direction == "tx"
                    and opcode != "unknown"
                    and validation != "validated_tx"
                )
            ):
                raise HAAPIError("Malformed HIL GATT frame or validation")
        previous = seq
        previous_elapsed = elapsed
        by_seq[seq] = row
    return by_seq


def _check_v2_routes(
    result: dict[str, Any],
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
) -> None:
    """Validate per-generation proof and row-time binding before private I/O."""
    observations = result.get("route_observations")
    if not isinstance(observations, list):
        raise HAAPIError("Missing HIL v2 route observations")
    if len(observations) > 256:
        raise HAAPIError("Too many HIL v2 route observations")
    routes: dict[int, dict[str, Any]] = {}
    previous_seq = 0
    previous_elapsed = 0.0
    for item in observations:
        if not isinstance(item, dict):
            raise HAAPIError("Malformed route observation")
        required = {
            "session_generation",
            "seq",
            "utc",
            "elapsed_ms",
            "observed_route",
            "backend_kind",
            "evidence",
        }
        if not required.issubset(item) or set(item) - (
            required | {"adapter_id", "proxy_id"}
        ):
            raise HAAPIError("Malformed route observation fields")
        gen = item["session_generation"]
        seq = item["seq"]
        elapsed = item["elapsed_ms"]
        try:
            when = datetime.fromisoformat(item["utc"])
        except (TypeError, ValueError) as ex:
            raise HAAPIError("Invalid route observation timestamp") from ex
        if (
            not _natural_int(gen)
            or gen < 1
            or gen in routes
            or not _natural_int(seq)
            or seq <= previous_seq
            or type(elapsed) not in (float, int)
            or not math.isfinite(elapsed)
            or elapsed < previous_elapsed
            or when.utcoffset() is None
            or when.utcoffset().total_seconds() != 0
        ):
            raise HAAPIError("Invalid route observation generation or order")
        route = item["observed_route"]
        backend = item["backend_kind"]
        evidence = item["evidence"]
        if route == "local_adapter":
            adapter = item.get("adapter_id")
            if (
                backend != "bluez"
                or evidence != "bluez_device_path"
                or type(adapter) is not str
                or re.fullmatch(r"hci[0-9]+", adapter) is None
                or "proxy_id" in item
            ):
                raise HAAPIError("Malformed local connected backend proof")
        elif route == "active_proxy":
            proxy = item.get("proxy_id")
            if (
                backend != "esphome"
                or evidence != "esphome_scanner_binding"
                or type(proxy) is not str
                or not proxy
                or len(proxy) > 128
                or not proxy.isprintable()
                or "adapter_id" in item
            ):
                raise HAAPIError("Malformed active proxy connected backend proof")
        elif route == "unverified":
            if (
                backend != "unknown"
                or evidence
                not in (
                    "no_ha_wrapper",
                    "not_connected",
                    "missing_tracking",
                    "local_backend_mismatch",
                    "bluez_path_mismatch",
                    "esphome_backend_mismatch",
                    "esphome_scanner_mismatch",
                    "unsupported_backend",
                    "inspection_failed",
                )
                or "adapter_id" in item
                or "proxy_id" in item
            ):
                raise HAAPIError("Unverified connected route has forged backend proof")
        else:
            raise HAAPIError("Unrecognized connected route")
        routes[gen] = item
        previous_seq = seq
        previous_elapsed = elapsed

    observed = [row["observed_route"] for row in observations]
    summary = (
        "unverified"
        if not observed or "unverified" in observed
        else observed[0]
        if len(set(observed)) == 1
        else "mixed"
    )
    if result.get("observed_route") != summary:
        raise HAAPIError("Connected route summary disagrees with its evidence")

    found_observations: set[int] = set()
    for row in events + frames:
        generation = row.get("session_generation")
        route_item = routes.get(generation)
        expected = (
            route_item["observed_route"]
            if route_item is not None and row["seq"] >= route_item["seq"]
            else "unverified"
        )
        if row["observed_route"] != expected:
            raise HAAPIError("Connected backend route contradicts event timing")
        if row["event"] == "route_observed":
            if (
                route_item is None
                or row["seq"] != route_item["seq"]
                or row.get("utc") != route_item["utc"]
                or row.get("elapsed_ms") != route_item["elapsed_ms"]
                or row.get("backend_kind") != route_item["backend_kind"]
                or row.get("evidence") != route_item["evidence"]
            ):
                raise HAAPIError("Route observation is not bound to its generation")
            found_observations.add(generation)
    if result["dropped_events"] == 0 and found_observations != set(routes):
        raise HAAPIError("Missing connected backend observation event")


def _validate_trace_result(
    result: Any,
    *,
    run_id: str,
    scenario_id: str,
    capture_id: str,
    requested_route: str,
    expected_version: int | None = None,
    expected_scope: str = "runtime_entry",
) -> None:
    """Fail closed on private v1/v2 evidence, before writing any raw files."""
    is_probe = (
        isinstance(result, dict) and result.get("capture_scope") == "config_probe"
    )
    if (
        not isinstance(result, dict)
        or result.get("capture_scope", "runtime_entry") != expected_scope
        or result.get("schema_version") not in (1, 2)
        or (
            expected_version is not None
            and result.get("schema_version") != expected_version
        )
        or result.get("run_id") != run_id
        or result.get("scenario_id") != scenario_id
        or result.get("capture_id") != capture_id
        or result.get("requested_route") != requested_route
        or result.get("qualification_allowed") is not False
        or (
            result.get("observed_route") != "unverified"
            if result.get("schema_version") == 1
            else result.get("observed_route")
            not in ("local_adapter", "active_proxy", "mixed", "unverified")
        )
        or result.get("capture_layer") != "gatt_api_not_rf"
        or result.get("private_raw_bytes") is not True
        or result.get("closed") is not True
        or type(result.get("expired")) is not bool
        or type(result.get("complete")) is not bool
        or result.get("termination_reason")
        not in (
            ("probe_finished", "probe_skipped", "probe_interrupted", "expired")
            if is_probe
            else ("requested_stop", "runtime_stopped", "expired")
        )
    ):
        raise HAAPIError("Capture metadata mismatch or final stop not acknowledged")
    if is_probe and (
        result.get("schema_version") != 2
        or not isinstance(result.get("flow_id"), str)
        or not result["flow_id"]
        or result.get("probe_state") != "finished"
        or result.get("probe_outcome")
        not in ("success", "failure", "cancelled", "skipped", "interrupted")
        or (
            result["termination_reason"] == "probe_finished"
            and result["probe_outcome"] not in ("success", "failure", "cancelled")
        )
        or (
            result["termination_reason"] == "probe_skipped"
            and result["probe_outcome"] != "skipped"
        )
        or (
            result["termination_reason"] == "probe_interrupted"
            and result["probe_outcome"] != "interrupted"
        )
    ):
        raise HAAPIError("Invalid probe capture lifecycle metadata")
    for key in ("events", "frames"):
        if not isinstance(result.get(key), list) or not all(
            isinstance(row, dict) for row in result[key]
        ):
            raise HAAPIError("Invalid private HIL trace records")
    for name in ("event_count", "frame_count", "dropped_events", "dropped_frames"):
        if not _natural_int(result.get(name)):
            raise HAAPIError("Invalid HIL trace count")
    events = result["events"]
    frames = result["frames"]
    if (
        result["event_count"] != len(events)
        or result["frame_count"] != len(frames)
        or result.get("has_gatt_frames") is not bool(frames)
    ):
        raise HAAPIError("Incomplete private HIL trace record count")
    expected_complete = (
        result["termination_reason"]
        == ("probe_finished" if is_probe else "requested_stop")
        and not result["expired"]
        and result["dropped_events"] == 0
        and result["dropped_frames"] == 0
    )
    if result["complete"] is not expected_complete or (
        is_probe and result.get("capture_complete") is not expected_complete
    ):
        raise HAAPIError("Contradictory HIL capture integrity fields")
    version = result["schema_version"]
    events_by_seq = _check_trace_rows(
        events,
        run_id=run_id,
        scenario_id=scenario_id,
        frame=False,
        schema_version=version,
    )
    _check_trace_rows(
        frames,
        run_id=run_id,
        scenario_id=scenario_id,
        frame=True,
        schema_version=version,
    )
    if version == 2:
        _check_v2_routes(result, events, frames)
    elif "route_observations" in result:
        raise HAAPIError("Unexpected connected backend proof in v1 trace")
    common_fields = (
        "schema_version",
        "seq",
        "utc",
        "elapsed_ms",
        "run_id",
        "scenario_id",
        "event",
        "session_generation",
        "layer",
        "observed_route",
    )
    for record in frames:
        event = events_by_seq.get(record["seq"])
        if event is None:
            if result["dropped_events"] == 0:
                raise HAAPIError("HIL frame is missing its corresponding event")
            continue
        if any(record[key] != event[key] for key in common_fields) or (
            ("transaction_id" in record or "transaction_id" in event)
            and (
                "transaction_id" not in record
                or "transaction_id" not in event
                or record["transaction_id"] != event["transaction_id"]
            )
        ):
            raise HAAPIError("HIL frame metadata differs from its matching event")


def _save_private_trace(output_dir: Path, result: dict[str, Any]) -> None:
    """Write only validated private evidence, scrubbing partial failures."""
    private_paths = (
        output_dir / "events.jsonl",
        output_dir / "frames.jsonl",
        output_dir / "trace.json",
    )
    try:
        for path, key in zip(private_paths[:2], ("events", "frames"), strict=True):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for record in result[key]:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
        metadata = {
            key: value
            for key, value in result.items()
            if key not in ("events", "frames")
        }
        fd = os.open(private_paths[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except (OSError, TypeError, ValueError) as ex:
        for path in private_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HAAPIError("Private HIL trace could not be saved") from ex


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
    print("Private config-flow probe saved to:", output_dir)
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


def execute(args: argparse.Namespace) -> int:
    """Execute read-only preflight and explicitly requested private HIL capture."""
    if args.command == "run":
        if args.list_suites:
            print("Available HIL suites:")
            for suite in SUITES:
                print(f"  {suite}")
            return 0
        if args.resume is not None:
            raise HAAPIError("Scenario checkpoint resume is not yet available")
        if args.dry_run:
            suite = args.suite or "read-only"
            planned = plan_scenarios(suite, non_interactive=args.non_interactive)
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
        if args.suite not in (None, "read-only"):
            raise HAAPIError(
                "This suite is declarative but execution is not implemented yet; use --dry-run"
            )
    url, token = _credential(args)
    api = HAClient(url, token, allow_insecure_http=args.allow_insecure_http)
    config, manifest, devices = _discovery(api)
    # Optional admin-only HA Bluetooth scanner inventory. Its raw scanner
    # identifiers and BLE addresses never leave HAClient; only counts do.
    get_scanners = getattr(api, "scanner_candidates", None)
    candidates = get_scanners() if callable(get_scanners) else None
    environment = build_environment(config, manifest, candidates)
    print("Home Assistant:", environment["home_assistant"]["version"]["value"])
    installed = environment["integration"]["installed_manifest_version"]["value"]
    print("ALLPOWERS manifest version:", installed or "unknown")
    print("Installed integration commit: unverified by the standard HA API")
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
    run_id = new_run_id()
    sample = read_only_sample(api, selected)
    session = build_session(
        run_id=run_id,
        requested_route=args.route,
        device=selected,
        environment=environment,
        sample=sample,
    )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_dir = write_run(
        args.output_root, f"{timestamp}_{run_id}", environment, session
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
        print("Private HIL trace saved to:", output_dir)
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
    print("Read-only preflight saved to:", output_dir)
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
