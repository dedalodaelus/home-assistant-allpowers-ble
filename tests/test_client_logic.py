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
        "hardware_version": "0.3",
        "firmware_version": "1.1",
        "raw_flags": 0xA0,
        "raw_hardware_version": 0x03,
        "raw_firmware_version": 0x11,
    }
    values.update(changes)
    return SettingsData(**values)


def _connected_client(options: ConnectionOptions | None = None):
    client = AllpowersBLEClient(
        hass=object(),
        address="aa:bb:cc:dd:ee:ff",
        advertised_name="ALLPOWERS R600",
        device_identity="ALLPOWERS R600",
        options=options or ConnectionOptions(),
    )
    fake = FakeClient()
    client._client = fake
    client._write_characteristic = FakeCharacteristic()
    client._connected = True
    client._active_session_generation = 1
    client._settings = _settings()
    client._settings_monotonic = monotonic()
    client._schedule_status_refresh = lambda: None
    return client, fake


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control", ["ac", "dc", "light", "eco", "work_mode", "eco_timeout", "car_charger"]
)
async def test_noop_confirmed_control_does_not_block_real_change(control: str) -> None:
    client, fake = _connected_client(ConnectionOptions(enable_car_charger=True))
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    requests = {
        "ac": (client.async_set_ac, False),
        "dc": (client.async_set_dc, False),
        "light": (client.async_set_light, True),
        "eco": (client.async_set_eco, False),
        "work_mode": (client.async_set_work_mode, WorkMode.MUTE),
        "eco_timeout": (client.async_set_eco_timeout, 2),
        "car_charger": (client.async_set_car_charger, False),
    }
    command, value = requests[control]
    for _ in range(2):
        await command(value)
    assert fake.writes == []
    assert client._pending_output_transaction is None
    assert client._pending_settings_transaction is None

    if control in {"ac", "dc", "light"}:
        await client.async_set_ac(True)
    else:
        await client.async_set_work_mode(WorkMode.FAST)
    assert len(fake.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
@pytest.mark.parametrize("write_error", [False, True])
async def test_transaction_expiration_during_write_keeps_group_ambiguous(
    group: str, write_error: bool, notification_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = _connected_client()
    now = 100.0
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    monkeypatch.setattr(client, "_loop_time", lambda: now)
    original_write = fake.write_gatt_char
    notification = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x12, 50, 0, 100, 0, 25, 0, 90))
        if group == "output"
        else bytes((0xA1, 2, 0, 0, 0x03, 0x11)),
    )

    async def write_and_expire(*args, **kwargs) -> None:
        nonlocal now
        await original_write(*args, **kwargs)
        pending = (
            client._pending_output_transaction
            if group == "output"
            else client._pending_settings_transaction
        )
        now = pending.confirm_deadline_monotonic + 1
        client._notification_handler(FakeCharacteristic(), bytearray(notification))
        if write_error:
            raise RuntimeError("expired ambiguous write")

    monkeypatch.setattr(fake, "write_gatt_char", write_and_expire)
    command = client.async_set_ac if group == "output" else client.async_set_eco
    if write_error:
        with pytest.raises(RuntimeError, match="expired ambiguous"):
            await command(True)
    else:
        await command(True)
    assert client._pending_output_transaction is None
    assert client._pending_settings_transaction is None
    blocked = (
        client._output_blocked_until_version
        if group == "output"
        else client._settings_blocked_until_version
    )
    assert blocked == 2
    with pytest.raises(client_module.StateUnavailableError, match="ambiguous"):
        await command(True)
    assert len(fake.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
@pytest.mark.parametrize(
    "guard",
    [
        "pending",
        "ambiguous",
        "stale",
        "semantic",
        "unsupported",
        "disconnected",
        "generation",
    ],
)
async def test_noop_cannot_bypass_snapshot_guards(group: str, guard: str) -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    command = client.async_set_ac if group == "output" else client.async_set_eco
    if guard == "pending":
        await command(True)
    elif guard == "ambiguous":
        if group == "output":
            client._output_blocked_until_version = client._status_version + 1
        else:
            client._settings_blocked_until_version = client._settings_version + 1
    elif guard == "stale":
        client._status_monotonic = now - client.options.stale_timeout - 1
        client._settings_monotonic = now - client.options.settings_stale_timeout - 1
    elif guard == "semantic":
        client._status = _status(raw_flags=0x14)
        client._settings = _settings(work_mode=None)
    elif guard == "unsupported":
        client._settings = _settings(hardware_version="1.0", raw_hardware_version=0x10)
    elif guard == "disconnected":
        client._connected = False
    else:
        client._active_session_generation = None
    expected_error = (
        NotConnectedError
        if guard == "generation"
        else client_module.StateUnavailableError
    )
    with pytest.raises(expected_error):
        await command(False)
    assert len(fake.writes) == (1 if guard == "pending" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
async def test_transaction_notification_during_write(
    group: str, notification_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    original_write = fake.write_gatt_char
    notification = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x12, 50, 0, 100, 0, 25, 0, 90))
        if group == "output"
        else bytes((0xA1, 2, 0, 0, 0x03, 0x11)),
    )

    async def write_and_notify(*args, **kwargs) -> None:
        pending = (
            client._pending_output_transaction
            if group == "output"
            else client._pending_settings_transaction
        )
        assert pending.phase is client_module.TransactionPhase.SENDING
        await original_write(*args, **kwargs)
        client._notification_handler(FakeCharacteristic(), bytearray(notification))

    monkeypatch.setattr(fake, "write_gatt_char", write_and_notify)
    if group == "output":
        await client.async_set_ac(True)
        assert client._pending_output_transaction is None
        assert client._status.ac_enabled is True
    else:
        await client.async_set_eco(True)
        assert client._pending_settings_transaction is None
        assert client._settings.eco_enabled is True
    assert len(fake.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
async def test_transaction_rejects_unconfirmed_dependent_write(group: str) -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now

    if group == "output":
        await client.async_set_ac(True)
        with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
            await client.async_set_dc(True)
        assert client._status.ac_enabled is False
    else:
        await client.async_set_eco(True)
        with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
            await client.async_set_work_mode(WorkMode.FAST)
        assert client._settings.eco_enabled is False
    assert len(fake.writes) == 1


@pytest.mark.asyncio
async def test_transaction_keepalive_rejects_pending_settings() -> None:
    client, fake = _connected_client()
    client._settings_monotonic = asyncio.get_running_loop().time()
    await client.async_set_eco(True)

    with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
        await client.async_send_settings_keepalive()

    assert len(fake.writes) == 1
    assert client._settings.eco_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
@pytest.mark.parametrize(
    "outcome",
    ["error", "cancel", "disconnect", "generation", "matched_error", "timeout"],
)
async def test_transaction_failed_write_requires_new_telemetry(
    group: str, outcome: str, notification_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    entered = asyncio.Event()
    notification = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x12, 50, 0, 100, 0, 25, 0, 90))
        if group == "output"
        else bytes((0xA1, 2, 0, 0, 0x03, 0x11)),
    )

    async def failing_write(*args, **kwargs) -> None:
        del args, kwargs
        entered.set()
        if outcome == "cancel":
            await asyncio.Event().wait()
        if outcome == "matched_error":
            client._notification_handler(FakeCharacteristic(), bytearray(notification))
        if outcome == "disconnect":
            fake.is_connected = False
            return
        if outcome == "generation":
            client._active_session_generation = 2
            return
        if outcome == "timeout":
            raise TimeoutError("ambiguous write")
        raise RuntimeError("ambiguous write")

    monkeypatch.setattr(fake, "write_gatt_char", failing_write)
    command = client.async_set_ac if group == "output" else client.async_set_eco
    task = asyncio.create_task(command(True))
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
    error = (
        asyncio.CancelledError
        if outcome == "cancel"
        else NotConnectedError
        if outcome in {"disconnect", "generation"}
        else TimeoutError
        if outcome == "timeout"
        else RuntimeError
    )
    with pytest.raises(error):
        await task

    assert client._pending_output_transaction is None
    assert client._pending_settings_transaction is None
    if outcome == "generation":
        return
    blocked = (
        client._output_blocked_until_version
        if group == "output"
        else client._settings_blocked_until_version
    )
    version = client._status_version if group == "output" else client._settings_version
    assert blocked == version + 1
    with pytest.raises(client_module.StateUnavailableError):
        await command(False)
    assert fake.writes == []

    if outcome not in {"disconnect", "timeout"}:
        monkeypatch.undo()
        client._notification_handler(FakeCharacteristic(), bytearray(notification))
        await command(False)
        assert len(fake.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
async def test_transaction_contradiction_during_write_revokes_convergence(
    group: str, notification_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    original_write = fake.write_gatt_char
    matching = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x12, 50, 0, 100, 0, 25, 0, 90))
        if group == "output"
        else bytes((0xA1, 2, 0, 0, 0x03, 0x11)),
    )
    contradictory = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x10, 50, 0, 100, 0, 25, 0, 90))
        if group == "output"
        else bytes((0xA0, 2, 0, 0, 0x03, 0x11)),
    )

    async def write_and_notify(*args, **kwargs) -> None:
        await original_write(*args, **kwargs)
        for notification in (matching, matching, contradictory):
            client._notification_handler(FakeCharacteristic(), bytearray(notification))

    monkeypatch.setattr(fake, "write_gatt_char", write_and_notify)
    command = client.async_set_ac if group == "output" else client.async_set_eco
    await command(True)
    pending = (
        client._pending_output_transaction
        if group == "output"
        else client._pending_settings_transaction
    )
    assert pending.phase is client_module.TransactionPhase.AWAITING_CONFIRMATION
    with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
        await command(False)
    assert len(fake.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings"])
async def test_transaction_pre_send_notification_is_not_confirmation(
    group: str, notification_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings_monotonic = now
    encoder_name = (
        "encode_output_control" if group == "output" else "encode_settings_control"
    )
    encoder = getattr(client_module, encoder_name)
    notification = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x12, 50, 0, 100, 0, 25, 0, 90))
        if group == "output"
        else bytes((0xA1, 2, 0, 0, 0x03, 0x11)),
    )

    def encode_and_notify(*args, **kwargs) -> bytes:
        client._notification_handler(FakeCharacteristic(), bytearray(notification))
        return encoder(*args, **kwargs)

    monkeypatch.setattr(client_module, encoder_name, encode_and_notify)
    command = client.async_set_ac if group == "output" else client.async_set_eco
    await command(True)
    pending = (
        client._pending_output_transaction
        if group == "output"
        else client._pending_settings_transaction
    )
    assert pending is not None
    assert pending.source_version == 1
    assert pending.phase is client_module.TransactionPhase.AWAITING_CONFIRMATION
    client._notification_handler(FakeCharacteristic(), bytearray(notification))
    assert client._pending_output_transaction is None
    assert client._pending_settings_transaction is None


