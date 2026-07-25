"""Transport-state tests using minimal dependency stubs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib
from time import monotonic
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


@pytest.mark.asyncio
async def test_stale_status_notification_does_not_clear_pending_output_transaction(
    status_frame: bytes,
) -> None:
    client, fake = _connected_client()
    client._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=True)
    client._status_monotonic = asyncio.get_running_loop().time()

    await client.async_set_ac(True)
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    await client.async_set_dc(True)

    assert fake.writes[0][7] == 0x22
    assert fake.writes[1][7] == 0x23


@pytest.mark.asyncio
async def test_consecutive_output_commands_handle_delayed_and_duplicate_confirmations(
    notification_builder,
) -> None:
    client, fake = _connected_client()
    client._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    client._status_monotonic = asyncio.get_running_loop().time()

    await client.async_set_ac(True)
    await client.async_set_dc(True)

    pending = client._pending_output_transaction
    assert pending is not None
    assert pending.target_ac is True
    assert pending.target_dc is True
    assert pending.target_light is False

    contradictory = notification_builder(
        0x01,
        bytes((0x02, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78)),
    )
    client._notification_handler(FakeCharacteristic(), bytearray(contradictory))
    assert client._pending_output_transaction is not None

    matching = notification_builder(
        0x01,
        bytes((0x03, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78)),
    )
    client._notification_handler(FakeCharacteristic(), bytearray(matching))
    assert client._pending_output_transaction is None

    client._notification_handler(FakeCharacteristic(), bytearray(matching))
    assert client._pending_output_transaction is None

    await client.async_set_light(True)
    assert fake.writes[-1][7] == 0x23


@pytest.mark.asyncio
async def test_output_timeout_blocks_dependent_writes_until_fresh_status(
    status_frame: bytes,
) -> None:
    client, fake = _connected_client()
    client._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=True)
    client._status_monotonic = asyncio.get_running_loop().time()

    await client.async_set_ac(True)
    pending = client._pending_output_transaction
    assert pending is not None
    client._pending_output_transaction = replace(
        pending,
        confirm_deadline_monotonic=asyncio.get_running_loop().time() - 1,
    )

    with pytest.raises(client_module.StateUnavailableError, match="timed out"):
        await client.async_set_dc(True)

    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    await client.async_set_dc(True)
    assert len(fake.writes) == 2


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
async def test_consecutive_settings_commands_handle_contradictory_and_duplicate_confirmations(
    notification_builder,
) -> None:
    client, _ = _connected_client()
    client._settings = _settings()
    client._settings_monotonic = asyncio.get_running_loop().time()

    await client.async_set_eco(True)
    await client.async_set_work_mode(WorkMode.FAST)

    pending = client._pending_settings_transaction
    assert pending is not None
    assert pending.target.eco_enabled is True
    assert pending.target.work_mode is WorkMode.FAST

    contradictory = notification_builder(
        0x03,
        bytes((0xA1, 2, 0x00, 0x00, 0x10, 0x11)),
    )
    client._notification_handler(FakeCharacteristic(), bytearray(contradictory))
    assert client._pending_settings_transaction is not None

    matching = notification_builder(
        0x03,
        bytes((0xA5, 2, 0x00, 0x00, 0x10, 0x11)),
    )
    client._notification_handler(FakeCharacteristic(), bytearray(matching))
    assert client._pending_settings_transaction is None

    client._notification_handler(FakeCharacteristic(), bytearray(matching))
    assert client._pending_settings_transaction is None


@pytest.mark.asyncio
async def test_multiple_clients_keep_transactions_isolated(
    notification_builder,
) -> None:
    first, _ = _connected_client()
    second, _ = _connected_client()
    first._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    second._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    now = asyncio.get_running_loop().time()
    first._status_monotonic = now
    second._status_monotonic = now

    await first.async_set_ac(True)
    await second.async_set_dc(True)

    assert first._pending_output_transaction is not None
    assert second._pending_output_transaction is not None

    ac_only = notification_builder(
        0x01,
        bytes((0x02, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78)),
    )
    dc_only = notification_builder(
        0x01,
        bytes((0x01, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78)),
    )

    first._notification_handler(FakeCharacteristic(), bytearray(ac_only))
    assert first._pending_output_transaction is None
    assert second._pending_output_transaction is not None

    second._notification_handler(FakeCharacteristic(), bytearray(dc_only))
    assert second._pending_output_transaction is None


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
async def test_command_builders_require_active_session_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _connected_client()
    client._status = _status()
    client._settings = _settings()
    client._status_monotonic = asyncio.get_running_loop().time()
    client._settings_monotonic = asyncio.get_running_loop().time()
    client._active_session_generation = None

    async def no_write(_frame: bytes) -> None:
        return None

    monkeypatch.setattr(client, "_write_frame_unlocked", no_write)

    with pytest.raises(NotConnectedError, match="not connected"):
        await client.async_set_ac(True)
    with pytest.raises(NotConnectedError, match="not connected"):
        await client.async_set_eco(True)
    with pytest.raises(NotConnectedError, match="not connected"):
        await client.async_send_settings_keepalive()


def test_ambiguous_blocked_state_rejects_writes_until_next_version() -> None:
    client, _ = _connected_client()
    now = monotonic()
    client._status = _status()
    client._status_monotonic = now
    client._status_version = 1
    client._output_blocked_until_version = 2

    with pytest.raises(client_module.StateUnavailableError, match="ambiguous"):
        client._safe_output_snapshot()

    client._settings = _settings()
    client._settings_monotonic = now
    client._settings_version = 1
    client._settings_blocked_until_version = 2

    with pytest.raises(client_module.StateUnavailableError, match="ambiguous"):
        client._safe_settings_snapshot()


def test_notification_handler_updates_pending_transaction_error_paths(
    status_frame: bytes,
    settings_frame: bytes,
) -> None:
    client, _ = _connected_client()
    now = monotonic()

    client._pending_output_transaction = client_module.PendingOutputTransaction(
        session_generation=2,
        source_version=1,
        target_dc=True,
        target_ac=True,
        target_light=True,
        sent_monotonic=now,
        confirm_deadline_monotonic=now + 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert client._pending_output_transaction is None

    client._pending_output_transaction = client_module.PendingOutputTransaction(
        session_generation=1,
        source_version=1,
        target_dc=True,
        target_ac=True,
        target_light=True,
        sent_monotonic=now - 20,
        confirm_deadline_monotonic=now - 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert client._pending_output_transaction is None
    assert client._output_blocked_until_version is not None

    client._pending_settings_transaction = client_module.PendingSettingsTransaction(
        session_generation=2,
        source_version=1,
        target=_settings(),
        sent_monotonic=now,
        confirm_deadline_monotonic=now + 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert client._pending_settings_transaction is None

    client._pending_settings_transaction = client_module.PendingSettingsTransaction(
        session_generation=1,
        source_version=1,
        target=_settings(),
        sent_monotonic=now - 20,
        confirm_deadline_monotonic=now - 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert client._pending_settings_transaction is None
    assert client._settings_blocked_until_version is not None


def test_notification_handler_clears_pending_transactions_on_exact_match(
    status_frame: bytes,
    settings_frame: bytes,
) -> None:
    client, _ = _connected_client()
    now = monotonic()

    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert client._status is not None
    client._pending_output_transaction = client_module.PendingOutputTransaction(
        session_generation=1,
        source_version=client._status_version,
        target_dc=client._status.dc_enabled,
        target_ac=client._status.ac_enabled,
        target_light=client._status.light_enabled,
        sent_monotonic=now,
        confirm_deadline_monotonic=now + 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert client._pending_output_transaction is None

    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert client._settings is not None
    client._pending_settings_transaction = client_module.PendingSettingsTransaction(
        session_generation=1,
        source_version=client._settings_version,
        target=client._settings,
        sent_monotonic=now,
        confirm_deadline_monotonic=now + 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert client._pending_settings_transaction is None


def test_notification_handler_keeps_pending_settings_when_confirmation_mismatches(
    settings_frame: bytes,
) -> None:
    client, _ = _connected_client()
    now = monotonic()

    client._pending_settings_transaction = client_module.PendingSettingsTransaction(
        session_generation=1,
        source_version=1,
        target=_settings(raw_flags=0xFF),
        sent_monotonic=now,
        confirm_deadline_monotonic=now + 10,
    )
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))

    assert client._pending_settings_transaction is not None


@pytest.mark.asyncio
async def test_callback_factories_ignore_stale_generation_and_client_mismatch(
    status_frame: bytes,
) -> None:
    client, _ = _connected_client()
    loop = asyncio.get_running_loop()
    first = FakeClient()
    second = FakeClient()

    client._loop = loop
    client._client = first
    client._active_session_generation = 1

    stale_disconnect = client._make_disconnected_callback(2)
    stale_disconnect(first)
    await asyncio.sleep(0)
    assert not client._disconnect_event.is_set()

    wrong_client_disconnect = client._make_disconnected_callback(1)
    wrong_client_disconnect(second)
    await asyncio.sleep(0)
    assert not client._disconnect_event.is_set()

    stale_notify = client._make_notification_handler(2, first)
    stale_notify(FakeCharacteristic(), bytearray(status_frame))
    assert client._status is None

    wrong_client_notify = client._make_notification_handler(1, second)
    wrong_client_notify(FakeCharacteristic(), bytearray(status_frame))
    assert client._status is None


def test_callback_factory_ignores_disconnect_when_loop_is_missing() -> None:
    client, _ = _connected_client()
    client._loop = None
    client._client = FakeClient()
    client._active_session_generation = 1

    callback = client._make_disconnected_callback(1)
    callback(client._client)

    assert not client._disconnect_event.is_set()


@pytest.mark.asyncio
async def test_write_rejects_session_change_after_transport_write() -> None:
    client, fake = _connected_client()

    async def write_and_swap(*args, **kwargs) -> None:
        del args, kwargs
        client._active_session_generation = 2

    fake.write_gatt_char = write_and_swap  # type: ignore[method-assign]

    with pytest.raises(NotConnectedError, match="session changed"):
        await client._write_frame_unlocked(b"test")


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


@pytest.mark.asyncio
async def test_device_name_notification_ignores_empty_name(
    notification_builder,
) -> None:
    client, _ = _connected_client()
    original_name = client.advertised_name
    frame = notification_builder(0x35, b"\x00\x00")

    client._notification_handler(FakeCharacteristic(), bytearray(frame))

    assert client.advertised_name == original_name


def test_notification_handler_without_packets_or_discards_skips_emit_update() -> None:
    class IdleDecoder:
        def __init__(self) -> None:
            self.discarded_frames = 0

        def feed(self, data: bytearray) -> list[object]:
            del data
            return []

    client, _ = _connected_client()
    callbacks = 0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    client._decoder = IdleDecoder()  # type: ignore[assignment]
    client.set_update_callback(update)
    client._notification_handler(FakeCharacteristic(), bytearray(b"noop"))

    assert callbacks == 0


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
