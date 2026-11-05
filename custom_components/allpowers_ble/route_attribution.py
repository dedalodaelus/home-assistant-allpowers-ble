"""Defensive, connection-time attribution for HA 2026.10 Bluetooth backends.

Only a successfully connected HA Bleak wrapper is authoritative. Scanner
advertisements and the requested route are never evidence. All inspected
backend/scanner attributes are version-dependent and strictly optional.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

Route = Literal["local_adapter", "active_proxy", "unverified"]
BackendKind = Literal["bluez", "esphome", "unknown"]
Evidence = Literal[
    "bluez_device_path",
    "esphome_scanner_binding",
    "no_ha_wrapper",
    "not_connected",
    "missing_tracking",
    "local_backend_mismatch",
    "bluez_path_mismatch",
    "esphome_backend_mismatch",
    "esphome_scanner_mismatch",
    "unsupported_backend",
    "inspection_failed",
]


@dataclass(frozen=True, slots=True)
class ConnectedRoute:
    """Snapshot of verifiable connected backend facts, without object references."""

    observed_route: Route = "unverified"
    backend_kind: BackendKind = "unknown"
    evidence: Evidence = "missing_tracking"
    adapter_id: str | None = None
    proxy_id: str | None = None


def _backend_types() -> tuple[
    type[Any], type[Any], type[Any], type[Any] | None, type[Any] | None
]:
    """Import optional backend types only when a connection is established."""
    from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
    from habluetooth.scanner_bleak import HaScanner
    from habluetooth.wrappers import HaBleakClientWrapper

    try:
        from bleak_esphome.backend.client import ESPHomeClient
        from bleak_esphome.backend.scanner import ESPHomeScanner
    except ImportError:
        return HaBleakClientWrapper, HaScanner, BleakClientBlueZDBus, None, None
    return HaBleakClientWrapper, HaScanner, BleakClientBlueZDBus, ESPHomeScanner, ESPHomeClient


def connected_route(client: object) -> ConnectedRoute:
    """Read effective backend/scanner after connect(), never discovery candidates."""
    try:
        wrapper_type, local_scanner, bluez_type, proxy_scanner, proxy_type = (
            _backend_types()
        )
        if not isinstance(client, wrapper_type):
            return ConnectedRoute(evidence="no_ha_wrapper")
        if not client.is_connected:
            return ConnectedRoute(evidence="not_connected")
        backend = client._backend
        scanner = client._connected_scanner
        if backend is None or scanner is None:
            return ConnectedRoute(evidence="missing_tracking")
        if not backend.is_connected:
            return ConnectedRoute(evidence="not_connected")

        if isinstance(scanner, local_scanner):
            if not isinstance(backend, bluez_type):
                return ConnectedRoute(evidence="local_backend_mismatch")
            adapter = scanner.adapter
            if not isinstance(adapter, str) or re.fullmatch(r"hci[0-9]+", adapter) is None:
                return ConnectedRoute(evidence="bluez_path_mismatch")
            index = scanner.adapter_idx
            path = backend._device_path
            if (
                type(index) is not int
                or index != int(adapter[3:])
                or not isinstance(path, str)
                or re.fullmatch(r"/org/bluez/hci([0-9]+)/dev_[0-9A-Fa-f_]+", path)
                is None
                or path.split("/")[3] != adapter
            ):
                return ConnectedRoute(evidence="bluez_path_mismatch")
            return ConnectedRoute(
                "local_adapter", "bluez", "bluez_device_path", adapter_id=adapter
            )

        if proxy_scanner is not None and isinstance(scanner, proxy_scanner):
            if proxy_type is None or not isinstance(backend, proxy_type):
                return ConnectedRoute(evidence="esphome_backend_mismatch")
            source = scanner.source
            if (
                backend._scanner is not scanner
                or type(source) is not str
                or not source
                or len(source) > 128
                or not source.isprintable()
                or backend._source != source
            ):
                return ConnectedRoute(evidence="esphome_scanner_mismatch")
            return ConnectedRoute(
                "active_proxy", "esphome", "esphome_scanner_binding",
                proxy_id=source,
            )

        return ConnectedRoute(evidence="unsupported_backend")
    except (Exception, BaseException) as ex:
        # Suppress backend property errors, including cancellation, without
        # leaking private IDs or changing the production connection lifecycle.
        del ex
        return ConnectedRoute(evidence="inspection_failed")
