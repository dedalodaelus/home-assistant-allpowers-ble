"""Capability-aware guided HIL tier-plan tests."""

from __future__ import annotations

from scripts.hil.guided import build_tier_plan


def _capabilities(*, light: str = "present") -> dict:
    return {
        "model": "R600",
        "profile": "r600-hw-0.3",
        "hardware_revision": "0.3",
        "firmware_revision": "1.1",
        "features": {
            "telemetry": {
                "physical_presence": "present",
                "production_authorized": False,
                "candidate_hil_eligible": False,
                "runtime_ready": True,
            },
            **{
                feature: {
                    "physical_presence": light
                    if feature == "light_output"
                    else "present",
                    "production_authorized": True,
                    "candidate_hil_eligible": False,
                    "runtime_ready": True,
                }
                for feature in (
                    "ac_output",
                    "dc_output",
                    "light_output",
                    "eco_mode",
                    "work_mode",
                    "eco_timeout",
                    "car_charger",
                )
            },
        },
    }


def _controls(*, missing: str | None = None) -> dict:
    keys = {
        "ac_output": "ac_output",
        "dc_output": "dc_output",
        "light_output": "light",
        "eco_mode": "eco_mode",
        "work_mode": "work_mode",
        "eco_timeout": "eco_timeout",
        "car_charger": "car_charger",
    }
    return {
        control: {
            "available": control != missing,
            "state": "off"
            if control in {"ac_output", "dc_output", "light", "eco_mode", "car_charger"}
            else "normal",
            "eligible": control != missing,
        }
        for control in keys.values()
    }


def test_silver_plan_marks_confirmed_absent_light_not_applicable() -> None:
    plan = build_tier_plan(
        "silver",
        _capabilities(light="absent"),
        _controls(),
        observed_route="active_proxy",
    )

    steps = {step["id"]: step for step in plan["steps"]}
    assert steps["ac_output_cycle"]["eligibility"] == "eligible"
    assert steps["dc_output_cycle"]["eligibility"] == "eligible"
    assert steps["light_output_cycle"]["eligibility"] == "not_applicable"
    assert "physical absence" in steps["light_output_cycle"]["reasons"][0]
    assert plan["attained_tier"] is None


def test_silver_plan_blocks_unknown_controls_and_unverified_route() -> None:
    plan = build_tier_plan(
        "silver",
        _capabilities(),
        _controls(missing="dc_output"),
        observed_route=None,
    )

    steps = {step["id"]: step for step in plan["steps"]}
    assert steps["ac_output_cycle"]["eligibility"] == "blocked"
    assert (
        "connected GATT route has not been verified"
        in steps["ac_output_cycle"]["reasons"]
    )
    assert "control state is unavailable" in steps["dc_output_cycle"]["reasons"]
    assert steps["light_output_cycle"]["eligibility"] == "blocked"


def test_candidate_eligibility_does_not_replace_backend_grant() -> None:
    capabilities = _capabilities()
    capabilities["features"]["ac_output"]["production_authorized"] = False
    capabilities["features"]["ac_output"]["candidate_hil_eligible"] = True

    plan = build_tier_plan(
        "silver", capabilities, _controls(), observed_route="local_adapter"
    )

    ac_step = next(step for step in plan["steps"] if step["id"] == "ac_output_cycle")
    assert ac_step["eligibility"] == "blocked"
    assert any(
        "single-use backend HIL grant" in reason for reason in ac_step["reasons"]
    )


def test_gold_includes_settings_and_guarded_recovery_but_no_soak() -> None:
    plan = build_tier_plan(
        "gold", _capabilities(), _controls(), observed_route="local_adapter"
    )

    ids = {step["id"] for step in plan["steps"]}
    assert "eco_mode_reversal" in ids
    assert "guarded_recovery" in ids
    assert "soak_24h" not in ids
