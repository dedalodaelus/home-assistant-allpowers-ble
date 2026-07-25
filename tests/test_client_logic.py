"""Transport-state tests using minimal dependency stubs."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from custom_components.allpowers_ble.options import ConnectionOptions
from custom_components.allpowers_ble.protocol import SettingsData, StatusData, WorkMode

from tests.ha_stubs import (
    FakeBleakError,
    FakeCharacteristic,
    FakeClient,
)


client_module = importlib.import_module("custom_components.allpowers_ble.client")

AllpowersBLEClient = client_module.AllpowersBLEClient
NotConnectedError = client_module.NotConnectedError
UnsupportedDeviceError = client_module.UnsupportedDeviceError


def _status(**changes: object) -> StatusData:
    values: dict[str, object] = {
        "dc_enabled": False,
        "ac_enabled": False,
        "light_enabled": True,
        "battery_percent": 50,
        "input_power_w": 100,
        "output_power_w": 25,
        "remaining_minutes": 90,
        "raw_flags": 0x10,
    }
    values.update(changes)
    return StatusData(**values)


def _settings(**changes: object) -> SettingsData:
    values: dict[str, object] = {
        "eco_enabled": False,
        "work_mode": WorkMode.MUTE,
        "car_charger_enabled": False,
        "eco_timeout_hours": 2,
        "hardware_version": "1.0",
        "firmware_version": "1.1",
        "raw_flags": 0xA0,
        "raw_hardware_version": 0x10,
        "raw_firmware_version": 0x11,
    }
    values.update(changes)
    return SettingsData(**values)


def _connected_client(options: ConnectionOptions | None = None):
    client = AllpowersBLEClient(
        hass=object(),
        address="aa:bb:cc:dd:ee:ff",
        advertised_name="ALLPOWERS R600",
        options=options or ConnectionOptions(),
    )
    fake = FakeClient()
    client._client = fake
    client._write_characteristic = FakeCharacteristic()
    client._connected = True
    client._active_session_generation = 1
    client._schedule_status_refresh = lambda: None
    return client, fake


@pytest.mark.asyncio
async def test_sequential_output_commands_use_shadow_snapshot() -> None:
    client, fake = _connected_client()
    client._status = _status()
    client._status_monotonic = asyncio.get_running_loop().time()

    await client.async_set_ac(True)
    await client.async_set_dc(True)

    assert fake.writes[0][7] == 0x22
    assert fake.writes[1][7] == 0x23


def test_snapshot_guards_reject_disconnected_state() -> None:
    client, _ = _connected_client()
    client._connected = False

    with pytest.raises(client_module.StateUnavailableError, match="fresh status"):
        client._safe_output_snapshot()
    with pytest.raises(client_module.StateUnavailableError, match="fresh settings"):
        client._safe_settings_snapshot()


@pytest.mark.asyncio
async def test_output_command_requires_fresh_status() -> None:
    client, _ = _connected_client()
    client._status = _status()
    client._status_monotonic = (
        asyncio.get_running_loop().time() - client.options.stale_timeout - 1
    )

    with pytest.raises(client_module.StateUnavailableError, match="fresh status"):
        await client.async_set_ac(True)


@pytest.mark.asyncio
async def test_sequential_settings_commands_preserve_unknown_bits() -> None:
    client, fake = _connected_client()
    client._settings = _settings()
    client._settings_monotonic = asyncio.get_running_loop().time()

    await client.async_set_eco(True)
    await client.async_set_work_mode(WorkMode.FAST)

    assert fake.writes[0][7] == 0xA1
    assert fake.writes[1][7] == 0xA5
    assert fake.writes[1][8] == 2


@pytest.mark.asyncio
async def test_settings_command_requires_fresh_settings() -> None:
    client, _ = _connected_client()

    with pytest.raises(client_module.StateUnavailableError, match="fresh settings"):
        await client.async_set_eco(True)


@pytest.mark.asyncio
async def test_car_charger_must_be_explicitly_enabled() -> None:
    client, _ = _connected_client()
    client._settings = _settings()
    client._settings_monotonic = asyncio.get_running_loop().time()

    with pytest.raises(client_module.StateUnavailableError, match="disabled"):
        await client.async_set_car_charger(True)


@pytest.mark.asyncio
async def test_car_charger_enabled_option() -> None:
    options = ConnectionOptions(enable_car_charger=True)
    client, fake = _connected_client(options)
    client._settings = _settings()
    client._settings_monotonic = asyncio.get_running_loop().time()

    await client.async_set_car_charger(True)

    assert fake.writes[-1][7] == 0xB0


@pytest.mark.asyncio
async def test_settings_keepalive_reuses_raw_snapshot() -> None:
    client, fake = _connected_client()
    client._settings = _settings(raw_flags=0xE4, eco_timeout_hours=6)
    client._settings_monotonic = asyncio.get_running_loop().time()

    await client.async_send_settings_keepalive()

    assert fake.writes[-1][7:9] == bytes((0xE4, 6))


@pytest.mark.asyncio
async def test_output_write_does_not_update_settings_keepalive_activity() -> None:
    options = ConnectionOptions(settings_keepalive=True)
    client, _ = _connected_client(options)
    client._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    client._status_monotonic = asyncio.get_running_loop().time()
    client._initial_settings_keepalive_pending = True
    client._last_settings_keepalive_monotonic = None

    await client.async_set_ac(True)

    assert client._initial_settings_keepalive_pending is True
    assert client._last_settings_keepalive_monotonic is None


@pytest.mark.asyncio
async def test_settings_write_updates_settings_keepalive_activity() -> None:
    options = ConnectionOptions(settings_keepalive=True)
    client, _ = _connected_client(options)
    client._settings = _settings(eco_enabled=False)
    client._settings_monotonic = asyncio.get_running_loop().time()
    client._initial_settings_keepalive_pending = True
    client._last_settings_keepalive_monotonic = None

    await client.async_set_eco(True)

    assert client._initial_settings_keepalive_pending is False
    assert client._last_settings_keepalive_monotonic is not None


@pytest.mark.asyncio
async def test_status_request_records_write() -> None:
    client, fake = _connected_client()

    await client.async_request_status()

    assert fake.writes == [bytes.fromhex("A5 65 B1 00 01 06 01 00 00 00 00 00")]
    assert client._last_status_request_monotonic is not None


@pytest.mark.asyncio
async def test_write_rejects_disconnected_client() -> None:
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="R600",
        options=ConnectionOptions(),
    )

    with pytest.raises(NotConnectedError):
        await client.async_request_status()


@pytest.mark.asyncio
async def test_write_error_updates_statistics_and_reconnect_event() -> None:
    client, fake = _connected_client()
    fake.raise_on_write = FakeBleakError("radio failed")

    with pytest.raises(FakeBleakError):
        await client.async_request_status()

    assert client.snapshot().statistics.write_errors == 1
    assert client._disconnect_event.is_set()
    assert "radio failed" in (client.snapshot().last_error or "")


@pytest.mark.asyncio
async def test_notification_updates_status_settings_and_counters(
    status_frame: bytes,
    settings_frame: bytes,
) -> None:
    client, _ = _connected_client()
    callbacks = 0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    client.set_update_callback(update)
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    snapshot = client.snapshot()

    assert snapshot.status is not None
    assert snapshot.settings is not None
    assert snapshot.statistics.notifications == 2
    assert snapshot.statistics.valid_packets == 2
    assert client._ready_event.is_set()
    assert callbacks == 2


def test_invalid_notification_updates_protocol_errors(status_frame: bytes) -> None:
    client, _ = _connected_client()
    invalid = bytearray(status_frame)
    invalid[-1] ^= 1

    client._notification_handler(FakeCharacteristic(), invalid)

    assert client.snapshot().statistics.protocol_errors == 1
    assert client.snapshot().statistics.valid_packets == 0


@pytest.mark.asyncio
async def test_device_name_notification_updates_name(notification_builder) -> None:
    client, _ = _connected_client()
    frame = notification_builder(0x35, b"Living room R600")

    client._notification_handler(FakeCharacteristic(), bytearray(frame))

    assert client.advertised_name == "Living room R600"


def test_update_advertisement_emits_only_on_change() -> None:
    client, _ = _connected_client()
    callbacks = 0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    client.set_update_callback(update)
    info = SimpleNamespace(name="R600 New", rssi=-61)
    client.update_advertisement(info)
    client.update_advertisement(info)

    assert client.advertised_name == "R600 New"
    assert client.snapshot().rssi == -61
    assert callbacks == 1


@pytest.mark.asyncio
async def test_apply_options_updates_snapshot_callback() -> None:
    client, _ = _connected_client()
    callbacks = 0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    client.set_update_callback(update)
    options = ConnectionOptions(status_interval=15, stale_timeout=31)

    await client.async_apply_options(options)

    assert client.options == options
    assert callbacks == 1


@pytest.mark.asyncio
async def test_disconnect_client_updates_statistics() -> None:
    client, fake = _connected_client()

    await client._disconnect_client()

    assert fake.disconnect_calls == 1
    assert client.snapshot().connected is False
    assert client.snapshot().statistics.disconnects == 1
    assert client.snapshot().last_disconnected_at is not None


def test_required_characteristics() -> None:
    notify = FakeCharacteristic()
    write = FakeCharacteristic()

    class Services:
        def get_service(self, uuid: str) -> object | None:
            return object() if uuid == client_module.SERVICE_UUID else None

        def get_characteristic(self, uuid: str) -> object | None:
            return {
                client_module.NOTIFY_UUID: notify,
                client_module.WRITE_UUID: write,
            }.get(uuid)

    fake = FakeClient()
    fake.services = Services()

    assert client_module._required_characteristics(fake) == (notify, write)


def test_required_service_is_enforced() -> None:
    class Services:
        def get_service(self, uuid: str) -> None:
            del uuid
            return None

    fake = FakeClient()
    fake.services = Services()

    with pytest.raises(UnsupportedDeviceError, match="service"):
        client_module._required_characteristics(fake)


def test_required_characteristics_are_enforced() -> None:
    class Services:
        def get_service(self, uuid: str) -> object:
            del uuid
            return object()

        def get_characteristic(self, uuid: str) -> None:
            del uuid
            return None

    fake = FakeClient()
    fake.services = Services()

    with pytest.raises(UnsupportedDeviceError, match="characteristics"):
        client_module._required_characteristics(fake)
