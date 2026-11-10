"""Admin-only bounded GATT trace capture, disabled unless explicitly started."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import OrderedDict
from datetime import UTC, datetime
import re
import secrets
import stat
from pathlib import Path
from time import monotonic
from typing import Any

import probatio as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .client_observer import ClientObserver
from .const import DOMAIN
from .hil_client import HilClientTracer
from .route_attribution import ConnectedRoute
from .protocol.codec import (
    HEADER,
    SETTINGS_WRITE_COMMAND,
    STATUS_COMMAND,
    STATUS_REQUEST,
    xor_checksum,
)

KEY = DOMAIN + "_hil_capture"
SCHEMA_VERSION = 2
TTL = 120
MAX_EVENTS = 256
MAX_FRAMES = 128
SEALED_RETRIEVAL_SECONDS = 30
MAX_SEALED_CAPTURES = 4
MAX_BUILD_FILES = 256
MAX_BUILD_ENTRIES = 1024
MAX_BUILD_FILE_BYTES = 2 * 1024 * 1024
MAX_BUILD_TOTAL_BYTES = 8 * 1024 * 1024


def classify_tx(frame: bytes | bytearray) -> str:
    """Only classify complete known TX wire formats, never arbitrary bytes."""
    if bytes(frame) == STATUS_REQUEST:
        return "status_request"
    if len(frame) not in (9, 10) or frame[:2] != HEADER:
        return "unknown"
    if frame[2] != 0 or xor_checksum(frame) or frame[3:5] != bytes((0xB1, 0x01)):
        return "unknown"
    if len(frame) == 9 and frame[5:7] == bytes((STATUS_COMMAND, 0)):
        return "output_control"
    if len(frame) == 10 and frame[5:7] == bytes((SETTINGS_WRITE_COMMAND, 2)):
        return "settings_control"
    return "unknown"


_START_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/start",
    vol.Required("entry_id"): str,
    vol.Required("run_id"): str,
    vol.Required("scenario_id"): str,
    vol.Required("requested_route"): str,
}

_STOP_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/stop",
    vol.Required("entry_id"): str,
    vol.Required("capture_id"): str,
}

_FETCH_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): "allpowers_ble/hil/fetch",
    vol.Required("entry_id"): str,
    vol.Required("capture_id"): str,
}

_BUILD_SCHEMA: dict[Any, Any] = {vol.Required("type"): "allpowers_ble/hil/build"}


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Fields that reveal replacement or mutation during a fingerprint scan."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _scan_build_tree(
    root: Path,
) -> tuple[
    dict[Path, tuple[int, ...]], dict[Path, tuple[tuple[str, tuple[int, ...]], ...]]
]:
    """Collect a bounded, path-safe snapshot or fail the entire scan."""
    if root.is_symlink() or not stat.S_ISDIR(root.lstat().st_mode):
        raise OSError("invalid integration directory")
    resolved_root = root.resolve(strict=True)
    files: dict[Path, tuple[int, ...]] = {}
    directories: dict[Path, tuple[tuple[str, tuple[int, ...]], ...]] = {}
    pending = [resolved_root]
    entries_seen = 0

    while pending:
        current = pending.pop()
        if current.is_symlink() or not current.resolve(strict=True).is_relative_to(
            resolved_root
        ):
            raise OSError("integration path escaped root")
        snapshot: list[tuple[str, tuple[int, ...]]] = []
        child_directories: list[Path] = []
        with os.scandir(current) as iterator:
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_BUILD_ENTRIES:
                    raise OSError("too many integration entries")
                name = entry.name
                path = current / name
                metadata = entry.stat(follow_symlinks=False)
                identity = _file_identity(metadata)
                if stat.S_ISLNK(metadata.st_mode):
                    raise OSError("symlink in integration")
                if stat.S_ISDIR(metadata.st_mode):
                    if name == "__pycache__":
                        continue
                    snapshot.append((name, identity))
                    child_directories.append(path)
                elif name.endswith(".pyc"):
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError("invalid generated integration file")
                else:
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError("invalid integration file")
                    if metadata.st_size > MAX_BUILD_FILE_BYTES:
                        raise OSError("integration file exceeds size limit")
                    snapshot.append((name, identity))
                    files[path] = identity
                    if len(files) > MAX_BUILD_FILES:
                        raise OSError("too many integration files")
        directories[current] = tuple(sorted(snapshot))
        pending.extend(reversed(sorted(child_directories)))
    if not files:
        raise OSError("empty integration")
    if sum(identity[3] for identity in files.values()) > MAX_BUILD_TOTAL_BYTES:
        raise OSError("integration exceeds total size limit")
    return files, directories


def _integration_fingerprint(root: Path | None = None) -> str:
    """Fingerprint packaged files with bounded I/O and race/change detection."""
    root = (root or Path(__file__).parent).absolute()
    if root.is_symlink():
        raise OSError("invalid integration directory")
    root = root.resolve(strict=True)
    files, directories = _scan_build_tree(root)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        expected = files[path]
        relative = path.relative_to(root).as_posix()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = _file_identity(os.fstat(stream.fileno()))
            if before != expected:
                raise OSError("integration file changed during scan")
            content = stream.read(MAX_BUILD_FILE_BYTES + 1)
            after = _file_identity(os.fstat(stream.fileno()))
        if len(content) != expected[3]:
            raise OSError("integration file changed or exceeds size limit")
        if after != before or _file_identity(path.lstat()) != expected:
            raise OSError("integration file changed during scan")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    current_files, current_directories = _scan_build_tree(root)
    if current_files != files or current_directories != directories:
        raise OSError("integration tree changed during scan")
    return digest.hexdigest()


@websocket_api.websocket_command(_BUILD_SCHEMA)
@websocket_api.async_response
async def ws_build(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Expose only the admin-authorized installed-file fingerprint."""
    if not _authorized(connection, msg):
        return
    try:
        fingerprint = await hass.async_add_executor_job(_integration_fingerprint)
    except Exception:
        connection.send_error(
            msg["id"], "scan_failed", "Integration fingerprint unavailable"
        )
        return
    connection.send_result(msg["id"], {"schema_version": 1, "fingerprint": fingerprint})


