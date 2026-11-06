"""Admin-scoped, single-use HIL tracing of HA-owned config-flow probes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
import re
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .hil_client import HilClientTracer
from .hil_trace import (
    KEY,
    SCHEMA_VERSION,
    TTL,
    Capture,
    CaptureStore,
    _authorized,
    _deny,
    _forget,
    _store,
)

REGISTRY_KEY = DOMAIN + "_hil_probe_candidates"
_ADDRESS = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
_STEPS = frozenset(("confirm", "user", "reconfigure"))
_OUTCOMES = {
    "success": ("probe_succeeded", "probe_finished"),
    "failure": ("probe_failed", "probe_finished"),
    "cancelled": ("probe_cancelled", "probe_finished"),
    "skipped": ("probe_skipped", "probe_skipped"),
    "interrupted": ("probe_interrupted", "probe_interrupted"),
}


@dataclass(slots=True)
class FlowCandidates:
    """Opaque server-generated selectors, never exported as BLE addresses."""

    step: str
    tokens: dict[str, str]
    deadline: float
    timer: asyncio.TimerHandle


class ProbeCapture(Capture):
    """A flow-bound private capture with no fabricated config entry."""

    def __init__(
        self,
        flow_id: str,
        target: str,
        candidate_token: str,
        owner: str,
        run_id: str,
        scenario_id: str,
        requested_route: str,
    ) -> None:
        super().__init__(None, owner, run_id, scenario_id, requested_route)
        self.flow_id = flow_id
        self.target_address: str | None = target
        self.candidate_token: str | None = candidate_token
        self.probe_state = "armed"
        self.probe_outcome: str | None = None

    def finish(self, outcome: str, *, reason: str | None = None) -> None:
        """Seal once; a stopped probe cannot be reattached or become complete."""
        if not self.active():
            return
        event, termination = _OUTCOMES[outcome]
        self.record(event, session_generation=1, reason_category=reason)
        self.probe_outcome = outcome
        self.probe_state = "finished"
        self.stop(termination)
        self.target_address = None
        self.candidate_token = None

    def snapshot(self) -> dict[str, Any]:
        """Report diagnostic integrity independently from a successful probe."""
        result = super().snapshot()
        complete = (
            self.termination_reason == "probe_finished"
            and not result["expired"]
            and not self.dropped_events
            and not self.dropped_frames
        )
        result.update(
            capture_scope="config_probe",
            flow_id=self.flow_id,
            probe_state=self.probe_state,
            probe_outcome=self.probe_outcome,
            capture_complete=complete,
            complete=complete,
        )
        return result

    def erase(self) -> None:
        """Scrub pending private target fields as well as recorded bytes."""
        self.target_address = None
        self.candidate_token = None
        super().erase()


def _registry(hass: HomeAssistant) -> dict[str, FlowCandidates]:
    records = hass.data.get(REGISTRY_KEY)
    if not isinstance(records, dict):
        records = {}
        hass.data[REGISTRY_KEY] = records
    return records


def register_flow_candidates(
    hass: HomeAssistant,
    flow_id: str | None,
    step: str,
    addresses: list[str],
) -> None:
    """Publish only currently selectable exact targets from a real HA form."""
    if not isinstance(flow_id, str) or not flow_id or step not in _STEPS:
        return
    valid = sorted(
        {
            address.upper()
            for address in addresses
            if isinstance(address, str) and _ADDRESS.fullmatch(address.upper())
        }
    )
    records = _registry(hass)
    old = records.get(flow_id)
    if old is not None and old.deadline > monotonic():
        if old.step == step and sorted(old.tokens.values()) == valid:
            return
    if old is not None:
        old.timer.cancel()
        records.pop(flow_id, None)
    if not valid:
        return
    tokens = {secrets.token_urlsafe(18): address for address in valid}
    deadline = monotonic() + TTL

    def expire() -> None:
        if records.get(flow_id) is record:
            del records[flow_id]
            if not records and hass.data.get(REGISTRY_KEY) is records:
                hass.data.pop(REGISTRY_KEY, None)

    record = FlowCandidates(
        step=step,
        tokens=tokens,
        deadline=deadline,
        timer=asyncio.get_running_loop().call_later(TTL, expire),
    )
    records[flow_id] = record


def _active_flow(hass: HomeAssistant, flow_id: str) -> dict[str, Any] | None:
    """Verify the public FlowManager API, not private handler internals."""
    try:
        manager = hass.config_entries.flow
        detail = manager.async_get(flow_id)
        if not isinstance(detail, dict) or detail.get("handler") != DOMAIN:
            return None
        for item in manager.async_progress():
            if (
                isinstance(item, dict)
                and item.get("flow_id") == flow_id
                and item.get("handler") == DOMAIN
            ):
                return item
    except AttributeError, KeyError, ValueError:
        return None
    return None


def _eligible(hass: HomeAssistant, flow_id: str) -> FlowCandidates | None:
    record = _registry(hass).get(flow_id)
    if (
        record is None
        or monotonic() >= record.deadline
        or _active_flow(hass, flow_id) is None
    ):
        return None
    return record


def consume_probe(
    hass: HomeAssistant, flow_id: str | None, address: str
) -> HilClientTracer | None:
    """Claim one arm only for the actual selected flow and exact target."""
    if not isinstance(flow_id, str):
        return None
    store = hass.data.get(KEY)
    if not isinstance(store, CaptureStore):
        return None
    capture = store.active
    record = _eligible(hass, flow_id)
    if (
        not isinstance(capture, ProbeCapture)
        or capture.flow_id != flow_id
        or capture.probe_state != "armed"
        or capture.closed
        or capture.target_address != address.upper()
        or record is None
        or record.tokens.get(capture.candidate_token or "") != address.upper()
    ):
        return None
    capture.probe_state = "probing"
    capture.record("probe_started", session_generation=1)
    return HilClientTracer(capture)


def finish_probe(
    tracer: HilClientTracer | None,
    outcome: str,
    *,
    reason: str | None = None,
) -> None:
    """Complete evidence without altering the actual config-flow result."""
    if tracer is not None and isinstance(tracer.capture, ProbeCapture):
        tracer.capture.finish(outcome, reason=reason)


def skip_probe(hass: HomeAssistant, flow_id: str | None, address: str) -> None:
    """Explicitly mark the no-GATT active-session-reuse path as skipped."""
    tracer = consume_probe(hass, flow_id, address)
    finish_probe(tracer, "skipped", reason="active_session_reused")


def _probe_lookup(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> ProbeCapture | None:
    store = hass.data.get(KEY)
    capture = store.find(msg["capture_id"]) if isinstance(store, CaptureStore) else None
    if (
        not isinstance(capture, ProbeCapture)
        or capture.flow_id != msg["flow_id"]
        or capture.user_id != connection.user.id
    ):
        _deny(connection, msg, "not_found")
        return None
    if not capture.retrievable():
        _forget(hass, store, capture)
        _deny(connection, msg, "expired")
        return None
    return capture


_LIST_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/probe/list",
}
_ARM_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/probe/arm",
    vol.Required("flow_id"): str,
    vol.Required("candidate_token"): str,
    vol.Required("run_id"): str,
    vol.Required("scenario_id"): str,
    vol.Required("requested_route"): str,
}
_FETCH_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/probe/fetch",
    vol.Required("flow_id"): str,
    vol.Required("capture_id"): str,
}
_STOP_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/probe/stop",
    vol.Required("flow_id"): str,
    vol.Required("capture_id"): str,
}


@websocket_api.websocket_command(_LIST_SCHEMA)
@websocket_api.async_response
async def ws_probe_list(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    if not _authorized(connection, msg):
        return
    found: list[dict[str, Any]] = []
    for flow_id, record in _registry(hass).items():
        progress = _active_flow(hass, flow_id)
        if record.deadline <= monotonic() or progress is None:
            continue
        context = progress.get("context")
        source = context.get("source") if isinstance(context, dict) else None
        safe_source = (
            source if source in ("bluetooth", "user", "reconfigure") else "other"
        )
        found.append(
            {
                "flow_id": flow_id,
                "step": record.step,
                "source": safe_source,
                "candidates": [
                    {"token": token, "label": "Candidate " + str(i)}
                    for i, token in enumerate(record.tokens, 1)
                ],
            }
        )
    connection.send_result(msg["id"], {"flows": found})


@websocket_api.websocket_command(_ARM_SCHEMA)
@websocket_api.async_response
async def ws_probe_arm(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    if not _authorized(connection, msg):
        return
    record = _eligible(hass, msg["flow_id"])
    target = record.tokens.get(msg["candidate_token"]) if record is not None else None
    if target is None:
        _deny(connection, msg, "not_found")
        return
    if (
        re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", msg["run_id"]) is None
        or re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", msg["scenario_id"]) is None
        or msg["requested_route"] not in ("auto", "local_adapter", "active_proxy")
    ):
        _deny(connection, msg, "invalid_format")
        return
    store = _store(hass)
    old = store.active
    if old is not None and old.active():
        _deny(connection, msg, "busy")
        return
    if old is not None:
        if old.entry_id is not None:
            entry = hass.config_entries.async_get_entry(old.entry_id)
            runtime = getattr(entry, "runtime_data", None)
            observer = store.observer_for(old)
            if runtime is not None and observer is not None:
                runtime.client.detach_observer(observer)
        _forget(hass, store, old)
        hass.data[KEY] = store
    capture = ProbeCapture(
        flow_id=msg["flow_id"],
        target=target,
        candidate_token=msg["candidate_token"],
        owner=connection.user.id,
        run_id=msg["run_id"],
        scenario_id=msg["scenario_id"],
        requested_route=msg["requested_route"],
    )
    store.active = capture

    def expire() -> None:
        _forget(hass, store, capture)

    capture._on_expire = expire
    capture._on_seal = store.seal
    capture._expiry_handle = asyncio.get_running_loop().call_later(TTL, expire)
    capture.record("probe_armed", session_generation=None)
    connection.send_result(
        msg["id"],
        {
            "capture_id": capture.capture_id,
            "schema_version": SCHEMA_VERSION,
            "ttl_seconds": TTL,
        },
    )


@websocket_api.websocket_command(_STOP_SCHEMA)
@websocket_api.async_response
async def ws_probe_stop(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    if not _authorized(connection, msg):
        return
    capture = _probe_lookup(hass, connection, msg)
    if capture is not None:
        if not capture.closed:
            capture.finish("interrupted", reason="requested_stop")
        connection.send_result(msg["id"], capture.snapshot())


@websocket_api.websocket_command(_FETCH_SCHEMA)
@websocket_api.async_response
async def ws_probe_fetch(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    if not _authorized(connection, msg):
        return
    capture = _probe_lookup(hass, connection, msg)
    if capture is not None:
        connection.send_result(msg["id"], capture.snapshot())


async def async_register(hass: HomeAssistant) -> None:
    """Expose explicit admin-only probe commands; nothing is armed by default."""
    websocket_api.async_register_command(hass, ws_probe_list)
    websocket_api.async_register_command(hass, ws_probe_arm)
    websocket_api.async_register_command(hass, ws_probe_stop)
    websocket_api.async_register_command(hass, ws_probe_fetch)
