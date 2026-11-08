"""Deterministic offline transaction-safety rehearsal for the HIL CLI."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import secrets
from typing import Any

from .api import HAAPIError
from .storage import write_run, write_scenario_progress


def _confirmation_matches(
    *, expected_generation: int, observed_generation: int, fresh: bool, value: str
) -> bool:
    """Accept only fresh expected telemetry from the submitting session."""
    return (
        observed_generation == expected_generation and fresh is True and value == "on"
    )


def _may_retry(*, confirmed: bool, pending: bool, connected: bool) -> bool:
    """Require confirmation and an idle live session before a later write."""
    return confirmed and not pending and connected


def _simulate_transactions() -> list[dict[str, Any]]:
    """Exercise the declared safety invariants without opening an HA connection."""
    ac = True
    dc = False
    light = True
    generation = 7
    pending: str | None = None
    frames: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    # First command enters pending state. A conflicting write is rejected and
    # must not produce a second GATT frame.
    pending = "ac-on"
    frames.append({"command": "output_control", "bitmap": 0b101})
    before = len(frames)
    rejected = pending is not None
    if not rejected:
        frames.append({"command": "output_control", "bitmap": 0b111})
    rows.append(
        {
            "step_id": "reject_concurrent_output",
            "outcome": "pass" if rejected and len(frames) == before else "fail",
            "expected": "zero additional output_control frames while a transaction is pending",
            "observed_frames": len(frames) - before,
        }
    )

    # Only matching fresh telemetry in the originating generation releases
    # the pending transaction. The subsequent command preserves unrelated bits.
    matching_confirmation = pending == "ac-on" and _confirmation_matches(
        expected_generation=generation,
        observed_generation=7,
        fresh=True,
        value="on",
    )
    if matching_confirmation:
        ac = True
        pending = None
    retry_allowed = _may_retry(
        confirmed=matching_confirmation, pending=pending is not None, connected=True
    )
    if retry_allowed:
        dc = True
        bitmap = int(ac) | (int(dc) << 1) | (int(light) << 2)
        frames.append({"command": "output_control", "bitmap": bitmap})
        rows.append(
            {
                "step_id": "confirm_then_retry_with_combined_state",
                "outcome": "pass" if bitmap == 0b111 else "fail",
                "expected": "fresh same-generation confirmation followed by combined bitmap 0b111",
                "observed_bitmap": bitmap,
            }
        )

    # Keepalives and settings observations are read-only in this rehearsal and
    # may not replay a pending write.
    pending = "settings-change"
    before = len(frames)
    for _ in ("settings_refresh", "manual_keepalive", "automatic_keepalive"):
        if pending is None:
            frames.append({"command": "output_control", "bitmap": 0})
    rows.append(
        {
            "step_id": "no_replay_during_settings_or_keepalive",
            "outcome": "pass" if len(frames) == before else "fail",
            "expected": "no control frame replayed by settings refresh or keepalive",
            "observed_frames": len(frames) - before,
        }
    )

    # Ambiguous and stale evidence must never confirm or authorize an automatic
    # retry. Each row represents one independently rejected evidence shape.
    cases = (
        ("contradictory_telemetry", False, False, True),
        ("missing_telemetry", False, False, True),
        ("late_telemetry", False, False, True),
        ("duplicate_telemetry", False, False, True),
        ("stale_session", False, False, True),
        ("gatt_error", False, False, True),
        ("timeout", False, True, True),
        ("cancellation", False, False, True),
        ("disconnect_reconnect", False, False, False),
        ("integration_reload", False, False, False),
    )
    for case, confirmed, pending, connected in cases:
        retry_count = int(
            _may_retry(confirmed=confirmed, pending=pending, connected=connected)
        )
        rows.append(
            {
                "step_id": f"ambiguous_{case}",
                "outcome": "pass" if retry_count == 0 else "fail",
                "expected": "inconclusive evidence and no automatic retry",
                "automatic_retries": retry_count,
            }
        )
    return rows


def execute_transaction_safety_simulation(output_root: Path) -> int:
    """Persist a private synthetic report, explicitly separate from HIL evidence."""
    run_id = secrets.token_hex(8)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_dir = write_run(
        output_root,
        f"{timestamp}_{run_id}",
        {"evidence_mode": "simulation", "hardware_qualification": False},
        {"run_id": run_id, "route": "synthetic", "device": "synthetic"},
    )
    steps = _simulate_transactions()
    fingerprint = "0" * 64
    write_scenario_progress(
        output_dir,
        run_id,
        "transaction_safety_simulation",
        fingerprint,
        steps,
    )
    report = {
        "schema_version": 1,
        "evidence_mode": "simulation",
        "hardware_qualification": False,
        "steps": steps,
        "simulation_status": (
            "pass" if all(row["outcome"] == "pass" for row in steps) else "fail"
        ),
        "qualification_status": "not_applicable",
    }
    try:
        path = output_dir / "transaction-safety-simulation.json"
        path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        path.chmod(0o600)
    except OSError as ex:
        raise HAAPIError("Unable to persist private transaction-safety report") from ex
    print(f"Transaction-safety simulation: {report['simulation_status']}")
    print(
        "Synthetic model only; this run provides no hardware or integration qualification"
    )
    print("Private report:", path)
    return 0 if report["simulation_status"] == "pass" else 2
