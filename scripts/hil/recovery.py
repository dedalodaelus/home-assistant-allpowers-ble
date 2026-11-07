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


def _execute_recovery_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    services: SuiteServices,
    resume_state: dict[str, Any] | None = None,
) -> int:
    """Guide a human recovery action and observe a new, fresh runtime session."""
    scenario = next(
        item for item in services.load_scenarios() if item.id == "recovery_reconnect"
    )
    entry_id = device.config_entry_id
    fingerprint = services.device_fingerprint(run_id, device, api.origin, args.route)
    steps: list[dict[str, Any]] = (
        [dict(step) for step in resume_state["steps"]]
        if resume_state is not None
        else []
    )
    run_started = services.monotonic()

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
        steps.append(
            {
                "step_id": "baseline",
                "outcome": "pass",
                "session_generation": generation,
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
                "utc": services.datetime.now(UTC).isoformat(),
                "elapsed_ms": int((services.monotonic() - run_started) * 1000),
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
            "utc": services.datetime.now(UTC).isoformat(),
            "elapsed_ms": int((services.monotonic() - run_started) * 1000),
        }
        steps.append(action_step)
        checkpoint()
    else:
        print("Resuming observation only; no operator action will be repeated.")

    deadline = services.monotonic() + scenario.steps[2]["timeout_seconds"]
    saw_disconnect = False
    recovered: dict[str, Any] | None = None
    try:
        while services.monotonic() < deadline:
            try:
                current = api.hil_capabilities(entry_id)
            except HAAPIError:
                services.sleep(2)
                continue
            if current["connected"] is not True:
                saw_disconnect = True
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
        and services.route_matches_request(args.route, trace.get("observed_route"))
    )
    observation["outcome"] = services.recovery_verdict(
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