@pytest.mark.asyncio
async def test_sequential_output_commands_use_confirmed_snapshot(
    notification_builder,
) -> None:
    client, fake = _connected_client()
    client._status = _status()
    client._status_monotonic = asyncio.get_running_loop().time()

    await client.async_set_ac(True)
    matching = notification_builder(0x01, bytes((0x12, 50, 0, 100, 0, 25, 0, 90)))
    client._notification_handler(FakeCharacteristic(), bytearray(matching))
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
    with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
        await client.async_set_dc(True)

    assert fake.writes[0][7] == 0x22
    assert len(fake.writes) == 1
    assert client._pending_output_transaction is not None


@pytest.mark.asyncio
async def test_consecutive_output_commands_handle_delayed_and_duplicate_confirmations(
    notification_builder,
) -> None:
    client, fake = _connected_client()
    client._status = _status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    client._status_monotonic = asyncio.get_running_loop().time()

    await client.async_set_ac(True)
    with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
        await client.async_set_dc(True)
    ac_only = notification_builder(
        0x01, bytes((0x02, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78))
    )
    client._notification_handler(FakeCharacteristic(), bytearray(ac_only))
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
    await client.async_set_dc(False)
    assert len(fake.writes) == 2


def test_snapshot_guards_reject_disconnected_state() -> None:
    client, _ = _connected_client()
    client._connected = False

    with pytest.raises(client_module.StateUnavailableError, match="fresh status"):
        client._safe_output_snapshot()
    with pytest.raises(client_module.StateUnavailableError, match="fresh settings"):
        client._safe_settings_snapshot()

    client._connected = True
    client._settings_monotonic = None
    with pytest.raises(client_module.StateUnavailableError, match="fresh settings"):
        client._safe_settings_snapshot()


