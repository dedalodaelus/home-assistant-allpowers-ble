"""Golden v1 evidence and focused observer/attachment equivalence regressions."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields
from unittest.mock import MagicMock

import pytest

from custom_components.allpowers_ble.client_observer import (
    ClientObserver,
    ObserverSlot,
    TransactionObservation,
)
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
    events = [row.copy() for row in capture.events]
    frames = [row.copy() for row in capture.frames]
    for row in events + frames:
        row.pop("utc", None)
        row.pop("elapsed_ms", None)
    return events, frames


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
        "model_profile_selected",
        4,
        profile="r600",
        classification="supported",
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
        "write_attempt",
        4,
        transaction_id="tx-fixed-42",
        frame=payload,
        write_with_response=True,
    )
    rx = bytes((1, 2))
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
        4,
        packet_types=("StatusData",),
        profile="r600",
        hardware_revision="2",
        discarded_frames=0,
        discarded_bytes=1,
    )
    original.record("write_ok", session_generation=4, transaction_id="tx-fixed-42")
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
        "disconnected",
        session_generation=4,
        reason_category="telemetry_watchdog",
    )
    observer.connection("disconnected", 4, stage="telemetry_watchdog")
    original.record("client_stopped", session_generation=None)
    original.stop("runtime_stopped")
    observer.stop_runtime(None)

    assert _normalized_rows(original) == _normalized_rows(refactored)
    for key in (
        "closed",
        "termination_reason",
        "complete",
        "event_count",
        "frame_count",
        "dropped_events",
        "dropped_frames",
        "schema_version",
        "observed_route",
        "qualification_allowed",
    ):
        assert original.snapshot()[key] == refactored.snapshot()[key]
    assert original.frames[0]["hex"] == payload.hex()
    assert original.frames[0]["seq"] == original.events[5]["seq"]


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
        "transaction_inherited",
        "transaction_inherited",
    ]
    assert [row["attribution"] for row in capture.events] == [
        "prior_capture",
        "unattributed",
    ]
    trace_id = tracer.new_transaction_id()
    assert trace_id.startswith("tx-") and trace_id != capture.capture_id

    tracer.finalize_pending(
        (
            PendingTraceState(3, "tx-timeout", "output", "sending", 10),
            unknown,
            confirmed,
        ),
        now=15,
        disconnected=True,
    )
    assert [row["event"] for row in capture.events[-2:]] == [
        "transaction_timed_out",
        "transaction_abandoned",
    ]
    assert [row["reason"] for row in capture.events[-2:]] == [
        "deadline_passed",
        "session_disconnected",
    ]
    tracer.finalize_pending((unknown,), now=15, disconnected=False)
    assert capture.events[-1]["event"] == "transaction_unresolved"
    assert capture.events[-1]["reason"] == "deadline_open"


def test_error_write_paths_stay_private_and_causally_distinct() -> None:
    capture = _capture()
    tracer = HilClientTracer(capture)
    raw = b"\xaa\xbb"
    tracer.connection(
        "connect_failed",
        7,
        stage="gatt_connection",
        error_type="TimeoutError",
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
        "connect_failed",
        "write_rejected",
        "write_attempt",
        "write_error",
        "write_ok",
    ]
    assert capture.events[0]["exception_type"] == "TimeoutError"
    assert capture.events[1]["reason"] == "not_connected"
    assert capture.frames[0]["hex"] == raw.hex()
    assert capture.frames[1]["write_with_response"] is False
    assert capture.events[3]["reason_category"] == "cancelled"
    assert "AA:BB" not in str(capture.events)


def _capture_of(client) -> Capture | None:
    """Keep the production client's capture identity private to the observer."""
    slot = client._observer
    return slot.target.capture if slot is not None else None


