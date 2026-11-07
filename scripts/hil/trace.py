"""Validation and private persistence for HIL trace evidence."""

from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from .api import HAAPIError


def _natural_int(value: Any) -> bool:
    """Exclude booleans from integer fields and forbid negative counters."""
    return type(value) is int and value >= 0


def _check_trace_rows(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    scenario_id: str,
    frame: bool,
    schema_version: int = 1,
) -> dict[int, dict[str, Any]]:
    """Validate ordered v1/v2 event/frame rows without decoding private payloads."""
    by_seq: dict[int, dict[str, Any]] = {}
    previous = 0
    previous_elapsed = 0.0
    for row in rows:
        seq = row.get("seq")
        if (
            not _natural_int(seq)
            or seq <= previous
            or row.get("schema_version") != schema_version
            or row.get("run_id") != run_id
            or row.get("scenario_id") != scenario_id
            or row.get("layer") != "gatt_api"
            or row.get("observed_route")
            not in (
                ("unverified",)
                if schema_version == 1
                else ("local_adapter", "active_proxy", "unverified")
            )
            or not isinstance(row.get("event"), str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", row["event"]) is None
        ):
            raise HAAPIError("Malformed HIL trace event/frame identity or order")
        utc_value = row.get("utc")
        try:
            when = datetime.fromisoformat(utc_value)
        except (TypeError, ValueError) as ex:
            raise HAAPIError("Malformed HIL trace timestamp") from ex
        if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
            raise HAAPIError("HIL trace timestamp must be UTC")
        elapsed = row.get("elapsed_ms")
        generation = row.get("session_generation")
        if (
            type(elapsed) not in (float, int)
            or not math.isfinite(elapsed)
            or elapsed < previous_elapsed
            or (
                generation is not None
                and (not _natural_int(generation) or generation < 1)
            )
        ):
            raise HAAPIError("Malformed HIL trace timing or session generation")
        if frame:
            direction = row.get("direction")
            opcode = row.get("command_type")
            validation = row.get("frame_validation")
            encoded = row.get("hex")
            if (
                direction not in ("rx", "tx")
                or opcode
                not in (
                    "status_request",
                    "output_control",
                    "settings_control",
                    "unknown",
                )
                or validation not in ("validated_tx", "unvalidated_chunk")
                or not isinstance(encoded, str)
                or len(encoded) > 512
                or len(encoded) % 2
                or re.fullmatch(r"[0-9a-f]*", encoded) is None
                or (
                    direction == "rx"
                    and (opcode != "unknown" or validation != "unvalidated_chunk")
                )
                or (
                    validation == "validated_tx"
                    and (direction != "tx" or opcode == "unknown")
                )
                or (
                    direction == "tx"
                    and opcode != "unknown"
                    and validation != "validated_tx"
                )
            ):
                raise HAAPIError("Malformed HIL GATT frame or validation")
        previous = seq
        previous_elapsed = elapsed
        by_seq[seq] = row
    return by_seq


def _check_v2_routes(
    result: dict[str, Any],
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
) -> None:
    """Validate per-generation proof and row-time binding before private I/O."""
    observations = result.get("route_observations")
    if not isinstance(observations, list):
        raise HAAPIError("Missing HIL v2 route observations")
    if len(observations) > 256:
        raise HAAPIError("Too many HIL v2 route observations")
    routes: dict[int, dict[str, Any]] = {}
    previous_seq = 0
    previous_elapsed = 0.0
    for item in observations:
        if not isinstance(item, dict):
            raise HAAPIError("Malformed route observation")
        required = {
            "session_generation",
            "seq",
            "utc",
            "elapsed_ms",
            "observed_route",
            "backend_kind",
            "evidence",
        }
        if not required.issubset(item) or set(item) - (
            required | {"adapter_id", "proxy_id"}
        ):
            raise HAAPIError("Malformed route observation fields")
        gen = item["session_generation"]
        seq = item["seq"]
        elapsed = item["elapsed_ms"]
        try:
            when = datetime.fromisoformat(item["utc"])
        except (TypeError, ValueError) as ex:
            raise HAAPIError("Invalid route observation timestamp") from ex
        if (
            not _natural_int(gen)
            or gen < 1
            or gen in routes
            or not _natural_int(seq)
            or seq <= previous_seq
            or type(elapsed) not in (float, int)
            or not math.isfinite(elapsed)
            or elapsed < previous_elapsed
            or when.utcoffset() is None
            or when.utcoffset().total_seconds() != 0
        ):
            raise HAAPIError("Invalid route observation generation or order")
        route = item["observed_route"]
        backend = item["backend_kind"]
        evidence = item["evidence"]
        if route == "local_adapter":
            adapter = item.get("adapter_id")
            if (
                backend != "bluez"
                or evidence != "bluez_device_path"
                or type(adapter) is not str
                or re.fullmatch(r"hci[0-9]+", adapter) is None
                or "proxy_id" in item
            ):
                raise HAAPIError("Malformed local connected backend proof")
        elif route == "active_proxy":
            proxy = item.get("proxy_id")
            if (
                backend != "esphome"
                or evidence != "esphome_scanner_binding"
                or type(proxy) is not str
                or not proxy
                or len(proxy) > 128
                or not proxy.isprintable()
                or "adapter_id" in item
            ):
                raise HAAPIError("Malformed active proxy connected backend proof")
        elif route == "unverified":
            if (
                backend != "unknown"
                or evidence
                not in (
                    "no_ha_wrapper",
                    "not_connected",
                    "missing_tracking",
                    "local_backend_mismatch",
                    "bluez_path_mismatch",
                    "esphome_backend_mismatch",
                    "esphome_scanner_mismatch",
                    "unsupported_backend",
                    "inspection_failed",
                )
                or "adapter_id" in item
                or "proxy_id" in item
            ):
                raise HAAPIError("Unverified connected route has forged backend proof")
        else:
            raise HAAPIError("Unrecognized connected route")
        routes[gen] = item
        previous_seq = seq
        previous_elapsed = elapsed

    observed = [row["observed_route"] for row in observations]
    summary = (
        "unverified"
        if not observed or "unverified" in observed
        else observed[0]
        if len(set(observed)) == 1
        else "mixed"
    )
    if result.get("observed_route") != summary:
        raise HAAPIError("Connected route summary disagrees with its evidence")

    found_observations: set[int] = set()
    for row in events + frames:
        generation = row.get("session_generation")
        route_item = routes.get(generation)
        expected = (
            route_item["observed_route"]
            if route_item is not None and row["seq"] >= route_item["seq"]
            else "unverified"
        )
        if row["observed_route"] != expected:
            raise HAAPIError("Connected backend route contradicts event timing")
        if row["event"] == "route_observed":
            if (
                route_item is None
                or row["seq"] != route_item["seq"]
                or row.get("utc") != route_item["utc"]
                or row.get("elapsed_ms") != route_item["elapsed_ms"]
                or row.get("backend_kind") != route_item["backend_kind"]
                or row.get("evidence") != route_item["evidence"]
            ):
                raise HAAPIError("Route observation is not bound to its generation")
            found_observations.add(generation)
    if result["dropped_events"] == 0 and found_observations != set(routes):
        raise HAAPIError("Missing connected backend observation event")


def _validate_trace_result(
    result: Any,
    *,
    run_id: str,
    scenario_id: str,
    capture_id: str,
    requested_route: str,
    expected_version: int | None = None,
    expected_scope: str = "runtime_entry",
) -> None:
    """Fail closed on private v1/v2 evidence, before writing any raw files."""
    is_probe = (
        isinstance(result, dict) and result.get("capture_scope") == "config_probe"
    )
    if (
        not isinstance(result, dict)
        or result.get("capture_scope", "runtime_entry") != expected_scope
        or result.get("schema_version") not in (1, 2)
        or (
            expected_version is not None
            and result.get("schema_version") != expected_version
        )
        or result.get("run_id") != run_id
        or result.get("scenario_id") != scenario_id
        or result.get("capture_id") != capture_id
        or result.get("requested_route") != requested_route
        or result.get("qualification_allowed") is not False
        or (
            result.get("observed_route") != "unverified"
            if result.get("schema_version") == 1
            else result.get("observed_route")
            not in ("local_adapter", "active_proxy", "mixed", "unverified")
        )
        or result.get("capture_layer") != "gatt_api_not_rf"
        or result.get("private_raw_bytes") is not True
        or result.get("closed") is not True
        or type(result.get("expired")) is not bool
        or type(result.get("complete")) is not bool
        or result.get("termination_reason")
        not in (
            ("probe_finished", "probe_skipped", "probe_interrupted", "expired")
            if is_probe
            else ("requested_stop", "runtime_stopped", "expired")
        )
    ):
        raise HAAPIError("Capture metadata mismatch or final stop not acknowledged")
    if is_probe and (
        result.get("schema_version") != 2
        or not isinstance(result.get("flow_id"), str)
        or not result["flow_id"]
        or result.get("probe_state") != "finished"
        or result.get("probe_outcome")
        not in ("success", "failure", "cancelled", "skipped", "interrupted")
        or (
            result["termination_reason"] == "probe_finished"
            and result["probe_outcome"] not in ("success", "failure", "cancelled")
        )
        or (
            result["termination_reason"] == "probe_skipped"
            and result["probe_outcome"] != "skipped"
        )
        or (
            result["termination_reason"] == "probe_interrupted"
            and result["probe_outcome"] != "interrupted"
        )
    ):
        raise HAAPIError("Invalid probe capture lifecycle metadata")
    for key in ("events", "frames"):
        if not isinstance(result.get(key), list) or not all(
            isinstance(row, dict) for row in result[key]
        ):
            raise HAAPIError("Invalid private HIL trace records")
    for name in ("event_count", "frame_count", "dropped_events", "dropped_frames"):
        if not _natural_int(result.get(name)):
            raise HAAPIError("Invalid HIL trace count")
    events = result["events"]
    frames = result["frames"]
    if (
        result["event_count"] != len(events)
        or result["frame_count"] != len(frames)
        or result.get("has_gatt_frames") is not bool(frames)
    ):
        raise HAAPIError("Incomplete private HIL trace record count")
    expected_complete = (
        result["termination_reason"]
        == ("probe_finished" if is_probe else "requested_stop")
        and not result["expired"]
        and result["dropped_events"] == 0
        and result["dropped_frames"] == 0
    )
    if result["complete"] is not expected_complete or (
        is_probe and result.get("capture_complete") is not expected_complete
    ):
        raise HAAPIError("Contradictory HIL capture integrity fields")
    version = result["schema_version"]
    events_by_seq = _check_trace_rows(
        events,
        run_id=run_id,
        scenario_id=scenario_id,
        frame=False,
        schema_version=version,
    )
    _check_trace_rows(
        frames,
        run_id=run_id,
        scenario_id=scenario_id,
        frame=True,
        schema_version=version,
    )
    if version == 2:
        _check_v2_routes(result, events, frames)
    elif "route_observations" in result:
        raise HAAPIError("Unexpected connected backend proof in v1 trace")
    common_fields = (
        "schema_version",
        "seq",
        "utc",
        "elapsed_ms",
        "run_id",
        "scenario_id",
        "event",
        "session_generation",
        "layer",
        "observed_route",
    )
    for record in frames:
        event = events_by_seq.get(record["seq"])
        if event is None:
            if result["dropped_events"] == 0:
                raise HAAPIError("HIL frame is missing its corresponding event")
            continue
        if any(record[key] != event[key] for key in common_fields) or (
            ("transaction_id" in record or "transaction_id" in event)
            and (
                "transaction_id" not in record
                or "transaction_id" not in event
                or record["transaction_id"] != event["transaction_id"]
            )
        ):
            raise HAAPIError("HIL frame metadata differs from its matching event")


def _save_private_trace(output_dir: Path, result: dict[str, Any]) -> None:
    """Write only validated private evidence, scrubbing partial failures."""
    private_paths = (
        output_dir / "events.jsonl",
        output_dir / "frames.jsonl",
        output_dir / "trace.json",
    )
    try:
        for path, key in zip(private_paths[:2], ("events", "frames"), strict=True):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for record in result[key]:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
        metadata = {
            key: value
            for key, value in result.items()
            if key not in ("events", "frames")
        }
        fd = os.open(private_paths[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except (OSError, TypeError, ValueError) as ex:
        for path in private_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HAAPIError("Private HIL trace could not be saved") from ex
