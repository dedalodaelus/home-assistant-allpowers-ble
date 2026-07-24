"""Focused edge-case tests for safety and lifecycle branches."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.allpowers_ble import config_flow, entity, select
from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import DeviceNotFoundError
from custom_components.allpowers_ble.options import ConnectionOptions
from custom_components.allpowers_ble.protocol import (
    StateUnavailableError,
    updated_settings,
)

from tests.ha_stubs import FakeBleakError, FakeClient, FakeDevice
from tests.helpers import ADDRESS, configured_entry, settings
from tests.test_client_runtime import IterationStop, Services, make_client
from tests.test_config_flow import new_flow, service_info


@pytest.mark.asyncio
async def test_connect_cleanup_tolerates_disconnect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenClient(FakeClient):
        async def start_notify(self, characteristic: object, callback: object) -> None:
            del characteristic, callback
            raise RuntimeError("notify failed")

        async def disconnect(self) -> None:
            raise FakeBleakError("cleanup failed")

    client = make_client()
    fake = BrokenClient()
    fake.services = Services()

    async def establish(*args: Any, **kwargs: Any) -> FakeClient:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    with pytest.raises(RuntimeError, match="notify failed"):
        await client._connect_once()


@pytest.mark.asyncio
async def test_delayed_refresh_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    monkeypatch.setattr(client_module, "COMMAND_REFRESH_DELAY", 100)
    task = asyncio.create_task(client._delayed_status_refresh())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class ReturnStop:
    """Event-like object whose wait completes and then stops the loop."""

    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    async def wait(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_maintenance_stop_disconnected_and_keepalive_not_due() -> None:
    stopped = make_client()
    stopped._stop_event = ReturnStop()  # type: ignore[assignment]
    await stopped._maintenance_loop()

    disconnected = make_client()
    disconnected._stop_event = IterationStop()  # type: ignore[assignment]
    await disconnected._maintenance_loop()

    options = ConnectionOptions(settings_keepalive=True)
    not_due = make_client(options)
    not_due._connected = True
    not_due._connected_monotonic = monotonic()
    not_due._last_packet_monotonic = monotonic()
    not_due._last_status_request_monotonic = monotonic()
    not_due._settings = settings()
    not_due._settings_monotonic = monotonic()
    not_due._last_settings_keepalive_monotonic = monotonic()
    not_due._initial_settings_keepalive_pending = False
    not_due._stop_event = IterationStop()  # type: ignore[assignment]
    await not_due._maintenance_loop()


@pytest.mark.asyncio
async def test_manual_flow_shows_probe_error_and_skips_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = new_flow()
    info = service_info()
    flow._discovered_devices[ADDRESS] = info

    async def fail_probe(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise DeviceNotFoundError("no route")

    monkeypatch.setattr(config_flow, "async_probe_device", fail_probe)
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_request_active_scan",
        lambda hass: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [info],
    )
    result = await flow.async_step_user({"address": ADDRESS})
    assert result["errors"] == {"base": "cannot_connect"}

    duplicate_flow = new_flow()
    duplicate_flow._async_current_ids = (
        lambda include_ignore: {ADDRESS}
    )  # type: ignore[method-assign]
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [info, info],
    )
    duplicate_flow._discover_candidates()
    assert not duplicate_flow._discovered_devices


@pytest.mark.asyncio
async def test_base_entity_availability_and_eco_error_wrapping() -> None:
    entry, client, _, _ = configured_entry()
    assert entity.AllpowersStatusEntity(entry, "status").available
    assert entity.AllpowersSettingsEntity(entry, "settings").available
    assert entity.AllpowersOutputControlEntity(entry, "output").available
    assert entity.AllpowersSettingsControlEntity(entry, "control").available

    eco = select.AllpowersEcoTimeoutSelect(entry)
    client.errors["set_eco_timeout"] = StateUnavailableError("stale settings")
    with pytest.raises(HomeAssistantError, match="stale settings"):
        await eco.async_select_option("one_hour")


def test_option_boolean_and_keepalive_relationship_validation() -> None:
    values = ConnectionOptions().as_dict()
    values["settings_keepalive"] = 1
    values["enable_car_charger"] = 0
    options = ConnectionOptions.from_mapping(values)
    assert options.settings_keepalive
    assert not options.enable_car_charger

    values["settings_stale_timeout"] = values["settings_keepalive_interval"]
    with pytest.raises(ValueError, match="settings_stale_timeout"):
        ConnectionOptions.from_mapping(values)

    values = ConnectionOptions().as_dict()
    values["settings_keepalive"] = "false"
    with pytest.raises(ValueError, match="must be a boolean"):
        ConnectionOptions.from_mapping(values)


def test_updated_settings_preserves_reserved_work_mode() -> None:
    current = settings(raw_flags=0xA6, work_mode=None)
    updated = updated_settings(current, eco_enabled=True)
    assert updated.work_mode is None
    assert updated.raw_flags & 0x06 == 0x06
