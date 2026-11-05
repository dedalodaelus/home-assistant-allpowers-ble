"""Synchronous, opt-in HIL observer for the existing BLE client boundaries.

Only precomputed, value-only state is accepted. The observer never opens a BLE
connection, decides transaction outcomes, or changes runtime pending state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .client_observer import TransactionObservation
from .const import NOTIFY_UUID, WRITE_UUID

if TYPE_CHECKING:
    from .hil_trace import Capture


# Preserve the trace-only name for existing unit contracts.
PendingTraceState = TransactionObservation


class HilClientTracer:
    """Format and dispatch optional HIL records without performing BLE I/O."""

    def __init__(self, capture: Capture) -> None:
        self.capture = capture

    def matches(self, expected: Capture) -> bool:
        """Protect a newer attachment from an earlier capture's cleanup."""
        return self.capture is expected

    def attach_runtime(
        self,
        output: TransactionObservation | None,
        settings: TransactionObservation | None,
    ) -> None:
        """Register outstanding immutable snapshots without changing BLE state."""
        self.attach(tuple(tx for tx in (output, settings) if tx is not None))

    def transaction_runtime(
        self,
        event: str,
        transaction: TransactionObservation,
        *,
        reason: str | None = None,
    ) -> None:
        """Record an already-decided transition from value-only fields."""
        self.transaction(event, transaction, reason=reason)

    def finalize_runtime(
        self,
        output: TransactionObservation | None,
        settings: TransactionObservation | None,
        *,
        now: float,
        disconnected: bool,
    ) -> None:
        """Report already-decided pending outcomes without touching BLE state."""
        self.finalize_pending(
            tuple(tx for tx in (output, settings) if tx is not None),
            now=now,
            disconnected=disconnected,
        )

    def observation_failed(self, method: str) -> None:
        """Record loss of evidence without raw data, error text or recursion."""
        del method
        if not self.capture.closed:
            self.capture.dropped_events += 1

    def attach(self, pending: tuple[TransactionObservation, ...]) -> None:
        """Record pre-existing TX identifiers without replaying their GATT writes."""
        for transaction in pending:
            if transaction.phase == "confirmed":
                continue
            self.capture.record(
                "transaction_inherited",
                session_generation=transaction.generation,
                transaction_id=transaction.transaction_id,
                transaction_kind=transaction.kind,
                transaction_phase=transaction.phase,
                origin="preexisting",
                attribution=(
                    "prior_capture"
                    if transaction.transaction_id is not None
                    else "unattributed"
                ),
            )

    def new_transaction_id(self) -> str:
        """Use the capture's private random correlation namespace."""
        return self.capture.new_transaction_id()

    def connection(
        self,
        event: str,
        generation: int | None,
        *,
        stage: str | None = None,
        error_type: str | None = None,
        profile: str | None = None,
        classification: str | None = None,
    ) -> None:
        """Emit safe connected/disconnected/failed-stage evidence."""
        if event == "model_profile_selected":
            self.capture.record(
                event,
                session_generation=generation,
                inferred_model_profile=profile,
                model_classification=classification,
            )
        elif event == "connect_failed":
            self.capture.record(
                event,
                session_generation=generation,
                reason_category=stage,
                exception_type=error_type,
            )
        elif event == "disconnected":
            self.capture.record(
                event,
                session_generation=generation,
                reason_category=stage,
            )
        else:
            self.capture.record(event, session_generation=generation)

    def control_rejection(self, generation: int | None, kind: str, reason: str) -> None:
        """Keep rejection categories separate from production safety decisions."""
        self.capture.record(
            "control_rejected",
            session_generation=generation,
            control_kind=kind,
            reason=reason,
        )

    def transaction(
        self, event: str, state: TransactionObservation, *, reason: str | None = None
    ) -> None:
        """Report an already-decided transaction transition."""
        self.capture.record(
            event,
            session_generation=state.generation,
            transaction_id=state.transaction_id,
            transaction_kind=state.kind,
            transaction_phase=state.phase,
            reason=reason,
        )

    def finalize_pending(
        self,
        pending: tuple[TransactionObservation, ...],
        *,
        now: float,
        disconnected: bool,
    ) -> None:
        """Annotate pending TX on stop/disconnect without mutating runtime state."""
        for transaction in pending:
            if transaction.phase == "confirmed":
                continue
            timed_out = now > transaction.deadline
            event = (
                "transaction_timed_out"
                if timed_out
                else "transaction_abandoned"
                if disconnected
                else "transaction_unresolved"
            )
            reason = (
                "deadline_passed"
                if timed_out
                else "session_disconnected"
                if disconnected
                else "deadline_open"
            )
            self.transaction(event, transaction, reason=reason)

    def notification_received(self, generation: int | None, data: bytes) -> None:
        """Retain raw RX solely in the bounded private capture."""
        self.capture.record(
            "notification_received",
            direction="rx",
            frame=data,
            characteristic_uuid=NOTIFY_UUID,
            session_generation=generation,
        )

    def notification_decoded(
        self,
        generation: int | None,
        *,
        packet_types: tuple[str, ...],
        profile: str,
        hardware_revision: str | None,
        discarded_frames: int,
        discarded_bytes: int,
    ) -> None:
        """Describe validated decoder results, not guesses about raw chunks."""
        self.capture.record(
            "notification_decoded",
            session_generation=generation,
            decoded_packets=len(packet_types),
            decoded_packet_types=list(packet_types),
            characteristic_uuid=NOTIFY_UUID,
            inferred_model_profile=profile,
            session_hardware_revision=hardware_revision,
            discarded_frames=discarded_frames,
            discarded_bytes=discarded_bytes,
        )

    def write(
        self,
        event: str,
        generation: int | None,
        *,
        transaction_id: str | None = None,
        frame: bytes | None = None,
        write_with_response: bool | None = None,
        error_type: str | None = None,
        reason_category: str | None = None,
    ) -> None:
        """Distinguish attempted, submitted, rejected, and failed GATT calls."""
        if event == "write_rejected":
            self.capture.record(
                event,
                direction="tx",
                frame=frame,
                session_generation=generation,
                transaction_id=transaction_id,
                reason="not_connected",
            )
        elif event == "write_attempt":
            self.capture.record(
                event,
                direction="tx",
                frame=frame,
                session_generation=generation,
                transaction_id=transaction_id,
                write_with_response=write_with_response,
                characteristic_uuid=WRITE_UUID,
            )
        elif event == "write_error":
            self.capture.record(
                event,
                session_generation=generation,
                transaction_id=transaction_id,
                error_type=error_type,
                reason_category=reason_category,
            )
        else:
            self.capture.record(
                event,
                session_generation=generation,
                transaction_id=transaction_id,
            )

    def write_unexpected_error(
        self,
        generation: int | None,
        *,
        transaction_id: str | None,
        error_type: str,
        session_changed: bool,
        cancelled: bool,
    ) -> None:
        """Assign finite error categories, without retaining the exception."""
        reason = (
            "session_changed"
            if session_changed
            else "cancelled"
            if cancelled
            else "unexpected_failure"
        )
        self.write(
            "write_error",
            generation,
            transaction_id=transaction_id,
            error_type=error_type,
            reason_category=reason,
        )

    def stop_runtime(self, generation: int | None) -> None:
        """Seal an interrupted runtime capture; never report a successful stop."""
        self.capture.record("client_stopped", session_generation=generation)
        self.capture.stop("runtime_stopped")
