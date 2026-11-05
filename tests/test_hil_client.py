"""Golden v1 evidence and focused observer/attachment equivalence regressions."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from custom_components.allpowers_ble.hil_client import (
    HilClientTracer,
    PendingTraceState,
)
from custom_components.allpowers_ble.hil_trace import Capture
from custom_components.allpowers_ble.protocol import encode_status_request
from tests.test_client_runtime import make_client


def _capture() -> Capture:
    return Capture("entry", "admin", "run12345", "scenario", "local_adapter")


def _normalized_rows(capture: Capture) -> tuple[list[dict], list[dict]]:
    """Only timestamps differ between the original and extracted producer."""
    def clean(rows: list[dict]) -> list[dict]:
        return [
            {key: value for key, value in row.items() if key not in ("utc", "elapsed_ms")}
            for row in rows
        ]

    return clean(capture.events), clean(capture.frames)


def test_scripted_v1_event_and_frame_equivalence() -> None:
    """Explicitly match the pre-refactor Capture.record event/field contract."""
    original = _capture()
    refactored = _capture()
    observer = HilClientTracer(refactored)
    tx = PendingTraceState(4, "tx-fixed-42", "output", "awaiting_confirmation", 100)

    original.record("connect_attempt", session_generation=4)
    observer.connection("connect_attempt", 4)
    original.record(
        "model_profile_selected",
        session_generation=4,
        inferred_model_profile="r600",
        model_classification="supported",
    )
    observer.connection(
        "model_profile_selected", 4, profile="r600", classification="supported"
    )
    original.record("connected", session_generation=4)
    observer.connection("connected", 4)

    original.record(
        "transaction_inherited",
        session_generation=4,
        transaction_id=tx.transaction_id,
        transaction_kind="output",
        transaction_phase="awaiting_confirmation",
        origin="preexisting",
        attribution="prior_capture",
    )
    observer.attach((tx,))
    original.record(
        "control_rejected",
        session_generation=4,
        control_kind="output",
        reason="confirmation_pending",
    )
    observer.control_rejection(4, "output", "confirmation_pending")
    payload = encode_status_request()
    original.record(
        "write_attempt",
        direction="tx",
        frame=payload,
        session_generation=4,
        transaction_id="tx-fixed-42",
        write_with_response=True,
        characteristic_uuid="0000fff2-0000-1000-8000-00805f9b34fb",
    )
    observer.write(
        "write_attempt", 4, transaction_id="tx-fixed-42", frame=payload,
        write_with_response=True,
    )
    rx = bytearray(b"\x01\x02")
    original.record(
        "notification_received",
        direction="rx",
        frame=rx,
        characteristic_uuid="0000fff1-0000-1000-8000-00805f9b34fb",
        session_generation=4,
    )
    observer.notification_received(4, rx)
    original.record(
        "notification_decoded",
        session_generation=4,
        decoded_packets=1,
        decoded_packet_types=["StatusData"],
        characteristic_uuid="0000fff1-0000-1000-8000-00805f9b34fb",
        inferred_model_profile="r600",
        session_hardware_revision="2",
        discarded_frames=0,
        discarded_bytes=1,
    )
    observer.notification_decoded(
        4, packet_types=("StatusData",), profile="r600",
        hardware_revision="2", discarded_frames=0, discarded_bytes=1,
    )
    original.record(
        "write_ok", session_generation=4, transaction_id="tx-fixed-42"
    )
    observer.write("write_ok", 4, transaction_id="tx-fixed-42")
    for event, reason in (
        ("transaction_submitted", None),
        ("transaction_confirmed", "matching_validated_telemetry"),
    ):
        original.record(
            event,
            session_generation=4,
            transaction_id=tx.transaction_id,
            transaction_kind=tx.kind,
            transaction_phase=tx.phase,
            reason=reason,
        )
        observer.transaction(event, tx, reason=reason)
    original.record(
        "disconnected", session_generation=4, reason_category="telemetry_watchdog"
    )
    observer.connection("disconnected", 4, stage="telemetry_watchdog")
    original.record("client_stopped", session_generation=None)
    original.stop("runtime_stopped")
    observer.stop_runtime(None)

    assert _normalized_rows(original) == _normalized_rows(refactored)
    for key in (
        "closed", "termination_reason", "complete", "event_count", "frame_count",
        "dropped_events", "dropped_frames", "schema_version", "observed_route",
        "qualification_allowed",
    ):
        assert original.snapshot()[key] == refactored.snapshot()[key]
    assert original.frames[0]["hex"] == payload.hex()
    assert original.frames[0]["seq"] == original.events[4]["seq"]


def test_observer_negative_and_inherited_outcomes() -> None:
    capture = _capture()
    tracer = HilClientTracer(capture)
    assert tracer.matches(capture)
    assert not tracer.matches(_capture())
    previous = PendingTraceState(2, "tx-known", "output", "sending", 20)
    unknown = PendingTraceState(3, None, "settings", "awaiting_confirmation", 20)
    confirmed = PendingTraceState(3, "tx-confirmed", "output", "confirmed", 20)
    with pytest.raises(FrozenInstanceError):
        previous.kind = "settings"  # type: ignore[misc]

    tracer.attach((previous, unknown, confirmed))
    assert [row["event"] for row in capture.events] == [
        "transaction_inherited", "transaction_inherited"
    ]
    assert [row["attribution"] for row in capture.events] == [
        "prior_capture", "unattributed"
    ]
    trace_id = tracer.new_transaction_id()
    assert trace_id.startswith("tx-") and trace_id != capture.capture_id

    tracer.finalize_pending(
        (
            PendingTraceState(3, "tx-timeout", "output", "sending", 10),
            unknown, confirmed,
        ),
        now=15,
        disconnected=True,
    )
    assert [row["event"] for row in capture.events[-2:]] == [
        "transaction_timed_out", "transaction_abandoned"
    ]
    assert [row["reason"] for row in capture.events[-2:]] == [
        "deadline_passed", "session_disconnected"
    ]
    tracer.finalize_pending((unknown,), now=15, disconnected=False)
    assert capture.events[-1]["event"] == "transaction_unresolved"
    assert capture.events[-1]["reason"] == "deadline_open"


def test_error_write_paths_stay_private_and_causally_distinct() -> None:
    capture = _capture()
    tracer = HilClientTracer(capture)
    raw = b"\xaa\xbb"
    tracer.connection(
        "connect_failed", 7, stage="gatt_connection", error_type="TimeoutError"
    )
    tracer.write("write_rejected", 7, frame=raw, transaction_id=None)
    tracer.write("write_attempt", 7, frame=raw, write_with_response=False)
    tracer.write(
        "write_error",
        7,
        error_type="CancelledError",
        reason_category="cancelled",
        transaction_id="tx-55",
    )
    tracer.write("write_ok", 7, transaction_id="tx-56")
    assert [row["event"] for row in capture.events] == [
        "connect_failed", "write_rejected", "write_attempt",
        "write_error", "write_ok",
    ]
    assert capture.events[0]["exception_type"] == "TimeoutError"
    assert capture.events[1]["reason"] == "not_connected"
    assert capture.frames[0]["hex"] == raw.hex()
    assert capture.frames[1]["write_with_response"] is False
    assert capture.events[3]["reason_category"] == "cancelled"
    assert "AA:BB" not in str(capture.events)


def test_production_client_identity_guard_and_disabled_allocation() -> None:
    client = make_client()
    assert client.hil_capture is None
    assert client._hil is None
    client.finalize_hil_capture(None)
    client.finalize_hil_capture(_capture())
    client.detach_hil_capture(_capture())
    assert client._hil is None

    a, b = _capture(), _capture()
    client.attach_hil_capture(a)
    assert client.hil_capture is a
    client.finalize_hil_capture(None)
    client.finalize_hil_capture(b)
    client.detach_hil_capture(b)
    assert client.hil_capture is a
    client.attach_hil_capture(b)
    assert client.hil_capture is b
    client.finalize_hil_capture(a)
    client.detach_hil_capture(a)
    assert client.hil_capture is b
    client.detach_hil_capture(b)
    assert client.hil_capture is None
    assert client._hil is None