def test_settings_revision_is_not_fresh_outside_current_gatt_session() -> None:
    client, _ = _connected_client()
    assert client._settings_are_fresh()

    client._disconnect_event.set()
    assert not client._settings_are_fresh()
    client._disconnect_event.clear()

    client._connected = False
    assert not client._settings_are_fresh()
    client._connected = True

    client._settings_monotonic = monotonic() - client.options.settings_stale_timeout - 1
    assert not client._settings_are_fresh()


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
async def test_output_command_rejects_unknown_status_bits_on_verified_profile() -> None:
    client, _ = _connected_client()
    client._settings = _settings(hardware_version="0.3", raw_hardware_version=0x03)
    client._settings_monotonic = asyncio.get_running_loop().time()
    client._status = _status(raw_flags=0x14)
    client._status_monotonic = asyncio.get_running_loop().time()

    with pytest.raises(client_module.StateUnavailableError, match="unknown flag bits"):
        await client.async_set_ac(True)


@pytest.mark.asyncio
async def test_sequential_settings_commands_preserve_unknown_bits(
    notification_builder,
) -> None:
    client, fake = _connected_client()
    client._settings = _settings()
    client._settings_monotonic = asyncio.get_running_loop().time()

    await client.async_set_eco(True)
    matching = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    client._notification_handler(FakeCharacteristic(), bytearray(matching))
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
    with pytest.raises(client_module.StateUnavailableError, match="confirmation"):
        await client.async_set_work_mode(WorkMode.FAST)
    eco_only = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    client._notification_handler(FakeCharacteristic(), bytearray(eco_only))
    await client.async_set_work_mode(WorkMode.FAST)

    pending = client._pending_settings_transaction
    assert pending is not None
    assert pending.target.eco_enabled is True
    assert pending.target.work_mode is WorkMode.FAST

    contradictory = notification_builder(
        0x03,
        bytes((0xA1, 2, 0x00, 0x00, 0x03, 0x11)),
    )
    client._notification_handler(FakeCharacteristic(), bytearray(contradictory))
    assert client._pending_settings_transaction is not None

    matching = notification_builder(
        0x03,
        bytes((0xA5, 2, 0x00, 0x00, 0x03, 0x11)),
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
    client, fake = _connected_client()
    client._settings_monotonic = None

    with pytest.raises(client_module.StateUnavailableError):
        await client.async_set_eco(True)
    assert not fake.writes


@pytest.mark.asyncio
async def test_settings_command_rejects_reserved_work_mode_on_verified_profile() -> (
    None
):
    client, _ = _connected_client()
    client._settings = _settings(
        hardware_version="0.3",
        raw_hardware_version=0x03,
        work_mode=None,
    )
    client._settings_monotonic = asyncio.get_running_loop().time()

    with pytest.raises(client_module.StateUnavailableError, match="reserved work mode"):
        await client.async_set_eco(True)


@pytest.mark.asyncio
async def test_settings_command_rejects_unsupported_timeout_on_verified_profile() -> (
    None
):
    client, _ = _connected_client()
    client._settings = _settings(
        hardware_version="0.3",
        raw_hardware_version=0x03,
        eco_timeout_hours=9,
    )
    client._settings_monotonic = asyncio.get_running_loop().time()

    with pytest.raises(
        client_module.StateUnavailableError, match="unsupported ECO timeout"
    ):
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
async def test_write_commands_are_rejected_for_read_only_profiles() -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings = _settings(hardware_version="1.0", raw_hardware_version=0x10)
    client._settings_monotonic = now

    with pytest.raises(
        client_module.StateUnavailableError,
        match="Unsupported output command for active model profile",
    ):
        await client.async_set_ac(True)
    with pytest.raises(
        client_module.StateUnavailableError,
        match="Unsupported settings command for active model profile",
    ):
        await client.async_set_eco(True)
    with pytest.raises(
        client_module.StateUnavailableError,
        match="Unsupported settings keepalive command for active model profile",
    ):
        await client.async_send_settings_keepalive()

    assert fake.writes == []


@pytest.mark.asyncio
async def test_runtime_profile_downgrade_blocks_subsequent_writes() -> None:
    client, fake = _connected_client()
    now = asyncio.get_running_loop().time()
    client._status = _status()
    client._status_monotonic = now
    client._settings = _settings(hardware_version="0.3", raw_hardware_version=0x03)
    client._settings_monotonic = now

    await client.async_set_ac(True)
    assert len(fake.writes) == 1

    client._status = _status(ac_enabled=True)
    client._status_monotonic = asyncio.get_running_loop().time()
    client._settings = _settings(hardware_version="1.0", raw_hardware_version=0x10)
    client._settings_monotonic = asyncio.get_running_loop().time()
    client._pending_output_transaction = None
    client._pending_settings_transaction = None

    with pytest.raises(client_module.StateUnavailableError, match="Unsupported output"):
        await client.async_set_dc(True)
    with pytest.raises(
        client_module.StateUnavailableError,
        match="Unsupported settings command",
    ):
        await client.async_set_eco(False)

    assert len(fake.writes) == 1


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

    assert client.snapshot().statistics.parser_discards == len(status_frame)
    assert client.snapshot().statistics.protocol_errors == 1
    assert client.snapshot().statistics.valid_packets == 0


def test_noise_notification_updates_only_parser_discards(status_frame: bytes) -> None:
    client, _ = _connected_client()

    client._notification_handler(
        FakeCharacteristic(), bytearray(b"noise" + status_frame)
    )

    assert client.snapshot().statistics.parser_discards == len(b"noise")
    assert client.snapshot().statistics.protocol_errors == 0
    assert client.snapshot().statistics.valid_packets == 1


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
            self.discarded_bytes = 0

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


def test_notification_handler_with_only_byte_discards_emits_update() -> None:
    class ByteDiscardDecoder:
        def __init__(self) -> None:
            self.discarded_frames = 0
            self.discarded_bytes = 0

        def feed(self, data: bytearray) -> list[object]:
            self.discarded_bytes += len(data)
            return []

    client, _ = _connected_client()
    callbacks = 0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    client._decoder = ByteDiscardDecoder()  # type: ignore[assignment]
    client.set_update_callback(update)
    client._notification_handler(FakeCharacteristic(), bytearray(b"noop"))

    assert callbacks == 1
    assert client.snapshot().statistics.parser_discards == len(b"noop")
    assert client.snapshot().statistics.protocol_errors == 0


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
    assert client._advertisement_generation == 2
    assert client._advertisement_event.is_set()


@pytest.mark.parametrize(
    "advertised_name",
    [None, "", "AA:BB:CC:DD:EE:FF", "ALLPOWERS", "BLE power station"],
)
@pytest.mark.parametrize("model_identity", ["AP R600 V2.0", "VOLIX P1800"])
def test_restart_identity_survives_unreliable_advertisements(
    model_identity: str,
    advertised_name: str | None,
) -> None:
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name=model_identity,
        device_identity=model_identity,
        options=ConnectionOptions(),
    )
    client._connected = True
    client._settings = _settings()
    client._settings_monotonic = monotonic()

    client.update_advertisement(SimpleNamespace(name=advertised_name, rssi=-55))

    support = client._runtime_model_support()
    assert support.verified
    assert support.profile == "r600-hw-0.3"
    assert support.capabilities.write_output_controls


