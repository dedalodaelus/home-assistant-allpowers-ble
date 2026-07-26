"""Reusable fakes and state builders for integration adapter tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS

from custom_components.allpowers_ble.coordinator import (
    AllpowersCoordinator,
    AllpowersRuntimeData,
)
from custom_components.allpowers_ble.models import (
    AllpowersSnapshot,
    ConnectionStatistics,
)
from custom_components.allpowers_ble.options import ConnectionOptions
from custom_components.allpowers_ble.protocol import SettingsData, StatusData, WorkMode


ADDRESS = "AA:BB:CC:DD:EE:FF"


def status(**changes: Any) -> StatusData:
    """Build representative live telemetry."""
    values: dict[str, Any] = {
        "dc_enabled": True,
        "ac_enabled": False,
        "light_enabled": True,
        "battery_percent": 73,
        "input_power_w": 300,
        "output_power_w": 150,
        "remaining_minutes": 120,
        "raw_flags": 0x13,
    }
    values.update(changes)
    return StatusData(**values)


def settings(**changes: Any) -> SettingsData:
    """Build representative settings with undocumented bits present."""
    values: dict[str, Any] = {
        "eco_enabled": True,
        "work_mode": WorkMode.STANDARD,
        "car_charger_enabled": False,
        "eco_timeout_hours": 4,
        "hardware_version": "1.2",
        "firmware_version": "3.4",
        "raw_flags": 0xA3,
        "raw_hardware_version": 0x12,
        "raw_firmware_version": 0x34,
    }
    values.update(changes)
    return SettingsData(**values)


def snapshot(
    *,
    connected: bool = True,
    status_data: StatusData | None = None,
    settings_data: SettingsData | None = None,
    status_age: float = 0.0,
    settings_age: float = 0.0,
    last_error: str | None = "synthetic error",
) -> AllpowersSnapshot:
    """Build a complete immutable integration snapshot."""
    now = monotonic()
    status_value = status() if status_data is None else status_data
    settings_value = settings() if settings_data is None else settings_data
    timestamp = datetime.now(timezone.utc)
    return AllpowersSnapshot(
        connected=connected,
        status=status_value,
        settings=settings_value,
        status_monotonic=now - status_age if status_value is not None else None,
        settings_monotonic=(now - settings_age if settings_value is not None else None),
        last_packet_monotonic=now,
        rssi=-61,
        advertised_name="ALLPOWERS R600",
        last_connected_at=timestamp,
        last_disconnected_at=None,
        last_packet_at=timestamp,
        last_error=last_error,
        statistics=ConnectionStatistics(
            connection_attempts=3,
            successful_connections=2,
            disconnects=1,
            reconnects=1,
            notifications=10,
            valid_packets=9,
            parser_discards=3,
            protocol_errors=1,
            write_errors=2,
            watchdog_resets=1,
        ),
    )


class FakeIntegrationClient:
    """Coordinator-facing client fake recording all commands."""

    def __init__(
        self,
        state: AllpowersSnapshot | None = None,
        options: ConnectionOptions | None = None,
    ) -> None:
        self._snapshot = state or snapshot()
        self.options = options or ConnectionOptions()
        self.advertised_name = self._snapshot.advertised_name
        self.callback: Any = None
        self.calls: list[tuple[str, Any]] = []
        self.errors: dict[str, Exception] = {}
        self.started = False
        self.stopped = False

    def set_update_callback(self, callback: Any) -> None:
        self.callback = callback

    def snapshot(self) -> AllpowersSnapshot:
        return self._snapshot

    def set_snapshot(self, value: AllpowersSnapshot, *, emit: bool = True) -> None:
        self._snapshot = value
        if emit and self.callback is not None:
            self.callback()

    async def async_start(self) -> None:
        self.started = True
        self.calls.append(("start", None))

    async def async_stop(self) -> None:
        self.stopped = True
        self.calls.append(("stop", None))

    async def async_wait_ready(self, timeout: float) -> None:
        self.calls.append(("wait_ready", timeout))
        self._raise("wait_ready")

    async def async_apply_options(self, options: ConnectionOptions) -> None:
        self.options = options
        self.calls.append(("apply_options", options))
        self._raise("apply_options")

    async def async_request_status(self) -> None:
        self.calls.append(("request_status", None))
        self._raise("request_status")

    async def async_reconnect(self, reason: str) -> None:
        self.calls.append(("reconnect", reason))
        self._raise("reconnect")

    async def async_send_settings_keepalive(self) -> None:
        self.calls.append(("settings_keepalive", None))
        self._raise("settings_keepalive")

    async def async_set_ac(self, enabled: bool) -> None:
        self.calls.append(("set_ac", enabled))
        self._raise("set_ac")

    async def async_set_dc(self, enabled: bool) -> None:
        self.calls.append(("set_dc", enabled))
        self._raise("set_dc")

    async def async_set_light(self, enabled: bool) -> None:
        self.calls.append(("set_light", enabled))
        self._raise("set_light")

    async def async_set_eco(self, enabled: bool) -> None:
        self.calls.append(("set_eco", enabled))
        self._raise("set_eco")

    async def async_set_car_charger(self, enabled: bool) -> None:
        self.calls.append(("set_car_charger", enabled))
        self._raise("set_car_charger")

    async def async_set_work_mode(self, mode: WorkMode) -> None:
        self.calls.append(("set_work_mode", mode))
        self._raise("set_work_mode")

    async def async_set_eco_timeout(self, hours: int) -> None:
        self.calls.append(("set_eco_timeout", hours))
        self._raise("set_eco_timeout")

    def _raise(self, name: str) -> None:
        if error := self.errors.get(name):
            raise error


class FakeConfigEntriesManager:
    """Small Home Assistant config-entry manager fake."""

    def __init__(self) -> None:
        self.updates: list[tuple[ConfigEntry[Any], dict[str, Any]]] = []
        self.forwarded: list[tuple[ConfigEntry[Any], tuple[str, ...]]] = []
        self.unloaded: list[tuple[ConfigEntry[Any], tuple[str, ...]]] = []
        self.unload_result = True

    def async_update_entry(
        self,
        entry: ConfigEntry[Any],
        *,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        version: int | None = None,
        minor_version: int | None = None,
    ) -> None:
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)
            self.updates.append((entry, dict(options)))
        if version is not None:
            entry.version = version
        if minor_version is not None:
            entry.minor_version = minor_version

    async def async_forward_entry_setups(
        self,
        entry: ConfigEntry[Any],
        platforms: tuple[str, ...],
    ) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(
        self,
        entry: ConfigEntry[Any],
        platforms: tuple[str, ...],
    ) -> bool:
        self.unloaded.append((entry, platforms))
        return self.unload_result


class FakeBus:
    """Event-bus fake retaining one-shot callbacks."""

    def __init__(self) -> None:
        self.listeners: list[tuple[str, Any]] = []

    def async_listen_once(self, event_type: str, callback: Any) -> Any:
        self.listeners.append((event_type, callback))
        return lambda: None


class FakeHass:
    """Home Assistant fake used by adapter tests."""

    def __init__(self) -> None:
        self.config_entries = FakeConfigEntriesManager()
        self.bus = FakeBus()


def configured_entry(
    *,
    state: AllpowersSnapshot | None = None,
    options: ConnectionOptions | None = None,
    hass: FakeHass | None = None,
) -> tuple[
    ConfigEntry[AllpowersRuntimeData],
    FakeIntegrationClient,
    AllpowersCoordinator,
    FakeHass,
]:
    """Build a fully wired config entry and coordinator."""
    runtime_options = options or ConnectionOptions()
    entry: ConfigEntry[AllpowersRuntimeData] = ConfigEntry(
        title="ALLPOWERS R600 AABB",
        data={CONF_ADDRESS: ADDRESS, "device_name": "ALLPOWERS R600"},
        options=runtime_options.as_dict(),
    )
    fake_hass = hass or FakeHass()
    client = FakeIntegrationClient(state=state, options=runtime_options)
    coordinator = AllpowersCoordinator(fake_hass, entry, client)  # type: ignore[arg-type]
    entry.runtime_data = AllpowersRuntimeData(client=client, coordinator=coordinator)  # type: ignore[arg-type]
    return entry, client, coordinator, fake_hass


def disconnected_snapshot() -> AllpowersSnapshot:
    """Return cached data with the transport disconnected."""
    return replace(snapshot(), connected=False)


SERVICE_INFO = SimpleNamespace(
    name="ALLPOWERS R600",
    address=ADDRESS,
    rssi=-55,
    service_uuids=["0000fff0-0000-1000-8000-00805f9b34fb"],
    connectable=True,
)