class Capture:
    """Ephemeral private trace; no disk or HA event-bus output."""

    def __init__(
        self,
        entry_id: str | None,
        user_id: str,
        run_id: str,
        scenario_id: str,
        requested_route: str,
    ) -> None:
        self.entry_id = entry_id
        self.user_id = user_id
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.requested_route = requested_route
        self.capture_id = secrets.token_urlsafe(18)
        self.started = monotonic()
        self.events: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.route_observations: dict[int, dict[str, Any]] = {}
        self.dropped_events = 0
        self.dropped_frames = 0
        self.closed = False
        self.termination_reason: str | None = None
        self.seq = 0
        self.transaction_seq = 0
        self._transaction_namespace = secrets.token_urlsafe(18)
        self.sealed_at: float | None = None
        self._on_expire: Any = None
        self._on_seal: Any = None
        self._expiry_handle: asyncio.TimerHandle | None = None

    def active(self) -> bool:
        if not self.closed and monotonic() - self.started >= TTL:
            self.closed = True
            self.termination_reason = "expired"
        return not self.closed

    def new_transaction_id(self) -> str:
        """Non-identifying, cross-capture-unique transaction correlation."""
        self.transaction_seq += 1
        return f"tx-{self._transaction_namespace}-{self.transaction_seq}"

    def retrievable(self) -> bool:
        """Keep a bounded sealed retry window even when stop nears the active TTL."""
        now = monotonic()
        if self.termination_reason in ("expired", "erased"):
            return False
        if self.sealed_at is not None:
            return now - self.sealed_at < SEALED_RETRIEVAL_SECONDS
        return now - self.started < TTL

    def observe_route(self, generation: int, route: ConnectedRoute) -> None:
        """Retain one immutable connected-route proof per successful generation."""
        if not self.active():
            return
        previous = self.route_observations.get(generation)
        if previous is not None:
            # A generation cannot change its scanner, backend or private source.
            old_identity = (
                previous["observed_route"],
                previous["backend_kind"],
                previous["evidence"],
                previous.get("adapter_id"),
                previous.get("proxy_id"),
            )
            new_identity = (
                route.observed_route,
                route.backend_kind,
                route.evidence,
                route.adapter_id,
                route.proxy_id,
            )
            if old_identity != new_identity:
                self.dropped_events += 1
            return
        if len(self.route_observations) >= MAX_EVENTS:
            self.dropped_events += 1
            return
        item: dict[str, Any] = {
            "session_generation": generation,
            "observed_route": route.observed_route,
            "backend_kind": route.backend_kind,
            "evidence": route.evidence,
        }
        if route.adapter_id is not None:
            item["adapter_id"] = route.adapter_id
        if route.proxy_id is not None:
            item["proxy_id"] = route.proxy_id
        self.route_observations[generation] = item
        self.record(
            "route_observed",
            session_generation=generation,
            backend_kind=route.backend_kind,
            evidence=route.evidence,
        )
        if self.events and self.events[-1]["seq"] == self.seq:
            last_row = self.events[-1]
            item.update({key: last_row[key] for key in ("seq", "utc", "elapsed_ms")})
        else:
            item["seq"] = self.seq
            item["utc"] = datetime.now(UTC).isoformat()
            item["elapsed_ms"] = round((monotonic() - self.started) * 1000, 3)

    @staticmethod
    def summary_route(routes: list[str]) -> str:
        """Unknown connected generations veto an otherwise verified summary."""
        if not routes or "unverified" in routes:
            return "unverified"
        unique = set(routes)
        return unique.pop() if len(unique) == 1 else "mixed"

    def record(
        self,
        event: str,
        *,
        frame: bytes | bytearray | None = None,
        direction: str | None = None,
        **fields: Any,
    ) -> None:
        """Capture failures never block or alter BLE traffic."""
        try:
            if not self.active():
                return
            self.seq += 1
            generation = fields.pop("session_generation", None)
            known = self.route_observations.get(generation)
            row: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "seq": self.seq,
                "utc": datetime.now(UTC).isoformat(),
                "elapsed_ms": round((monotonic() - self.started) * 1000, 3),
                "run_id": self.run_id,
                "scenario_id": self.scenario_id,
                "event": event,
                "session_generation": generation,
                "layer": "gatt_api",
                "observed_route": (
                    known["observed_route"] if known is not None else "unverified"
                ),
                **fields,
            }
            if frame is not None:
                if len(self.frames) >= MAX_FRAMES or len(frame) > 256:
                    self.dropped_frames += 1
                    row["frame_omitted"] = True
                else:
                    self.frames.append(
                        {
                            **row,
                            "direction": direction,
                            "hex": bytes(frame).hex(),
                            "command_type": (
                                classify_tx(frame) if direction == "tx" else "unknown"
                            ),
                            "frame_validation": (
                                "validated_tx"
                                if direction == "tx" and classify_tx(frame) != "unknown"
                                else "unvalidated_chunk"
                            ),
                        }
                    )
            if len(self.events) >= MAX_EVENTS:
                self.dropped_events += 1
            else:
                self.events.append(row)
        except Exception:
            self.dropped_events += 1

    def stop(self, reason: str = "requested_stop") -> None:
        """Seal once and rearm the same private erase timer for every caller."""
        if not self.active():
            return
        self.sealed_at = monotonic()
        self.termination_reason = reason
        self.closed = True
        if self._on_expire is not None:
            if self._expiry_handle is not None:
                self._expiry_handle.cancel()
            self._expiry_handle = asyncio.get_running_loop().call_later(
                SEALED_RETRIEVAL_SECONDS, self._on_expire
            )
        if self._on_seal is not None:
            self._on_seal(self)

    def erase(self) -> None:
        """Destroy transient private bytes on TTL or config-entry teardown."""
        self.closed = True
        self.termination_reason = "erased"
        self.sealed_at = monotonic()
        self.events.clear()
        self.frames.clear()
        self.route_observations.clear()
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None

    def snapshot(self) -> dict[str, Any]:
        self.active()
        expired = not self.retrievable()
        return {
            "schema_version": SCHEMA_VERSION,
            "capture_id": self.capture_id,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "event_count": len(self.events),
            "frame_count": len(self.frames),
            "requested_route": self.requested_route,
            "observed_route": self.summary_route(
                [item["observed_route"] for item in self.route_observations.values()]
            ),
            "route_observations": [
                item.copy() for item in self.route_observations.values()
            ],
            "capture_layer": "gatt_api_not_rf",
            "private_raw_bytes": True,
            "qualification_allowed": False,
            "has_gatt_frames": bool(self.frames),
            "closed": self.closed,
            "termination_reason": self.termination_reason,
            "expired": expired,
            "complete": (
                self.termination_reason == "requested_stop"
                and not expired
                and not self.dropped_events
                and not self.dropped_frames
            ),
            "dropped_events": self.dropped_events,
            "dropped_frames": self.dropped_frames,
            "events": [row.copy() for row in self.events],
            "frames": [row.copy() for row in self.frames],
        }


