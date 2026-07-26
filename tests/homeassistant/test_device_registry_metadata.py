"""Real Home Assistant device-registry checks for metadata refresh."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from custom_components.allpowers_ble.const import DOMAIN
from custom_components.allpowers_ble.coordinator import AllpowersCoordinator

from tests.helpers import FakeIntegrationClient, settings, snapshot

pytestmark = pytest.mark.homeassistant


@pytest.mark.asyncio
async def test_device_registry_metadata_refreshes_when_settings_arrive(
    hass,
) -> None:
    """Late settings should update hardware and firmware once in real registry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        version=1,
        minor_version=1,
        title="ALLPOWERS R600 AABB",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", "device_name": "ALLPOWERS R600"},
        options={},
    )
    entry.add_to_hass(hass)

    client = FakeIntegrationClient(
        state=replace(snapshot(), settings=None, settings_monotonic=None)
    )
    coordinator = AllpowersCoordinator(hass, entry, client)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF")},
        connections={(CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF")},
        manufacturer="ALLPOWERS",
        model="R600",
        name=entry.title,
    )
    assert device.hw_version is None
    assert device.sw_version is None

    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(
                hardware_version="1.2",
                firmware_version="3.4",
                raw_hardware_version=0x12,
                raw_firmware_version=0x34,
            ),
        )
    )

    updated = registry.async_get(device.id)
    assert updated is not None
    assert updated.hw_version == "1.2"
    assert updated.sw_version == "3.4"

    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(
                hardware_version="0xAF",
                firmware_version="0xFF",
                raw_hardware_version=0xAF,
                raw_firmware_version=0xFF,
            ),
        )
    )

    unchanged = registry.async_get(device.id)
    assert unchanged is not None
    assert unchanged.hw_version == "1.2"
    assert unchanged.sw_version == "3.4"

    await coordinator.async_shutdown()
