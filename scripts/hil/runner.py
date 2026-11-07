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
                PlannedScenario(
                    item,
                    False,
                    "requires live capability, fresh-state and route checks",
                )
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
    telemetry_contradictory: bool = False,
) -> str:
    """Fail-closed evidence verdict; a service call or physical yes is insufficient."""
    if unsupported:
        return "not_applicable"
    if telemetry_contradictory or physical_outcome == "no":
        return "fail"
    if interrupted or not telemetry_matches or not route_verified or not trace_complete:
        return "inconclusive"
    if physical_outcome != "yes" or not action_submitted or not transaction_confirmed:
        return "inconclusive"
    # The device has no transaction ID; callers must preserve this limitation.
    return "pass"


def correlate_transaction(
    trace: dict[str, Any], *, expected_generation: int, kind: str
) -> dict[str, Any] | None:
    """Find one submitted transaction confirmed by same-session telemetry."""
    events = trace.get("events")
    if not isinstance(events, list):
        return None
    submitted = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("event") == "transaction_submitted"
        and event.get("transaction_kind") == kind
        and event.get("session_generation") == expected_generation
        and isinstance(event.get("transaction_id"), str)
        and isinstance(event.get("seq"), int)
    ]
    for event in reversed(submitted):
        transaction_id = event["transaction_id"]
        confirmation = next(
            (
                item
                for item in events
                if isinstance(item, dict)
                and item.get("event") == "transaction_confirmed"
                and item.get("transaction_id") == transaction_id
                and item.get("session_generation") == expected_generation
                and item.get("reason") == "matching_validated_telemetry"
                and isinstance(item.get("seq"), int)
                and item["seq"] > event["seq"]
            ),
            None,
        )
        if confirmation is not None:
            return {
                "transaction_id": transaction_id,
                "submitted_seq": event["seq"],
                "confirmed_seq": confirmation["seq"],
                "session_generation": expected_generation,
                "kind": kind,
            }
    return None


def recovery_verdict(
    *,
    telemetry_matches: bool,
    physical_outcome: str | None,
    route_verified: bool,
    trace_complete: bool,
) -> str:
    """Evaluate read-only recovery evidence without requiring a write transaction."""
    if physical_outcome == "no":
        return "fail"
    if not telemetry_matches or physical_outcome != "yes":
        return "inconclusive"
    if not route_verified or not trace_complete:
        return "inconclusive"
    return "pass"


def soak_verdict(
    *,
    measured_seconds: float,
    sample_count: int,
    minimum_samples: int,
    maximum_gap_seconds: float,
    allowed_gap_seconds: int,
    trace_count: int,
    minimum_traces: int,
    dropped_traces: int,
    data_limit_hit: bool = False,
    interrupted: bool = False,
) -> str:
    """Pass only a full uninterrupted, adequately sampled 24-hour segment."""
    if data_limit_hit or interrupted:
        return "inconclusive"
    if maximum_gap_seconds > allowed_gap_seconds or dropped_traces:
        return "inconclusive"
    if (
        measured_seconds < 86400
        or sample_count < minimum_samples
        or trace_count < minimum_traces
    ):
        return "inconclusive"
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
