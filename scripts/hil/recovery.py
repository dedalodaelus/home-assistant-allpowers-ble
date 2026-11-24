"""Guided HIL recovery scenario execution."""

from __future__ import annotations

import argparse
from datetime import UTC
from pathlib import Path
import secrets
from typing import Any

from .api import HAAPIError, HAClient
from .discovery import Device
from .suite_runtime import SuiteServices
from .storage import run_root

_VERIFIED_ROUTES = {"local_adapter", "active_proxy"}


def _print_reconnect_instructions(observed_route: str) -> None:
    """Explain a safe manual interruption for the currently observed route."""
    print("Manual BLE reconnect test (observed route: " + observed_route + ")")
    print(
        "Goal: interrupt HA's connection to this station, then restore the same route."
    )
    if observed_route == "active_proxy":
        print(
            "Temporarily make the connected ESPHome Bluetooth Proxy unavailable "
            "(for example, power-cycle the proxy), restore it, and verify HA "
            "reconnects through active_proxy. Do not turn off the station or "
            "toggle its Bluetooth as a substitute for a proxy outage."
        )
    elif observed_route == "local_adapter":
        print("Physical USB removal is not required for a local_adapter test.")
        print(
            "On the Linux/BlueZ system that owns HA's adapter, use "
            "bluetoothctl list, then select <controller-address> and show. "
            "Run power off, wait for HA to detect the disconnect, then power on."
        )
        print(
            "This disrupts every device using that controller. For HA Container "
            "it is usually the host; for a VM use the guest that owns the passed- "
            "through controller. These commands may not be available in HA OS "
            "or arbitrary SSH add-ons. If you cannot control the HA adapter "
            "safely, skip."
        )
    else:
        print(
            "The connected route is not verified. Use only a route-neutral "
            "interruption that you can safely observe and restore; do not infer "
            "a route from --route or scanner availability. Otherwise skip."
        )


def _route_recovered_on_same_lane(expected_route: str, actual_route: Any) -> bool:
    """Require a reconnect on the route verified before the manual outage."""
    return expected_route in _VERIFIED_ROUTES and actual_route == expected_route


def _has_status_refresh_exchange(
    trace: Any, *, session_generation: int, observed_route: str
) -> bool:
    """Require this capture's completed status request and later decoded reply."""
    if not isinstance(trace, dict):
        return False
    events = trace.get("events")
    frames = trace.get("frames")
    if not isinstance(events, list) or not isinstance(frames, list):
        return False
    for frame in frames:
        if not (
            isinstance(frame, dict)
            and frame.get("event") == "write_attempt"
            and frame.get("direction") == "tx"
            and frame.get("command_type") == "status_request"
            and frame.get("frame_validation") == "validated_tx"
            and frame.get("session_generation") == session_generation
            and frame.get("observed_route") == observed_route
            and isinstance(frame.get("transaction_id"), str)
        ):
            continue
        transaction_id = frame["transaction_id"]
        frame_seq = frame.get("seq", 0)
        for write in events:
            if not (
                isinstance(write, dict)
                and write.get("event") == "write_ok"
                and write.get("transaction_id") == transaction_id
                and write.get("session_generation") == session_generation
                and write.get("observed_route") == observed_route
                and write.get("seq", 0) > frame_seq
            ):
                continue
            if any(
                isinstance(event, dict)
                and event.get("event") == "notification_decoded"
                and event.get("session_generation") == session_generation
                and event.get("observed_route") == observed_route
                and event.get("seq", 0) > write.get("seq", 0)
                and isinstance(event.get("decoded_packet_types"), list)
                and "StatusData" in event["decoded_packet_types"]
                for event in events
            ):
                return True
    return False


