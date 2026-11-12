"""Capability-aware Bronze/Silver/Gold plans for the existing HIL runner."""

from __future__ import annotations

from typing import Any


_OUTPUTS = ("ac_output", "dc_output", "light_output")
_SETTINGS = ("eco_mode", "work_mode", "eco_timeout", "car_charger")
_CONTROL = {"light_output": "light"}


def build_tier_plan(
    tier: str,
    capabilities: dict[str, Any] | None,
    controls: dict[str, Any] | None,
    *,
    observed_route: str | None,
    build_provenance: dict[str, Any] | None = None,
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
    controls = controls if isinstance(controls, dict) else {}
    model = snapshot.get("model")
    if not isinstance(model, str):
        model = "unknown"
    steps: list[dict[str, Any]] = []

    def add(step_id: str, feature: str, action: str, evidence: list[str]) -> None:
        facts = feature_map.get(feature, {})
        facts = facts if isinstance(facts, dict) else {}
        presence = facts.get("physical_presence", "unknown")
        control_key = _CONTROL.get(feature, feature)
        control = controls.get(control_key, {})
        control = control if isinstance(control, dict) else {}
        reasons: list[str] = []
        status = "eligible"
        if presence == "absent":
            status = "not_applicable"
            reasons.append("runtime profile substantiates physical absence")
        elif presence != "present":
            status = "blocked"
            reasons.append("physical presence is unknown or unreviewed")
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
            if control.get("eligible") is not True:
                status = "blocked"
                reasons.append("fresh same-session capability preflight is not ready")
        if observed_route not in {"local_adapter", "active_proxy"}:
            if status == "eligible":
                status = "blocked"
            reasons.append("connected GATT route has not been verified")
        original = control.get("state")
        steps.append(
            {
                "id": step_id,
                "feature": feature,
                "action": action,
                "original_state": original,
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
            }
        )

    add(
        "read_only_probe",
        "telemetry",
        "read_only",
        ["fresh telemetry", "physical plausibility"],
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
    present_outputs = [
        step
        for step in steps
        if step["feature"] in _OUTPUTS and step["eligibility"] == "eligible"
    ]
    if tier in {"silver", "gold"} and not present_outputs:
        steps.append(
            {
                "id": "minimum_output_requirement",
                "feature": "output",
                "action": "requirement",
                "original_state": None,
                "target_state": None,
                "eligibility": "blocked",
                "outcome": "pending",
                "reasons": [
                    "at least one physically present, authorized basic output is required"
                ],
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
        "steps": steps,
    }