def test_production_client_identity_guard_and_disabled_allocation() -> None:
    client = make_client()
    assert _capture_of(client) is None
    assert client._observer is None
    client.finalize_observer(None)
    client.finalize_observer(HilClientTracer(_capture()))
    client.detach_observer(HilClientTracer(_capture()))
    assert client._observer is None

    a, b = HilClientTracer(_capture()), HilClientTracer(_capture())
    client.attach_observer(a)
    assert _capture_of(client) is a.capture
    client.finalize_observer(None)
    client.finalize_observer(b)
    client.detach_observer(b)
    assert _capture_of(client) is a.capture
    client.attach_observer(b)
    assert _capture_of(client) is b.capture
    client.finalize_observer(a)
    client.detach_observer(a)
    assert _capture_of(client) is b.capture
    client.detach_observer(b)
    assert _capture_of(client) is None
    assert client._observer is None


def test_generic_observer_slot_isolated_from_transport_failures() -> None:
    """A faulty observer cannot change BLE results or fabricate correlation IDs."""
    broken = MagicMock(spec=ClientObserver)
    broken.connection.side_effect = RuntimeError("observer error")
    broken.new_transaction_id.side_effect = RuntimeError("allocation error")
    slot = ObserverSlot(broken)
    slot.emit("connection", "connected", 4)
    broken.connection.side_effect = asyncio.CancelledError()
    slot.emit("connection", "connected", 4)
    broken.new_transaction_id.side_effect = asyncio.CancelledError()
    assert slot.transaction_id() is None
    broken.new_transaction_id.side_effect = RuntimeError("allocation error")
    assert slot.transaction_id() is None
    broken.new_transaction_id.side_effect = None
    broken.new_transaction_id.return_value = "tx-success"
    assert slot.transaction_id() == "tx-success"
    slot.emit("control_rejection", 4, "output", "stale_telemetry")
    broken.control_rejection.assert_called_once_with(4, "output", "stale_telemetry")
    assert [call.args for call in broken.observation_failed.call_args_list] == [
        ("connection",),
        ("connection",),
        ("new_transaction_id",),
        ("new_transaction_id",),
    ]


def test_runtime_transaction_snapshot_mapping_preserves_original_objects() -> None:
    """A frozen minimal transaction view excludes real control target fields."""
    from custom_components.allpowers_ble.client import (
        PendingOutputTransaction,
        PendingSettingsTransaction,
        TransactionPhase,
    )
    from tests.helpers import settings

    client = make_client()
    capture = _capture()
    tracer = HilClientTracer(capture)
    output = PendingOutputTransaction(
        session_generation=9,
        source_version=1,
        target_dc=True,
        target_ac=False,
        target_light=False,
        sent_monotonic=7,
        confirm_deadline_monotonic=20,
        phase=TransactionPhase.SENDING,
        observation_id="tx-old-1",
    )
    setting = PendingSettingsTransaction(
        session_generation=8,
        source_version=2,
        target=settings(),
        sent_monotonic=7,
        confirm_deadline_monotonic=9,
        phase=TransactionPhase.AWAITING_CONFIRMATION,
        observation_id=None,
    )
    client._pending_output_transaction = output
    client._pending_settings_transaction = setting
    client.attach_observer(tracer)
    assert [
        (row["transaction_kind"], row["transaction_id"]) for row in capture.events
    ] == [("output", "tx-old-1"), ("settings", None)]
    output_view, settings_view = client._pending_observations()
    assert output_view == TransactionObservation(9, "tx-old-1", "output", "sending", 20)
    assert settings_view == TransactionObservation(
        8, None, "settings", "awaiting_confirmation", 9
    )
    assert tuple(f.name for f in fields(TransactionObservation)) == (
        "generation",
        "transaction_id",
        "kind",
        "phase",
        "deadline",
    )
    assert not hasattr(output_view, "target_dc")
    assert not hasattr(settings_view, "target")
    with pytest.raises(FrozenInstanceError):
        output_view.phase = "confirmed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        settings_view.deadline = 1.0  # type: ignore[misc]

    client._observe_transaction("transaction_submitted", output)
    client._observe_transaction(
        "transaction_failed", setting, reason="gatt_write_failed"
    )
    assert [row["transaction_phase"] for row in capture.events[2:]] == [
        "sending",
        "awaiting_confirmation",
    ]
    assert [row["transaction_kind"] for row in capture.events[2:]] == [
        "output",
        "settings",
    ]
    tracer.finalize_runtime(output_view, settings_view, now=15, disconnected=True)
    assert [row["event"] for row in capture.events[-2:]] == [
        "transaction_abandoned",
        "transaction_timed_out",
    ]
    assert [row["reason"] for row in capture.events[-2:]] == [
        "session_disconnected",
        "deadline_passed",
    ]
    assert client._pending_output_transaction is output
    assert client._pending_settings_transaction is setting
    assert output.observation_id == "tx-old-1"
    assert setting.observation_id is None


