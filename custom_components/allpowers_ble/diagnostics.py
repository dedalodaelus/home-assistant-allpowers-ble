"""Diagnostics support for ALLPOWERS BLE."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_DEVICE_NAME
from .coordinator import AllpowersConfigEntry
from .model_support import identify_model

TO_REDACT = {CONF_ADDRESS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
) -> dict[str, Any]:
    """Return redacted config-entry and transport diagnostics."""
    del hass
    return _diagnostics_payload(entry)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a device-registry entry."""
    del hass
    payload = _diagnostics_payload(entry)
    payload["device_registry"] = {
        "name": device.name,
        "model": device.model,
        "manufacturer": device.manufacturer,
    }
    return payload


def _diagnostics_payload(entry: AllpowersConfigEntry) -> dict[str, Any]:
    snapshot = entry.runtime_data.coordinator.data
    settings = snapshot.settings
    support = identify_model(
        snapshot.advertised_name,
        hardware_version=settings.hardware_version if settings else None,
        raw_hardware_version=settings.raw_hardware_version if settings else None,
    )
    payload: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": entry.runtime_data.client.options.as_dict(),
        },
        "model_support": asdict(support),
        "snapshot": _serialize(snapshot),
        "protocol": {
            "safety": {
                "combined_output_writes": True,
                "preserve_unknown_settings_bits": True,
                "requires_fresh_status_for_output_writes": True,
                "requires_fresh_settings_for_settings_writes": True,
            }
        },
    }
    payload["entry"]["data"].pop(CONF_DEVICE_NAME, None)
    return async_redact_data(payload, TO_REDACT)


def _serialize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialize(item) for item in value]
    return value
