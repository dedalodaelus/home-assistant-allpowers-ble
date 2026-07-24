"""Binary sensor entities for ALLPOWERS BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AllpowersConfigEntry, AllpowersCoordinator
from .entity import AllpowersEntity
from .models import AllpowersSnapshot


@dataclass(frozen=True, kw_only=True)
class AllpowersBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an ALLPOWERS binary sensor."""

    value_fn: Callable[[AllpowersSnapshot, AllpowersCoordinator], bool | None]
    available_fn: Callable[[AllpowersCoordinator], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[AllpowersBinarySensorDescription, ...] = (
    AllpowersBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, coordinator: data.connected,
        available_fn=lambda coordinator: True,
    ),
    AllpowersBinarySensorDescription(
        key="telemetry_available",
        translation_key="telemetry_available",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, coordinator: coordinator.status_is_fresh,
        available_fn=lambda coordinator: True,
    ),
    AllpowersBinarySensorDescription(
        key="settings_available",
        translation_key="settings_available",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, coordinator: coordinator.settings_are_fresh,
        available_fn=lambda coordinator: True,
    ),
    AllpowersBinarySensorDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda data, coordinator: (
            data.status.input_power_w > 0 if data.status else None
        ),
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    AllpowersBinarySensorDescription(
        key="discharging",
        translation_key="discharging",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda data, coordinator: (
            data.status.output_power_w > 0 if data.status else None
        ),
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    AllpowersBinarySensorDescription(
        key="ac_output",
        translation_key="ac_output",
        value_fn=lambda data, coordinator: (
            data.status.ac_enabled if data.status else None
        ),
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    AllpowersBinarySensorDescription(
        key="dc_output",
        translation_key="dc_output",
        value_fn=lambda data, coordinator: (
            data.status.dc_enabled if data.status else None
        ),
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    AllpowersBinarySensorDescription(
        key="light_output",
        translation_key="light_output",
        value_fn=lambda data, coordinator: (
            data.status.light_enabled if data.status else None
        ),
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ALLPOWERS BLE binary sensors."""
    del hass
    async_add_entities(
        AllpowersBinarySensor(entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class AllpowersBinarySensor(AllpowersEntity, BinarySensorEntity):
    """Generic coordinator-backed binary sensor."""

    entity_description: AllpowersBinarySensorDescription

    def __init__(
        self,
        entry: AllpowersConfigEntry,
        description: AllpowersBinarySensorDescription,
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the current binary state."""
        return self.entity_description.value_fn(
            self.coordinator.data, self.coordinator
        )

    @property
    def available(self) -> bool:
        """Return availability defined by the data source."""
        return self.entity_description.available_fn(self.coordinator)
