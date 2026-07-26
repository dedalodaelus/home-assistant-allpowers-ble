"""Shared entity base classes for ALLPOWERS BLE."""

from __future__ import annotations

import asyncio
from typing import NoReturn

from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS

from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import AllpowersClientError, NotConnectedError
from .const import DOMAIN, MANUFACTURER
from .coordinator import AllpowersConfigEntry, AllpowersCoordinator
from .model_support import ModelSupport, identify_model
from .protocol import StateUnavailableError


def _translated_command_error(key: str) -> HomeAssistantError:
    """Build a translatable Home Assistant command error."""
    try:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
        )
    except TypeError:
        # Compatibility fallback for runtimes where translation kwargs are unsupported.
        return HomeAssistantError(key)


def _state_error_key(error: StateUnavailableError) -> str:
    """Map state-guard failures to a stable user-facing translation key."""
    message = str(error).lower()
    if "confirmation timed out" in message or "state is ambiguous" in message:
        return "command_unconfirmed"
    if (
        "semantic validation" in message
        or "unsupported" in message
        or "disabled in integration options" in message
        or "reserved" in message
    ):
        return "command_unsupported"
    return "command_stale_state"


def raise_command_error(error: Exception) -> NoReturn:
    """Normalize command failures into translatable Home Assistant exceptions."""
    if isinstance(error, asyncio.CancelledError):
        raise error

    key = "command_transport"
    if isinstance(error, NotConnectedError):
        key = "command_disconnected"
    elif isinstance(error, TimeoutError):
        key = "command_timeout"
    elif isinstance(error, StateUnavailableError):
        key = _state_error_key(error)
    elif isinstance(error, AllpowersClientError) or isinstance(
        error, BLEAK_RETRY_EXCEPTIONS
    ):
        key = "command_transport"
    elif isinstance(error, (KeyError, ValueError)):
        key = "command_unsupported"

    raise _translated_command_error(key) from error


def runtime_model_support(coordinator: AllpowersCoordinator) -> ModelSupport:
    """Return revision-aware model support from the latest snapshot."""
    data = coordinator.data
    settings = data.settings
    return identify_model(
        data.advertised_name,
        hardware_version=settings.hardware_version if settings else None,
        raw_hardware_version=settings.raw_hardware_version if settings else None,
    )


class AllpowersEntity(CoordinatorEntity[AllpowersCoordinator]):
    """Base entity tied to one ALLPOWERS power station."""

    _attr_has_entity_name = True

    def __init__(self, entry: AllpowersConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._address = str(entry.data[CONF_ADDRESS]).upper()
        self._attr_unique_id = f"{self._address}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device-registry metadata from the latest protocol snapshot."""
        data = self.coordinator.data
        support = runtime_model_support(self.coordinator)
        settings = data.settings
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            manufacturer=MANUFACTURER,
            model=support.model,
            name=self._entry.title,
            hw_version=settings.hardware_version if settings else None,
            sw_version=settings.firmware_version if settings else None,
        )


class AllpowersStatusEntity(AllpowersEntity):
    """Entity backed by fresh status telemetry."""

    @property
    def available(self) -> bool:
        """Return availability based on telemetry freshness, not only connection."""
        return self.coordinator.status_is_fresh


class AllpowersSettingsEntity(AllpowersEntity):
    """Entity backed by fresh settings telemetry."""

    @property
    def available(self) -> bool:
        """Return availability based on settings freshness."""
        return self.coordinator.settings_are_fresh


class AllpowersOutputControlEntity(AllpowersEntity):
    """Output control requiring both connection and fresh status."""

    @property
    def available(self) -> bool:
        """Return whether a safe combined output command can be built."""
        support = runtime_model_support(self.coordinator)
        return (
            support.capabilities.write_output_controls
            and self.coordinator.controls_available
        )


class AllpowersSettingsControlEntity(AllpowersEntity):
    """Settings control requiring both connection and a fresh settings snapshot."""

    @property
    def available(self) -> bool:
        """Return whether a safe settings command can be built."""
        support = runtime_model_support(self.coordinator)
        return (
            support.capabilities.write_settings_controls
            and self.coordinator.settings_controls_available
        )
