"""Guided HIL soak scenario execution."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
from typing import Any

from .api import HAAPIError, HAClient
from .discovery import Device
from .suite_runtime import SuiteServices
from .storage import run_root


def _private_run_size(run_dir: Path) -> int:
    """Measure regular private files without following symlinks."""
    total = 0
    for root, directories, files in os.walk(run_dir, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise HAAPIError("Run output must not contain symlinks")
        for name in directories:
            if (root_path / name).is_symlink():
                raise HAAPIError("Run output must not contain symlinks")
        for name in files:
            path = root_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise HAAPIError("Run output contains a non-regular file")
            total += metadata.st_size
    return total


def _execute_soak_suite(
    api: HAClient,
    args: argparse.Namespace,
    device: Device,
    output_dir: Path,
    run_id: str,
    services: SuiteServices,
    resume_state: dict[str, Any] | None = None,
) -> int:
    """Collect capped read-only samples and short traces for a full 24 hours."""
    scenario = next(
        item for item in services.load_scenarios() if item.id == "soak_24h_observation"
    )
    parameters = scenario.parameters
    entry_id = device.config_entry_id
    fingerprint = services.device_fingerprint(run_id, device, api.origin, args.route)
    steps = (
        [dict(step) for step in resume_state["steps"]]
        if resume_state is not None
        else []
    )

    def checkpoint() -> None:
        services.write_scenario_progress(
            output_dir, run_id, scenario.id, fingerprint, steps
        )

    if entry_id is None or device.entry_state != "loaded":
        steps.append(
            {
                "step_id": "soak_segment",
                "segment": len(steps) + 1,
                "outcome": "not_applicable",
                "reason": "a loaded integration entry is required",
            }
        )
        checkpoint()
        return 2

    if resume_state is not None:
        if not steps or any(step.get("step_id") != "soak_segment" for step in steps):
            raise HAAPIError("Soak checkpoint contains unknown steps")
        if any(step.get("outcome") == "pass" for step in steps):
            raise HAAPIError("A completed 24-hour soak cannot be resumed")
        if any(
            type(step.get("segment")) is not int
            or step["segment"] < 1
            or type(step.get("trace_count", 0)) is not int
            or step.get("trace_count", 0) < 0
            or type(step.get("sample_file_index", 1)) is not int
            or step.get("sample_file_index", 1) < 1
            or step.get("data_limit_hit") is True
            for step in steps
        ):
            raise HAAPIError("Soak checkpoint state cannot be resumed safely")
        if steps:
            steps[-1]["outcome"] = "inconclusive"
            steps[-1]["restart_discontinuity"] = True
            steps[-1]["reason"] = "process restart leaves an unobserved time gap"
            checkpoint()

    sample_file_index = 1
    if steps:
        stored_index = steps[-1].get("sample_file_index", 1)
        if type(stored_index) is not int or stored_index < 1:
            raise HAAPIError("Soak checkpoint sample rotation state is invalid")
        sample_file_index = stored_index
    segment_number = 1 + max((int(step.get("segment", 0)) for step in steps), default=0)
    baseline = api.hil_capabilities(entry_id)
    if (
        baseline["verified"] is not True
        or baseline["identity_conflicted"]
        or baseline["connected"] is not True
        or baseline["status_fresh"] is not True
        or type(baseline["session_generation"]) is not int
    ):
        steps.append(
            {
                "step_id": "soak_segment",
                "segment": segment_number,
                "outcome": "inconclusive",
                "reason": "verified fresh connected baseline unavailable",
                "restart_discontinuity": resume_state is not None,
            }
        )
        checkpoint()
        return 2

    run_started = services.monotonic()
    segment_started: float | None = None
    previous_sample_monotonic: float | None = None
    previous_sample_utc: datetime | None = None
    sample_count = 0
    trace_count = 0
    maximum_gap = 0.0
    dropped_traces = 0
    clock_discontinuities = 0
    data_limit_hit = False
    active_segment: dict[str, Any] | None = None
    trace_required = (
        parameters["duration_seconds"] // parameters["trace_interval_seconds"]
    )

    def begin_segment(now: float, utc_now: datetime) -> None:
        nonlocal segment_started, sample_count, trace_count, maximum_gap
        nonlocal dropped_traces, clock_discontinuities, active_segment
        segment_started = now
        sample_count = 0
        trace_count = 0
        maximum_gap = 0.0
        dropped_traces = 0
        clock_discontinuities = 0
        active_segment = {
            "step_id": "soak_segment",
            "segment": segment_number,
            "outcome": "inconclusive",
            "segment_started_utc": utc_now.isoformat(),
            "measured_seconds": 0.0,
            "sample_count": 0,
            "trace_count": 0,
            "maximum_gap_seconds": 0.0,
            "dropped_traces": 0,
            "clock_discontinuities": 0,
            "sample_file_index": sample_file_index,
        }
        steps.append(active_segment)
        checkpoint()

    def append_record(record: dict[str, Any]) -> bool:
        nonlocal sample_file_index, data_limit_hit
        encoded = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode(
            "utf-8"
        )
        sample_path = output_dir / f"soak_samples_{sample_file_index:03d}.jsonl"
        if sample_path.is_symlink():
            raise HAAPIError("Soak sample path must not be a symlink")
        current_size = sample_path.stat().st_size if sample_path.exists() else 0
        if sample_path.exists():
            metadata = sample_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise HAAPIError("Soak sample file is not private")
        if current_size + len(encoded) > 256 * 1024:
            sample_file_index += 1
            sample_path = output_dir / f"soak_samples_{sample_file_index:03d}.jsonl"
            current_size = 0
        if (
            _private_run_size(output_dir) + len(encoded) + 4096
            > parameters["max_bytes"]
        ):
            data_limit_hit = True
            return False
        fd = os.open(sample_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(encoded)
            stream.flush()
        if active_segment is not None:
            active_segment["sample_file_index"] = sample_file_index
        return True

    def end_segment(reason: str) -> None:
        nonlocal active_segment, segment_started, segment_number
        if active_segment is not None:
            active_segment["outcome"] = "inconclusive"
            active_segment["reason"] = reason
            checkpoint()
        active_segment = None
        segment_started = None
        segment_number += 1

    if resume_state is not None:
        restart_utc = services.datetime.now(UTC)
        if not append_record(
            {
                "event": "process_restart_gap",
                "utc": restart_utc.isoformat(),
                "gap_seconds": None,
                "reason": "process restart makes the unobserved interval unknowable",
            }
        ):
            print("Soak storage cap reached before resume")
            return 2

    next_sample = run_started
    next_trace = run_started
    last_generation = baseline["session_generation"]
    trace_index = sum(step.get("trace_count", 0) for step in steps)
    while True:
        now = services.monotonic()
        utc_now = services.datetime.now(UTC)
        if now >= next_sample:
            gap = (
                now - previous_sample_monotonic
                if previous_sample_monotonic is not None
                else None
            )
            clock_jump = False
            if gap is not None and previous_sample_utc is not None:
                wall_gap = (utc_now - previous_sample_utc).total_seconds()
                clock_jump = abs(wall_gap - gap) > 5
            try:
                capabilities = api.hil_capabilities(entry_id)
                snapshot = services.read_only_sample(api, device)
            except HAAPIError:
                capabilities = None
                snapshot = {}
            healthy = bool(
                capabilities is not None
                and capabilities["verified"] is True
                and not capabilities["identity_conflicted"]
                and capabilities["connected"] is True
                and capabilities["status_fresh"] is True
                and type(capabilities["session_generation"]) is int
                and services.has_verified_telemetry(snapshot)
            )
            if gap is not None and gap > parameters["max_gap_seconds"]:
                maximum_gap = max(maximum_gap, gap)
                append_record(
                    {
                        "event": "sample_gap",
                        "utc": utc_now.isoformat(),
                        "gap_seconds": round(gap, 3),
                        "reason": "maximum allowed sample gap exceeded",
                    }
                )
                if active_segment is not None:
                    active_segment["maximum_gap_seconds"] = round(maximum_gap, 3)
                end_segment("maximum sample gap exceeded")
            if clock_jump:
                clock_discontinuities += 1
                append_record(
                    {
                        "event": "clock_discontinuity",
                        "utc": utc_now.isoformat(),
                        "monotonic_gap_seconds": round(gap or 0, 3),
                        "wall_gap_seconds": round(wall_gap, 3),
                    }
                )
                if active_segment is not None:
                    active_segment["clock_discontinuities"] = clock_discontinuities
                end_segment("system clock discontinuity observed")
            if not healthy:
                append_record(
                    {
                        "event": "sample_gap",
                        "utc": utc_now.isoformat(),
                        "gap_seconds": round(gap, 3) if gap is not None else None,
                        "reason": "runtime disconnected, unverified or stale",
                        "session_generation": (
                            capabilities.get("session_generation")
                            if capabilities is not None
                            else None
                        ),
                    }
                )
                end_segment("fresh connected status or verified telemetry unavailable")
            else:
                if segment_started is None:
                    begin_segment(now, utc_now)
                if (
                    previous_sample_monotonic is not None
                    and gap is not None
                    and gap > parameters["sample_interval_seconds"] + 30
                ):
                    append_record(
                        {
                            "event": "sampling_delay",
                            "utc": utc_now.isoformat(),
                            "gap_seconds": round(gap, 3),
                        }
                    )
                if capabilities["session_generation"] != last_generation:
                    append_record(
                        {
                            "event": "session_generation_change",
                            "utc": utc_now.isoformat(),
                            "previous_generation": last_generation,
                            "session_generation": capabilities["session_generation"],
                        }
                    )
                last_generation = capabilities["session_generation"]
                sample_count += 1
                if gap is not None:
                    maximum_gap = max(maximum_gap, gap)
                row = {
                    "event": "sample",
                    "utc": utc_now.isoformat(),
                    "elapsed_seconds": round(now - segment_started, 3),
                    "session_generation": capabilities["session_generation"],
                    "model": capabilities["model"],
                    "profile": capabilities["profile"],
                    "status_fresh": capabilities["status_fresh"],
                    "settings_fresh": capabilities["settings_fresh"],
                    "telemetry": snapshot,
                }
                if not append_record(row):
                    break
                active_segment["sample_count"] = sample_count
                active_segment["measured_seconds"] = round(now - segment_started, 3)
                active_segment["maximum_gap_seconds"] = round(maximum_gap, 3)
                active_segment["last_sample_utc"] = utc_now.isoformat()
                active_segment["last_session_generation"] = capabilities[
                    "session_generation"
                ]
                checkpoint()
            next_sample = now + parameters["sample_interval_seconds"]
            previous_sample_monotonic = now
            previous_sample_utc = utc_now

        now = services.monotonic()
        if segment_started is not None and now >= next_trace:
            trace_ok = False
            trace_reason = "trace start or finalization failed"
            capture_id: str | None = None
            trace: dict[str, Any] | None = None
            try:
                start = api.websocket(
                    [
                        {
                            "type": "allpowers_ble/hil/start",
                            "entry_id": entry_id,
                            "run_id": run_id,
                            "scenario_id": scenario.id,
                            "requested_route": args.route,
                        }
                    ]
                )[0]
                if (
                    isinstance(start, dict)
                    and start.get("schema_version") == 2
                    and isinstance(start.get("capture_id"), str)
                    and type(start.get("ttl_seconds")) is int
                    and start["ttl_seconds"] >= parameters["trace_window_seconds"]
                ):
                    capture_id = start["capture_id"]
                    try:
                        services.sleep(parameters["trace_window_seconds"])
                    finally:
                        try:
                            trace = api.websocket(
                                [
                                    {
                                        "type": "allpowers_ble/hil/stop",
                                        "entry_id": entry_id,
                                        "capture_id": capture_id,
                                    }
                                ]
                            )[0]
                        except HAAPIError:
                            trace = api.websocket(
                                [
                                    {
                                        "type": "allpowers_ble/hil/fetch",
                                        "entry_id": entry_id,
                                        "capture_id": capture_id,
                                    }
                                ]
                            )[0]
                    services.validate_trace_result(
                        trace,
                        run_id=run_id,
                        scenario_id=scenario.id,
                        capture_id=capture_id,
                        requested_route=args.route,
                        expected_version=2,
                    )
                    trace_ok = bool(
                        trace["complete"] is True
                        and services.route_matches_request(
                            args.route, trace["observed_route"]
                        )
                    )
                    trace_reason = (
                        "trace complete" if trace_ok else "route or trace incomplete"
                    )
                    encoded_size = len(
                        json.dumps(trace, sort_keys=True, ensure_ascii=True).encode()
                    )
                    if (
                        trace_ok
                        and _private_run_size(output_dir) + encoded_size + 4096
                        <= parameters["max_bytes"]
                    ):
                        trace_index += 1
                        trace_name = f"soak_trace_{trace_index:03d}"
                        trace_parent = output_dir / "traces"
                        if trace_parent.is_symlink():
                            raise HAAPIError(
                                "Private trace directory must not be a symlink"
                            )
                        trace_parent.mkdir(mode=0o700, exist_ok=True)
                        if (
                            trace_parent.is_symlink()
                            or stat.S_IMODE(trace_parent.stat().st_mode) & 0o077
                        ):
                            raise HAAPIError("Soak trace directory is not private")
                        trace_dir = trace_parent / trace_name
                        trace_dir.mkdir(mode=0o700, exist_ok=False)
                        services.save_private_trace(trace_dir, trace)
                        active_segment["last_trace"] = f"traces/{trace_name}"
                    elif trace_ok:
                        data_limit_hit = True
                        trace_ok = False
                        trace_reason = "per-run storage cap reached before trace save"
                    append_record(
                        {
                            "event": "trace_window",
                            "utc": services.datetime.now(UTC).isoformat(),
                            "capture_id_present": capture_id is not None,
                            "observed_route": trace.get("observed_route")
                            if isinstance(trace, dict)
                            else None,
                            "complete": trace.get("complete") is True
                            if isinstance(trace, dict)
                            else False,
                            "dropped_events": trace.get("dropped_events")
                            if isinstance(trace, dict)
                            else None,
                            "dropped_frames": trace.get("dropped_frames")
                            if isinstance(trace, dict)
                            else None,
                            "expired": trace.get("expired")
                            if isinstance(trace, dict)
                            else None,
                            "saved": trace_ok,
                            "reason": trace_reason,
                        }
                    )
            except KeyboardInterrupt:
                if active_segment is not None:
                    active_segment["outcome"] = "inconclusive"
                    active_segment["reason"] = "soak interrupted during trace window"
                    checkpoint()
                print("Soak interrupted; private checkpoint saved")
                return 2
            except HAAPIError, OSError:
                append_record(
                    {
                        "event": "trace_window_dropped",
                        "utc": services.datetime.now(UTC).isoformat(),
                        "reason": "trace could not be validated or saved",
                    }
                )
            if not trace_ok:
                dropped_traces += 1
                if active_segment is not None:
                    active_segment["dropped_traces"] = dropped_traces
                end_segment(trace_reason)
            else:
                trace_count += 1
                active_segment["trace_count"] = trace_count
                checkpoint()
            next_trace = services.monotonic() + parameters["trace_interval_seconds"]

        now = services.monotonic()
        if data_limit_hit:
            break
        if (
            active_segment is not None
            and segment_started is not None
            and now - segment_started >= parameters["duration_seconds"]
            and previous_sample_monotonic is not None
            and previous_sample_monotonic - segment_started
            >= parameters["duration_seconds"]
        ):
            outcome = services.soak_verdict(
                measured_seconds=previous_sample_monotonic - segment_started,
                sample_count=sample_count,
                minimum_samples=parameters["minimum_samples"],
                maximum_gap_seconds=maximum_gap,
                allowed_gap_seconds=parameters["max_gap_seconds"],
                trace_count=trace_count,
                minimum_traces=trace_required,
                dropped_traces=dropped_traces,
            )
            active_segment["measured_seconds"] = round(
                previous_sample_monotonic - segment_started, 3
            )
            active_segment["outcome"] = outcome
            active_segment["ended_utc"] = services.datetime.now(UTC).isoformat()
            checkpoint()
            print("Soak segment:", outcome)
            return 0 if outcome == "pass" else 2

        next_event = next_sample
        if active_segment is not None:
            next_event = min(
                next_event, next_trace, segment_started + parameters["duration_seconds"]
            )
        delay = max(0.1, next_event - services.monotonic())
        try:
            services.sleep(delay)
        except KeyboardInterrupt:
            if active_segment is not None:
                active_segment["outcome"] = "inconclusive"
                active_segment["reason"] = "operator interrupted soak sampling"
                active_segment["measured_seconds"] = round(
                    services.monotonic() - segment_started, 3
                )
            checkpoint()
            print("Soak interrupted; private checkpoint saved")
            return 2

    if active_segment is not None:
        active_segment["outcome"] = "inconclusive"
        active_segment["reason"] = "per-run evidence byte cap reached"
        active_segment["data_limit_hit"] = data_limit_hit
    checkpoint()
    print(
        "Soak stopped inconclusive; private checkpoint saved to:", run_root(output_dir)
    )
    return 2