@pytest.mark.asyncio
async def test_non_identifying_device_name_notification_preserves_identity(
    notification_builder,
) -> None:
    client, _ = _connected_client()
    persisted: list[str] = []
    client._model_identity_callback = persisted.append
    frame = notification_builder(0x35, b"Kitchen")

    client._notification_handler(FakeCharacteristic(), bytearray(frame))

    support = client._runtime_model_support()
    assert support.verified
    assert support.profile == "r600-hw-0.3"
    assert not persisted


def test_identifying_device_name_notification_persists_identity(
    notification_builder,
) -> None:
    client, _ = _connected_client()
    client._device_identity = ""
    persisted: list[str] = []
    client._model_identity_callback = persisted.append
    frame = notification_builder(0x35, b"AP R600 V2.0")

    client._notification_handler(FakeCharacteristic(), bytearray(frame))

    assert persisted == ["R600"]
    assert client.snapshot().model_identity == "R600"


def test_model_identity_persistence_waits_for_current_session_settings(
    notification_builder,
) -> None:
    client, _ = _connected_client()
    client._device_identity = ""
    client._settings_monotonic = None
    persisted: list[str] = []
    client._model_identity_callback = persisted.append

    client.update_advertisement(SimpleNamespace(name="AP R600 V2.0", rssi=-61))
    assert client.snapshot().model_identity == "R600"
    assert not persisted

    frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    client._notification_handler(FakeCharacteristic(), bytearray(frame))

    assert persisted == ["R600"]


