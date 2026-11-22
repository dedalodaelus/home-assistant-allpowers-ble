"""Capability-aware Bronze/Silver/Gold plans for the existing HIL runner."""

from __future__ import annotations

from typing import Any


_OUTPUTS = ("ac_output", "dc_output", "light_output")
_SETTINGS = ("eco_mode", "work_mode", "eco_timeout", "car_charger")
_CONTROL = {"light_output": "light"}


def capability_identity_matches_session(
    capabilities: Any,
    session: Any,
    *,
    selected_device: Any | None = None,
) -> bool:
    """Require capability, selected device, saved session and plan to share scope."""
    if not isinstance(capabilities, dict) or not isinstance(session, dict):
        return False
    session_device = session.get("device")
    plan = session.get("test_plan")
    plan_device = plan.get("device") if isinstance(plan, dict) else None
    if not isinstance(session_device, dict) or not isinstance(plan_device, dict):
        return False
    values = {
        "model": ("model", "model"),
        "hardware_revision": ("hardware_version", "hardware_revision"),
        "firmware_revision": ("firmware_version", "firmware_revision"),
    }
    for capability_key, (session_key, plan_key) in values.items():
        capability_value = capabilities.get(capability_key)
        session_record = session_device.get(session_key)
        if (
            not isinstance(capability_value, str)
            or not isinstance(session_record, dict)
            or session_record.get("value") != capability_value
            or session_record.get("verified") is not True
            or session_record.get("source") != "ha_device_registry"
            or plan_device.get(plan_key) != capability_value
        ):
            return False
        if selected_device is not None:
            selected_value = {
                "model": getattr(selected_device, "model", None),
                "hardware_revision": getattr(selected_device, "hardware", None),
                "firmware_revision": getattr(selected_device, "firmware", None),
            }[capability_key]
            if selected_value != capability_value:
                return False
    return True


