"""Select entities for ALLPOWERS BLE."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import NotConnectedError
from .coordinator import AllpowersConfigEntry
from .entity import AllpowersSettingsControlEntity
from .protocol import StateUnavailableError, WorkMode

WORK_MODE_OPTIONS = ("mute", "standard", "fast")
WORK_MODE_FROM_OPTION = {
    "mute": WorkMode.MUTE,
    "standard": WorkMode.STANDARD,
    "fast": WorkMode.FAST,
}
OPTION_FROM_WORK_MODE = {value: key for key, value in WORK_MODE_FROM_OPTION.items()}

ECO_TIMEOUT_OPTIONS = ("one_hour", "two_hours", "four_hours", "six_hours")
ECO_TIMEOUT_FROM_OPTION = {
    "one_hour": 1,
    "two_hours": 2,
    "four_hours": 4,
    "six_hours": 6,
}
OPTION_FROM_ECO_TIMEOUT = {
    value: key for key, value in ECO_TIMEOUT_FROM_OPTION.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ALLPOWERS BLE selects."""
    del hass
    async_add_entities(
        (AllpowersWorkModeSelect(entry), AllpowersEcoTimeoutSelect(entry))
    )


class AllpowersWorkModeSelect(AllpowersSettingsControlEntity, SelectEntity):
    """Charging work-mode selector."""

    _attr_translation_key = "work_mode"
    _attr_icon = "mdi:speedometer"
    _attr_options = list(WORK_MODE_OPTIONS)

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "work_mode")

    @property
    def current_option(self) -> str | None:
        """Return the current work mode or unknown for reserved values."""
        settings = self.coordinator.data.settings
        if settings is None or settings.work_mode is None:
            return None
        return OPTION_FROM_WORK_MODE[settings.work_mode]

    async def async_select_option(self, option: str) -> None:
        """Select a work mode."""
        try:
            mode = WORK_MODE_FROM_OPTION[option]
            await self.coordinator.client.async_set_work_mode(mode)
        except KeyError as ex:
            raise HomeAssistantError(f"Unsupported work mode: {option}") from ex
        except (StateUnavailableError, NotConnectedError) as ex:
            raise HomeAssistantError(str(ex)) from ex


class AllpowersEcoTimeoutSelect(AllpowersSettingsControlEntity, SelectEntity):
    """ECO shutdown-time selector."""

    _attr_translation_key = "eco_timeout"
    _attr_icon = "mdi:timer-outline"
    _attr_options = list(ECO_TIMEOUT_OPTIONS)

    def __init__(self, entry: AllpowersConfigEntry) -> None:
        super().__init__(entry, "eco_timeout")

    @property
    def current_option(self) -> str | None:
        """Return the current timeout or unknown for undocumented values."""
        settings = self.coordinator.data.settings
        if settings is None:
            return None
        return OPTION_FROM_ECO_TIMEOUT.get(settings.eco_timeout_hours)

    async def async_select_option(self, option: str) -> None:
        """Select an ECO shutdown timeout."""
        try:
            hours = ECO_TIMEOUT_FROM_OPTION[option]
            await self.coordinator.client.async_set_eco_timeout(hours)
        except KeyError as ex:
            raise HomeAssistantError(f"Unsupported ECO timeout: {option}") from ex
        except (StateUnavailableError, NotConnectedError) as ex:
            raise HomeAssistantError(str(ex)) from ex
