"""Number entities for live ALLPOWERS BLE connection tuning."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_SETTINGS_KEEPALIVE_INTERVAL,
    CONF_STATUS_INTERVAL,
    MAX_SETTINGS_KEEPALIVE_INTERVAL,
    MAX_STATUS_INTERVAL,
    MIN_SETTINGS_KEEPALIVE_INTERVAL,
    MIN_STATUS_INTERVAL,
)
from .coordinator import AllpowersConfigEntry
from .entity import AllpowersEntity
from .options import ConnectionOptions


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up advanced runtime-tuning numbers."""
    del hass
    async_add_entities(
        (
            AllpowersStatusIntervalNumber(entry),
            AllpowersKeepaliveIntervalNumber(entry),
        )
    )


class _AllpowersOptionNumber(AllpowersEntity, NumberEntity):
    """Persist a number directly into config-entry options."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    option_key: str

    async def _async_store(self, value: float) -> None:
        options = dict(self._entry.options)
        options[self.option_key] = value
        try:
            normalized = ConnectionOptions.from_mapping(options)
        except (TypeError, ValueError) as ex:
            raise HomeAssistantError(str(ex)) from ex
        self.hass.config_entries.async_update_entry(
            self._entry, options=normalized.as_dict()
        )


class AllpowersStatusIntervalNumber(_AllpowersOptionNumber):
    """Status request interval in seconds."""

    _attr_translation_key = "status_interval"
    _attr_icon = "mdi:update"
    _attr_native_min_value = MIN_STATUS_INTERVAL
    _attr_native_max_value = MAX_STATUS_INTERVAL
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    option_key = CONF_STATUS_INTERVAL

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "status_interval")

    @property
    def native_value(self) -> float:
        """Return the current status interval."""
        return self.coordinator.client.options.status_interval

    async def async_set_native_value(self, value: float) -> None:
        """Persist the status interval."""
        await self._async_store(value)


class AllpowersKeepaliveIntervalNumber(_AllpowersOptionNumber):
    """Optional settings keepalive interval in minutes."""

    _attr_translation_key = "settings_keepalive_interval"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_native_min_value = MIN_SETTINGS_KEEPALIVE_INTERVAL / 60
    _attr_native_max_value = MAX_SETTINGS_KEEPALIVE_INTERVAL / 60
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    option_key = CONF_SETTINGS_KEEPALIVE_INTERVAL

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "settings_keepalive_interval")

    @property
    def native_value(self) -> float:
        """Return the current keepalive interval in minutes."""
        return self.coordinator.client.options.settings_keepalive_interval / 60

    async def async_set_native_value(self, value: float) -> None:
        """Persist the keepalive interval in protocol seconds."""
        await self._async_store(value * 60)
