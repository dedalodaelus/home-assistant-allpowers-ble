"""Synchronous optional BLE observation API; independent of HIL and WebSocket."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .route_attribution import ConnectedRoute


@dataclass(frozen=True, slots=True)
class TransactionObservation:
    """Immutable, minimal transport outcome: no mutable production targets."""

    generation: int
    transaction_id: str | None
    kind: Literal["output", "settings"]
    phase: str
    deadline: float


class ClientObserver(Protocol):
    """A non-owning observer: callbacks cannot perform BLE decisions."""

    def attach_runtime(
        self,
        output: TransactionObservation | None,
        settings: TransactionObservation | None,
    ) -> None: ...

    def finalize_runtime(
        self,
        output: TransactionObservation | None,
        settings: TransactionObservation | None,
        *,
        now: float,
        disconnected: bool,
    ) -> None: ...

    def stop_runtime(self, generation: int | None) -> None: ...

    def new_transaction_id(self) -> str: ...

    def observation_failed(self, method: str) -> None:
        """Mark observer evidence incomplete without exposing exception details."""
        ...

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

    def route_connected(self, generation: int, route: ConnectedRoute) -> None: ...

    def probe_record(
        self,
        event: str,
        generation: int | None,
        *,
        reason_category: str | None = None,
    ) -> None: ...

    def control_rejection(
        self, generation: int | None, kind: str, reason: str
    ) -> None: ...

    def transaction_runtime(
        self,
        event: str,
        transaction: TransactionObservation,
        *,
        reason: str | None = None,
    ) -> None: ...

    def notification_received(self, generation: int | None, data: bytes) -> None: ...

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

    def _observation_failed(self, method: str) -> None:
        """Best-effort failure accounting; never call via emit or recurse."""
        try:
            self.target.observation_failed(method)
        except Exception, asyncio.CancelledError:
            pass

    def emit(self, method: str, /, *args: Any, **kwargs: Any) -> None:
        """An observer failure must never alter a BLE decision."""
        try:
            getattr(self.target, method)(*args, **kwargs)
        except Exception, asyncio.CancelledError:
            self._observation_failed(method)

    def transaction_id(self) -> str | None:
        """Invalid or unavailable observer correlation cannot interrupt a write."""
        try:
            value = self.target.new_transaction_id()
        except Exception, asyncio.CancelledError:
            self._observation_failed("new_transaction_id")
            return None
        if not isinstance(value, str) or not value.strip():
            self._observation_failed("new_transaction_id")
            return None
        return value
