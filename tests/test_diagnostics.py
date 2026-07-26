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
    assert payload["entry"]["title"] == "**REDACTED_NAME**"
    assert "device_name" not in payload["entry"]["data"]
    assert payload["entry"]["options"]["status_interval"] == 20
    assert payload["model_support"]["model"] == "R600"
    assert payload["model_support"]["classification"] == "verified"
    assert payload["model_support"]["profile"] == "r600-hw-1.2"
    assert payload["model_support"]["capabilities"]["write_output_controls"]
    assert payload["snapshot"]["advertised_name"] == "**REDACTED_NAME**"
    assert payload["snapshot"]["last_error"] == {
        "category": "RuntimeError",
        "detail": "synthetic error",
    }
    assert payload["snapshot"]["settings"]["work_mode"] == 1
    assert payload["snapshot"]["last_packet_at"].endswith("+00:00")
    assert payload["protocol"]["safety"]["preserve_unknown_settings_bits"]
    assert ADDRESS not in str(payload)
    assert "ALLPOWERS R600" not in str(payload)


@pytest.mark.asyncio
async def test_device_diagnostics_include_registry_metadata() -> None:
    entry, _, _, hass = configured_entry()
    device = DeviceEntry(name="Garage", model="R600", manufacturer="ALLPOWERS")

    payload = await diagnostics.async_get_device_diagnostics(hass, entry, device)

    assert payload["device_registry"]["name"] == "**REDACTED_NAME**"
    assert payload["device_registry"]["model"] == "R600"
    assert payload["device_registry"]["manufacturer"] == "ALLPOWERS"


@pytest.mark.asyncio
async def test_diagnostics_recursively_redact_nested_identifiers() -> None:
    payload = {
        "snapshot": {
            "last_error": "BleakError: route 112233445566 lost for Living room R600",
            "nested": {
                "text": "adapter saw aa-bb-cc-dd-ee-ff with ALLPOWERS R600",
                "list": ["11:22:33:44:55:66", "AP R600"],
            },
        }
    }

    sanitized = diagnostics._sanitize_diagnostics(
        payload, {"ALLPOWERS R600", "AP R600", "Living room R600"}
    )

    assert sanitized["snapshot"]["nested"]["text"] == (
        "adapter saw **REDACTED_ADDRESS** with **REDACTED_NAME**"
    )
    assert sanitized["snapshot"]["nested"]["list"] == [
        "**REDACTED_ADDRESS**",
        "**REDACTED_NAME**",
    ]
    assert sanitized["snapshot"]["last_error"] == {
        "category": "BleakError",
        "detail": "route **REDACTED_ADDRESS** lost for **REDACTED_NAME**",
    }


def test_sanitize_last_error_supports_edge_cases() -> None:
    assert diagnostics._sanitize_last_error(None, {"name"}) is None

    result_empty_detail = diagnostics._sanitize_last_error("TimeoutError:", set())
    assert result_empty_detail == {"category": "TimeoutError"}

    result_without_category = diagnostics._sanitize_last_error(
        "probe failed for aa:bb:cc:dd:ee:ff", set()
    )
    assert result_without_category == {
        "category": "RuntimeError",
        "detail": "probe failed for **REDACTED_ADDRESS**",
    }

    result_with_non_exception_prefix = diagnostics._sanitize_last_error(
        "Warning: AP R600 unstable", {"AP R600"}
    )
    assert result_with_non_exception_prefix == {
        "category": "RuntimeError",
        "detail": "Warning: **REDACTED_NAME** unstable",
    }


def test_sanitize_text_ignores_empty_sensitive_name_entries() -> None:
    assert diagnostics._sanitize_text("safe", {""}) == "safe"


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
