"""Tests for bundled scenario declarations and fail-closed planning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.hil.runner import plan, verdict
from scripts.hil.scenarios import _validate, load_scenarios


def test_bundled_read_only_scenario_is_valid_and_bounded() -> None:
    scenarios = load_scenarios()
    assert [scenario.id for scenario in scenarios] == ["read_only_snapshot"]
    assert all(1 <= step["timeout_seconds"] <= 86400 for step in scenarios[0].steps)


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