def _execute_recovery_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    services: SuiteServices,
    resume_state: dict[str, Any] | None = None,
) -> int:
    """Guide a human action and observe stable or recovered HA runtime state."""
    scenario = next(
        item for item in services.load_scenarios() if item.id == "recovery_reconnect"
    )
    entry_id = device.config_entry_id
    guided_reconnect_required = (
        getattr(args, "guided_reconnect_required", False) is True
    )
    fingerprint = services.device_fingerprint(run_id, device, api.origin, args.route)
    steps: list[dict[str, Any]] = (
        [dict(step) for step in resume_state["steps"]]
        if resume_state is not None
        else []
    )
    run_started = services.monotonic()
    resumed_with_gap = resume_state is not None

    def checkpoint() -> None:
        services.write_scenario_progress(
            output_dir, run_id, scenario.id, fingerprint, steps
        )

    def prompt(question: str) -> str:
        try:
            return services.prompt(question).strip()
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
        observed_route = baseline.get("observed_route", "unverified")
        steps.append(
            {
                "step_id": "baseline",
                "outcome": "pass",
                "session_generation": generation,
                "observed_route": observed_route,
                "connected": True,
                "status_fresh": True,
                "utc": services.datetime.now(UTC).isoformat(),
                "elapsed_ms": int((services.monotonic() - run_started) * 1000),
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
            or action_step.get("action") == "route interruption and restoration"
            and (
                action_step.get("disconnect_observed") is not True
                or action_step.get("restoration_confirmed") is not True
            )
            or guided_reconnect_required
            and action_step.get("action") != "route interruption and restoration"
            or any(
                step.get("outcome") != "inconclusive" for step in previous_observations
            )
        ):
            raise HAAPIError("Checkpoint is not safely resumable")
        generation = baseline_step["session_generation"]
        observed_route = baseline_step.get("observed_route", "unverified")
        if observed_route not in {*_VERIFIED_ROUTES, "unverified"}:
            observed_route = "unverified"
        steps.append(
            {
                "step_id": "resume",
                "outcome": "inconclusive",
                "utc": services.datetime.now(UTC).isoformat(),
                "elapsed_ms": int((services.monotonic() - run_started) * 1000),
                "clock_discontinuity": True,
                "reason": "process restart creates an unobserved interval",
            }
        )
        checkpoint()

    if resume_state is None:
        print("Select the manual recovery action you will perform:")
        if guided_reconnect_required:
            _print_reconnect_instructions(observed_route)
            print("  1. Interrupt and restore the selected BLE route")
            choices = {"1": "route interruption and restoration"}
            prompt_text = "Action [1], or 0 to skip: "
        else:
            _print_reconnect_instructions(observed_route)
            print("  1. Interrupt and restore the selected BLE route")
            print("  2. Use vendor-app contention, then close the app")
            print("  3. Reload or upgrade Home Assistant / the integration")
            choices = {
                "1": "route interruption and restoration",
                "2": "vendor-app contention",
                "3": "Home Assistant or integration reload/upgrade",
            }
            prompt_text = "Action [1-3], or 0 to skip: "
        choice = prompt(prompt_text)
        action = choices.get(choice)
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
        if action == "route interruption and restoration":
            print(
                "This tests the Home Assistant-to-station BLE route. It does not "
                "test a device Bluetooth toggle or an HA reload. A failover to "
                "another route does not prove recovery of the original route."
            )
            print(
                "First interrupt the selected route and leave it unavailable. "
                "The runner will poll Home Assistant for an explicit disconnected "
                "snapshot before asking you to restore it."
            )
            if prompt("Type 1 when the route is interrupted and remains down: ") != "1":
                steps.append(
                    {
                        "step_id": "operator_action",
                        "outcome": "skipped",
                        "action": action,
                        "reason": "operator did not confirm the interruption phase",
                    }
                )
                checkpoint()
                return 2
            disconnect_deadline = (
                services.monotonic() + scenario.steps[2]["timeout_seconds"]
            )
            disconnect_observed = False
            try:
                while services.monotonic() < disconnect_deadline:
                    try:
                        snapshot = api.hil_capabilities(entry_id)
                    except HAAPIError:
                        snapshot = None
                    if (
                        isinstance(snapshot, dict)
                        and snapshot.get("connected") is False
                    ):
                        disconnect_observed = True
                        break
                    remaining = disconnect_deadline - services.monotonic()
                    if remaining > 0:
                        services.sleep(min(2, remaining))
            except KeyboardInterrupt:
                steps.append(
                    {
                        "step_id": "operator_action",
                        "outcome": "inconclusive",
                        "action": action,
                        "operator_confirmed": False,
                        "disconnect_observed": False,
                        "reason": "operator interrupted the bounded outage observation",
                    }
                )
                checkpoint()
                print("Outage observation interrupted; private checkpoint saved")
                return 2
            if not disconnect_observed:
                steps.append(
                    {
                        "step_id": "operator_action",
                        "outcome": "inconclusive",
                        "action": action,
                        "operator_confirmed": False,
                        "disconnect_observed": False,
                        "reason": (
                            "Home Assistant did not report an explicit disconnected "
                            "state before the bounded interruption wait expired; "
                            "keep the route state safe and rerun only after it is restored"
                        ),
                        "utc": services.datetime.now(UTC).isoformat(),
                        "elapsed_ms": int((services.monotonic() - run_started) * 1000),
                    }
                )
                checkpoint()
                print(
                    "Inconclusive: no explicit HA disconnected snapshot was observed; "
                    "restore the route safely; the interruption is not qualified."
                )
                return 2
            action_step = {
                "step_id": "operator_action",
                "outcome": "inconclusive",
                "action": action,
                "operator_confirmed": False,
                "disconnect_observed": True,
                "interruption_observed": True,
                "observed_connected": False,
                "utc": services.datetime.now(UTC).isoformat(),
                "elapsed_ms": int((services.monotonic() - run_started) * 1000),
            }
            steps.append(action_step)
            checkpoint()
            if prompt("Type 1 after restoring the same BLE route: ") != "1":
                action_step["reason"] = "operator did not confirm route restoration"
                checkpoint()
                return 2
            action_step["outcome"] = "pass"
            action_step["operator_confirmed"] = True
            action_step["restoration_confirmed"] = True
            checkpoint()
        elif prompt("Type 1 after you have completed the selected action: ") != "1":
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
        if action != "route interruption and restoration":
            action_step = {
                "step_id": "operator_action",
                "outcome": "pass",
                "action": action,
                "operator_confirmed": True,
                "utc": services.datetime.now(UTC).isoformat(),
                "elapsed_ms": int((services.monotonic() - run_started) * 1000),
            }
        if action == "vendor-app contention":
            print(
                "Report only what happened in the official app; no recording is requested."
            )
            print("  1. Could not connect")
            print("  2. Connected")
            print("  3. Unable or unwilling to test")
            app_result = prompt("App result [1-3]: ")
            action_step["operator_app_result"] = {
                "1": "could_not_connect",
                "2": "connected",
                "3": "unable_or_unwilling",
            }.get(app_result, "unable_or_unwilling")
        if action != "route interruption and restoration":
            steps.append(action_step)
        checkpoint()
    else:
        print("Resuming observation only; no operator action will be repeated.")

    deadline = services.monotonic() + scenario.steps[2]["timeout_seconds"]
    saw_disconnect = bool(action_step.get("disconnect_observed") is True)
    recovered: dict[str, Any] | None = None
    stable: dict[str, Any] | None = None
    stable_since: float | None = None
    vendor_action = action_step.get("action") == "vendor-app contention"
    try:
        while services.monotonic() < deadline:
            try:
                current = api.hil_capabilities(entry_id)
            except HAAPIError:
                services.sleep(2)
                continue
            if current["connected"] is not True:
                saw_disconnect = True
                stable_since = None
            if (
                vendor_action
                and current["verified"] is True
                and not current["identity_conflicted"]
                and current["connected"] is True
                and current["status_fresh"] is True
                and current["session_generation"] == generation
            ):
                stable_since = stable_since or services.monotonic()
                if services.monotonic() - stable_since >= 4:
                    stable = current
                    break
            else:
                stable_since = None
            if (
                current["verified"] is True
                and not current["identity_conflicted"]
                and current["connected"] is True
                and current["status_fresh"] is True
                and type(current["session_generation"]) is int
                and current["session_generation"] != generation
            ):
                recovered = current
                break
            services.sleep(2)
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
        "utc": services.datetime.now(UTC).isoformat(),
        "elapsed_ms": int((services.monotonic() - run_started) * 1000),
        "session_generation_before": generation,
        "disconnect_observed": saw_disconnect,
    }
    steps.append(observation)
    trace: dict[str, Any] | None = None
    trace_disconnect_observed = False
    observed_session = recovered or stable
    if observed_session is not None:
        observation.update(
            {
                "session_generation_after": observed_session["session_generation"],
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
                    if guided_reconnect_required:
                        refresh_button = device.refresh_button
                        if refresh_button is None:
                            raise HAAPIError(
                                "A registry-resolved status refresh button is required"
                            )
                        api.request_read_only_refresh(refresh_button)
                    if vendor_action or guided_reconnect_required:
                        ttl_seconds = start.get("ttl_seconds")
                        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
                            raise HAAPIError("Invalid bounded recovery capture TTL")
                        capture_deadline = services.monotonic() + ttl_seconds - 1
                        print(
                            "Waiting for decoded status telemetry for up to",
                            ttl_seconds - 1,
                            "seconds (bounded by the trace TTL).",
                        )
                        while services.monotonic() < capture_deadline:
                            try:
                                partial = api.websocket(
                                    [
                                        {
                                            "type": "allpowers_ble/hil/fetch",
                                            "entry_id": entry_id,
                                            "capture_id": capture_id,
                                        }
                                    ]
                                )[0]
                            except HAAPIError:
                                partial = None
                            events = (
                                partial.get("events")
                                if isinstance(partial, dict)
                                else None
                            )
                            matching_status = (
                                _has_status_refresh_exchange(
                                    partial,
                                    session_generation=observed_session[
                                        "session_generation"
                                    ],
                                    observed_route=(
                                        partial.get("observed_route")
                                        if isinstance(partial, dict)
                                        else "unverified"
                                    ),
                                )
                                if guided_reconnect_required
                                else isinstance(events, list)
                                and any(
                                    isinstance(event, dict)
                                    and event.get("event") == "notification_decoded"
                                    and event.get("session_generation")
                                    == observed_session["session_generation"]
                                    and isinstance(
                                        event.get("decoded_packet_types"), list
                                    )
                                    and "StatusData" in event["decoded_packet_types"]
                                    for event in events
                                )
                            )
                            if matching_status:
                                break
                            remaining = capture_deadline - services.monotonic()
                            if remaining > 0:
                                services.sleep(min(2, remaining))
                    else:
                        services.sleep(2)
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
                services.validate_trace_result(
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
                services.save_private_trace(trace_dir, trace)
                observation["trace"] = f"traces/{trace_name}"
                observation["route"] = trace.get("observed_route")
                observation["trace_complete"] = trace.get("complete") is True
                if action_step.get("action") == "route interruption and restoration":
                    observation["route_matches_pre_action"] = (
                        _route_recovered_on_same_lane(
                            observed_route, trace.get("observed_route")
                        )
                    )
                    if not observation["route_matches_pre_action"]:
                        print(
                            "Observed reconnect route:",
                            trace.get("observed_route", "unverified"),
                        )
                        print(
                            "This does not establish recovery on the route "
                            "verified before the action."
                        )
                events = trace.get("events")
                if isinstance(events, list) and any(
                    isinstance(event, dict)
                    and event.get("event") in {"disconnect", "disconnected"}
                    for event in events
                ):
                    trace_disconnect_observed = True
                    saw_disconnect = True
                    observation["disconnect_observed"] = True
                    if recovered is None:
                        observation["ha_observation"] = "inconclusive"
                observation["fresh_status_telemetry"] = (
                    _has_status_refresh_exchange(
                        trace,
                        session_generation=observed_session["session_generation"],
                        observed_route=trace.get("observed_route", "unverified"),
                    )
                    if guided_reconnect_required
                    else bool(
                        isinstance(events, list)
                        and any(
                            isinstance(event, dict)
                            and event.get("event") == "notification_decoded"
                            and event.get("session_generation")
                            == observed_session["session_generation"]
                            and isinstance(event.get("decoded_packet_types"), list)
                            and "StatusData" in event["decoded_packet_types"]
                            for event in events
                        )
                    )
                )
        except KeyboardInterrupt:
            observation["reason"] = "operator interrupted the bounded route capture"
            checkpoint()
            print("Route capture interrupted; private checkpoint saved")
            return 2
        except HAAPIError, OSError:
            observation["trace"] = "unavailable"

    if vendor_action:
        observation["operator_app_result"] = action_step.get(
            "operator_app_result", "unable_or_unwilling"
        )
        observation["route_verified"] = bool(
            trace is not None
            and services.route_matches_request(args.route, trace.get("observed_route"))
        )
        machine_evidence_complete = bool(
            observed_session is not None
            and observation.get("fresh_status_telemetry") is True
            and not trace_disconnect_observed
            and (recovered is not None or not saw_disconnect)
            and not resumed_with_gap
            and action_step.get("operator_app_result") != "unable_or_unwilling"
            and observation["route_verified"]
            and trace is not None
            and trace.get("complete") is True
        )
        observation["outcome"] = "pass" if machine_evidence_complete else "inconclusive"
        observation["ha_observation"] = (
            ("ha_recovered" if recovered is not None else "ha_stable")
            if machine_evidence_complete
            else "inconclusive"
        )
        observation["limitation"] = (
            "operator-reported app result; HA session and fresh status are observed, "
            "but app-side causality and uninterrupted intervals outside capture are unknown"
        )
        checkpoint()
        print(
            "HA observation:",
            observation.get("ha_observation", "inconclusive"),
            observation["outcome"],
        )
        print("Scenario checkpoint saved to:", run_root(output_dir))
        return 0 if observation["outcome"] == "pass" else 2

    print(
        "Physical recovery outcome: 1=recovered, 2=not recovered, 3=unable to observe"
    )
    physical = prompt("Outcome [1-3]: ")
    physical_outcome = {"1": "yes", "2": "no", "3": "unable_to_observe"}.get(physical)
    observation["physical_outcome"] = physical_outcome
    observation["route_verified"] = bool(
        trace is not None
        and (
            _route_recovered_on_same_lane(observed_route, trace.get("observed_route"))
            if action_step.get("action") == "route interruption and restoration"
            else services.route_matches_request(args.route, trace.get("observed_route"))
        )
    )
    reconnect_confirmed = bool(
        recovered is not None
        and type(recovered.get("session_generation")) is int
        and recovered["session_generation"] != generation
    )
    observation["outcome"] = services.recovery_verdict(
        telemetry_matches=(
            recovered is not None
            and (not guided_reconnect_required or reconnect_confirmed)
            and (
                not guided_reconnect_required
                or (
                    saw_disconnect
                    and observation.get("fresh_status_telemetry") is True
                    and observation.get("connected") is True
                    and observation.get("status_fresh") is True
                )
            )
        ),
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
    print("Scenario checkpoint saved to:", run_root(output_dir))
    return 0 if observation["outcome"] == "pass" else 2
