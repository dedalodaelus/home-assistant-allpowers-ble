"""Capability-aware guided HIL tier-plan tests."""

from __future__ import annotations

from typing import Any

from custom_components.allpowers_ble.model_support import MODEL_FEATURE_PROFILES
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


def test_gold_marks_minimum_outputs_not_applicable_when_all_are_absent() -> None:
    profile_id = "ap-r1500-lite-hw-0.1-evidence"
    capabilities = _capabilities(profile_id)
    for feature in ("ac_output", "dc_output", "light_output"):
        capabilities["features"][feature]["physical_presence"] = "absent"

    plan = build_tier_plan(
        "gold", capabilities, _controls(profile_id), observed_route="local_adapter"
    )

    requirement = _steps(plan)["minimum_output_requirement"]
    assert requirement["eligibility"] == "not_applicable"
    assert requirement["reasons"] == [
        "no basic physical output applies to this profile"
    ]