@pytest.mark.parametrize(
    "stored_name", [None, "", "AA:BB:CC:DD:EE:FF", "Station Patio"]
)
def test_unverified_stored_identity_stays_read_only_until_valid_name(
    stored_name: str | None,
) -> None:
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name=stored_name or "",
        device_identity=stored_name,
        options=ConnectionOptions(),
    )
    client._connected = True
    client._settings = _settings()
    client._settings_monotonic = monotonic()

    assert not client._runtime_model_support().capabilities.write_output_controls

    client.update_advertisement(SimpleNamespace(name="AP R600 V2.0", rssi=-55))

    support = client._runtime_model_support()
    assert support.verified
    assert support.capabilities.write_output_controls


def test_contradictory_model_advertisement_is_read_only_until_resolved() -> None:
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="AP R600 V2.0",
        device_identity="AP R600 V2.0",
        options=ConnectionOptions(),
    )
    client._connected = True
    client._settings = _settings()
    client._settings_monotonic = monotonic()

    client.update_advertisement(SimpleNamespace(name="AP S300", rssi=-55))
    assert client.snapshot().model_identity == "R600"
    assert client._identity_conflicted
    assert not client._runtime_model_support().capabilities.write_output_controls

    client.update_advertisement(SimpleNamespace(name="AP R600 V2.0", rssi=-55))
    assert not client._identity_conflicted
    assert client._runtime_model_support().capabilities.write_output_controls


