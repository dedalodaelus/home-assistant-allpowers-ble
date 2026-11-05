"""Synchronous optional BLE observation API; independent of HIL and WebSocket."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol


class PendingTransactionView(Protocol):
    """Value-only read access to existing production transaction metadata."""

    @property
    def session_generation(self) -> int: ...

    @property
    def observation_id(self) -> str | None: ...

    @property
    def phase(self) -> Enum: ...

    @property
    def confirm_deadline_monotonic(self) -> float: ...


class ClientObserver(Protocol):
    """A non-owning observer: callbacks cannot perform BLE decisions."""

    def attach_runtime(
        self,
        output: PendingTransactionView | None,
        settings: PendingTransactionView | None,
    ) -> None: ...

    def finalize_runtime(
        self,
        output: PendingTransactionView | None,
        settings: PendingTransactionView | None,
        *,
        now: float,
        disconnected: bool,
    ) -> None: ...

    def stop_runtime(self, generation: int | None) -> None: ...

    def new_transaction_id(self) -> str: ...

    def connection(
        self,
        event: str,
        generation: int | None,
        *,
        stage: str | None = None,
        error_type: str | None = None,
        profile: str | None = None,
        classification: str | None = None,
    ) -> None: ...

    def control_rejection(
        self, generation: int | None, kind: str, reason: str
    ) -> None: ...

    def transaction_runtime(
        self,
        event: str,
        transaction: PendingTransactionView,
        *,
        reason: str | None = None,
    ) -> None: ...

    def notification_received(
        self, generation: int | None, data: bytearray
    ) -> None: ...

    def notification_decoded(
        self,
        generation: int | None,
        *,
        packet_types: tuple[str, ...],
        profile: str,
        hardware_revision: str | None,
        discarded_frames: int,
        discarded_bytes: int,
    ) -> None: ...

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
    ) -> None: ...

    def write_unexpected_error(
        self,
        generation: int | None,
        *,
        transaction_id: str | None,
        error_type: str,
        session_changed: bool,
        cancelled: bool,
    ) -> None: ...


class ObserverSlot:
    """Fail-isolated observer adapter with exact instance identity."""

    def __init__(self, target: ClientObserver) -> None:
        self.target = target

    def emit(self, method: str, /, *args: Any, **kwargs: Any) -> None:
        """An observer failure must never alter a BLE decision."""
        try:
            getattr(self.target, method)(*args, **kwargs)
        except Exception:
            return

    def transaction_id(self) -> str | None:
        """Missing observer correlation cannot interrupt an actual write."""
        try:
            return self.target.new_transaction_id()
        except Exception:
            return None
