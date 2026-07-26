"""Sensor entities for ALLPOWERS BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AllpowersConfigEntry, AllpowersCoordinator
from .entity import AllpowersEntity
from .models import AllpowersSnapshot


type SensorValue = int | float | str | None


@dataclass(frozen=True, kw_only=True)
class AllpowersSensorDescription(SensorEntityDescription):
    """Describe an ALLPOWERS sensor."""

    value_fn: Callable[[AllpowersSnapshot], SensorValue]
    available_fn: Callable[[AllpowersCoordinator], bool]


def _sensor_description(**kwargs: Any) -> AllpowersSensorDescription:
    """Build sensor descriptions in a pylint-friendly way."""
    return AllpowersSensorDescription(**kwargs)


SENSOR_DESCRIPTIONS: tuple[AllpowersSensorDescription, ...] = (
    _sensor_description(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.status.battery_percent if data.status else None,
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    _sensor_description(
        key="input_power",
        translation_key="input_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.status.input_power_w if data.status else None,
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    _sensor_description(
        key="output_power",
        translation_key="output_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.status.output_power_w if data.status else None,
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    _sensor_description(
        key="remaining_time",
        translation_key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda data: data.status.remaining_minutes if data.status else None,
        available_fn=lambda coordinator: coordinator.status_is_fresh,
    ),
    _sensor_description(
        key="rssi",
        translation_key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.rssi,
        available_fn=lambda coordinator: coordinator.data.rssi is not None,
    ),
    _sensor_description(
        key="hardware_version",
        translation_key="hardware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.settings.hardware_version if data.settings else None,
        available_fn=lambda coordinator: coordinator.settings_are_fresh,
    ),
    _sensor_description(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.settings.firmware_version if data.settings else None,
        available_fn=lambda coordinator: coordinator.settings_are_fresh,
    ),
    _sensor_description(
        key="reconnects",
        translation_key="reconnects",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.statistics.reconnects,
        available_fn=lambda coordinator: True,
    ),
    _sensor_description(
        key="protocol_errors",
        translation_key="protocol_errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.statistics.protocol_errors,
        available_fn=lambda coordinator: True,
    ),
    _sensor_description(
        key="parser_discards",
        translation_key="parser_discards",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.statistics.parser_discards,
        available_fn=lambda coordinator: True,
    ),
    _sensor_description(
        key="watchdog_resets",
        translation_key="watchdog_resets",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.statistics.watchdog_resets,
        available_fn=lambda coordinator: True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ALLPOWERS BLE sensors."""
    del hass
    async_add_entities(
        AllpowersSensor(entry, description) for description in SENSOR_DESCRIPTIONS
    )


class AllpowersSensor(AllpowersEntity, SensorEntity):
    """Generic coordinator-backed ALLPOWERS sensor."""

    entity_description: AllpowersSensorDescription

    def __init__(
        self,
        entry: AllpowersConfigEntry,
        description: AllpowersSensorDescription,
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> SensorValue:
        """Return the current sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return availability defined by the sensor data source."""
        return self.entity_description.available_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the last transport error only on diagnostic counters."""
        if self.entity_description.key not in {
            "reconnects",
            "protocol_errors",
            "parser_discards",
            "watchdog_resets",
        }:
            return None
        return {"last_error": self.coordinator.data.last_error}
