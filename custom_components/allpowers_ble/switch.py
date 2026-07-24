"""Switch entities for ALLPOWERS BLE."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import NotConnectedError
from .coordinator import AllpowersConfigEntry
from .entity import (
    AllpowersOutputControlEntity,
    AllpowersSettingsControlEntity,
)
from .protocol import StateUnavailableError


@dataclass(frozen=True, kw_only=True)
class AllpowersSwitchDescription(SwitchEntityDescription):
    """Describe an ALLPOWERS switch."""

    value_fn: Callable[[AllpowersConfigEntry], bool | None]
    command_fn: Callable[[AllpowersConfigEntry, bool], Awaitable[None]]
    settings_control: bool = False


SWITCH_DESCRIPTIONS: tuple[AllpowersSwitchDescription, ...] = (
    AllpowersSwitchDescription(
        key="ac_output",
        translation_key="ac_output",
        icon="mdi:power-socket",
        value_fn=lambda entry: (
            entry.runtime_data.coordinator.data.status.ac_enabled
            if entry.runtime_data.coordinator.data.status
            else None
        ),
        command_fn=lambda entry, enabled: entry.runtime_data.client.async_set_ac(
            enabled
        ),
    ),
    AllpowersSwitchDescription(
        key="dc_output",
        translation_key="dc_output",
        icon="mdi:current-dc",
        value_fn=lambda entry: (
            entry.runtime_data.coordinator.data.status.dc_enabled
            if entry.runtime_data.coordinator.data.status
            else None
        ),
        command_fn=lambda entry, enabled: entry.runtime_data.client.async_set_dc(
            enabled
        ),
    ),
    AllpowersSwitchDescription(
        key="light",
        translation_key="light",
        icon="mdi:lightbulb",
        value_fn=lambda entry: (
            entry.runtime_data.coordinator.data.status.light_enabled
            if entry.runtime_data.coordinator.data.status
            else None
        ),
        command_fn=lambda entry, enabled: entry.runtime_data.client.async_set_light(
            enabled
        ),
    ),
    AllpowersSwitchDescription(
        key="eco_mode",
        translation_key="eco_mode",
        icon="mdi:leaf",
        settings_control=True,
        value_fn=lambda entry: (
            entry.runtime_data.coordinator.data.settings.eco_enabled
            if entry.runtime_data.coordinator.data.settings
            else None
        ),
        command_fn=lambda entry, enabled: entry.runtime_data.client.async_set_eco(
            enabled
        ),
    ),
    AllpowersSwitchDescription(
        key="car_charger",
        translation_key="car_charger",
        icon="mdi:car-battery",
        settings_control=True,
        entity_registry_enabled_default=False,
        value_fn=lambda entry: (
            entry.runtime_data.coordinator.data.settings.car_charger_enabled
            if entry.runtime_data.coordinator.data.settings
            else None
        ),
        command_fn=lambda entry, enabled: (
            entry.runtime_data.client.async_set_car_charger(enabled)
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ALLPOWERS BLE switches."""
    del hass
    async_add_entities(
        AllpowersSettingsSwitch(entry, description)
        if description.settings_control
        else AllpowersOutputSwitch(entry, description)
        for description in SWITCH_DESCRIPTIONS
    )


class _AllpowersSwitchMixin:
    """Shared switch implementation."""

    entity_description: AllpowersSwitchDescription
    _entry: AllpowersConfigEntry

    @property
    def is_on(self) -> bool | None:
        """Return the current switch state."""
        return self.entity_description.value_fn(self._entry)

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn on the feature."""
        del kwargs
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn off the feature."""
        del kwargs
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self.entity_description.command_fn(self._entry, enabled)
        except (StateUnavailableError, NotConnectedError) as ex:
            raise HomeAssistantError(str(ex)) from ex


class AllpowersOutputSwitch(
    AllpowersOutputControlEntity,
    _AllpowersSwitchMixin,
    SwitchEntity,
):
    """AC, DC, or light output switch."""

    def __init__(
        self,
        entry: AllpowersConfigEntry,
        description: AllpowersSwitchDescription,
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description
        self._entry = entry


class AllpowersSettingsSwitch(
    AllpowersSettingsControlEntity,
    _AllpowersSwitchMixin,
    SwitchEntity,
):
    """Settings-backed switch preserving unknown protocol bits."""

    def __init__(
        self,
        entry: AllpowersConfigEntry,
        description: AllpowersSwitchDescription,
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description
        self._entry = entry

    @property
    def available(self) -> bool:
        """Require a fresh settings snapshot and explicit opt-in when experimental."""
        if (
            self.entity_description.key == "car_charger"
            and not self.coordinator.client.options.enable_car_charger
        ):
            return False
        return self.coordinator.settings_controls_available
