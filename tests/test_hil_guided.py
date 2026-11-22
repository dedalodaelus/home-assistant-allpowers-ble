"""Capability-aware guided HIL tier-plan tests."""

from __future__ import annotations

from typing import Any

from custom_components.allpowers_ble.model_support import MODEL_FEATURE_PROFILES
from custom_components.allpowers_ble.model_support import (
    ModelFeature,
    identify_model,
    model_feature_capability,
)
from scripts.hil.guided import build_tier_plan


def _capabilities(profile_id: str) -> dict[str, Any]:
    profile = MODEL_FEATURE_PROFILES[profile_id]
    features = {
        feature.value: {
            "physical_presence": declaration.physical_presence.value,
            "production_authorized": declaration.production_authorized,
            "candidate_hil_eligible": declaration.hil_candidate_eligible,
            "runtime_ready": True,
        }
        for feature, declaration in profile.features.items()
    }
    return {
        "model": profile.model,
        "profile": profile.profile,
        "hardware_revision": profile.hardware_revision,
        "firmware_revision": profile.firmware_evidence,
        "verified": profile_id == "r600-hw-0.3",
        "identity_conflicted": False,
        "connected": True,
        "status_fresh": True,
        "settings_fresh": True,
        "pending_transaction": False,
        "capabilities": {
            "read_telemetry": True,
            "write_output_controls": profile_id == "r600-hw-0.3",
            "write_settings_controls": profile_id == "r600-hw-0.3",
        },
        "features": features,
    }


def _controls(profile_id: str) -> dict[str, Any]:
    profile = MODEL_FEATURE_PROFILES[profile_id]
    names = {
        "ac_output": "ac_output",
        "dc_output": "dc_output",
        "light_output": "light",
        "eco_mode": "eco_mode",
        "work_mode": "work_mode",
        "eco_timeout": "eco_timeout",
        "car_charger": "car_charger",
    }
    result = {}
    for feature, control in names.items():
        declaration = profile.features[
            next(key for key in profile.features if key.value == feature)
        ]
        absent = declaration.physical_presence.value == "absent"
        result[control] = {
            "available": not absent,
            "state": "off",
            "eligible": declaration.production_authorized,
        }
    return result


def _steps(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["id"]: step for step in plan["steps"]}


def test_bronze_uses_read_capability_for_real_profiles() -> None:
    for profile_id in ("r600-hw-0.3", "ap-r1500-lite-hw-0.1-evidence"):
        plan = build_tier_plan(
            "bronze",
            _capabilities(profile_id),
            _controls(profile_id),
            observed_route=None,
        )

        steps = _steps(plan)
        assert steps["read_only_probe"]["eligibility"] == "eligible"
        assert steps["physical_plausibility_observation"]["eligibility"] == "eligible"
        assert all(step["action"] != "write" for step in steps.values())


def test_r1500_lite_confirmed_absent_light_stays_not_applicable() -> None:
    profile_id = "ap-r1500-lite-hw-0.1-evidence"
    plan = build_tier_plan(
        "silver",
        _capabilities(profile_id),
        _controls(profile_id),
        observed_route=None,
    )

    light = _steps(plan)["light_output_cycle"]
    assert light["eligibility"] == "not_applicable"
    assert light["action"] == "not_applicable"
    assert light["original_state"] is None
    assert light["target_state"] is None
    assert light["reasons"] == ["runtime profile substantiates physical absence"]


def test_r600_unknown_outputs_and_settings_remain_blocked() -> None:
    plan = build_tier_plan(
        "gold",
        _capabilities("r600-hw-0.3"),
        _controls("r600-hw-0.3"),
        observed_route="active_proxy",
    )

    steps = _steps(plan)
    assert steps["ac_output_cycle"]["eligibility"] == "blocked"
    assert (
        "physical presence is unknown or unreviewed"
        in steps["ac_output_cycle"]["reasons"]
    )
    assert steps["eco_mode_reversal"]["eligibility"] == "blocked"
    assert (
        "physical presence is unknown or unreviewed"
        in steps["eco_mode_reversal"]["reasons"]
    )
    assert steps["guarded_recovery"]["eligibility"] == "eligible"
    assert "soak_24h" not in steps


