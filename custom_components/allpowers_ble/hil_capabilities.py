"""Admin-only, read-only capability snapshot for guided HIL tooling."""

from __future__ import annotations

import re
from typing import Any

import probatio as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import runtime_model_support

_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): DOMAIN + "/hil/capabilities",
    vol.Required("entry_id"): str,
}
_REVISION = re.compile(r"\A[0-9]{1,3}(?:\.[0-9]{1,3}){0,3}\Z")


def _safe_revision(value: Any) -> str | None:
    """Return only bounded protocol revision strings."""
    if isinstance(value, str) and _REVISION.fullmatch(value):
        return value
    return None


def _deny(connection: Any, msg: dict[str, Any], code: str) -> None:
    connection.send_error(msg["id"], code, "HIL capability request rejected")


@websocket_api.websocket_command(_SCHEMA)
@websocket_api.async_response
async def ws_capabilities(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    """Return allowlisted runtime facts for one loaded integration entry."""
    if not getattr(connection.user, "is_admin", False):
        _deny(connection, msg, "unauthorized")
        return
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        _deny(connection, msg, "not_found")
        return
    runtime = getattr(entry, "runtime_data", None)
    if entry.state is not ConfigEntryState.LOADED or runtime is None:
        _deny(connection, msg, "not_ready")
        return

    coordinator = runtime.coordinator
    data = coordinator.data
    support = runtime_model_support(coordinator)
    settings = data.settings
    hardware = data.session_hardware_version
    raw_hardware = data.session_raw_hardware_version
    if hardware is None and coordinator.settings_are_fresh and settings is not None:
        hardware = settings.hardware_version
        raw_hardware = settings.raw_hardware_version
    settings_fresh = coordinator.settings_are_fresh
    firmware = (
        settings.firmware_version if settings is not None and settings_fresh else None
    )
    raw_firmware = (
        settings.raw_firmware_version
        if settings is not None and settings_fresh
        else None
    )
    options = runtime.client.options
    generation = getattr(runtime.client, "active_session_generation", None)
    if type(generation) is not int or generation < 1:
        generation = None

    capability_flags = support.capabilities
    result = {
        "schema_version": 1,
        "model": support.model,
        "profile": support.profile,
        "classification": support.classification,
        "verified": support.verified,
        "identity_conflicted": data.identity_conflicted,
        "hardware_revision": _safe_revision(hardware),
        "raw_hardware_revision": (
            raw_hardware
            if type(raw_hardware) is int and 0 <= raw_hardware <= 255
            else None
        ),
        "firmware_revision": _safe_revision(firmware),
        "raw_firmware_revision": (
            raw_firmware
            if type(raw_firmware) is int and 0 <= raw_firmware <= 255
            else None
        ),
        "capabilities": {
            "read_telemetry": bool(capability_flags.read_telemetry),
            "write_output_controls": bool(capability_flags.write_output_controls),
            "write_settings_controls": bool(capability_flags.write_settings_controls),
            "write_settings_keepalive": bool(capability_flags.write_settings_keepalive),
            "car_charger_enabled": bool(options.enable_car_charger),
        },
        "connected": bool(coordinator.connected),
        "status_fresh": bool(coordinator.status_is_fresh),
        "settings_fresh": bool(settings_fresh),
        "session_generation": generation,
        "pending_transaction": bool(runtime.client.has_pending_transaction),
    }
    connection.send_result(msg["id"], result)


async def async_register(hass: HomeAssistant) -> None:
    """Register the read-only runtime metadata command."""
    websocket_api.async_register_command(hass, ws_capabilities)
