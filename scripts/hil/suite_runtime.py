"""Injected shared capabilities used by the guided HIL suite executors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class SuiteServices:
    """Keep suite modules independent from CLI setup and each other."""

    device_fingerprint: Callable[..., str]
    has_verified_telemetry: Callable[..., bool]
    save_private_trace: Callable[..., None]
    validate_trace_result: Callable[..., None]
    control_value_available: Callable[..., bool]
    write_scenario_progress: Callable[..., None]
    load_scenarios: Callable[..., Any]
    correlate_transaction: Callable[..., Any]
    route_matches_request: Callable[..., bool]
    verdict: Callable[..., str]
    recovery_verdict: Callable[..., str]
    soak_verdict: Callable[..., str]
    short_stability_verdict: Callable[..., str]
    read_only_sample: Callable[..., dict[str, Any]]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    prompt: Callable[[str], str]
    datetime: Any