@pytest.mark.parametrize(
    ("stored_identity", "contradictory_name"),
    [("R600", "VOLIX P1800"), ("VOLIX P1800", "R600")],
)
def test_verified_family_contradiction_stays_read_only_across_restart(
    stored_identity: str,
    contradictory_name: str,
    notification_builder,
) -> None:
    persisted = {"identity": stored_identity, "conflict": False}
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name=stored_identity,
        device_identity=stored_identity,
        options=ConnectionOptions(),
        model_identity_callback=lambda identity: persisted.update(identity=identity),
        identity_conflict_callback=lambda conflicted: persisted.update(
            conflict=conflicted
        ),
    )
    client._connected = True
    client._settings = _settings()
    client._settings_monotonic = monotonic()

    client.update_advertisement(SimpleNamespace(name=contradictory_name, rssi=-55))

    assert persisted == {"identity": stored_identity, "conflict": True}
    assert client.snapshot().model_identity == stored_identity
    assert not client._runtime_model_support().capabilities.write_output_controls

    restarted = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name=contradictory_name,
        device_identity=persisted["identity"],
        identity_conflicted=persisted["conflict"],
        options=ConnectionOptions(),
        model_identity_callback=lambda identity: persisted.update(identity=identity),
        identity_conflict_callback=lambda conflicted: persisted.update(
            conflict=conflicted
        ),
    )
    restarted._connected = True
    restarted.update_advertisement(SimpleNamespace(name="AA:BB:CC:DD:EE:FF", rssi=-54))
    settings_frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    restarted._notification_handler(FakeCharacteristic(), bytearray(settings_frame))

    assert persisted == {"identity": stored_identity, "conflict": True}
    assert restarted.snapshot().model_identity == stored_identity
    assert not restarted._runtime_model_support().capabilities.write_output_controls

    restarted.update_advertisement(SimpleNamespace(name=stored_identity, rssi=-54))
    assert persisted == {"identity": stored_identity, "conflict": False}
    assert restarted._runtime_model_support().capabilities.write_output_controls


@pytest.mark.parametrize("contradiction", ["AP S300", "AP S500", "AP S700"])
def test_identity_conflict_persists_across_restart_and_recovers_safely(
    contradiction: str,
    notification_builder,
) -> None:
    persisted = {"identity": "R600", "conflict": False}
    first_client, _ = _connected_client()
    first_client._model_identity_callback = lambda identity: persisted.update(
        identity=identity
    )
    first_client._identity_conflict_callback = lambda conflicted: persisted.update(
        conflict=conflicted
    )

    first_client.update_advertisement(SimpleNamespace(name=contradiction, rssi=-55))

    assert first_client.snapshot().model_identity == "R600"
    assert persisted == {"identity": "R600", "conflict": True}
    assert not first_client._runtime_model_support().capabilities.write_output_controls

    restarted = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name=contradiction,
        device_identity=persisted["identity"],
        identity_conflicted=persisted["conflict"],
        model_identity_callback=lambda identity: persisted.update(identity=identity),
        identity_conflict_callback=lambda conflicted: persisted.update(
            conflict=conflicted
        ),
        options=ConnectionOptions(),
    )
    restarted._connected = True
    restarted._active_session_generation = 2
    restarted.update_advertisement(SimpleNamespace(name="AA:BB:CC:DD:EE:FF", rssi=-54))
    assert restarted.snapshot().model_identity == "R600"
    assert not restarted._runtime_model_support().capabilities.write_output_controls

    settings_frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    restarted._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert restarted._identity_conflicted
    restarted.update_advertisement(SimpleNamespace(name="AP R600 V2.0", rssi=-54))

    assert persisted == {"identity": "R600", "conflict": False}
    assert restarted.snapshot().model_identity == "R600"
    assert restarted._runtime_model_support().capabilities.write_output_controls


