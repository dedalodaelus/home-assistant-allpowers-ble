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
    preflight: dict[str, Any] = {
        "step_id": "settings_preflight",
        "target": key,
        "requested_value": target,
        "current_value": (
            state.get("state") if state.get("available") is True else "unavailable"
        ),
        "first_transition": target,
        "restoration_value": (
            state.get("state") if state.get("available") is True else "unavailable"
        ),
        "outcome": "inconclusive",
    }
    steps.append(preflight)
    checkpoint()
    reasons: list[str] = []
    if caps.get("verified") is not True:
        reasons.append("installed integration identity is not verified")
    if caps.get("identity_conflicted") is not False:
        reasons.append("device identity is conflicting or unavailable")
    if caps.get("connected") is not True:
        reasons.append("device is not connected")
    if caps.get("pending_transaction") is not False:
        reasons.append("a BLE transaction is pending")
    if caps.get("status_fresh") is not True or caps.get("settings_fresh") is not True:
        reasons.append(
            "settings/status telemetry is missing or stale; refresh telemetry"
        )
    if type(caps.get("session_generation")) is not int:
        reasons.append("same-session generation evidence is unavailable")
    if state.get("available") is not True:
        reasons.append(
            "control is unavailable; verify the entity is enabled and settings are fresh"
        )
    capability_ok = feature_write_authorized(caps, key, "write_settings_controls")
    if caps.get("schema_version") == 2:
        feature_facts = caps.get("features")
        feature_reported = isinstance(feature_facts, dict) and isinstance(
            feature_facts.get(key), dict
        )
    else:
        reported_capabilities = caps.get("capabilities")
        feature_reported = (
            isinstance(reported_capabilities, dict)
            and "write_settings_controls" in reported_capabilities
        )
    unsupported_capability = feature_reported and not capability_ok
    if not capability_ok:
        reasons.append("settings write capability is unsupported or not authorized")
    if reasons:
        preflight.update({"reason": "; ".join(reasons)})
        if unsupported_capability:
            preflight["outcome"] = "not_applicable"
        checkpoint()
        print(
            f"Preflight {key}: current={preflight['current_value']}, "
            f"requested={target}, first transition={preflight['first_transition']}, "
            f"restoration={preflight['restoration_value']}"
        )
        print(f"Settings preflight blocked: {preflight['reason']}")
        return 2
    initial = state["state"]
    first_target = target
    if initial == target:
        if key == "eco_mode":
            options = state.get("options")
            reverse = "off" if target == "on" else "on"
            if not isinstance(options, list) or reverse not in options:
                preflight.update(
                    {
                        "current_value": initial,
                        "first_transition": None,
                        "restoration_value": initial,
                        "outcome": "skipped",
                        "reason": f"ECO is already {initial}; safe reversal requires the supported option {reverse}",
                    }
                )
                checkpoint()
                print(f"Settings preflight skipped: {preflight['reason']}")
                return 2
            first_target = reverse
        else:
            preflight.update(
                {
                    "current_value": initial,
                    "first_transition": None,
                    "restoration_value": initial,
                    "outcome": "skipped",
                    "reason": f"{key} is already {initial}; no safe alternative is declared; change it to a supported value other than {target} and rerun",
                }
            )
            checkpoint()
            print(f"Settings preflight skipped: {preflight['reason']}")
            return 2
    if not services.control_value_available(key, state, first_target):
        preflight.update(
            {
                "current_value": initial,
                "first_transition": first_target,
                "restoration_value": initial,
                "outcome": "not_applicable",
                "reason": f"requested transition {first_target} is not among the control's supported values",
            }
        )
        checkpoint()
        print(f"Settings preflight blocked: {preflight['reason']}")
        return 2
    preflight.update(
        {
            "current_value": initial,
            "first_transition": first_target,
            "restoration_value": initial,
            "outcome": "skipped",
            "reason": "read-only preflight complete; authorization required before each write",
        }
    )
    checkpoint()
    print(f"Setting scenario: {scenario.title}")
    print(
        f"Preflight {key}: current={initial}, requested={target}, "
        f"first transition={first_target}, restoration={initial}"
    )
    try:
        authorized = services.prompt(
            f"Type 1 to authorize the single settings change {initial} -> {first_target}: "
        ).strip()
    except EOFError, KeyboardInterrupt:
        authorized = ""
    if authorized != "1":
        preflight["authorization"] = "declined"
        preflight["reason"] = (
            "first settings write authorization declined; no write attempted"
        )
        checkpoint()
        print(preflight["reason"])
        return 2
    preflight["authorization"] = "granted"
    checkpoint()
    change = _execute_settings_write(
        api,
        args,
        device,
        output_dir,
        run_id,
        scenario,
        key,
        entity_id,
        first_target,
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
            elif can_restore and cleanup_state.get("state") == first_target:
                change["cleanup_status"] = "awaiting_restoration_authorization"
                checkpoint_step(change)
                change["cleanup_authorization"] = "pending"
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
                    change["cleanup_authorization"] = "granted"
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
                        first_target,
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
                    change["cleanup_authorization"] = "declined"
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
    preflight["restoration_authorization"] = "pending"
    checkpoint()
    try:
        restore_authorized = services.prompt(
            "Type 1 to authorize restoring the original setting: "
        ).strip()
    except EOFError, KeyboardInterrupt:
        restore_authorized = ""
    if restore_authorized != "1":
        preflight["restoration_authorization"] = "declined"
        checkpoint()
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
    preflight["restoration_authorization"] = "granted"
    checkpoint()
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
        first_target,
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