class CaptureStore:
    """One active trace plus at most four privately retrievable sealed traces."""

    def __init__(self) -> None:
        self.active: Capture | None = None
        self.active_observer: ClientObserver | None = None
        self.sealed: OrderedDict[str, Capture] = OrderedDict()

    def observer_for(self, capture: Capture) -> ClientObserver | None:
        """Only the active capture can own an attached observer."""
        return self.active_observer if self.active is capture else None

    def find(self, capture_id: str) -> Capture | None:
        """Look up an active or sealed capture without changing its retention."""
        if self.active is not None and self.active.capture_id == capture_id:
            return self.active
        return self.sealed.get(capture_id)

    def remove(self, capture: Capture) -> None:
        """Remove one exact capture, never another with a newer ID."""
        if self.active is capture:
            self.active = None
            self.active_observer = None
        if self.sealed.get(capture.capture_id) is capture:
            del self.sealed[capture.capture_id]

    def seal(self, capture: Capture) -> None:
        """Retain independent retries; FIFO evicts only the oldest sealed run."""
        self.remove(capture)
        self.sealed[capture.capture_id] = capture
        if len(self.sealed) > MAX_SEALED_CAPTURES:
            _, oldest = self.sealed.popitem(last=False)
            oldest.erase()

    def empty(self) -> bool:
        return self.active is None and not self.sealed


