"""Verify HA 2026.10.4's installed Bluetooth patch and real WS route evidence."""

from __future__ import annotations

from types import SimpleNamespace

import bleak
import bleak_retry_connector as brc
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak_esphome.backend.client import ESPHomeClient
from bleak_esphome.backend.scanner import ESPHomeScanner
from habluetooth.scanner_bleak import HaScanner
from habluetooth.usage import HaBleakClientWithServiceCache, install_multiple_bleak_catcher
from habluetooth.wrappers import HaBleakClientWrapper
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.hil_trace import async_register, clear_entry_capture
from custom_components.allpowers_ble.route_attribution import ConnectedRoute, connected_route
from tests.ha_stubs import FakeClient, FakeDevice
from tests.helpers import FakeHass
from tests.test_client_runtime import ProbeClient, Services, make_client

pytestmark = pytest.mark.homeassistant


class Wrapper(HaBleakClientWrapper):
    def __init__(self, scanner, backend):
        self._connected_scanner = scanner
        self._backend = backend


class Local(HaScanner):
    def __init__(self, adapter):
        self.adapter = adapter


class BlueZ(BleakClientBlueZDBus):
    def __init__(self, device_path):
        self._device_path = device_path

    @property
    def is_connected(self):
        return True


class ProxyScanner(ESPHomeScanner):
    def __init__(self, source):
        self.source = source


class ProxyClient(ESPHomeClient):
    def __init__(self, scanner, source):
        self._scanner = scanner
        self._source = source

    @property
    def is_connected(self):
        return True


def test_actual_upstream_types_verify_bluez_and_esphome_connected_paths():
    local_scanner = Local("hci1")
    local = connected_route(
        Wrapper(local_scanner, BlueZ("/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"))
    )
    assert local == ConnectedRoute(
        "local_adapter", "bluez", "bluez_device_path", adapter_id="hci1"
    )

    proxy_scanner = ProxyScanner("private-remote-source")
    proxy = connected_route(
        Wrapper(proxy_scanner, ProxyClient(proxy_scanner, "private-remote-source"))
    )
    assert proxy == ConnectedRoute(
        "active_proxy", "esphome", "esphome_scanner_binding",
        proxy_id="private-remote-source",
    )
    assert connected_route(
        Wrapper(proxy_scanner, ProxyClient(ProxyScanner("private-remote-source"), "private-remote-source"))
    ).observed_route == "unverified"
    assert connected_route(
        Wrapper(local_scanner, BlueZ("/org/bluez/hci2/dev_AA_BB"))
    ).observed_route == "unverified"


@pytest.mark.asyncio
async def test_real_habluetooth_catcher_patched_after_client_import_is_used(
    monkeypatch, status_frame
):
    """Exercise the actual HA class patch, not an invented replacement class."""
    original = (
        bleak.BleakScanner,
        bleak.BleakClient,
        brc.BleakClient,
        brc.BleakClientWithServiceCache,
    )
    try:
        install_multiple_bleak_catcher()
        assert brc.BleakClientWithServiceCache is HaBleakClientWithServiceCache
        selected = []

        async def connect(cls, *args, **kwargs):
            del args, kwargs
            selected.append(cls)
            fake = FakeClient()
            fake.services = Services()
            return fake

        monkeypatch.setattr(client_module, "establish_connection", connect)
        client = make_client()
        monkeypatch.setattr(client, "_fresh_ble_device", FakeDevice)
        await client._connect_once()
        assert selected == [HaBleakClientWithServiceCache]
        await client._disconnect_client()

        probe = ProbeClient([status_frame])

        async def probe_connect(cls, *args, **kwargs):
            del args, kwargs
            selected.append(cls)
            return probe

        monkeypatch.setattr(client_module, "establish_connection", probe_connect)
        monkeypatch.setattr(
            client_module.bluetooth,
            "async_ble_device_from_address",
            lambda *args, **kwargs: FakeDevice(),
        )
        result = await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.1,
        )
        assert result.status.battery_percent == 73
        assert selected == [HaBleakClientWithServiceCache] * 2
        assert len(probe.writes) == 1
    finally:
        (
            bleak.BleakScanner,
            bleak.BleakClient,
            brc.BleakClient,
            brc.BleakClientWithServiceCache,
        ) = original


@pytest.mark.asyncio
async def test_real_ha_authenticated_ws_route_snapshot_on_existing_connection(
    hass: HomeAssistant, hass_ws_client
):
    """An admin can inspect a pre-existing connection without another GATT I/O."""
    await async_register(hass)
    client = make_client()
    client._connected = True
    client._active_session_generation = 4
    client._connected_route = (
        4, ConnectedRoute(
            "active_proxy", "esphome", "esphome_scanner_binding",
            proxy_id="PRIVATE-PROXY",
        )
    )
    entry = MockConfigEntry(domain="allpowers_ble", data={})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(client=client)

    ws = await hass_ws_client()
    await ws.send_json_auto_id({
        "type": "allpowers_ble/hil/start",
        "entry_id": entry.entry_id,
        "run_id": "test12345",
        "scenario_id": "route-real-ws",
        "requested_route": "local_adapter",
    })
    begun = await ws.receive_json()
    assert begun["success"] is True
    assert begun["result"]["schema_version"] == 2
    capture_id = begun["result"]["capture_id"]
    await ws.send_json_auto_id({
        "type": "allpowers_ble/hil/stop",
        "entry_id": entry.entry_id,
        "capture_id": capture_id,
    })
    ended = await ws.receive_json()
    assert ended["success"] is True
    result = ended["result"]
    assert result["observed_route"] == "active_proxy"
    assert result["requested_route"] == "local_adapter"
    assert result["route_observations"][0]["session_generation"] == 4
    assert result["route_observations"][0]["proxy_id"] == "PRIVATE-PROXY"
    assert result["qualification_allowed"] is False
    assert all("PRIVATE-PROXY" not in str(row) for row in result["events"])
    assert any(row["event"] == "route_observed" for row in result["events"])
    assert result["frame_count"] == 0
    clear_entry_capture(hass, entry.entry_id)
