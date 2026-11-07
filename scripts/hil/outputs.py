"""Guided HIL outputs scenario execution."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .api import HAAPIError, HAClient
from .discovery import Device
from .suite_runtime import SuiteServices


def _execute_output_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    environment: dict[str, Any],
    output_dir: Path,
    run_id: str,
    services: SuiteServices,
) -> int:
    """Run the bounded AC cycle, requiring separate consent for each write."""
    requested_scenario = getattr(args, "scenario_id", None) or "output_ac_cycle"
    scenario = next(
        (
            item
            for item in services.load_scenarios()
            if item.id == requested_scenario and item.suite == "outputs"
        ),
        None,
    )
    if scenario is None:
        raise HAAPIError("Unknown output scenario")
    entry_id = device.config_entry_id
    steps: list[dict[str, Any]] = []
    fingerprint = services.device_fingerprint(run_id, device, api.origin, args.route)

    def checkpoint() -> None:
        services.write_scenario_progress(
            output_dir, run_id, scenario.id, fingerprint, steps
        )

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
    print("Device:", device.model)
    for index, declaration in enumerate(scenario.steps, start=1):
        key, desired = declaration["target"], declaration["expected"]
        settings_control = key in {"eco_mode", "car_charger"}
        freshness_key = "settings_fresh" if settings_control else "status_fresh"
        capability_key = (
            "write_settings_controls" if settings_control else "write_output_controls"
        )
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
                or caps[freshness_key] is not True
                or type(generation) is not int
                or caps["capabilities"][capability_key] is not True
                or (
                    key == "car_charger"
                    and caps["capabilities"]["car_charger_enabled"] is not True
                )
                or not state.get("available")
                or state.get("state") != ("off" if desired == "on" else "on")
            ):
                row["reason"] = "live capability or expected pre-state check failed"
                checkpoint()
                print("Step", index, "blocked by live preflight")
                break
            print(f"Step {index}: {key} {state['state']} → {desired}")
            control_label = key.replace("_", " ")
            print(
                f"Confirm only if the station is attended and {control_label} "
                "is safe to change."
            )
            if (
                services.prompt("Type 1 to authorize this single action: ").strip()
                != "1"
            ):
                row["outcome"] = "skipped"
                row["reason"] = "authorization not granted"
                checkpoint()
                break
            row["pre_state"] = state["state"]
            row["session_generation"] = generation
            row["authorized"] = True

            try:
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
            except HAAPIError:
                row["prewrite_trace_failed"] = True
                raise
            if (
                not isinstance(capture, dict)
                or capture.get("schema_version") != 2
                or not isinstance(capture.get("capture_id"), str)
            ):
                row["reason"] = "trace start was not validated"
                checkpoint()
                break
            capture_id = capture["capture_id"]
            # Authorization can sit at the prompt while HA state changes. Once
            # tracing is armed, repeat every live precondition at the write boundary.
            recheck_caps = api.hil_capabilities(entry_id)
            recheck_state = api.control_state(key, entity_id)
            if (
                recheck_caps.get("verified") is not True
                or recheck_caps.get("identity_conflicted")
                or recheck_caps.get("connected") is not True
                or recheck_caps.get("pending_transaction")
                or recheck_caps.get(freshness_key) is not True
                or recheck_caps.get("session_generation") != generation
                or recheck_caps.get("capabilities", {}).get(capability_key) is not True
                or (
                    key == "car_charger"
                    and recheck_caps.get("capabilities", {}).get("car_charger_enabled")
                    is not True
                )
                or not recheck_state.get("available")
                or recheck_state.get("state") != state["state"]
            ):
                row["reason"] = "live pre-write recheck failed after authorization"
                try:
                    api.websocket(
                        [
                            {
                                "type": "allpowers_ble/hil/stop",
                                "entry_id": entry_id,
                                "capture_id": capture_id,
                            }
                        ]
                    )
                except HAAPIError:
                    row["trace_stop_failed"] = True
                checkpoint()
                print("Step stopped: live pre-write state changed")
                break
            action_submitted = False
            observed_caps: dict[str, Any] | None = None
            post: dict[str, Any] = {"available": False, "state": None}
            row["service_attempted"] = True
            try:
                api.call_control(key, entity_id, desired)
                action_submitted = True
            except HAAPIError:
                row["service_call"] = "unacknowledged; no retry performed"
            finally:
                deadline = services.monotonic() + declaration["timeout_seconds"]
                while services.monotonic() < deadline:
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
                        observed_caps.get(freshness_key) is True
                        and post.get("available") is True
                        and post.get("state") == desired
                    ):
                        break
                    services.sleep(min(1, max(0, deadline - services.monotonic())))
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
            services.validate_trace_result(
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
            services.save_private_trace(trace_dir, trace)
            row["trace"] = f"traces/{declaration['id']}"
            tx = services.correlate_transaction(
                trace,
                expected_generation=generation,
                kind="settings" if settings_control else "output",
            )
            row["transaction"] = tx
            after_caps = observed_caps or api.hil_capabilities(entry_id)
            physical = services.prompt(
                "Physical outcome: 1=yes, 2=no, 3=unable to observe: "
            ).strip()
            physical_outcome = {"1": "yes", "2": "no", "3": "unable_to_observe"}.get(
                physical
            )
            route_verified = services.route_matches_request(
                args.route, trace.get("observed_route")
            )
            telemetry_matches = (
                after_caps.get("session_generation") == generation
                and after_caps.get(freshness_key) is True
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
            row["outcome"] = services.verdict(
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
    # If the operator authorized the planned AC-off action but its trace could
    # not be armed, the service was never called. Reuse that still-current
    # authorization for one cleanup attempt, after repeating the live safety
    # checks. Never use this path after an ambiguous service call.
    if len(steps) == 2 and steps[0].get("outcome") == "pass":
        cleanup = steps[1]
        restore_key = scenario.steps[-1]["target"]
        restore_step_id = scenario.steps[-1]["id"]
        restore_expected = scenario.steps[-1]["expected"]
        restore_entity = device.control_entities.get(restore_key)
        restore_pre_state = "off" if restore_expected == "on" else "on"
        restore_is_settings = restore_key in {"eco_mode", "car_charger"}
        restore_freshness_key = (
            "settings_fresh" if restore_is_settings else "status_fresh"
        )
        restore_capability_key = (
            "write_settings_controls"
            if restore_is_settings
            else "write_output_controls"
        )
        if (
            cleanup.get("step_id") == restore_step_id
            and cleanup.get("authorized") is True
            and cleanup.get("prewrite_trace_failed") is True
            and cleanup.get("service_attempted") is not True
        ):
            cleanup.update(
                {
                    "cleanup_attempted": True,
                    "cleanup_target": restore_expected,
                    "outcome": "inconclusive",
                }
            )
            checkpoint()
            try:
                current_caps = api.hil_capabilities(entry_id)
                current_state = api.control_state(restore_key, restore_entity)
                if (
                    current_caps.get("verified") is not True
                    or current_caps.get("identity_conflicted") is not False
                    or current_caps.get("connected") is not True
                    or current_caps.get(restore_freshness_key) is not True
                    or current_caps.get("pending_transaction") is not False
                    or current_caps.get("session_generation")
                    != cleanup.get("session_generation")
                    or current_caps.get("capabilities", {}).get(restore_capability_key)
                    is not True
                    or (
                        restore_key == "car_charger"
                        and current_caps.get("capabilities", {}).get(
                            "car_charger_enabled"
                        )
                        is not True
                    )
                    or current_state.get("available") is not True
                    or current_state.get("state") != restore_pre_state
                ):
                    cleanup["cleanup_reason"] = (
                        "fresh same-session telemetry did not prove the "
                        "control state and safe restoration"
                    )
                else:
                    restore_capture = api.websocket(
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
                        not isinstance(restore_capture, dict)
                        or restore_capture.get("schema_version") != 2
                        or not isinstance(restore_capture.get("capture_id"), str)
                    ):
                        cleanup["cleanup_reason"] = (
                            "restoration trace could not be armed"
                        )
                    else:
                        restore_capture_id = restore_capture["capture_id"]
                        restore_action_submitted = False
                        restore_caps: dict[str, Any] | None = None
                        restored: dict[str, Any] = {
                            "available": False,
                            "state": None,
                        }
                        try:
                            cleanup["cleanup_service_attempted"] = True
                            checkpoint()
                            api.call_control(
                                restore_key,
                                restore_entity,
                                restore_expected,
                            )
                            restore_action_submitted = True
                        except HAAPIError:
                            cleanup["cleanup_service_call"] = (
                                "unacknowledged; no retry performed"
                            )
                        finally:
                            restore_deadline = services.monotonic() + 30
                            while services.monotonic() < restore_deadline:
                                try:
                                    restore_caps = api.hil_capabilities(entry_id)
                                    restored = api.control_state(
                                        restore_key,
                                        restore_entity,
                                    )
                                except HAAPIError:
                                    break
                                if (
                                    restore_caps.get("session_generation")
                                    != cleanup.get("session_generation")
                                    or restore_caps.get("connected") is not True
                                ):
                                    break
                                if (
                                    restore_caps.get(restore_freshness_key) is True
                                    and restored.get("available") is True
                                    and restored.get("state") == restore_expected
                                ):
                                    break
                                services.sleep(
                                    min(
                                        1,
                                        max(0, restore_deadline - services.monotonic()),
                                    )
                                )
                            restore_trace = api.websocket(
                                [
                                    {
                                        "type": "allpowers_ble/hil/stop",
                                        "entry_id": entry_id,
                                        "capture_id": restore_capture_id,
                                    }
                                ]
                            )[0]
                        services.validate_trace_result(
                            restore_trace,
                            run_id=run_id,
                            scenario_id=scenario.id,
                            capture_id=restore_capture_id,
                            requested_route=args.route,
                            expected_version=2,
                        )
                        trace_name = f"{restore_key}_cleanup"
                        trace_dir = output_dir / "traces" / trace_name
                        trace_dir.parent.mkdir(mode=0o700, exist_ok=True)
                        trace_dir.mkdir(mode=0o700, exist_ok=False)
                        services.save_private_trace(trace_dir, restore_trace)
                        restore_tx = services.correlate_transaction(
                            restore_trace,
                            expected_generation=cleanup["session_generation"],
                            kind="settings" if restore_is_settings else "output",
                        )
                        physical_restore = services.prompt(
                            f"Restoration physical outcome: 1={restore_expected}, "
                            f"2=not {restore_expected}, 3=unable to observe: "
                        ).strip()
                        physical_restore_outcome = {
                            "1": "yes",
                            "2": "no",
                            "3": "unable_to_observe",
                        }.get(physical_restore)
                        cleanup.update(
                            {
                                "cleanup_trace": f"traces/{trace_name}",
                                "cleanup_post_state": restored.get("state"),
                                "cleanup_transaction": restore_tx,
                                "cleanup_physical_outcome": physical_restore_outcome,
                                "cleanup_route": restore_trace.get("observed_route"),
                            }
                        )
                        cleanup["cleanup_outcome"] = services.verdict(
                            telemetry_matches=(
                                restore_caps is not None
                                and restore_caps.get("session_generation")
                                == cleanup.get("session_generation")
                                and restore_caps.get(restore_freshness_key) is True
                                and restored.get("available") is True
                                and restored.get("state") == restore_expected
                            ),
                            telemetry_contradictory=(
                                restored.get("available") is True
                                and restored.get("state") not in {"off", "on"}
                            ),
                            physical_outcome=physical_restore_outcome,
                            route_verified=services.route_matches_request(
                                args.route, restore_trace.get("observed_route")
                            ),
                            trace_complete=restore_trace.get("complete") is True,
                            action_submitted=restore_action_submitted,
                            transaction_confirmed=restore_tx is not None,
                        )
                        cleanup["cleanup_reason"] = (
                            "restoration evidence collected; scenario remains "
                            "inconclusive because its planned trace could not start"
                        )
            except HAAPIError, OSError, EOFError, KeyboardInterrupt:
                cleanup["cleanup_reason"] = (
                    "safe restoration could not be verified; no retry performed"
                )
            checkpoint()

    restoration_passed = bool(steps and steps[-1].get("cleanup_outcome") == "pass")
    if (len(steps) < len(scenario.steps) and not restoration_passed) or (
        steps and steps[-1].get("outcome") != "pass"
    ):
        if not restoration_passed:
            print(
                "Check the output physically and restore its intended state manually if needed."
            )
    print("Private scenario results saved to:", output_dir)
    return (
        0
        if len(steps) == len(scenario.steps)
        and all(s.get("outcome") == "pass" for s in steps)
        else 2
    )