def _store(hass: HomeAssistant) -> CaptureStore:
    store = hass.data.get(KEY)
    if not isinstance(store, CaptureStore):
        store = CaptureStore()
        hass.data[KEY] = store
    return store


def _forget(hass: HomeAssistant, store: CaptureStore, capture: Capture) -> None:
    """Erase only the requested capture and keep unrelated active runs."""
    store.remove(capture)
    capture.erase()
    if store.empty() and hass.data.get(KEY) is store:
        hass.data.pop(KEY, None)


def _deny(connection: Any, msg: dict[str, Any], code: str) -> None:
    connection.send_error(msg["id"], code, "HIL request rejected")


def _authorized(connection: Any, msg: dict[str, Any]) -> bool:
    if not getattr(connection.user, "is_admin", False):
        _deny(connection, msg, "unauthorized")
        return False
    return True


def _lookup(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> Capture | None:
    store = hass.data.get(KEY)
    if not isinstance(store, CaptureStore):
        _deny(connection, msg, "not_found")
        return None
    capture = store.find(msg["capture_id"])
    if (
        capture is None
        or capture.entry_id is None
        or capture.entry_id != msg["entry_id"]
        or capture.user_id != connection.user.id
    ):
        _deny(connection, msg, "not_found")
        return None
    if not capture.retrievable():
        entry = hass.config_entries.async_get_entry(capture.entry_id)
        runtime = getattr(entry, "runtime_data", None)
        observer = store.observer_for(capture)
        if runtime is not None and observer is not None:
            runtime.client.detach_observer(observer)
        _forget(hass, store, capture)
        _deny(connection, msg, "expired")
        return None
    return capture


@websocket_api.websocket_command(_START_SCHEMA)
@websocket_api.async_response
async def ws_start(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if not _authorized(connection, msg):
        return
    if (
        not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", msg["run_id"])
        or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", msg["scenario_id"])
        or msg["requested_route"] not in ("auto", "local_adapter", "active_proxy")
    ):
        _deny(connection, msg, "invalid_format")
        return
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        _deny(connection, msg, "not_found")
        return
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        _deny(connection, msg, "not_ready")
        return
    store = _store(hass)
    old = store.active
    if old is not None and old.active():
        _deny(connection, msg, "busy")
        return
    if old is not None:
        old_entry = (
            hass.config_entries.async_get_entry(old.entry_id)
            if old.entry_id is not None
            else None
        )
        old_runtime = getattr(old_entry, "runtime_data", None)
        old_observer = store.observer_for(old)
        if old_runtime is not None and old_observer is not None:
            old_runtime.client.detach_observer(old_observer)
        _forget(hass, store, old)
        # Expired A may have removed the empty store; B must re-register it.
        hass.data[KEY] = store
    capture = Capture(
        entry.entry_id,
        connection.user.id,
        msg["run_id"],
        msg["scenario_id"],
        msg["requested_route"],
    )
    observer = HilClientTracer(capture)
    store.active = capture
    store.active_observer = observer

    def expire() -> None:
        runtime.client.detach_observer(observer)
        _forget(hass, store, capture)

    capture._on_expire = expire
    capture._on_seal = store.seal
    capture._expiry_handle = asyncio.get_running_loop().call_later(TTL, expire)
    capture.record(
        "capture_started",
        session_generation=runtime.client._active_session_generation,
    )
    runtime.client.attach_observer(observer)
    connection.send_result(
        msg["id"],
        {
            "capture_id": capture.capture_id,
            "ttl_seconds": TTL,
            "schema_version": SCHEMA_VERSION,
        },
    )


@websocket_api.websocket_command(_STOP_SCHEMA)
@websocket_api.async_response
async def ws_stop(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if not _authorized(connection, msg):
        return
    capture = _lookup(hass, connection, msg)
    if capture is None or capture.entry_id is None:
        return
    entry = hass.config_entries.async_get_entry(capture.entry_id)
    runtime = getattr(entry, "runtime_data", None)
    store = hass.data.get(KEY)
    observer = store.observer_for(capture) if isinstance(store, CaptureStore) else None
    if not capture.closed:
        if runtime is not None and observer is not None:
            runtime.client.finalize_observer(observer)
        capture.record("capture_stopped")
        capture.stop("requested_stop")
    if runtime is not None and observer is not None:
        runtime.client.detach_observer(observer)
    connection.send_result(msg["id"], capture.snapshot())


@websocket_api.websocket_command(_FETCH_SCHEMA)
@websocket_api.async_response
async def ws_fetch(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if not _authorized(connection, msg):
        return
    capture = _lookup(hass, connection, msg)
    if capture is not None:
        connection.send_result(msg["id"], capture.snapshot())


async def async_register(hass: HomeAssistant) -> None:
    """Register opt-in commands without enabling capture."""
    websocket_api.async_register_command(hass, ws_start)
    websocket_api.async_register_command(hass, ws_stop)
    websocket_api.async_register_command(hass, ws_fetch)
    websocket_api.async_register_command(hass, ws_build)


def clear_entry_capture(hass: HomeAssistant, entry_id: str) -> None:
    """Erase all active/sealed evidence on this entry's unload or reload."""
    store = hass.data.get(KEY)
    if not isinstance(store, CaptureStore):
        return
    candidates: list[Capture] = list(store.sealed.values())
    active = store.active
    if active is not None:
        candidates.append(active)
    entry = hass.config_entries.async_get_entry(entry_id)
    runtime = getattr(entry, "runtime_data", None)
    for capture in candidates:
        if capture.entry_id != entry_id:
            continue
        observer = store.observer_for(capture)
        if runtime is not None and observer is not None:
            runtime.client.detach_observer(observer)
        _forget(hass, store, capture)
