"""Command line preflight and read-only snapshot for HIL issue #228."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
from time import monotonic, sleep
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
    unknown,
)
from .storage import (
    load_scenario_checkpoint,
    write_run,
    write_scenario_progress,
)
from .runner import (
    correlate_transaction,
    plan as plan_scenarios,
    plan_as_dict,
    recovery_verdict,
    soak_verdict,
    verdict,
)
from .scenarios import SUITES, load_scenarios


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
        settings_key = key in settings_controls
        control_capable = capability_flags.get(
            "write_settings_controls" if settings_key else "write_output_controls",
            False,
        )
        freshness = facts.get("settings_fresh" if settings_key else "status_fresh")
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
                and control_capable is True
                and observed["available"] is True
            ),
        }
    return result


def _device_fingerprint(run_id: str, device: Device, ha_origin: str) -> str:
    """Create a run-scoped device/server fingerprint without persisting IDs."""
    material = f"{run_id}\0{ha_origin}\0{device.config_entry_id or ''}\0{device.device_id}".encode()
    return hashlib.sha256(material).hexdigest()


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


def _execute_output_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    environment: dict[str, Any],
    output_dir: Path,
    run_id: str,
) -> int:
    """Run the bounded AC cycle, requiring separate consent for each write."""
    scenario = next(item for item in load_scenarios() if item.id == "output_ac_cycle")
    entry_id = device.config_entry_id
    steps: list[dict[str, Any]] = []
    fingerprint = _device_fingerprint(run_id, device, api.origin)

    def checkpoint() -> None:
        write_scenario_progress(output_dir, run_id, scenario.id, fingerprint, steps)

    if args.non_interactive or entry_id is None or device.entry_state != "loaded":
        steps.append(
            {
                "scenario_id": scenario.id,
                "outcome": "not_applicable",
                "reason": "interactive authorization or loaded entry unavailable",
            }
        )
        checkpoint()
        print(
            "Output scenario blocked: interactive authorization and a loaded entry are required"
        )
        return 2

    print("Scenario:", scenario.title)
    print("Device:", device.model, "profile:", device.profile or "unknown")
    for index, declaration in enumerate(scenario.steps, start=1):
        key, desired = declaration["target"], declaration["expected"]
        entity_id = device.control_entities.get(key)
        row: dict[str, Any] = {
            "step_id": declaration["id"],
            "target": key,
            "expected": desired,
            "outcome": "inconclusive",
        }
        steps.append(row)
        try:
            caps = api.hil_capabilities(entry_id)
            generation = caps.get("session_generation")
            state = (
                api.control_state(key, entity_id) if entity_id else {"available": False}
            )
            if (
                caps["verified"] is not True
                or caps["identity_conflicted"]
                or caps["connected"] is not True
                or caps["pending_transaction"]
                or caps["status_fresh"] is not True
                or type(generation) is not int
                or caps["capabilities"]["write_output_controls"] is not True
                or not state.get("available")
                or state.get("state") != ("off" if desired == "on" else "on")
            ):
                row["reason"] = "live capability or expected pre-state check failed"
                checkpoint()
                print("Step", index, "blocked by live preflight")
                break
            print(f"Step {index}: {key} {state['state']} → {desired}")
            print(
                "Confirm only if the station is attended and the AC output is safe to change."
            )
            if input("Type 1 to authorize this single action: ").strip() != "1":
                row["outcome"] = "skipped"
                row["reason"] = "authorization not granted"
                checkpoint()
                break

            capture = api.websocket(
                [
                    {
                        "type": "allpowers_ble/hil/start",
                        "entry_id": entry_id,
                        "run_id": run_id,
                        "scenario_id": scenario.id,
                        "requested_route": args.route,
                    }
                ]
            )[0]
            if (
                not isinstance(capture, dict)
                or capture.get("schema_version") != 2
                or not isinstance(capture.get("capture_id"), str)
            ):
                row["reason"] = "trace start was not validated"
                checkpoint()
                break
            capture_id = capture["capture_id"]
            row["pre_state"] = state["state"]
            row["session_generation"] = generation
            action_submitted = False
            observed_caps: dict[str, Any] | None = None
            post: dict[str, Any] = {"available": False, "state": None}
            try:
                api.call_control(key, entity_id, desired)
                action_submitted = True
            except HAAPIError:
                row["service_call"] = "unacknowledged; no retry performed"
            finally:
                deadline = monotonic() + declaration["timeout_seconds"]
                while monotonic() < deadline:
                    try:
                        observed_caps = api.hil_capabilities(entry_id)
                        post = api.control_state(key, entity_id)
                    except HAAPIError:
                        row["telemetry"] = "unavailable during bounded observation"
                        break
                    if (
                        observed_caps.get("session_generation") != generation
                        or observed_caps.get("connected") is not True
                    ):
                        break
                    if (
                        observed_caps.get("status_fresh") is True
                        and post.get("available") is True
                        and post.get("state") == desired
                    ):
                        break
                    sleep(min(1, max(0, deadline - monotonic())))
                fetch_cmd = {
                    "type": "allpowers_ble/hil/fetch",
                    "entry_id": entry_id,
                    "capture_id": capture_id,
                }
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
                    trace = api.websocket([fetch_cmd])[0]
            _validate_trace_result(
                trace,
                run_id=run_id,
                scenario_id=scenario.id,
                capture_id=capture_id,
                requested_route=args.route,
                expected_version=2,
            )
            trace_dir = output_dir / "traces" / declaration["id"]
            trace_dir.parent.mkdir(mode=0o700, exist_ok=True)
            trace_dir.mkdir(mode=0o700, exist_ok=False)
            _save_private_trace(trace_dir, trace)
            row["trace"] = f"traces/{declaration['id']}"
            tx = correlate_transaction(
                trace, expected_generation=generation, kind="output"
            )
            row["transaction"] = tx
            after_caps = observed_caps or api.hil_capabilities(entry_id)
            physical = input(
                "Physical outcome: 1=yes, 2=no, 3=unable to observe: "
            ).strip()
            physical_outcome = {"1": "yes", "2": "no", "3": "unable_to_observe"}.get(
                physical
            )
            route_verified = trace.get("observed_route") in {
                "local_adapter",
                "active_proxy",
            }
            telemetry_matches = (
                after_caps.get("session_generation") == generation
                and after_caps.get("status_fresh") is True
                and post.get("available") is True
                and post.get("state") == desired
            )
            row.update(
                {
                    "post_state": post.get("state"),
                    "physical_outcome": physical_outcome,
                    "route": trace.get("observed_route"),
                    "causal_limit": "device transaction IDs are unavailable; convergence is not causal proof",
                }
            )
            row["outcome"] = verdict(
                telemetry_matches=telemetry_matches,
                telemetry_contradictory=post.get("available") is True
                and post.get("state") not in (desired, state["state"]),
                physical_outcome=physical_outcome,
                route_verified=route_verified,
                trace_complete=trace.get("complete") is True,
                action_submitted=action_submitted,
                transaction_confirmed=tx is not None,
            )
            checkpoint()
            print("Step outcome:", row["outcome"])
            if row["outcome"] != "pass":
                break
        except HAAPIError, OSError, EOFError, KeyboardInterrupt:
            row["reason"] = (
                "operation interrupted or evidence incomplete; no automatic retry"
            )
            checkpoint()
            print(
                "Step inconclusive; inspect the station and restore it manually if needed"
            )
            break
    if len(steps) < len(scenario.steps) or (
        steps and steps[-1].get("outcome") != "pass"
    ):
        print(
            "Check the AC output physically and restore the intended state manually if needed."
        )
    print("Private scenario results saved to:", output_dir)
    return (
        0
        if len(steps) == len(scenario.steps)
        and all(s.get("outcome") == "pass" for s in steps)
        else 2
    )


def _execute_recovery_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    resume_state: dict[str, Any] | None = None,
) -> int:
    """Guide a human recovery action and observe a new, fresh runtime session."""
    scenario = next(
        item for item in load_scenarios() if item.id == "recovery_reconnect"
    )
    entry_id = device.config_entry_id
    fingerprint = _device_fingerprint(run_id, device, api.origin)
    steps: list[dict[str, Any]] = (
        [dict(step) for step in resume_state["steps"]]
        if resume_state is not None
        else []
    )
    run_started = monotonic()

    def checkpoint() -> None:
        write_scenario_progress(output_dir, run_id, scenario.id, fingerprint, steps)

    def prompt(question: str) -> str:
        try:
            return input(question).strip()
        except (KeyboardInterrupt, EOFError) as ex:
            steps.append({"step_id": "interrupted", "outcome": "inconclusive"})
            checkpoint()
            raise HAAPIError(
                "Recovery run interrupted; private checkpoint saved"
            ) from ex

    if args.non_interactive or entry_id is None or device.entry_state != "loaded":
        steps.append(
            {
                "step_id": "baseline",
                "outcome": "not_applicable",
                "reason": "interactive recovery and a loaded entry are required",
            }
        )
        checkpoint()
        print("Recovery suite requires an interactive operator and loaded entry")
        return 2

    if resume_state is None:
        try:
            baseline = api.hil_capabilities(entry_id)
        except HAAPIError:
            steps.append(
                {
                    "step_id": "baseline",
                    "outcome": "inconclusive",
                    "reason": "runtime capability snapshot unavailable",
                }
            )
            checkpoint()
            return 2
        if (
            baseline["verified"] is not True
            or baseline["identity_conflicted"]
            or baseline["connected"] is not True
            or baseline["status_fresh"] is not True
            or type(baseline["session_generation"]) is not int
        ):
            steps.append(
                {
                    "step_id": "baseline",
                    "outcome": "not_applicable",
                    "reason": "verified connected fresh baseline is unavailable",
                }
            )
            checkpoint()
            print("Recovery suite blocked: no verified, connected, fresh baseline")
            return 2
        generation = baseline["session_generation"]
        steps.append(
            {
                "step_id": "baseline",
                "outcome": "pass",
                "session_generation": generation,
                "connected": True,
                "status_fresh": True,
                "utc": datetime.now(UTC).isoformat(),
                "elapsed_ms": int((monotonic() - run_started) * 1000),
            }
        )
        checkpoint()
    else:
        baseline_steps = [step for step in steps if step.get("step_id") == "baseline"]
        action_steps = [
            step for step in steps if step.get("step_id") == "operator_action"
        ]
        baseline_step = baseline_steps[0] if len(baseline_steps) == 1 else None
        action_step = action_steps[0] if len(action_steps) == 1 else None
        previous_observations = [
            step for step in steps if step.get("step_id") == "recovered_session"
        ]
        if (
            any(
                step.get("step_id")
                not in {
                    "baseline",
                    "operator_action",
                    "recovered_session",
                    "interrupted",
                    "resume",
                }
                for step in steps
            )
            or baseline_step is None
            or baseline_step.get("outcome") != "pass"
            or type(baseline_step.get("session_generation")) is not int
            or baseline_step["session_generation"] < 1
            or action_step is None
            or action_step.get("outcome") != "pass"
            or not isinstance(action_step.get("action"), str)
            or action_step["action"]
            not in {
                "route interruption and restoration",
                "vendor-app contention",
                "Home Assistant or integration reload/upgrade",
            }
            or action_step.get("operator_confirmed") is not True
            or any(
                step.get("outcome") != "inconclusive" for step in previous_observations
            )
        ):
            raise HAAPIError("Checkpoint is not safely resumable")
        generation = baseline_step["session_generation"]
        steps.append(
            {
                "step_id": "resume",
                "outcome": "inconclusive",
                "utc": datetime.now(UTC).isoformat(),
                "elapsed_ms": int((monotonic() - run_started) * 1000),
                "clock_discontinuity": True,
                "reason": "process restart creates an unobserved interval",
            }
        )
        checkpoint()

    if resume_state is None:
        print("Select the manual recovery action you will perform:")
        print("  1. Interrupt and restore the selected BLE route")
        print("  2. Use vendor-app contention, then close the app")
        print("  3. Reload or upgrade Home Assistant / the integration")
        choice = prompt("Action [1-3], or 0 to skip: ")
        action = {
            "1": "route interruption and restoration",
            "2": "vendor-app contention",
            "3": "Home Assistant or integration reload/upgrade",
        }.get(choice)
        if action is None:
            steps.append(
                {
                    "step_id": "operator_action",
                    "outcome": "skipped",
                    "reason": "operator declined or selected no action",
                }
            )
            checkpoint()
            return 2
        print("The runner will not perform this action. Keep the station attended.")
        if prompt("Type 1 after you have completed the selected action: ") != "1":
            steps.append(
                {
                    "step_id": "operator_action",
                    "outcome": "skipped",
                    "action": action,
                    "reason": "operator did not confirm completion",
                }
            )
            checkpoint()
            return 2
        action_step: dict[str, Any] = {
            "step_id": "operator_action",
            "outcome": "pass",
            "action": action,
            "operator_confirmed": True,
            "utc": datetime.now(UTC).isoformat(),
            "elapsed_ms": int((monotonic() - run_started) * 1000),
        }
        steps.append(action_step)
        checkpoint()
    else:
        print("Resuming observation only; no operator action will be repeated.")

    deadline = monotonic() + scenario.steps[2]["timeout_seconds"]
    saw_disconnect = False
    recovered: dict[str, Any] | None = None
    try:
        while monotonic() < deadline:
            try:
                current = api.hil_capabilities(entry_id)
            except HAAPIError:
                saw_disconnect = True
                sleep(2)
                continue
            if current["connected"] is not True:
                saw_disconnect = True
            if (
                current["verified"] is True
                and not current["identity_conflicted"]
                and current["connected"] is True
                and current["status_fresh"] is True
                and type(current["session_generation"]) is int
                and (current["session_generation"] != generation or saw_disconnect)
            ):
                recovered = current
                break
            sleep(2)
    except KeyboardInterrupt:
        steps.append(
            {
                "step_id": "recovered_session",
                "outcome": "inconclusive",
                "reason": "operator interrupted recovery observation",
            }
        )
        checkpoint()
        print("Recovery observation interrupted; private checkpoint saved")
        return 2

    attempt = 1 + sum(step.get("step_id") == "recovered_session" for step in steps)
    observation: dict[str, Any] = {
        "step_id": "recovered_session",
        "attempt": attempt,
        "outcome": "inconclusive",
        "utc": datetime.now(UTC).isoformat(),
        "elapsed_ms": int((monotonic() - run_started) * 1000),
        "session_generation_before": generation,
        "disconnect_observed": saw_disconnect,
    }
    steps.append(observation)
    trace: dict[str, Any] | None = None
    if recovered is not None:
        observation.update(
            {
                "session_generation_after": recovered["session_generation"],
                "connected": True,
                "status_fresh": True,
            }
        )
        try:
            start = api.websocket(
                [
                    {
                        "type": "allpowers_ble/hil/start",
                        "entry_id": entry_id,
                        "run_id": run_id,
                        "scenario_id": scenario.id,
                        "requested_route": args.route,
                    }
                ]
            )[0]
            if (
                isinstance(start, dict)
                and start.get("schema_version") == 2
                and isinstance(start.get("capture_id"), str)
            ):
                capture_id = start["capture_id"]
                try:
                    sleep(2)
                finally:
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
                        trace = api.websocket(
                            [
                                {
                                    "type": "allpowers_ble/hil/fetch",
                                    "entry_id": entry_id,
                                    "capture_id": capture_id,
                                }
                            ]
                        )[0]
                _validate_trace_result(
                    trace,
                    run_id=run_id,
                    scenario_id=scenario.id,
                    capture_id=capture_id,
                    requested_route=args.route,
                    expected_version=2,
                )
                trace_name = f"recovery_route_{attempt:02d}_{secrets.token_hex(4)}"
                traces_dir = output_dir / "traces"
                if traces_dir.is_symlink():
                    raise HAAPIError("Private trace directory must not be a symlink")
                traces_dir.mkdir(mode=0o700, exist_ok=True)
                if not traces_dir.is_dir() or traces_dir.is_symlink():
                    raise HAAPIError("Invalid private trace directory")
                trace_dir = traces_dir / trace_name
                trace_dir.mkdir(mode=0o700, exist_ok=False)
                _save_private_trace(trace_dir, trace)
                observation["trace"] = f"traces/{trace_name}"
                observation["route"] = trace.get("observed_route")
                observation["trace_complete"] = trace.get("complete") is True
        except KeyboardInterrupt:
            observation["reason"] = "operator interrupted the bounded route capture"
            checkpoint()
            print("Route capture interrupted; private checkpoint saved")
            return 2
        except HAAPIError, OSError:
            observation["trace"] = "unavailable"

    print(
        "Physical recovery outcome: 1=recovered, 2=not recovered, 3=unable to observe"
    )
    physical = prompt("Outcome [1-3]: ")
    physical_outcome = {"1": "yes", "2": "no", "3": "unable_to_observe"}.get(physical)
    observation["physical_outcome"] = physical_outcome
    observation["route_verified"] = bool(
        trace is not None
        and trace.get("observed_route") in {"local_adapter", "active_proxy"}
    )
    observation["outcome"] = recovery_verdict(
        telemetry_matches=recovered is not None,
        physical_outcome=physical_outcome,
        route_verified=observation["route_verified"],
        trace_complete=bool(trace is not None and trace.get("complete") is True),
    )
    observation["limitation"] = (
        "route recovery is observed after the human action; no causal actor is proven; "
        "scenario evidence does not grant build-level HIL qualification"
    )
    checkpoint()
    print("Recovery observation:", observation["outcome"])
    print("Scenario checkpoint saved to:", output_dir)
    return 0 if observation["outcome"] == "pass" else 2


def _private_run_size(run_dir: Path) -> int:
    """Measure regular private files without following symlinks."""
    total = 0
    for root, directories, files in os.walk(run_dir, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise HAAPIError("Run output must not contain symlinks")
        for name in directories:
            if (root_path / name).is_symlink():
                raise HAAPIError("Run output must not contain symlinks")
        for name in files:
            path = root_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise HAAPIError("Run output contains a non-regular file")
            total += metadata.st_size
    return total


def _execute_soak_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    resume_state: dict[str, Any] | None = None,
) -> int:
    """Collect capped read-only samples and short traces for a full 24 hours."""
    scenario = next(
        item for item in load_scenarios() if item.id == "soak_24h_observation"
    )
    parameters = scenario.parameters
    entry_id = device.config_entry_id
    fingerprint = _device_fingerprint(run_id, device, api.origin)
    steps = (
        [dict(step) for step in resume_state["steps"]]
        if resume_state is not None
        else []
    )

    def checkpoint() -> None:
        write_scenario_progress(output_dir, run_id, scenario.id, fingerprint, steps)

    if entry_id is None or device.entry_state != "loaded":
        steps.append(
            {
                "step_id": "soak_segment",
                "segment": len(steps) + 1,
                "outcome": "not_applicable",
                "reason": "a loaded integration entry is required",
            }
        )
        checkpoint()
        return 2

    if resume_state is not None:
        if not steps or any(step.get("step_id") != "soak_segment" for step in steps):
            raise HAAPIError("Soak checkpoint contains unknown steps")
        if any(step.get("outcome") == "pass" for step in steps):
            raise HAAPIError("A completed 24-hour soak cannot be resumed")
        if any(
            type(step.get("segment")) is not int
            or step["segment"] < 1
            or type(step.get("trace_count", 0)) is not int
            or step.get("trace_count", 0) < 0
            or type(step.get("sample_file_index", 1)) is not int
            or step.get("sample_file_index", 1) < 1
            or step.get("data_limit_hit") is True
            for step in steps
        ):
            raise HAAPIError("Soak checkpoint state cannot be resumed safely")
        if steps:
            steps[-1]["outcome"] = "inconclusive"
            steps[-1]["restart_discontinuity"] = True
            steps[-1]["reason"] = "process restart leaves an unobserved time gap"
            checkpoint()

    sample_file_index = 1
    if steps:
        stored_index = steps[-1].get("sample_file_index", 1)
        if type(stored_index) is not int or stored_index < 1:
            raise HAAPIError("Soak checkpoint sample rotation state is invalid")
        sample_file_index = stored_index
    segment_number = 1 + max((int(step.get("segment", 0)) for step in steps), default=0)
    baseline = api.hil_capabilities(entry_id)
    if (
        baseline["verified"] is not True
        or baseline["identity_conflicted"]
        or baseline["connected"] is not True
        or baseline["status_fresh"] is not True
        or type(baseline["session_generation"]) is not int
    ):
        steps.append(
            {
                "step_id": "soak_segment",
                "segment": segment_number,
                "outcome": "inconclusive",
                "reason": "verified fresh connected baseline unavailable",
                "restart_discontinuity": resume_state is not None,
            }
        )
        checkpoint()
        return 2

    run_started = monotonic()
    segment_started: float | None = None
    previous_sample_monotonic: float | None = None
    previous_sample_utc: datetime | None = None
    sample_count = 0
    trace_count = 0
    maximum_gap = 0.0
    dropped_traces = 0
    clock_discontinuities = 0
    data_limit_hit = False
    active_segment: dict[str, Any] | None = None
    trace_required = (
        parameters["duration_seconds"] // parameters["trace_interval_seconds"]
    )

    def begin_segment(now: float, utc_now: datetime) -> None:
        nonlocal segment_started, sample_count, trace_count, maximum_gap
        nonlocal dropped_traces, clock_discontinuities, active_segment
        segment_started = now
        sample_count = 0
        trace_count = 0
        maximum_gap = 0.0
        dropped_traces = 0
        clock_discontinuities = 0
        active_segment = {
            "step_id": "soak_segment",
            "segment": segment_number,
            "outcome": "inconclusive",
            "segment_started_utc": utc_now.isoformat(),
            "measured_seconds": 0.0,
            "sample_count": 0,
            "trace_count": 0,
            "maximum_gap_seconds": 0.0,
            "dropped_traces": 0,
            "clock_discontinuities": 0,
            "sample_file_index": sample_file_index,
        }
        steps.append(active_segment)
        checkpoint()

    def append_record(record: dict[str, Any]) -> bool:
        nonlocal sample_file_index, data_limit_hit
        encoded = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode(
            "utf-8"
        )
        sample_path = output_dir / f"soak_samples_{sample_file_index:03d}.jsonl"
        if sample_path.is_symlink():
            raise HAAPIError("Soak sample path must not be a symlink")
        current_size = sample_path.stat().st_size if sample_path.exists() else 0
        if sample_path.exists():
            metadata = sample_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise HAAPIError("Soak sample file is not private")
        if current_size + len(encoded) > 256 * 1024:
            sample_file_index += 1
            sample_path = output_dir / f"soak_samples_{sample_file_index:03d}.jsonl"
            current_size = 0
        if (
            _private_run_size(output_dir) + len(encoded) + 4096
            > parameters["max_bytes"]
        ):
            data_limit_hit = True
            return False
        fd = os.open(sample_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(encoded)
            stream.flush()
        if active_segment is not None:
            active_segment["sample_file_index"] = sample_file_index
        return True

    def end_segment(reason: str) -> None:
        nonlocal active_segment, segment_started, segment_number
        if active_segment is not None:
            active_segment["outcome"] = "inconclusive"
            active_segment["reason"] = reason
            checkpoint()
        active_segment = None
        segment_started = None
        segment_number += 1

    if resume_state is not None:
        restart_utc = datetime.now(UTC)
        if not append_record(
            {
                "event": "process_restart_gap",
                "utc": restart_utc.isoformat(),
                "gap_seconds": None,
                "reason": "process restart makes the unobserved interval unknowable",
            }
        ):
            print("Soak storage cap reached before resume")
            return 2

    next_sample = run_started
    next_trace = run_started
    last_generation = baseline["session_generation"]
    trace_index = sum(step.get("trace_count", 0) for step in steps)
    while True:
        now = monotonic()
        utc_now = datetime.now(UTC)
        if now >= next_sample:
            gap = (
                now - previous_sample_monotonic
                if previous_sample_monotonic is not None
                else None
            )
            clock_jump = False
            if gap is not None and previous_sample_utc is not None:
                wall_gap = (utc_now - previous_sample_utc).total_seconds()
                clock_jump = abs(wall_gap - gap) > 5
            try:
                capabilities = api.hil_capabilities(entry_id)
                snapshot = read_only_sample(api, device)
            except HAAPIError:
                capabilities = None
                snapshot = {}
            healthy = bool(
                capabilities is not None
                and capabilities["verified"] is True
                and not capabilities["identity_conflicted"]
                and capabilities["connected"] is True
                and capabilities["status_fresh"] is True
                and type(capabilities["session_generation"]) is int
            )
            if gap is not None and gap > parameters["max_gap_seconds"]:
                maximum_gap = max(maximum_gap, gap)
                append_record(
                    {
                        "event": "sample_gap",
                        "utc": utc_now.isoformat(),
                        "gap_seconds": round(gap, 3),
                        "reason": "maximum allowed sample gap exceeded",
                    }
                )
                if active_segment is not None:
                    active_segment["maximum_gap_seconds"] = round(maximum_gap, 3)
                end_segment("maximum sample gap exceeded")
            if clock_jump:
                clock_discontinuities += 1
                append_record(
                    {
                        "event": "clock_discontinuity",
                        "utc": utc_now.isoformat(),
                        "monotonic_gap_seconds": round(gap or 0, 3),
                        "wall_gap_seconds": round(wall_gap, 3),
                    }
                )
                if active_segment is not None:
                    active_segment["clock_discontinuities"] = clock_discontinuities
                end_segment("system clock discontinuity observed")
            if not healthy:
                append_record(
                    {
                        "event": "sample_gap",
                        "utc": utc_now.isoformat(),
                        "gap_seconds": round(gap, 3) if gap is not None else None,
                        "reason": "runtime disconnected, unverified or stale",
                        "session_generation": (
                            capabilities.get("session_generation")
                            if capabilities is not None
                            else None
                        ),
                    }
                )
                end_segment("fresh connected status unavailable")
            else:
                if segment_started is None:
                    begin_segment(now, utc_now)
                if (
                    previous_sample_monotonic is not None
                    and gap is not None
                    and gap > parameters["sample_interval_seconds"] + 30
                ):
                    append_record(
                        {
                            "event": "sampling_delay",
                            "utc": utc_now.isoformat(),
                            "gap_seconds": round(gap, 3),
                        }
                    )
                if capabilities["session_generation"] != last_generation:
                    append_record(
                        {
                            "event": "session_generation_change",
                            "utc": utc_now.isoformat(),
                            "previous_generation": last_generation,
                            "session_generation": capabilities["session_generation"],
                        }
                    )
                last_generation = capabilities["session_generation"]
                sample_count += 1
                if gap is not None:
                    maximum_gap = max(maximum_gap, gap)
                row = {
                    "event": "sample",
                    "utc": utc_now.isoformat(),
                    "elapsed_seconds": round(now - segment_started, 3),
                    "session_generation": capabilities["session_generation"],
                    "model": capabilities["model"],
                    "profile": capabilities["profile"],
                    "status_fresh": capabilities["status_fresh"],
                    "settings_fresh": capabilities["settings_fresh"],
                    "telemetry": snapshot,
                }
                if not append_record(row):
                    break
                active_segment["sample_count"] = sample_count
                active_segment["measured_seconds"] = round(now - segment_started, 3)
                active_segment["maximum_gap_seconds"] = round(maximum_gap, 3)
                active_segment["last_sample_utc"] = utc_now.isoformat()
                active_segment["last_session_generation"] = capabilities[
                    "session_generation"
                ]
                checkpoint()
            next_sample = now + parameters["sample_interval_seconds"]
            previous_sample_monotonic = now
            previous_sample_utc = utc_now

        now = monotonic()
        if segment_started is not None and now >= next_trace:
            trace_ok = False
            trace_reason = "trace start or finalization failed"
            capture_id: str | None = None
            trace: dict[str, Any] | None = None
            try:
                start = api.websocket(
                    [
                        {
                            "type": "allpowers_ble/hil/start",
                            "entry_id": entry_id,
                            "run_id": run_id,
                            "scenario_id": scenario.id,
                            "requested_route": args.route,
                        }
                    ]
                )[0]
                if (
                    isinstance(start, dict)
                    and start.get("schema_version") == 2
                    and isinstance(start.get("capture_id"), str)
                    and type(start.get("ttl_seconds")) is int
                    and start["ttl_seconds"] >= parameters["trace_window_seconds"]
                ):
                    capture_id = start["capture_id"]
                    try:
                        sleep(parameters["trace_window_seconds"])
                    finally:
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
                            trace = api.websocket(
                                [
                                    {
                                        "type": "allpowers_ble/hil/fetch",
                                        "entry_id": entry_id,
                                        "capture_id": capture_id,
                                    }
                                ]
                            )[0]
                    _validate_trace_result(
                        trace,
                        run_id=run_id,
                        scenario_id=scenario.id,
                        capture_id=capture_id,
                        requested_route=args.route,
                        expected_version=2,
                    )
                    trace_ok = bool(
                        trace["complete"] is True
                        and trace["observed_route"] in {"local_adapter", "active_proxy"}
                    )
                    trace_reason = (
                        "trace complete" if trace_ok else "route or trace incomplete"
                    )
                    encoded_size = len(
                        json.dumps(trace, sort_keys=True, ensure_ascii=True).encode()
                    )
                    if (
                        trace_ok
                        and _private_run_size(output_dir) + encoded_size + 4096
                        <= parameters["max_bytes"]
                    ):
                        trace_index += 1
                        trace_name = f"soak_trace_{trace_index:03d}"
                        trace_parent = output_dir / "traces"
                        if trace_parent.is_symlink():
                            raise HAAPIError(
                                "Private trace directory must not be a symlink"
                            )
                        trace_parent.mkdir(mode=0o700, exist_ok=True)
                        if (
                            trace_parent.is_symlink()
                            or stat.S_IMODE(trace_parent.stat().st_mode) & 0o077
                        ):
                            raise HAAPIError("Soak trace directory is not private")
                        trace_dir = trace_parent / trace_name
                        trace_dir.mkdir(mode=0o700, exist_ok=False)
                        _save_private_trace(trace_dir, trace)
                        active_segment["last_trace"] = f"traces/{trace_name}"
                    elif trace_ok:
                        data_limit_hit = True
                        trace_ok = False
                        trace_reason = "per-run storage cap reached before trace save"
                    append_record(
                        {
                            "event": "trace_window",
                            "utc": datetime.now(UTC).isoformat(),
                            "capture_id_present": capture_id is not None,
                            "observed_route": trace.get("observed_route")
                            if isinstance(trace, dict)
                            else None,
                            "complete": trace.get("complete") is True
                            if isinstance(trace, dict)
                            else False,
                            "dropped_events": trace.get("dropped_events")
                            if isinstance(trace, dict)
                            else None,
                            "dropped_frames": trace.get("dropped_frames")
                            if isinstance(trace, dict)
                            else None,
                            "expired": trace.get("expired")
                            if isinstance(trace, dict)
                            else None,
                            "saved": trace_ok,
                            "reason": trace_reason,
                        }
                    )
            except KeyboardInterrupt:
                if active_segment is not None:
                    active_segment["outcome"] = "inconclusive"
                    active_segment["reason"] = "soak interrupted during trace window"
                    checkpoint()
                print("Soak interrupted; private checkpoint saved")
                return 2
            except HAAPIError, OSError:
                append_record(
                    {
                        "event": "trace_window_dropped",
                        "utc": datetime.now(UTC).isoformat(),
                        "reason": "trace could not be validated or saved",
                    }
                )
            if not trace_ok:
                dropped_traces += 1
                if active_segment is not None:
                    active_segment["dropped_traces"] = dropped_traces
                end_segment(trace_reason)
            else:
                trace_count += 1
                active_segment["trace_count"] = trace_count
                checkpoint()
            next_trace = monotonic() + parameters["trace_interval_seconds"]

        now = monotonic()
        if data_limit_hit:
            break
        if (
            active_segment is not None
            and segment_started is not None
            and now - segment_started >= parameters["duration_seconds"]
            and previous_sample_monotonic is not None
            and previous_sample_monotonic - segment_started
            >= parameters["duration_seconds"]
        ):
            outcome = soak_verdict(
                measured_seconds=previous_sample_monotonic - segment_started,
                sample_count=sample_count,
                minimum_samples=parameters["minimum_samples"],
                maximum_gap_seconds=maximum_gap,
                allowed_gap_seconds=parameters["max_gap_seconds"],
                trace_count=trace_count,
                minimum_traces=trace_required,
                dropped_traces=dropped_traces,
            )
            active_segment["measured_seconds"] = round(
                previous_sample_monotonic - segment_started, 3
            )
            active_segment["outcome"] = outcome
            active_segment["ended_utc"] = datetime.now(UTC).isoformat()
            checkpoint()
            print("Soak segment:", outcome)
            return 0 if outcome == "pass" else 2

        next_event = next_sample
        if active_segment is not None:
            next_event = min(
                next_event, next_trace, segment_started + parameters["duration_seconds"]
            )
        delay = max(0.1, next_event - monotonic())
        try:
            sleep(delay)
        except KeyboardInterrupt:
            if active_segment is not None:
                active_segment["outcome"] = "inconclusive"
                active_segment["reason"] = "operator interrupted soak sampling"
                active_segment["measured_seconds"] = round(
                    monotonic() - segment_started, 3
                )
            checkpoint()
            print("Soak interrupted; private checkpoint saved")
            return 2

    if active_segment is not None:
        active_segment["outcome"] = "inconclusive"
        active_segment["reason"] = "per-run evidence byte cap reached"
        active_segment["data_limit_hit"] = data_limit_hit
    checkpoint()
    print("Soak stopped inconclusive; private checkpoint saved to:", output_dir)
    return 2


def execute(args: argparse.Namespace) -> int:
    """Execute read-only preflight and explicitly requested private HIL capture."""
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
        if args.suite not in (
            None,
            "read-only",
            "outputs",
            "recovery",
            "soak-24h",
        ):
            raise HAAPIError(
                "This suite is declarative but execution is not implemented yet; use --dry-run"
            )
        if args.suite in {"outputs", "recovery"} and args.non_interactive:
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
    if resume_state is not None:
        fingerprint = _device_fingerprint(resume_state["run_id"], selected, api.origin)
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
