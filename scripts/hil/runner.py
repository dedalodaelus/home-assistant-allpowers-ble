"""Pure planning and verdict helpers for the guided HIL scenario runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .scenarios import Scenario, load_scenarios


@dataclass(frozen=True)
class PlannedScenario:
    """Scenario and whether the current invocation may execute it."""

    scenario: Scenario
    eligible: bool
    reason: str


def plan(suite: str, *, non_interactive: bool = False) -> tuple[PlannedScenario, ...]:
    """Build a side-effect-free plan; writes are never eligible yet."""
    if suite == "all":
        selected = [item for item in load_scenarios() if item.suite == "read-only"]
    else:
        selected = [item for item in load_scenarios() if item.suite == suite]
    result = []
    for item in selected:
        if item.mode == "read_only":
            result.append(PlannedScenario(item, True, "read-only observations"))
        elif item.mode == "interactive_write" and non_interactive:
            result.append(
                PlannedScenario(item, False, "requires interactive authorization")
            )
        else:
            result.append(
                PlannedScenario(item, False, "scenario execution is not implemented")
            )
    return tuple(result)


def verdict(
    *,
    unsupported: bool = False,
    interrupted: bool = False,
    telemetry_matches: bool = False,
    physical_outcome: str | None = None,
    route_verified: bool = False,
    trace_complete: bool = False,
    action_submitted: bool = False,
    transaction_confirmed: bool = False,
) -> str:
    """Fail-closed evidence verdict; a service call or physical yes is insufficient."""
    if unsupported:
        return "not_applicable"
    if interrupted or not telemetry_matches or not route_verified or not trace_complete:
        return "inconclusive"
    if physical_outcome == "no":
        return "fail"
    if physical_outcome != "yes" or not action_submitted or not transaction_confirmed:
        return "inconclusive"
    # The device has no transaction ID; callers must preserve this limitation.
    return "pass"


def plan_as_dict(item: PlannedScenario) -> dict[str, Any]:
    """Return a safe, concise plan entry suitable for terminal output."""
    return {
        "id": item.scenario.id,
        "suite": item.scenario.suite,
        "title": item.scenario.title,
        "eligible": item.eligible,
        "reason": item.reason,
        "requested_effects": sorted({step["action"] for step in item.scenario.steps}),
    }