@pytest.mark.parametrize("rejected_identity", ["AP S500", "AP S700"])
def test_first_rejected_identity_stays_recoverable_after_restart(
    rejected_identity: str,
    notification_builder,
) -> None:
    persisted = {"identity": "", "conflict": False}
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="ALLPOWERS power station",
        model_identity_callback=lambda identity: persisted.update(identity=identity),
        identity_conflict_callback=lambda conflicted: persisted.update(
            conflict=conflicted
        ),
        options=ConnectionOptions(),
    )
    client._connected = True

    client.update_advertisement(SimpleNamespace(name=rejected_identity, rssi=-55))
    client.update_advertisement(SimpleNamespace(name=rejected_identity, rssi=-55))

    assert persisted == {"identity": "", "conflict": True}
    assert client.snapshot().model_identity == ""
    assert client.snapshot().identity_conflicted
    assert not client._runtime_model_support().capabilities.write_output_controls

    restarted = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="ALLPOWERS power station",
        device_identity=persisted["identity"],
        identity_conflicted=persisted["conflict"],
        model_identity_callback=lambda identity: persisted.update(identity=identity),
        identity_conflict_callback=lambda conflicted: persisted.update(
            conflict=conflicted
        ),
        options=ConnectionOptions(),
    )
    restarted._connected = True
    restarted.update_advertisement(SimpleNamespace(name="AA:BB:CC:DD:EE:FF", rssi=-54))
    assert restarted.snapshot().model_identity == ""
    assert restarted.snapshot().identity_conflicted
    assert not restarted._runtime_model_support().capabilities.write_output_controls

    settings_frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    restarted._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    restarted.update_advertisement(SimpleNamespace(name="AP R600 V2.0", rssi=-54))

    assert persisted == {"identity": "R600", "conflict": False}
    assert restarted.snapshot().model_identity == "R600"
    assert not restarted.snapshot().identity_conflicted
    assert restarted._runtime_model_support().capabilities.write_output_controls


def test_first_rejected_identity_latches_without_persistence_callbacks() -> None:
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="ALLPOWERS power station",
        options=ConnectionOptions(),
    )

    client.update_advertisement(SimpleNamespace(name="AP S500", rssi=-55))

    assert client.snapshot().model_identity == ""
    assert client.snapshot().identity_conflicted
    assert not client._runtime_model_support().capabilities.write_output_controls


def test_missing_advertisement_does_not_promote_display_name() -> None:
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="AP R600 V2.0",
        device_identity="",
        options=ConnectionOptions(),
    )
    client._connected = True
    client._settings = _settings()
    client._settings_monotonic = monotonic()

    client.update_advertisement(SimpleNamespace(name=None, rssi=None))

    assert client.snapshot().advertised_name == "AP R600 V2.0"
    assert client.snapshot().model_identity == ""
    assert not client._runtime_model_support().capabilities.write_output_controls


def test_identity_only_advertisement_emits_update_and_persists_once() -> None:
    persisted: list[str] = []
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="AP R600 V2.0",
        device_identity="",
        model_identity_callback=persisted.append,
        options=ConnectionOptions(),
    )
    updates: list[None] = []
    client._connected = True
    client._rssi = -55
    client._settings = _settings()
    client._settings_monotonic = monotonic()
    client.set_update_callback(lambda: updates.append(None))
    service_info = SimpleNamespace(name="AP R600 V2.0", rssi=-55)

    client.update_advertisement(service_info)
    client.update_advertisement(service_info)

    assert persisted == ["R600"]
    assert len(updates) == 1
    assert client.snapshot().model_identity == "R600"