def test_gold_only_plans_car_charger_exclusion_with_verified_safe_disable() -> None:
    capabilities = _capabilities("r600-hw-0.3")
    capabilities["capabilities"]["car_charger_enabled"] = False
    controls = _controls("r600-hw-0.3")
    controls["car_charger"]["available"] = False

    plan = build_tier_plan(
        "gold",
        capabilities,
        controls,
        observed_route="active_proxy",
    )

    exclusion = _steps(plan)["car_charger_reversal"]
    assert exclusion["physical_presence"] == "unknown"
    assert exclusion["action"] == "exclude"
    assert exclusion["eligibility"] == "review_required"
    assert exclusion["safe_disable_proof"] == {
        "runtime_option_enabled": False,
        "normal_control_available": False,
    }


def test_gold_does_not_exclude_enabled_or_available_car_charger() -> None:
    capabilities = _capabilities("r600-hw-0.3")
    capabilities["capabilities"]["car_charger_enabled"] = True
    controls = _controls("r600-hw-0.3")
    controls["car_charger"]["available"] = False

    plan = build_tier_plan(
        "gold",
        capabilities,
        controls,
        observed_route="active_proxy",
    )
    assert _steps(plan)["car_charger_reversal"]["action"] == "write"
    assert _steps(plan)["car_charger_reversal"]["eligibility"] == "blocked"


def test_verified_r600_inventory_preflight_unlocks_acquisition_without_qualification() -> (
    None
):
    support = identify_model("R600", hardware_version="0.3", raw_hardware_version=0x03)
    features = {
        feature.value: {
            "physical_presence": model_feature_capability(
                support, feature
            ).physical_presence.value,
            "production_authorized": model_feature_capability(
                support, feature
            ).production_authorized,
            "candidate_hil_eligible": model_feature_capability(
                support, feature
            ).hil_candidate_eligible,
            "runtime_ready": True,
        }
        for feature in ModelFeature
    }
    capabilities = _capabilities("r600-hw-0.3")
    capabilities["features"] = features
    controls = _controls("r600-hw-0.3")
    from scripts.hil.cli import _collect_physical_inventory_preflight

    prompts: list[str] = []
    inventory = _collect_physical_inventory_preflight(
        capabilities,
        controls,
        prompt=lambda message: prompts.append(message) or "PRESENT",
    )
    assert len(prompts) == 7
    assert all("R600 0.3" in prompt for prompt in prompts)

    blocked = build_tier_plan(
        "gold", capabilities, controls, observed_route="local_adapter"
    )
    assert _steps(blocked)["ac_output_cycle"]["eligibility"] == "blocked"

    plan = build_tier_plan(
        "gold",
        capabilities,
        controls,
        observed_route="local_adapter",
        inventory_preflight=inventory,
    )
    steps = _steps(plan)
    for step_id in (
        "ac_output_cycle",
        "dc_output_cycle",
        "light_output_cycle",
        "eco_mode_reversal",
        "work_mode_reversal",
        "eco_timeout_reversal",
        "car_charger_reversal",
    ):
        assert steps[step_id]["eligibility"] == "eligible"
        assert steps[step_id]["inventory_preflight"] == "operator_inspected_present"
        assert steps[step_id]["physical_presence"] == "unknown"
        assert steps[step_id]["outcome"] == "pending"
    assert plan["physical_inventory_preflight"]["source"] == (
        "operator_physical_inspection"
    )


def test_inventory_preflight_cannot_override_absent_or_unauthorized_feature() -> None:
    profile_id = "ap-r1500-lite-hw-0.1-evidence"
    capabilities = _capabilities(profile_id)
    plan = build_tier_plan(
        "silver",
        capabilities,
        _controls(profile_id),
        observed_route="local_adapter",
        inventory_preflight={
            "light_output": {
                "outcome": "present",
                "basis": "operator_physical_inspection",
            },
            "ac_output": {
                "outcome": "present",
                "basis": "operator_physical_inspection",
            },
        },
    )
    steps = _steps(plan)
    assert steps["light_output_cycle"]["eligibility"] == "not_applicable"
    assert steps["ac_output_cycle"]["eligibility"] == "blocked"


