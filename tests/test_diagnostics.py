"""Diagnostics payload and redaction tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from homeassistant.helpers.device_registry import DeviceEntry

from custom_components.allpowers_ble import diagnostics
from custom_components.allpowers_ble.protocol import WorkMode

from tests.helpers import ADDRESS, configured_entry


@pytest.mark.asyncio
async def test_config_entry_diagnostics_redact_address_and_serialize_state() -> None:
    entry, _, _, hass = configured_entry()

    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert payload["entry"]["data"]["address"] == "**REDACTED**"
    assert "device_name" not in payload["entry"]["data"]
    assert payload["entry"]["options"]["status_interval"] == 20
    assert payload["model_support"]["model"] == "R600"
    assert payload["model_support"]["classification"] == "verified"
    assert payload["model_support"]["profile"] == "r600-hw-1.2"
    assert payload["model_support"]["capabilities"]["write_output_controls"]
    assert payload["snapshot"]["settings"]["work_mode"] == 1
    assert payload["snapshot"]["last_packet_at"].endswith("+00:00")
    assert payload["protocol"]["safety"]["preserve_unknown_settings_bits"]
    assert ADDRESS not in str(payload)


@pytest.mark.asyncio
async def test_device_diagnostics_include_registry_metadata() -> None:
    entry, _, _, hass = configured_entry()
    device = DeviceEntry(name="Garage", model="R600", manufacturer="ALLPOWERS")

    payload = await diagnostics.async_get_device_diagnostics(hass, entry, device)

    assert payload["device_registry"] == {
        "name": "Garage",
        "model": "R600",
        "manufacturer": "ALLPOWERS",
    }


def test_serialize_handles_nested_values() -> None:
    now = datetime.now(timezone.utc)
    value = {
        "mode": WorkMode.FAST,
        "when": now,
        "sequence": (WorkMode.MUTE, {WorkMode.STANDARD}),
        "plain": 7,
    }

    serialized = diagnostics._serialize(value)

    assert serialized["mode"] == 2
    assert serialized["when"] == now.isoformat()
    assert serialized["sequence"][0] == 0
    assert serialized["sequence"][1] == [1]
    assert serialized["plain"] == 7
