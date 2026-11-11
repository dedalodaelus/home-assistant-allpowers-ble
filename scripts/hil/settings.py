"""Guided HIL settings scenario execution."""

from __future__ import annotations

import argparse
from datetime import UTC
from pathlib import Path
from typing import Any, Callable

from .api import HAAPIError, HAClient
from .capabilities import feature_write_authorized
from .discovery import Device
from .suite_runtime import SuiteServices


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
    services: SuiteServices,
) -> dict[str, Any]:
    """Execute one explicitly authorized, traced settings service action."""
    row: dict[str, Any] = {
        "step_id": trace_name,
        "target": key,
        "pre_state": expected_pre_state,
        "expected": target,
        "session_generation": generation,
        "outcome": "inconclusive",
        "started_utc": services.datetime.now(UTC).isoformat(),
    }
    started = services.monotonic()
    on_update(row)
    try:
        caps = api.hil_capabilities(device.config_entry_id)
        current = api.control_state(key, entity_id)
    except HAAPIError:
        row["reason"] = "settings preflight unavailable; service not attempted"
        row["ended_utc"] = services.datetime.now(UTC).isoformat()
        on_update(row)
        return row
    if (
        caps.get("verified") is not True
        or caps.get("identity_conflicted") is not False
        or caps.get("connected") is not True
        or caps.get("status_fresh") is not True
        or caps.get("settings_fresh") is not True
        or caps.get("pending_transaction") is not False
        or caps.get("session_generation") != generation
        or not feature_write_authorized(caps, key, "write_settings_controls")
        or current.get("available") is not True
        or current.get("state") != expected_pre_state
        or not services.control_value_available(key, current, target)
    ):
        row["reason"] = "fresh same-session settings preflight failed"
        row["ended_utc"] = services.datetime.now(UTC).isoformat()
        on_update(row)
        return row

    capture_id: str | None = None
    trace: dict[str, Any] | None = None
    observed_caps: dict[str, Any] | None = None
    post: dict[str, Any] = {"available": False, "state": None, "options": []}
    action_submitted = False
    try:
        capture = api.websocket(
            [
                {
                    "type": "allpowers_ble/hil/start",
                    "entry_id": device.config_entry_id,
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
            row["reason"] = "settings trace start was not validated"
            row["ended_utc"] = services.datetime.now(UTC).isoformat()
            on_update(row)
            return row
        capture_id = capture["capture_id"]
        write_caps = api.hil_capabilities(device.config_entry_id)
        write_state = api.control_state(key, entity_id)
        if (
            write_caps.get("verified") is not True
            or write_caps.get("identity_conflicted") is not False
            or write_caps.get("connected") is not True
            or write_caps.get("status_fresh") is not True
            or write_caps.get("settings_fresh") is not True
            or write_caps.get("pending_transaction") is not False
            or write_caps.get("session_generation") != generation
            or not feature_write_authorized(write_caps, key, "write_settings_controls")
            or write_state.get("available") is not True
            or write_state.get("state") != expected_pre_state
            or not services.control_value_available(key, write_state, target)
        ):
            row["reason"] = "settings changed after trace start; service not attempted"
            row["ended_utc"] = services.datetime.now(UTC).isoformat()
            on_update(row)
            return row
        row["service_attempted"] = True
        on_update(row)
        try:
            api.call_control(key, entity_id, target)
            action_submitted = True
            row["service_call"] = "acknowledged"
        except HAAPIError:
            row["service_call"] = "unacknowledged; no retry performed"
        deadline = services.monotonic() + 30
        while services.monotonic() < deadline:
            try:
                observed_caps = api.hil_capabilities(device.config_entry_id)
                post = api.control_state(key, entity_id)
            except HAAPIError:
                row["reason"] = "settings telemetry unavailable during observation"
                break
            if (
                observed_caps.get("session_generation") != generation
                or observed_caps.get("connected") is not True
            ):
                row["reason"] = "settings session changed during observation"
                break
            if (
                observed_caps.get("settings_fresh") is True
                and post.get("available") is True
                and post.get("state") == target
            ):
                break
            services.sleep(min(1, max(0, deadline - services.monotonic())))
    except HAAPIError, EOFError, KeyboardInterrupt:
        row["reason"] = "settings action could not be traced; no service retry"
        row["ended_utc"] = services.datetime.now(UTC).isoformat()
        row["elapsed_seconds"] = round(services.monotonic() - started, 3)
        on_update(row)
        return row
    finally:
        if capture_id is not None:
            try:
                trace = api.websocket(
                    [
                        {
                            "type": "allpowers_ble/hil/stop",
                            "entry_id": device.config_entry_id,
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
                                "entry_id": device.config_entry_id,
                                "capture_id": capture_id,
                            }
                        ]
                    )[0]
                except HAAPIError:
                    trace = None
            if row.get("service_attempted") is True:
                row.update(
                    {
                        "post_state": post.get("state"),
                        "post_state_available": post.get("available") is True,
                        "post_settings_fresh": (
                            observed_caps is not None
                            and observed_caps.get("settings_fresh") is True
                        ),
                        "post_session_generation": (
                            observed_caps.get("session_generation")
                            if observed_caps is not None
                            else None
                        ),
                        "post_pending_transaction": (
                            observed_caps.get("pending_transaction")
                            if observed_caps is not None
                            else None
                        ),
                    }
                )
                on_update(row)

    if not isinstance(trace, dict):
        row["reason"] = "settings trace could not be finalized"
        row["ended_utc"] = services.datetime.now(UTC).isoformat()
        row["elapsed_seconds"] = round(services.monotonic() - started, 3)
        on_update(row)
        return row
    try:
        services.validate_trace_result(
            trace,
            run_id=run_id,
            scenario_id=scenario.id,
            capture_id=capture_id,
            requested_route=args.route,
            expected_version=2,
        )
        trace_dir = output_dir / "traces" / trace_name
        trace_dir.parent.mkdir(mode=0o700, exist_ok=True)
        trace_dir.mkdir(mode=0o700, exist_ok=False)
        services.save_private_trace(trace_dir, trace)
    except HAAPIError, OSError:
        row["reason"] = "settings trace could not be validated or saved; no retry"
        row["ended_utc"] = services.datetime.now(UTC).isoformat()
        row["elapsed_seconds"] = round(services.monotonic() - started, 3)
        on_update(row)
        return row
    transaction = services.correlate_transaction(
        trace, expected_generation=generation, kind="settings"
    )
    row.update(
        {
            "trace": f"traces/{trace_name}",
            "transaction": transaction,
            "post_state": post.get("state"),
            "route": trace.get("observed_route"),
            "causal_limit": "device transaction IDs are unavailable; convergence is not causal proof",
        }
    )
    on_update(row)
    try:
        physical = services.prompt(
            f"Physical outcome for {key}: 1=correct, 2=incorrect, 3=unable to observe: "
        ).strip()
    except EOFError, KeyboardInterrupt:
        row["reason"] = "physical observation interrupted; no service retry"
        row["ended_utc"] = services.datetime.now(UTC).isoformat()
        row["elapsed_seconds"] = round(services.monotonic() - started, 3)
        on_update(row)
        return row
    physical_outcome = {"1": "yes", "2": "no", "3": "unable_to_observe"}.get(physical)
    route_verified = services.route_matches_request(
        args.route, trace.get("observed_route")
    )
    row.update(
        {
            "trace": f"traces/{trace_name}",
            "transaction": transaction,
            "post_state": post.get("state"),
            "physical_outcome": physical_outcome,
            "route": trace.get("observed_route"),
            "causal_limit": "device transaction IDs are unavailable; convergence is not causal proof",
            "ended_utc": services.datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(services.monotonic() - started, 3),
        }
    )
    row["outcome"] = services.verdict(
        telemetry_matches=(
            observed_caps is not None
            and observed_caps.get("session_generation") == generation
            and observed_caps.get("settings_fresh") is True
            and observed_caps.get("pending_transaction") is False
            and post.get("available") is True
            and post.get("state") == target
        ),
        telemetry_contradictory=post.get("available") is True
        and post.get("state") not in (target, expected_pre_state),
        physical_outcome=physical_outcome,
        route_verified=route_verified,
        trace_complete=trace.get("complete") is True,
        action_submitted=action_submitted,
        transaction_confirmed=transaction is not None,
    )
    on_update(row)
    return row


def _execute_settings_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    services: SuiteServices,
) -> int:
    """Run one explicitly selected setting and offer a guarded restoration."""
    scenario_id = getattr(args, "scenario_id", None)
    scenario = next(
        (
            item
            for item in services.load_scenarios()
            if item.id == scenario_id and item.suite == "settings"
        ),
        None,
    )
    if scenario is None:
        raise HAAPIError("Select a declared settings scenario with --scenario-id")
    entry_id = device.config_entry_id
    fingerprint = services.device_fingerprint(run_id, device, api.origin, args.route)
    steps: list[dict[str, Any]] = []

    def checkpoint() -> None:
        services.write_scenario_progress(
            output_dir, run_id, scenario.id, fingerprint, steps
        )

    def checkpoint_step(row: dict[str, Any]) -> None:
        if not any(existing is row for existing in steps):
            steps.append(row)
        checkpoint()

    if args.non_interactive or entry_id is None or device.entry_state != "loaded":
        steps.append(
            {
                "step_id": "settings_preflight",
                "outcome": "not_applicable",
                "reason": "interactive authorization or loaded entry unavailable",
            }
        )
        checkpoint()
        return 2
    declaration = scenario.steps[0]
    key = declaration["target"]
    entity_id = device.control_entities.get(key)
    try:
        caps = api.hil_capabilities(entry_id)
        state = api.control_state(key, entity_id) if entity_id else {"available": False}
    except HAAPIError:
        caps = {}
        state = {"available": False, "state": None, "options": []}
    target = declaration["expected"]
    if (
        caps.get("verified") is not True
        or caps.get("identity_conflicted") is not False
        or caps.get("connected") is not True
        or caps.get("pending_transaction") is not False
        or caps.get("settings_fresh") is not True
        or not feature_write_authorized(caps, key, "write_settings_controls")
        or type(caps.get("session_generation")) is not int
        or state.get("available") is not True
        or not services.control_value_available(key, state, target)
    ):
        steps.append(
            {
                "step_id": declaration["id"],
                "target": key,
                "outcome": "not_applicable",
                "reason": "verified fresh settings control is unavailable",
            }
        )
        checkpoint()
        return 2
    initial = state["state"]
    if initial == target:
        steps.append(
            {
                "step_id": declaration["id"],
                "target": key,
                "pre_state": initial,
                "expected": target,
                "outcome": "skipped",
                "reason": "selected option already matches the current setting",
            }
        )
        checkpoint()
        return 2
    print(f"Setting scenario: {scenario.title}")
    print(f"Current {key}: {initial}; requested value: {target}")
    try:
        authorized = services.prompt(
            "Type 1 to authorize this single settings change: "
        ).strip()
    except EOFError, KeyboardInterrupt:
        authorized = ""
    if authorized != "1":
        steps.append(
            {
                "step_id": declaration["id"],
                "target": key,
                "outcome": "skipped",
                "reason": "authorization not granted",
            }
        )
        checkpoint()
        return 2
    change = _execute_settings_write(
        api,
        args,
        device,
        output_dir,
        run_id,
        scenario,
        key,
        entity_id,
        target,
        initial,
        caps["session_generation"],
        declaration["id"],
        checkpoint_step,
        services,
    )
    if change.get("outcome") != "pass":
        # A service response can be lost after the setting changed. Offer one
        # separately authorized, traced restoration only when fresh state and
        # a clear transaction slot still prove that the changed value is live.
        if change.get("service_attempted") is True:
            change["cleanup_status"] = "checking_live_state"
            checkpoint_step(change)
            try:
                cleanup_caps = api.hil_capabilities(entry_id)
                cleanup_state = api.control_state(key, entity_id)
            except HAAPIError:
                cleanup_caps = {}
                cleanup_state = {"available": False, "state": None}
            can_restore = (
                cleanup_caps.get("verified") is True
                and cleanup_caps.get("identity_conflicted") is False
                and cleanup_caps.get("connected") is True
                and cleanup_caps.get("status_fresh") is True
                and cleanup_caps.get("settings_fresh") is True
                and cleanup_caps.get("pending_transaction") is False
                and cleanup_caps.get("session_generation") == caps["session_generation"]
                and feature_write_authorized(
                    cleanup_caps, key, "write_settings_controls"
                )
                and cleanup_state.get("available") is True
            )
            if can_restore and cleanup_state.get("state") == initial:
                change["cleanup_status"] = "original_state_confirmed_no_restore_needed"
                checkpoint_step(change)
            elif can_restore and cleanup_state.get("state") == target:
                change["cleanup_status"] = "awaiting_restoration_authorization"
                checkpoint_step(change)
                try:
                    cleanup_authorized = (
                        services.prompt(
                            f"Fresh telemetry confirms {key} changed despite incomplete "
                            f"evidence. Type 1 to authorize one traced restoration to "
                            f"{initial}: "
                        ).strip()
                        == "1"
                    )
                except EOFError, KeyboardInterrupt:
                    cleanup_authorized = False
                if cleanup_authorized:
                    change["cleanup_status"] = "restoration_authorized"
                    checkpoint_step(change)
                    restoration = _execute_settings_write(
                        api,
                        args,
                        device,
                        output_dir,
                        run_id,
                        scenario,
                        key,
                        entity_id,
                        initial,
                        target,
                        caps["session_generation"],
                        "restore_after_ambiguous_write",
                        checkpoint_step,
                        services,
                    )
                    change["cleanup_status"] = (
                        "restoration_confirmed"
                        if restoration.get("outcome") == "pass"
                        else "restoration_inconclusive"
                    )
                    checkpoint_step(change)
                    if restoration.get("outcome") == "pass":
                        print(
                            "Ambiguous settings action was restored; the original "
                            "scenario remains inconclusive"
                        )
                else:
                    change["cleanup_status"] = "restoration_authorization_declined"
                    checkpoint_step(change)
            else:
                change["cleanup_status"] = "manual_inspection_required"
                change["cleanup_reason"] = (
                    "fresh same-session state did not prove the original or "
                    "requested value with no transaction pending"
                )
                checkpoint_step(change)
        print("Settings change inconclusive; inspect and restore manually if needed")
        return 2
    try:
        restore_authorized = services.prompt(
            "Type 1 to authorize restoring the original setting: "
        ).strip()
    except EOFError, KeyboardInterrupt:
        restore_authorized = ""
    if restore_authorized != "1":
        steps.append(
            {
                "step_id": "restore_original",
                "target": key,
                "expected": initial,
                "outcome": "skipped",
                "reason": "restoration not authorized; manual restoration may be needed",
            }
        )
        checkpoint()
        print("Restore the original setting manually when safe")
        return 2
    restoration = _execute_settings_write(
        api,
        args,
        device,
        output_dir,
        run_id,
        scenario,
        key,
        entity_id,
        initial,
        target,
        caps["session_generation"],
        "restore_original",
        checkpoint_step,
        services,
    )
    if restoration.get("outcome") != "pass":
        print("Setting restoration inconclusive; inspect and restore manually")
        return 2
    print("Setting changed and restored with fresh same-session evidence")
    return 0
