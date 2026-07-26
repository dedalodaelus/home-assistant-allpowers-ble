"""Button entities for ALLPOWERS BLE."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AllpowersConfigEntry
from .entity import (
    AllpowersEntity,
    AllpowersSettingsControlEntity,
    raise_command_error,
    runtime_model_support,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ALLPOWERS BLE buttons."""
    del hass
    support = runtime_model_support(entry.runtime_data.coordinator)
    entities: list[ButtonEntity] = [
        AllpowersRefreshButton(entry),
        AllpowersReconnectButton(entry),
    ]
    if support.capabilities.write_settings_keepalive:
        entities.append(AllpowersSettingsKeepaliveButton(entry))
    async_add_entities(tuple(entities))


class AllpowersRefreshButton(AllpowersEntity, ButtonEntity):
    """Request an immediate status notification."""

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "refresh")

    @property
    def available(self) -> bool:
        """Return whether a request can be written."""
        return self.coordinator.connected

    async def async_press(self) -> None:
        """Request status now."""
        try:
            await self.coordinator.client.async_request_status()
        except Exception as ex:
            raise_command_error(ex)


class AllpowersReconnectButton(AllpowersEntity, ButtonEntity):
    """Force a clean Bluetooth reconnect."""

    _attr_translation_key = "reconnect"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "reconnect")

    async def async_press(self) -> None:
        """Request reconnect."""
        try:
            await self.coordinator.client.async_reconnect("Reconnect requested by user")
        except Exception as ex:
            raise_command_error(ex)


class AllpowersSettingsKeepaliveButton(
    AllpowersSettingsControlEntity,
    ButtonEntity,
):
    """Re-send the current raw settings snapshot on demand."""

    _attr_translation_key = "settings_keepalive"
    _attr_icon = "mdi:send-clock-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "settings_keepalive")

    async def async_press(self) -> None:
        """Send the settings keepalive."""
        try:
            await self.coordinator.client.async_send_settings_keepalive()
        except Exception as ex:
            raise_command_error(ex)