def build_tier_plan(
    tier: str,
    capabilities: dict[str, Any] | None,
    controls: dict[str, Any] | None,
    *,
    observed_route: str | None,
    build_provenance: dict[str, Any] | None = None,
    inventory_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ordered, fail-closed operator plan from one runtime snapshot.

    This function is deliberately side-effect free. A requested route is not
    accepted as observed route evidence, and candidate eligibility is never a
    substitute for the per-action backend grant.
    """
    if tier not in {"bronze", "silver", "gold"}:
        raise ValueError("tier must be bronze, silver, or gold")
    snapshot = capabilities if isinstance(capabilities, dict) else {}
    feature_map = snapshot.get("features", {})
    feature_map = feature_map if isinstance(feature_map, dict) else {}
    capability_flags = snapshot.get("capabilities", {})
    capability_flags = capability_flags if isinstance(capability_flags, dict) else {}
    controls = controls if isinstance(controls, dict) else {}
    inventory_preflight = (
        inventory_preflight if isinstance(inventory_preflight, dict) else {}
    )
    model = snapshot.get("model")
    if not isinstance(model, str):
        model = "unknown"
    steps: list[dict[str, Any]] = []

    def add(step_id: str, feature: str, action: str, evidence: list[str]) -> None:
        facts = feature_map.get(feature, {})
        facts = facts if isinstance(facts, dict) else {}
        presence = facts.get("physical_presence", "unknown")
        inspection = inventory_preflight.get(feature)
        inspected_present = bool(
            presence == "unknown"
            and facts.get("production_authorized") is True
            and snapshot.get("verified") is True
            and snapshot.get("identity_conflicted") is False
            and isinstance(snapshot.get("model"), str)
            and snapshot.get("model") != "unknown"
            and isinstance(snapshot.get("hardware_revision"), str)
            and isinstance(inspection, dict)
            and inspection.get("outcome") == "present"
            and inspection.get("basis") == "operator_physical_inspection"
        )
        eligibility_presence = "present" if inspected_present else presence
        control_key = _CONTROL.get(feature, feature)
        control = controls.get(control_key, {})
        control = control if isinstance(control, dict) else {}
        reasons: list[str] = []
        absent = presence == "absent"
        status = "not_applicable" if absent else "eligible"
        if absent:
            reasons.append("runtime profile substantiates physical absence")
            action = "not_applicable"
        if presence == "absent":
            pass
        elif feature == "telemetry":
            if capability_flags.get("read_telemetry") is not True:
                status = "blocked"
                reasons.append("read-only telemetry capability is unavailable")
            if (
                snapshot.get("identity_conflicted") is not False
                or snapshot.get("connected") is not True
                or snapshot.get("status_fresh") is not True
                or snapshot.get("pending_transaction") is not False
                or facts.get("runtime_ready") is False
            ):
                status = "blocked"
                reasons.append("fresh connected telemetry preflight is not ready")
        elif eligibility_presence != "present":
            status = "blocked"
            reasons.append("physical presence is unknown or unreviewed")
        elif inspected_present:
            reasons.append(
                "operator physical-inventory preflight permits acquisition only; independent review is still required"
            )
        if action == "write":
            if facts.get("production_authorized") is not True:
                status = "blocked"
                if facts.get("candidate_hil_eligible") is True:
                    reasons.append(
                        "candidate needs a fresh, single-use backend HIL grant"
                    )
                else:
                    reasons.append(
                        "no production write authorization or reviewed HIL candidate"
                    )
            if control.get("available") is not True:
                status = "blocked"
                reasons.append("control state is unavailable")
            candidate_preflight = bool(
                facts.get("candidate_hil_eligible") is True
                and control.get("candidate_eligible") is True
            )
            if control.get("eligible") is not True and not candidate_preflight:
                status = "blocked"
                reasons.append("fresh same-session capability preflight is not ready")
        route_required = action == "write" or step_id in {
            "basic_reconnect",
            "guarded_recovery",
        }
        if route_required and observed_route not in {"local_adapter", "active_proxy"}:
            if status == "eligible":
                status = "blocked"
            reasons.append("connected GATT route has not been verified")
        original = control.get("state")
        safely_disabled = bool(
            feature == "car_charger"
            and snapshot.get("schema_version") == 2
            and snapshot.get("verified") is True
            and snapshot.get("identity_conflicted") is False
            and snapshot.get("connected") is True
            and snapshot.get("settings_fresh") is True
            and snapshot.get("pending_transaction") is False
            and type(snapshot.get("session_generation")) is int
            and snapshot["session_generation"] >= 1
            and capability_flags.get("car_charger_enabled") is False
            and control.get("available") is False
            and control.get("availability_verified") is True
            and facts.get("runtime_ready") is True
            and facts.get("physical_presence") == "unknown"
        )
        if safely_disabled:
            status = "review_required"
            action = "exclude"
            reasons.append(
                "optional control is disabled by policy and its normal HA control is unavailable; explicit review is required"
            )
        steps.append(
            {
                "id": step_id,
                "feature": feature,
                "physical_presence": presence
                if presence in {"present", "absent", "unknown"}
                else "unknown",
                "inventory_preflight": (
                    "operator_inspected_present"
                    if inspected_present
                    else "unknown"
                    if presence == "unknown"
                    else "not_required"
                ),
                "action": action,
                "original_state": None if absent else original,
                "target_state": (
                    "temporary alternate value, then restore original"
                    if action == "write"
                    else None
                ),
                "eligibility": status,
                "outcome": "pending",
                "reasons": sorted(set(reasons)),
                "required_evidence": evidence,
                "human_review": "pending",
                "safe_disable_proof": (
                    {
                        "runtime_option_enabled": False,
                        "normal_control_available": False,
                    }
                    if safely_disabled
                    else None
                ),
            }
        )

    add(
        "read_only_probe",
        "telemetry",
        "read_only",
        ["fresh telemetry", "physical plausibility"],
    )
    add(
        "physical_plausibility_observation",
        "telemetry",
        "manual",
        ["separate operator observation", "fresh telemetry context"],
    )
    if tier in {"silver", "gold"}:
        for feature in _OUTPUTS:
            add(
                f"{feature}_cycle",
                feature,
                "write",
                [
                    "per-action consent",
                    "same-session GATT transaction",
                    "readback",
                    "physical observation",
                    "separate restore consent",
                ],
            )
        add(
            "basic_reconnect",
            "telemetry",
            "manual",
            ["fresh telemetry", "verified route", "physical confirmation"],
        )
    if tier == "gold":
        for feature in _SETTINGS:
            add(
                f"{feature}_reversal",
                feature,
                "write",
                [
                    "per-action consent",
                    "same-session GATT transaction",
                    "readback",
                    "restore consent",
                ],
            )
        add(
            "guarded_recovery",
            "telemetry",
            "manual",
            ["checkpoint", "verified route", "physical confirmation"],
        )
    if tier in {"silver", "gold"}:
        output_facts = [
            feature_map.get(feature)
            if isinstance(feature_map.get(feature), dict)
            else {}
            for feature in _OUTPUTS
        ]
        output_presence = [
            facts.get("physical_presence", "unknown") for facts in output_facts
        ]
        has_qualifying_output = any(
            facts.get("physical_presence") == "present"
            and (
                facts.get("production_authorized") is True
                or facts.get("candidate_hil_eligible") is True
            )
            for facts in output_facts
        )
        all_absent = all(value == "absent" for value in output_presence)
        requirement_reason = (
            "minimum output requirement awaits a reviewed output PASS"
            if has_qualifying_output
            else (
                "tier requires a basic output PASS; all declared outputs are absent"
                if all_absent
                else "no present, production-authorized or reviewed candidate output is available"
            )
        )
        steps.append(
            {
                "id": "minimum_output_requirement",
                "feature": "output",
                "action": "requirement",
                "original_state": None,
                "target_state": None,
                "eligibility": "eligible" if has_qualifying_output else "blocked",
                "outcome": "pending",
                "reasons": [requirement_reason],
                "required_evidence": [],
                "human_review": "pending",
            }
        )
    return {
        "schema_version": 1,
        "requested_tier": tier,
        "attained_tier": None,
        "review_state": "pending",
        "device": {
            "model": model,
            "hardware_revision": snapshot.get("hardware_revision"),
            "firmware_revision": snapshot.get("firmware_revision"),
            "profile": snapshot.get("profile"),
            "verified_route": observed_route,
            "build_provenance": build_provenance or {},
        },
        "operator_actions_estimate": sum(
            2 if step["action"] == "write" else 1
            for step in steps
            if step["eligibility"] != "not_applicable"
        ),
        "restoration_plan": "Restore each changed control to its captured original state with separate consent; stop after disconnect or ambiguous outcome.",
        "physical_inventory_preflight": {
            "source": "operator_physical_inspection",
            "features": {
                feature: {"outcome": "present", "basis": "operator_physical_inspection"}
                for feature in sorted(
                    {
                        step["feature"]
                        for step in steps
                        if step.get("inventory_preflight")
                        == "operator_inspected_present"
                    }
                )
            },
        },
        "steps": steps,
    }
