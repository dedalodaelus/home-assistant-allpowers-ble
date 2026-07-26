"""Push coordinator for ALLPOWERS BLE state."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from time import monotonic
from typing import override

from homeassistant.const import CONF_ADDRESS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .client import AllpowersBLEClient
from .models import AllpowersSnapshot
from .options import ConnectionOptions

_LOGGER = logging.getLogger(__name__)


type AllpowersConfigEntry = ConfigEntry[AllpowersRuntimeData]


@dataclass(slots=True)
class AllpowersRuntimeData:
    """Runtime objects owned by one config entry."""

    client: AllpowersBLEClient
    coordinator: AllpowersCoordinator


class AllpowersCoordinator(DataUpdateCoordinator[AllpowersSnapshot]):
    """Bridge client push callbacks into CoordinatorEntity updates."""

    config_entry: AllpowersConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AllpowersConfigEntry,
        client: AllpowersBLEClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=None,
            always_update=False,
        )
        self.client = client
        self.client.set_update_callback(self._handle_client_update)
        self.async_set_updated_data(client.snapshot())

    async def async_start(self) -> None:
        """Start the active BLE client."""
        await self.client.async_start()

    async def async_wait_ready(self, timeout: float) -> None:
        """Wait for initial valid telemetry."""
        await self.client.async_wait_ready(timeout)

    async def async_shutdown(self) -> None:
        """Stop the active client and release callbacks."""
        self.client.set_update_callback(None)
        await self.client.async_stop()

    async def async_apply_options(self, options: ConnectionOptions) -> None:
        """Apply config-entry options live."""
        await self.client.async_apply_options(options)
        self.async_set_updated_data(self.client.snapshot())

    @override
    async def _async_update_data(self) -> AllpowersSnapshot:
        """Return cached push data; network I/O is handled by the client."""
        return self.client.snapshot()

    @callback
    def _handle_client_update(self) -> None:
        self.async_set_updated_data(self.client.snapshot())
        self._async_refresh_device_registry_metadata()

    @callback
    def _async_refresh_device_registry_metadata(self) -> None:
        """Refresh persisted version metadata when valid settings arrive or change."""
        settings = self.data.settings
        if settings is None:
            return

        address = str(self.config_entry.data.get(CONF_ADDRESS, "")).upper()
        if not address:
            return

        get_registry = getattr(dr, "async_get", None)
        if get_registry is None:
            return

        registry = get_registry(self.hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
        )
        if device is None:
            return

        updates: dict[str, str] = {}
        hardware_version = _validated_registry_version(
            settings.hardware_version,
            settings.raw_hardware_version,
        )
        firmware_version = _validated_registry_version(
            settings.firmware_version,
            settings.raw_firmware_version,
        )
        if hardware_version is not None and hardware_version != device.hw_version:
            updates["hw_version"] = hardware_version
        if firmware_version is not None and firmware_version != device.sw_version:
            updates["sw_version"] = firmware_version

        if updates:
            registry.async_update_device(device.id, **updates)

    @property
    def connected(self) -> bool:
        """Return whether the GATT connection is active."""
        return self.data.connected

    @property
    def status_is_fresh(self) -> bool:
        """Return whether status telemetry can safely drive entities."""
        return self.connected and _is_fresh(
            self.data.status_monotonic,
            self.client.options.stale_timeout,
        )

    @property
    def settings_are_fresh(self) -> bool:
        """Return whether settings can safely be modified."""
        return self.connected and _is_fresh(
            self.data.settings_monotonic,
            self.client.options.settings_stale_timeout,
        )

    @property
    def controls_available(self) -> bool:
        """Return whether output writes are currently safe."""
        return self.status_is_fresh

    @property
    def settings_controls_available(self) -> bool:
        """Return whether settings writes are currently safe."""
        return self.settings_are_fresh


def _is_fresh(timestamp: float | None, timeout: float) -> bool:
    return timestamp is not None and monotonic() - timestamp <= timeout


def _validated_registry_version(value: str | None, raw: int | None) -> str | None:
    """Return a stable registry version only for semantically valid bytes."""
    if not value:
        return None
    if not isinstance(raw, int) or raw == 0:
        return None

    high = raw >> 4
    low = raw & 0x0F
    if high > 9 or low > 9:
        return None

    normalized = value.strip()
    return normalized or None
