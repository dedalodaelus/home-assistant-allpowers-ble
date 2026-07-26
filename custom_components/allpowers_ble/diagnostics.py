"""Diagnostics support for ALLPOWERS BLE."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_DEVICE_NAME
from .coordinator import AllpowersConfigEntry
from .model_support import identify_model

TO_REDACT = {CONF_ADDRESS}
REDACTED_NAME = "**REDACTED_NAME**"
REDACTED_ADDRESS = "**REDACTED_ADDRESS**"
_SENSITIVE_NAME_FIELDS = {"advertised_name", "device_name", "name", "title"}
_LAST_ERROR_FIELD = "last_error"
_EXCEPTION_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)$")
_BLE_COLON_OR_HYPHEN_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
_BLE_PLAIN_HEX_RE = re.compile(r"(?i)\b[0-9a-f]{12}\b")


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
    payload = _diagnostics_payload(entry, device_name=device.name)
    payload["device_registry"] = {
        "name": device.name,
        "model": device.model,
        "manufacturer": device.manufacturer,
    }
    return _sanitize_diagnostics(payload, {device.name} if device.name else set())


def _diagnostics_payload(
    entry: AllpowersConfigEntry,
    *,
    device_name: str | None = None,
) -> dict[str, Any]:
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
    sensitive_names = {
        str(value)
        for value in (
            entry.title,
            snapshot.advertised_name,
            entry.data.get(CONF_DEVICE_NAME),
            device_name,
        )
        if value
    }
    redacted_payload = async_redact_data(payload, TO_REDACT)
    return _sanitize_diagnostics(redacted_payload, sensitive_names)


def _sanitize_diagnostics(value: Any, sensitive_names: set[str]) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str in _SENSITIVE_NAME_FIELDS:
                sanitized[key_str] = REDACTED_NAME
                continue
            if key_str == _LAST_ERROR_FIELD:
                sanitized[key_str] = _sanitize_last_error(item, sensitive_names)
                continue
            sanitized[key_str] = _sanitize_diagnostics(item, sensitive_names)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_diagnostics(item, sensitive_names) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, sensitive_names)
    return value


def _sanitize_last_error(value: Any, sensitive_names: set[str]) -> Any:
    if not isinstance(value, str):
        return value
    category: str | None = None
    detail = value
    if ":" in value:
        maybe_category, maybe_detail = value.split(":", maxsplit=1)
        maybe_category = maybe_category.strip()
        if _EXCEPTION_TYPE_RE.match(maybe_category):
            category = maybe_category
            detail = maybe_detail.strip()
    if category is None:
        category = "RuntimeError"
    result: dict[str, str] = {"category": category}
    sanitized_detail = _sanitize_text(detail, sensitive_names)
    if sanitized_detail:
        result["detail"] = sanitized_detail
    return result


def _sanitize_text(text: str, sensitive_names: set[str]) -> str:
    sanitized = _BLE_COLON_OR_HYPHEN_RE.sub(REDACTED_ADDRESS, text)
    sanitized = _BLE_PLAIN_HEX_RE.sub(REDACTED_ADDRESS, sanitized)
    for sensitive_name in sensitive_names:
        if not sensitive_name:
            continue
        sanitized = re.sub(
            re.escape(sensitive_name),
            REDACTED_NAME,
            sanitized,
            flags=re.IGNORECASE,
        )
    return sanitized


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