def test_candidate_eligibility_does_not_replace_backend_grant() -> None:
    capabilities = _capabilities("r600-hw-0.3")
    feature = capabilities["features"]["ac_output"]
    feature["physical_presence"] = "present"
    feature["production_authorized"] = False
    feature["candidate_hil_eligible"] = True
    controls = _controls("r600-hw-0.3")
    controls["ac_output"]["eligible"] = False

    plan = build_tier_plan(
        "silver", capabilities, controls, observed_route="local_adapter"
    )

    ac_step = _steps(plan)["ac_output_cycle"]
    assert ac_step["eligibility"] == "blocked"
    assert any(
        "single-use backend HIL grant" in reason for reason in ac_step["reasons"]
    )


def test_candidate_fresh_preflight_is_separate_from_production_authorization() -> None:
    capabilities = _capabilities("r600-hw-0.3")
    capabilities.update(
        schema_version=2,
        session_generation=12,
    )
    capabilities["features"]["ac_output"].update(
        physical_presence="present",
        production_authorized=False,
        candidate_hil_eligible=True,
    )
    controls = _controls("r600-hw-0.3")
    controls["ac_output"].update(
        available=True,
        state="off",
        eligible=False,
        candidate_eligible=True,
    )

    plan = build_tier_plan(
        "silver", capabilities, controls, observed_route="local_adapter"
    )

    step = _steps(plan)["ac_output_cycle"]
    assert step["eligibility"] == "blocked"
    assert step["reasons"] == ["candidate needs a fresh, single-use backend HIL grant"]


def test_route_is_required_for_writes_but_not_bronze_read_only_probe() -> None:
    profile_id = "ap-r1500-lite-hw-0.1-evidence"
    capabilities = _capabilities(profile_id)
    capabilities["features"]["ac_output"]["physical_presence"] = "present"
    capabilities["features"]["ac_output"]["production_authorized"] = True
    controls = _controls(profile_id)
    controls["ac_output"]["eligible"] = True

    plan = build_tier_plan("silver", capabilities, controls, observed_route=None)

    steps = _steps(plan)
    assert steps["read_only_probe"]["eligibility"] == "eligible"
    assert steps["ac_output_cycle"]["eligibility"] == "blocked"
    assert (
        "connected GATT route has not been verified"
        in steps["ac_output_cycle"]["reasons"]
    )


def test_gold_minimum_output_remains_blocked_when_all_outputs_are_absent() -> None:
    profile_id = "ap-r1500-lite-hw-0.1-evidence"
    capabilities = _capabilities(profile_id)
    for feature in ("ac_output", "dc_output", "light_output"):
        capabilities["features"][feature]["physical_presence"] = "absent"

    plan = build_tier_plan(
        "gold", capabilities, _controls(profile_id), observed_route="local_adapter"
    )

    requirement = _steps(plan)["minimum_output_requirement"]
    assert requirement["eligibility"] == "blocked"
    assert requirement["reasons"] == [
        "tier requires a basic output PASS; all declared outputs are absent"
    ]


def test_present_authorized_output_requirement_waits_for_route_and_pass() -> None:
    profile_id = "ap-r1500-lite-hw-0.1-evidence"
    capabilities = _capabilities(profile_id)
    capabilities["features"]["ac_output"]["physical_presence"] = "present"
    capabilities["features"]["ac_output"]["production_authorized"] = True
    controls = _controls(profile_id)
    controls["ac_output"]["eligible"] = True

    plan = build_tier_plan("silver", capabilities, controls, observed_route=None)

    steps = _steps(plan)
    assert steps["ac_output_cycle"]["eligibility"] == "blocked"
    assert (
        "connected GATT route has not been verified"
        in steps["ac_output_cycle"]["reasons"]
    )
    requirement = steps["minimum_output_requirement"]
    assert requirement["eligibility"] == "eligible"
    assert requirement["outcome"] == "pending"
    assert requirement["reasons"] == [
        "minimum output requirement awaits a reviewed output PASS"
    ]
