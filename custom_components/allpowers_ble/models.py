"""Integration-level immutable state models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .protocol import SettingsData, StatusData


@dataclass(frozen=True, slots=True)
class ConnectionStatistics:
    """Connection and parser counters exposed through diagnostics."""

    connection_attempts: int = 0
    successful_connections: int = 0
    disconnects: int = 0
    reconnects: int = 0
    notifications: int = 0
    valid_packets: int = 0
    protocol_errors: int = 0
    write_errors: int = 0
    watchdog_resets: int = 0
    telemetry_watchdog_resets: int = 0
    transport_watchdog_resets: int = 0


@dataclass(frozen=True, slots=True)
class AllpowersSnapshot:
    """Complete state snapshot consumed by Home Assistant entities."""

    connected: bool
    status: StatusData | None
    settings: SettingsData | None
    status_monotonic: float | None
    settings_monotonic: float | None
    last_packet_monotonic: float | None
    rssi: int | None
    advertised_name: str
    last_connected_at: datetime | None
    last_disconnected_at: datetime | None
    last_packet_at: datetime | None
    last_error: str | None
    statistics: ConnectionStatistics