def test_runtime_observer_handles_empty_and_confirmed_pending() -> None:
    """Attach/finalize skip absent and confirmed transactions without mutation."""
    from custom_components.allpowers_ble.client import (
        PendingOutputTransaction,
        TransactionPhase,
    )

    capture = _capture()
    tracer = HilClientTracer(capture)
    tracer.attach_runtime(None, None)
    tracer.finalize_runtime(None, None, now=5, disconnected=True)
    confirmed = PendingOutputTransaction(
        session_generation=1,
        source_version=0,
        target_dc=True,
        target_ac=True,
        target_light=False,
        sent_monotonic=1,
        confirm_deadline_monotonic=10,
        phase=TransactionPhase.CONFIRMED,
        observation_id="tx-confirmed",
    )
    view = make_client()._transaction_observation(confirmed)
    tracer.attach_runtime(view, None)
    tracer.finalize_runtime(view, None, now=1, disconnected=False)
    assert capture.events == []


def test_write_error_reason_is_private_value_only() -> None:
    """The observer classifies unexpected write outcomes without exception data."""
    capture = _capture()
    tracer = HilClientTracer(capture)
    for changed, cancelled, expected in (
        (True, True, "session_changed"),
        (False, True, "cancelled"),
        (False, False, "unexpected_failure"),
    ):
        tracer.write_unexpected_error(
            4,
            transaction_id="tx-4",
            error_type="RuntimeError",
            session_changed=changed,
            cancelled=cancelled,
        )
        assert capture.events[-1]["reason_category"] == expected
        assert capture.events[-1]["error_type"] == "RuntimeError"
    assert capture.frames == []

@pytest.mark.parametrize("invalid_id", [None, "", "   ", 0])
def test_invalid_observer_transaction_id_fails_closed(invalid_id) -> None:
    """No malformed identifier may silently produce a purportedly complete HIL."""
    capture = _capture()
    tracer = HilClientTracer(capture)
    slot = ObserverSlot(tracer)
    tracer.new_transaction_id = lambda: invalid_id  # type: ignore[method-assign]
    assert slot.transaction_id() is None
    assert capture.dropped_events == 1
    assert not capture.frames
    capture.stop()
    assert capture.snapshot()["complete"] is False


def test_observer_failure_handler_is_nonrecursive_and_nonleaking() -> None:
    """Even a failing failure-report hook cannot change BLE execution."""
    target = MagicMock(spec=ClientObserver)
    target.notification_received.side_effect = RuntimeError("PRIVATE:MAC")
    target.observation_failed.side_effect = asyncio.CancelledError()
    slot = ObserverSlot(target)
    slot.emit("notification_received", 3, bytes((1, 2, 3)))
    target.observation_failed.assert_called_once_with("notification_received")
    target.observation_failed.side_effect = RuntimeError("PRIVATE:IP")
    slot.emit("notification_received", 3, bytes((1, 2, 3)))
    assert target.observation_failed.call_count == 2


def test_late_observer_failures_never_modify_sealed_capture() -> None:
    """A sealed run is immutable even if its old observer is invoked late."""
    capture = _capture()
    tracer = HilClientTracer(capture)
    tracer.observation_failed("notification_received")
    assert capture.dropped_events == 1
    capture.stop("requested_stop")
    tracer.observation_failed("transaction_runtime")
    assert capture.dropped_events == 1
