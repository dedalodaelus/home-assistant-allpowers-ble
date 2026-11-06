"""Real Home Assistant regression tests for reconfigure localization."""

from __future__ import annotations

from time import monotonic

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from custom_components.allpowers_ble import config_flow
from custom_components.allpowers_ble.const import (
    CONF_DEVICE_IDENTITY,
    CONF_DEVICE_NAME,
    CONF_REGISTRY_ID,
    DOMAIN,
    SERVICE_UUID,
)
from custom_components.allpowers_ble.options import ConnectionOptions

pytestmark = pytest.mark.homeassistant

ADDRESS = "AA:BB:CC:DD:EE:FF"
REGISTRY_ID = "existing-station-identity"


@pytest.mark.asyncio
async def test_same_device_reconfigure_uses_home_assistant_success_translation(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the existing entry identity and use Core's localized abort reason."""
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        version=1,
        minor_version=1,
        title="ALLPOWERS R600",
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_DEVICE_NAME: "ALLPOWERS R600",
            CONF_DEVICE_IDENTITY: "ALLPOWERS R600",
            CONF_REGISTRY_ID: REGISTRY_ID,
        },
        options=ConnectionOptions().as_dict(),
    )
    entry.add_to_hass(hass)

    async def request_scan(_hass: HomeAssistant) -> None:
        return None

    monkeypatch.setattr(
        config_flow.bluetooth, "async_request_active_scan", request_scan
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_discovered_service_info",
        lambda _hass, connectable: [
            BluetoothServiceInfoBleak(
                name="ALLPOWERS R600",
                address=ADDRESS,
                rssi=-55,
                manufacturer_data={},
                service_data={},
                service_uuids=[SERVICE_UUID],
                source="test",
                device=None,
                advertisement=None,
                connectable=connectable,
                time=monotonic(),
                tx_power=None,
            )
        ],
    )

    async def probe(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(config_flow, "async_probe_device", probe)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["type"] == "form"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ADDRESS: ADDRESS, CONF_DEVICE_NAME: "ALLPOWERS R600"},
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert result["translation_domain"] == "homeassistant"
    assert entry.unique_id == ADDRESS
    assert entry.data[CONF_REGISTRY_ID] == REGISTRY_ID
    assert entry.data[CONF_ADDRESS] == ADDRESS
