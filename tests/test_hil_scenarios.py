"""Tests for bundled scenario declarations and fail-closed planning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.hil.runner import plan, recovery_verdict, soak_verdict, verdict
from scripts.hil.scenarios import _validate, load_scenarios


def test_bundled_read_only_scenario_is_valid_and_bounded() -> None:
    scenarios = load_scenarios()
    assert [scenario.id for scenario in scenarios] == [
        "output_ac_cycle",
        "output_car_charger_cycle",
        "output_dc_cycle",
        "output_light_cycle",
        "read_only_snapshot",
        "recovery_reconnect",
        "settings_eco_mode",
        "settings_eco_timeout",
        "settings_work_mode",
        "soak_24h_observation",
    ]
    assert all(
        1 <= step["timeout_seconds"] <= 86400
        for scenario in scenarios
        for step in scenario.steps
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 99},
        {"suite": "unknown"},
        {
            "steps": [
                {
                    "id": "unsafe",
                    "instruction": "write",
                    "timeout_seconds": 0,
                    "action": "switch",
                    "target": "switch.anything",
                    "expected": "on",
                }
            ]
        },
        {
            "steps": [
                {
                    "id": "unsafe",
                    "instruction": "write",
                    "timeout_seconds": 2,
                    "action": "call_service",
                    "target": "script.turn_on",
                    "expected": "on",
                }
            ]
        },
    ],
)
def test_loader_rejects_unsafe_or_unbounded_declarations(
    change: dict[str, object],
) -> None:
    raw = json.loads(Path("scripts/hil/scenarios/read_only.json").read_text())
    raw.update(change)
    with pytest.raises(ValueError):
        _validate(raw)


def test_all_plan_never_implicitly_enables_writes() -> None:
    result = plan("all")
    assert result
    assert all(item.scenario.suite == "read-only" for item in result)
    assert all(item.eligible for item in result)


def test_write_plan_stays_blocked_until_live_preflight() -> None:
    planned = plan("outputs")
    assert [item.scenario.id for item in planned] == [
        "output_ac_cycle",
        "output_car_charger_cycle",
        "output_dc_cycle",
        "output_light_cycle",
    ]
    assert all(item.eligible is False for item in planned)
    assert all("live capability" in item.reason for item in planned)


def test_settings_plan_stays_blocked_until_live_preflight() -> None:
    planned = plan("settings")
    assert [item.scenario.id for item in planned] == [
        "settings_eco_mode",
        "settings_eco_timeout",
        "settings_work_mode",
    ]
    assert all(item.eligible is False for item in planned)
    assert all("live capability" in item.reason for item in planned)


@pytest.mark.parametrize(
    "evidence",
    [
        {"action_submitted": True},
        {"action_submitted": True, "telemetry_matches": True},
        {
            "action_submitted": True,
            "physical_outcome": "yes",
            "telemetry_matches": True,
            "route_verified": True,
            "trace_complete": True,
        },
        {
            "action_submitted": True,
            "telemetry_matches": True,
            "route_verified": True,
            "trace_complete": True,
            "physical_outcome": "unable_to_observe",
        },
    ],
)
def test_incomplete_evidence_never_passes(evidence: dict[str, object]) -> None:
    assert verdict(**evidence) == "inconclusive"


def test_contradictory_physical_result_fails_and_unsupported_is_not_applicable() -> (
    None
):
    assert verdict(unsupported=True) == "not_applicable"
    assert (
        verdict(
            physical_outcome="no",
            telemetry_matches=True,
            route_verified=True,
            trace_complete=True,
            action_submitted=True,
        )
        == "fail"
    )


def test_complete_step_evidence_can_pass_with_explicit_confirmation() -> None:
    assert (
        verdict(
            action_submitted=True,
            transaction_confirmed=True,
            telemetry_matches=True,
            route_verified=True,
            trace_complete=True,
            physical_outcome="yes",
        )
        == "pass"
    )


def test_recovery_needs_new_telemetry_route_trace_and_operator_confirmation() -> None:
    assert (
        recovery_verdict(
            telemetry_matches=True,
            physical_outcome="yes",
            route_verified=True,
            trace_complete=True,
        )
        == "pass"
    )
    assert (
        recovery_verdict(
            telemetry_matches=True,
            physical_outcome="yes",
            route_verified=False,
            trace_complete=True,
        )
        == "inconclusive"
    )
    assert (
        recovery_verdict(
            telemetry_matches=True,
            physical_outcome="no",
            route_verified=True,
            trace_complete=True,
        )
        == "fail"
    )


def test_soak_verdict_requires_full_duration_samples_and_complete_traces() -> None:
    evidence = {
        "measured_seconds": 86400,
        "sample_count": 289,
        "minimum_samples": 289,
        "maximum_gap_seconds": 300,
        "allowed_gap_seconds": 600,
        "trace_count": 24,
        "minimum_traces": 24,
        "dropped_traces": 0,
    }
    assert soak_verdict(**evidence) == "pass"
    assert soak_verdict(**{**evidence, "measured_seconds": 86399}) == "inconclusive"
    assert soak_verdict(**{**evidence, "maximum_gap_seconds": 601}) == "inconclusive"
    assert soak_verdict(**{**evidence, "dropped_traces": 1}) == "inconclusive"
    assert soak_verdict(**{**evidence, "interrupted": True}) == "inconclusive"


def test_transaction_correlation_requires_submitted_then_confirmed_same_session():
    from scripts.hil.runner import correlate_transaction

    write_event = {
        "seq": 2,
        "event": "write_attempt",
        "session_generation": 8,
        "transaction_id": "tx-safe-1",
    }
    trace = {
        "events": [
            write_event,
            {
                "seq": 3,
                "event": "transaction_submitted",
                "session_generation": 8,
                "transaction_id": "tx-safe-1",
                "transaction_kind": "output",
            },
            {
                "seq": 4,
                "event": "transaction_confirmed",
                "session_generation": 9,
                "transaction_id": "tx-safe-1",
                "reason": "matching_validated_telemetry",
            },
        ],
        "frames": [
            {
                **write_event,
                "direction": "tx",
                "command_type": "output_control",
            }
        ],
    }
    assert correlate_transaction(trace, expected_generation=8, kind="output") is None
    trace["events"][2]["session_generation"] = 8
    assert correlate_transaction(trace, expected_generation=8, kind="output") == {
        "transaction_id": "tx-safe-1",
        "submitted_seq": 3,
        "confirmed_seq": 4,
        "session_generation": 8,
        "kind": "output",
        "write_seq": 2,
        "command_type": "output_control",
    }


def test_transaction_correlation_rejects_missing_or_competing_control_frames():
    from scripts.hil.runner import correlate_transaction

    write = {
        "seq": 1,
        "event": "write_attempt",
        "session_generation": 8,
        "transaction_id": "tx-safe-1",
    }
    trace = {
        "events": [
            write,
            {
                "seq": 2,
                "event": "transaction_submitted",
                "session_generation": 8,
                "transaction_id": "tx-safe-1",
                "transaction_kind": "output",
            },
            {
                "seq": 3,
                "event": "transaction_confirmed",
                "session_generation": 8,
                "transaction_id": "tx-safe-1",
                "reason": "matching_validated_telemetry",
            },
        ],
        "frames": [],
    }
    assert correlate_transaction(trace, expected_generation=8, kind="output") is None

    trace["frames"] = [{**write, "direction": "tx", "command_type": "settings_control"}]
    assert correlate_transaction(trace, expected_generation=8, kind="output") is None

    trace["frames"].append(
        {
            "seq": 4,
            "event": "write_attempt",
            "session_generation": 8,
            "transaction_id": "other-transaction",
            "direction": "tx",
            "command_type": "output_control",
        }
    )
    assert correlate_transaction(trace, expected_generation=8, kind="output") is None


@pytest.mark.parametrize(
    ("requested", "observed", "allowed"),
    [
        ("auto", "local_adapter", True),
        ("auto", "active_proxy", True),
        ("auto", "mixed", False),
        ("active_proxy", "active_proxy", True),
        ("active_proxy", "local_adapter", False),
        ("local_adapter", "unverified", False),
    ],
)
def test_route_eligibility_respects_requested_lane(requested, observed, allowed):
    from scripts.hil.runner import route_matches_request

    assert route_matches_request(requested, observed) is allowed