def test_equivalent_r600_names_share_identity_and_persist_once() -> None:
    persisted: list[str] = []
    client = AllpowersBLEClient(
        hass=object(),
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="Unknown",
        device_identity="",
        model_identity_callback=persisted.append,
        options=ConnectionOptions(),
    )
    updates: list[None] = []
    client._connected = True
    client._settings = _settings()
    client._settings_monotonic = monotonic()
    client.set_update_callback(lambda: updates.append(None))

    for name in (
        "AP R600 V2.0",
        "ALLPOWERS R600",
        "allpowers r600",
        "allpowers r600",
    ):
        client.update_advertisement(SimpleNamespace(name=name, rssi=-55))

    assert persisted == ["R600"]
    assert client.snapshot().model_identity == "R600"
    assert client.snapshot().advertised_name == "allpowers r600"
    assert len(updates) == 3


@pytest.mark.asyncio
async def test_cached_revision_cannot_authorize_after_reconnect_before_settings(
    notification_builder,
) -> None:
    client, fake = _connected_client()
    client._session_hardware_revision = ("0.3", 0x03)
    client._activate_session_generation()
    assert client._session_hardware_revision is None
    client._settings_monotonic = None
    client._status = _status()
    client._status_monotonic = monotonic()
    status_frame = notification_builder(0x01, bytes((0x10, 50, 0, 100, 0, 25, 0, 90)))
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))

    assert not client._runtime_model_support().capabilities.write_output_controls
    with pytest.raises(client_module.StateUnavailableError):
        await client.async_set_ac(True)
    assert not fake.writes

    settings_frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert client._runtime_model_support().capabilities.write_output_controls

    await client._disconnect_client()
    assert client._session_hardware_revision is None
    assert not client._runtime_model_support().capabilities.write_output_controls


def test_verified_revision_survives_twenty_minutes_of_status_only_telemetry(
    notification_builder,
) -> None:
    client, _ = _connected_client()
    now = 100.0
    client._loop_time = lambda: now  # type: ignore[method-assign]
    settings_frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    status_frame = notification_builder(0x01, bytes((0x10, 50, 0, 100, 0, 25, 0, 90)))

    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    for _ in range(61):
        now += 20
        client._notification_handler(FakeCharacteristic(), bytearray(status_frame))

    assert now - client._settings_monotonic > 20 * 60
    assert not client._settings_are_fresh()
    assert client._runtime_model_support().capabilities.write_output_controls
    assert client._safe_output_snapshot() == (False, False, True)
    with pytest.raises(client_module.StateUnavailableError, match="fresh settings"):
        client._safe_settings_snapshot()


def test_update_advertisement_debounces_rssi_only_changes() -> None:
    client, _ = _connected_client()
    callbacks = 0
    now = 100.0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    def loop_time() -> float:
        return now

    client.set_update_callback(update)
    client._loop_time = loop_time  # type: ignore[method-assign]

    client.update_advertisement(SimpleNamespace(name="ALLPOWERS R600", rssi=-60))
    assert callbacks == 1

    now += 1
    client.update_advertisement(SimpleNamespace(name="ALLPOWERS R600", rssi=-61))
    assert callbacks == 1

    now += 1
    client.update_advertisement(SimpleNamespace(name="ALLPOWERS R600", rssi=-63))
    assert callbacks == 2


def test_update_advertisement_publishes_rssi_after_max_interval() -> None:
    client, _ = _connected_client()
    callbacks = 0
    now = 10.0

    def update() -> None:
        nonlocal callbacks
        callbacks += 1

    def loop_time() -> float:
        return now

    client.set_update_callback(update)
    client._loop_time = loop_time  # type: ignore[method-assign]

    client.update_advertisement(SimpleNamespace(name="ALLPOWERS R600", rssi=-60))
    assert callbacks == 1

    now += 31.0
    client.update_advertisement(SimpleNamespace(name="ALLPOWERS R600", rssi=-61))
    assert callbacks == 2


def test_should_publish_rssi_handles_none_transitions() -> None:
    client, _ = _connected_client()
    now = 42.0

    def loop_time() -> float:
        return now

    client._loop_time = loop_time  # type: ignore[method-assign]

    assert client._should_publish_rssi(None) is False

    client._last_published_rssi = -60
    assert client._should_publish_rssi(None) is True
    assert client._last_published_rssi is None
    assert client._last_rssi_publish_monotonic == now


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
