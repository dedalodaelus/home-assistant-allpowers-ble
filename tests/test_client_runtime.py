"""Connection-loop, maintenance, probing, and lifecycle tests."""

from __future__ import annotations

import asyncio
from time import monotonic
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import (
    AllpowersBLEClient,
    DeviceNotFoundError,
    NotConnectedError,
    ProbeConnectionTimeoutError,
    ProbeGattValidationError,
    ProbeNotificationSetupError,
    UnsupportedDeviceError,
)
from custom_components.allpowers_ble.options import ConnectionOptions
from custom_components.allpowers_ble.protocol import StateUnavailableError

from tests.ha_stubs import (
    FakeBleakError,
    FakeCharacteristic,
    FakeClient,
    FakeDevice,
)
from tests.helpers import FakeHass, settings, status


class Services:
    """GATT service collection with the required characteristics."""

    def __init__(self, *, service: bool = True, characteristics: bool = True) -> None:
        self.notify = FakeCharacteristic()
        self.write = FakeCharacteristic()
        self.service = service
        self.characteristics = characteristics

    def get_service(self, uuid: str) -> object | None:
        del uuid
        return object() if self.service else None

    def get_characteristic(self, uuid: str) -> object | None:
        if not self.characteristics:
            return None
        return {
            client_module.NOTIFY_UUID: self.notify,
            client_module.WRITE_UUID: self.write,
        }.get(uuid)


class IterationStop:
    """Event-like object that permits a fixed number of maintenance iterations."""

    def __init__(self, iterations: int = 1) -> None:
        self.iterations = iterations
        self.count = 0

    def is_set(self) -> bool:
        return self.count >= self.iterations

    async def wait(self) -> None:
        self.count += 1
        raise TimeoutError

    def set(self) -> None:
        self.count = self.iterations

    def clear(self) -> None:
        self.count = 0


def make_client(
    options: ConnectionOptions | None = None,
    *,
    name: str = "ALLPOWERS R600",
) -> AllpowersBLEClient:
    return AllpowersBLEClient(
        hass=FakeHass(),  # type: ignore[arg-type]
        address="aa:bb:cc:dd:ee:ff",
        advertised_name=name,
        options=options or ConnectionOptions(),
    )


def connect_fake(client: AllpowersBLEClient) -> FakeClient:
    fake = FakeClient()
    client._client = fake
    client._write_characteristic = FakeCharacteristic()
    client._connected = True
    client._active_session_generation = 1
    client._connected_monotonic = monotonic()
    return fake


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels_owned_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()

    async def long_running() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_connection_loop", long_running)
    monkeypatch.setattr(client, "_maintenance_loop", long_running)
    updates = 0

    def update() -> None:
        nonlocal updates
        updates += 1

    client.set_update_callback(update)
    await client.async_start()
    first_task = client._connection_task
    await client.async_start()
    assert client._connection_task is first_task
    assert client._maintenance_task is not None

    await client.async_stop()
    assert client._connection_task is None
    assert client._maintenance_task is None
    assert client._refresh_task is None
    assert client._update_callback is None
    assert updates == 0


@pytest.mark.asyncio
async def test_stop_without_running_tasks_is_safe() -> None:
    client = make_client()

    await client.async_stop()

    assert client._connection_task is None
    assert client._maintenance_task is None
    assert client._refresh_task is None


@pytest.mark.asyncio
async def test_wait_ready_apply_options_reconnect_and_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    client._ready_event.set()
    await client.async_wait_ready(0.1)

    enabled = ConnectionOptions(settings_keepalive=True)
    await client.async_apply_options(enabled)
    assert client._initial_settings_keepalive_pending
    disabled = ConnectionOptions(settings_keepalive=False)
    await client.async_apply_options(disabled)
    assert not client._initial_settings_keepalive_pending

    unchanged = make_client(ConnectionOptions(settings_keepalive=True))
    unchanged._initial_settings_keepalive_pending = False
    await unchanged.async_apply_options(ConnectionOptions(settings_keepalive=True))
    assert not unchanged._initial_settings_keepalive_pending

    disconnected = 0

    async def disconnect() -> None:
        nonlocal disconnected
        disconnected += 1

    monkeypatch.setattr(client, "_disconnect_client", disconnect)
    await client.async_reconnect("manual")
    assert client._disconnect_event.is_set()
    assert client.snapshot().last_error == "manual"
    assert disconnected == 1

    fake = connect_fake(client)
    client._status = status()
    client._status_monotonic = monotonic()
    client._settings = settings()
    client._settings_monotonic = monotonic()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    await client.async_set_light(False)
    await client.async_set_eco_timeout(6)
    assert len(fake.writes) == 2


@pytest.mark.asyncio
async def test_expired_pending_transactions_require_fresh_updates(
    monkeypatch: pytest.MonkeyPatch,
    status_frame: bytes,
    settings_frame: bytes,
) -> None:
    client = make_client()
    fake = connect_fake(client)
    now = monotonic()
    client._status = status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    client._status_monotonic = now
    client._status_version = 1
    client._pending_output_transaction = client_module.PendingOutputTransaction(
        session_generation=1,
        source_version=1,
        target_dc=True,
        target_ac=True,
        target_light=True,
        sent_monotonic=now - 10,
        confirm_deadline_monotonic=now - 1,
    )
    client._settings = settings(raw_flags=0xA0)
    client._settings_monotonic = now
    client._settings_version = 1
    client._pending_settings_transaction = client_module.PendingSettingsTransaction(
        session_generation=1,
        source_version=1,
        target=settings(raw_flags=0xFF),
        sent_monotonic=now - 10,
        confirm_deadline_monotonic=now - 1,
    )
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)

    with pytest.raises(StateUnavailableError, match="timed out"):
        await client.async_set_ac(True)
    with pytest.raises(StateUnavailableError, match="timed out"):
        await client.async_set_eco(True)

    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))

    await client.async_set_ac(True)
    await client.async_set_eco(True)

    assert len(fake.writes) == 2


@pytest.mark.asyncio
async def test_connection_loop_success_reconnect_and_error_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    attempts = 0
    disconnects = 0

    async def connect() -> None:
        nonlocal attempts
        attempts += 1
        client._disconnect_event.set()
        if attempts == 2:
            client._stop_event.set()

    async def disconnect() -> None:
        nonlocal disconnects
        disconnects += 1

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(client, "_connect_once", connect)
    monkeypatch.setattr(client, "_disconnect_client", disconnect)
    monkeypatch.setattr(client, "_sleep_until_retry", no_sleep)
    await client._connection_loop()
    assert attempts == 2
    assert disconnects == 2
    assert client.snapshot().statistics.reconnects == 1

    for error in (DeviceNotFoundError("missing"), RuntimeError("boom")):
        another = make_client()

        async def fail(error: Exception = error) -> None:
            another._stop_event.set()
            raise error

        monkeypatch.setattr(another, "_connect_once", fail)
        monkeypatch.setattr(another, "_disconnect_client", disconnect)
        await another._connection_loop()
        assert type(error).__name__ in (another.snapshot().last_error or "")


def test_retry_delay_with_jitter_is_bounded_and_seeded_per_client() -> None:
    options = ConnectionOptions(reconnect_max_delay=60)
    first = make_client(options)
    second = make_client(options)
    other = make_client(options, name="ALLPOWERS S700")
    other.address = "11:22:33:44:55:66"
    other._reconnect_jitter = client_module.Random(other.address).uniform

    base_delay = 40.0
    first_values = [first._retry_delay_with_jitter(base_delay) for _ in range(5)]
    second_values = [second._retry_delay_with_jitter(base_delay) for _ in range(5)]
    other_values = [other._retry_delay_with_jitter(base_delay) for _ in range(5)]

    assert first_values == second_values
    assert first_values != other_values
    assert all(0.0 <= value <= options.reconnect_max_delay for value in first_values)
    capped = first._retry_delay_with_jitter(120.0)
    assert 0.0 <= capped <= options.reconnect_max_delay


@pytest.mark.asyncio
async def test_connection_loop_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    entered = asyncio.Event()

    async def block() -> None:
        entered.set()
        await asyncio.Event().wait()

    async def disconnect() -> None:
        return None

    monkeypatch.setattr(client, "_connect_once", block)
    monkeypatch.setattr(client, "_disconnect_client", disconnect)
    task = asyncio.create_task(client._connection_loop())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_connection_loop_exits_immediately_when_stop_already_set() -> None:
    client = make_client()
    client._stop_event.set()

    await client._connection_loop()

    assert client.snapshot().statistics.connection_attempts == 0


@pytest.mark.asyncio
async def test_connect_once_success_missing_route_and_unsupported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    fake = FakeClient()
    fake.services = Services()
    captured: dict[str, Any] = {}

    async def establish(*args: Any, **kwargs: Any) -> FakeClient:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    await client._connect_once()
    assert client.snapshot().connected
    assert fake.notification_callback is not None
    assert fake.writes[-1] == client_module.encode_status_request()
    assert captured["ble_device_callback"] == client._fresh_ble_device
    assert client.snapshot().statistics.successful_connections == 1

    missing = make_client()
    monkeypatch.setattr(missing, "_fresh_ble_device", lambda: None)
    with pytest.raises(DeviceNotFoundError):
        await missing._connect_once()

    unsupported = make_client(name="AP S700")
    monkeypatch.setattr(unsupported, "_fresh_ble_device", lambda: FakeDevice())
    with pytest.raises(UnsupportedDeviceError):
        await unsupported._connect_once()


@pytest.mark.asyncio
async def test_connect_once_disconnects_when_gatt_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    fake = FakeClient()
    fake.services = Services(service=False)

    async def establish(*args: Any, **kwargs: Any) -> FakeClient:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    with pytest.raises(UnsupportedDeviceError):
        await client._connect_once()
    assert fake.disconnect_calls == 1


@pytest.mark.asyncio
async def test_disconnect_cancels_refresh_and_tolerates_ble_error() -> None:
    class ErrorDisconnectClient(FakeClient):
        async def disconnect(self) -> None:
            raise FakeBleakError("disconnect failed")

    client = make_client()
    fake = ErrorDisconnectClient()
    client._client = fake
    client._write_characteristic = FakeCharacteristic()
    client._connected = True
    client._active_session_generation = 1
    client._ready_event.set()
    client._pending_output_transaction = client_module.PendingOutputTransaction(
        session_generation=1,
        source_version=1,
        target_dc=True,
        target_ac=True,
        target_light=True,
        sent_monotonic=monotonic(),
        confirm_deadline_monotonic=monotonic() + 10,
    )
    client._pending_settings_transaction = client_module.PendingSettingsTransaction(
        session_generation=1,
        source_version=1,
        target=settings(),
        sent_monotonic=monotonic(),
        confirm_deadline_monotonic=monotonic() + 10,
    )
    refresh = asyncio.create_task(asyncio.sleep(100))
    client._refresh_task = refresh

    await client._disconnect_client()

    assert refresh.cancelled() or refresh.cancelling()
    assert client._client is None
    assert not client.snapshot().connected
    assert not client._ready_event.is_set()
    assert client._pending_output_transaction is None
    assert client._pending_settings_transaction is None


@pytest.mark.asyncio
async def test_disconnected_callback_handles_loop_states() -> None:
    client = make_client()
    client._disconnected_callback(FakeClient())
    assert not client._disconnect_event.is_set()

    client._loop = asyncio.get_running_loop()
    client._disconnected_callback(FakeClient())
    await asyncio.sleep(0)
    assert client._disconnect_event.is_set()

    client._disconnect_event.clear()
    client._loop = SimpleNamespace(is_closed=lambda: True)  # type: ignore[assignment]
    client._disconnected_callback(FakeClient())
    assert not client._disconnect_event.is_set()


@pytest.mark.asyncio
async def test_disconnect_waits_for_in_flight_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingWriteClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = asyncio.Event()
            self.release_write = asyncio.Event()

        async def write_gatt_char(
            self,
            characteristic: object,
            data: bytes,
            *,
            response: bool,
        ) -> None:
            self.write_started.set()
            await self.release_write.wait()
            await super().write_gatt_char(characteristic, data, response=response)

    client = make_client()
    fake = BlockingWriteClient()
    client._client = fake
    client._write_characteristic = FakeCharacteristic()
    client._connected = True
    client._active_session_generation = 1
    client._status = status(dc_enabled=False, ac_enabled=False, light_enabled=False)
    client._status_monotonic = monotonic()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)

    write_task = asyncio.create_task(client.async_set_ac(True))
    await fake.write_started.wait()

    disconnect_task = asyncio.create_task(client._disconnect_client())
    await asyncio.sleep(0)
    assert not disconnect_task.done()

    fake.release_write.set()
    await write_task
    await disconnect_task

    assert fake.disconnect_calls == 1
    assert not client.snapshot().connected


@pytest.mark.asyncio
async def test_cancelled_transport_write_releases_operation_lock() -> None:
    class BlockingWriteClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = asyncio.Event()
            self.release_write = asyncio.Event()

        async def write_gatt_char(
            self,
            characteristic: object,
            data: bytes,
            *,
            response: bool,
        ) -> None:
            self.write_started.set()
            await self.release_write.wait()
            await super().write_gatt_char(characteristic, data, response=response)

    client = make_client()
    fake = BlockingWriteClient()
    client._client = fake
    client._write_characteristic = FakeCharacteristic()
    client._connected = True
    client._active_session_generation = 1

    first = asyncio.create_task(client.async_request_status())
    await fake.write_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    fake.release_write.set()
    await client.async_request_status()

    assert len(fake.writes) == 1


@pytest.mark.asyncio
async def test_session_generation_rejects_stale_disconnect_and_notifications(
    status_frame: bytes,
) -> None:
    client = make_client()
    loop = asyncio.get_running_loop()
    client._loop = loop

    first = FakeClient()
    second = FakeClient()

    generation_one = client._activate_session_generation()
    client._client = first
    stale_disconnect = client._make_disconnected_callback(generation_one)
    stale_notify = client._make_notification_handler(generation_one, first)

    generation_two = client._activate_session_generation()
    client._client = second
    client._connected = True
    active_disconnect = client._make_disconnected_callback(generation_two)
    active_notify = client._make_notification_handler(generation_two, second)

    stale_notify(FakeCharacteristic(), bytearray(status_frame))
    assert client._status is None
    assert not client._ready_event.is_set()

    active_notify(FakeCharacteristic(), bytearray(status_frame))
    assert client._status is not None
    assert client._ready_event.is_set()

    client._disconnect_event.clear()
    stale_disconnect(first)
    await asyncio.sleep(0)
    assert not client._disconnect_event.is_set()

    active_disconnect(second)
    await asyncio.sleep(0)
    assert client._disconnect_event.is_set()


@pytest.mark.asyncio
async def test_scheduled_status_refresh_runs_and_handles_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    connect_fake(client)
    monkeypatch.setattr(client_module, "COMMAND_REFRESH_DELAY", 0)
    calls = 0

    async def request() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(client, "async_request_status", request)
    client._schedule_status_refresh()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 1
    assert client._refresh_task is None

    old = asyncio.create_task(asyncio.sleep(100))
    client._refresh_task = old
    client._schedule_status_refresh()
    assert old.cancelling()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    async def disconnected() -> None:
        raise NotConnectedError("offline")

    monkeypatch.setattr(client, "async_request_status", disconnected)
    await client._delayed_status_refresh()


@pytest.mark.asyncio
async def test_maintenance_watchdog_and_status_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    client._connected = True
    client._connected_monotonic = monotonic() - 100
    client._stop_event = IterationStop()  # type: ignore[assignment]
    reasons: list[str] = []

    async def reconnect(reason: str) -> None:
        reasons.append(reason)

    monkeypatch.setattr(client, "async_reconnect", reconnect)
    await client._maintenance_loop()
    assert reasons == ["Telemetry watchdog expired"]
    assert client.snapshot().statistics.watchdog_resets == 1
    assert client.snapshot().statistics.telemetry_watchdog_resets == 1
    assert client.snapshot().statistics.transport_watchdog_resets == 0

    status_missing = make_client()
    status_missing._connected = True
    status_missing._connected_monotonic = monotonic() - 100
    status_missing._last_packet_monotonic = monotonic()
    status_missing._stop_event = IterationStop()  # type: ignore[assignment]
    status_missing_reasons: list[str] = []

    async def reconnect_status_missing(reason: str) -> None:
        status_missing_reasons.append(reason)

    monkeypatch.setattr(status_missing, "async_reconnect", reconnect_status_missing)
    await status_missing._maintenance_loop()
    assert status_missing_reasons == ["Telemetry watchdog expired"]
    assert status_missing.snapshot().statistics.watchdog_resets == 1
    assert status_missing.snapshot().statistics.telemetry_watchdog_resets == 1

    requester = make_client()
    requester._connected = True
    requester._connected_monotonic = monotonic()
    requester._last_packet_monotonic = monotonic()
    requester._stop_event = IterationStop()  # type: ignore[assignment]
    requests = 0

    async def request() -> None:
        nonlocal requests
        requests += 1

    monkeypatch.setattr(requester, "async_request_status", request)
    await requester._maintenance_loop()
    assert requests == 1

    requester._stop_event = IterationStop()  # type: ignore[assignment]

    async def fail_request() -> None:
        raise FakeBleakError("radio")

    monkeypatch.setattr(requester, "async_request_status", fail_request)
    await requester._maintenance_loop()


@pytest.mark.asyncio
async def test_maintenance_transport_watchdog_uses_any_packet_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    client._connected = True
    client._connected_monotonic = monotonic() - 100
    client._status_monotonic = monotonic()
    client._last_packet_monotonic = monotonic() - 100
    client._stop_event = IterationStop()  # type: ignore[assignment]
    reasons: list[str] = []

    async def reconnect(reason: str) -> None:
        reasons.append(reason)

    monkeypatch.setattr(client, "async_reconnect", reconnect)
    await client._maintenance_loop()
    assert reasons == ["Transport watchdog expired"]
    assert client.snapshot().statistics.watchdog_resets == 1
    assert client.snapshot().statistics.telemetry_watchdog_resets == 0
    assert client.snapshot().statistics.transport_watchdog_resets == 1


@pytest.mark.asyncio
async def test_maintenance_initial_and_periodic_settings_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = ConnectionOptions(settings_keepalive=True)
    client = make_client(options)
    client._connected = True
    client._connected_monotonic = monotonic()
    client._last_packet_monotonic = monotonic()
    client._last_status_request_monotonic = monotonic()
    client._settings = settings()
    client._settings_monotonic = monotonic()
    client._initial_settings_keepalive_pending = True
    client._stop_event = IterationStop()  # type: ignore[assignment]
    sends = 0

    async def send() -> None:
        nonlocal sends
        sends += 1
        client._initial_settings_keepalive_pending = False
        client._last_settings_keepalive_monotonic = monotonic()

    monkeypatch.setattr(client, "async_send_settings_keepalive", send)
    await client._maintenance_loop()
    assert sends == 1

    client._stop_event = IterationStop()  # type: ignore[assignment]
    client._initial_settings_keepalive_pending = False
    client._last_settings_keepalive_monotonic = (
        monotonic() - options.settings_keepalive_interval - 1
    )
    await client._maintenance_loop()
    assert sends == 2

    client._stop_event = IterationStop()  # type: ignore[assignment]
    client._last_settings_keepalive_monotonic = (
        monotonic() - options.settings_keepalive_interval - 1
    )

    async def fail_send() -> None:
        raise StateUnavailableError("stale")

    monkeypatch.setattr(client, "async_send_settings_keepalive", fail_send)
    await client._maintenance_loop()


@pytest.mark.asyncio
async def test_sleep_fresh_device_and_freshness_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    await client._sleep_until_retry(0.001)
    client._stop_event.set()
    await client._sleep_until_retry(1)

    sentinel = FakeDevice()
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable: sentinel,
    )
    assert client._fresh_ble_device() is sentinel

    client._connected = True
    client._status_monotonic = monotonic()
    client._settings_monotonic = monotonic()
    updates = 0

    def update() -> None:
        nonlocal updates
        updates += 1

    client.set_update_callback(update)
    client._emit_freshness_change(monotonic())
    client._emit_freshness_change(monotonic())
    assert updates == 1
    client._emit_freshness_change(
        monotonic() + client.options.settings_stale_timeout + 1
    )
    assert updates == 2


class ProbeClient(FakeClient):
    """Fake client that can emit notifications in response to a status request."""

    def __init__(
        self,
        frames: list[bytes],
        *,
        disconnect_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.services = Services()
        self.frames = frames
        self.disconnect_error = disconnect_error

    async def write_gatt_char(
        self,
        characteristic: object,
        data: bytes,
        *,
        response: bool,
    ) -> None:
        await super().write_gatt_char(characteristic, data, response=response)
        assert self.notification_callback is not None
        for frame in self.frames:
            self.notification_callback(FakeCharacteristic(), bytearray(frame))

    async def disconnect(self) -> None:
        if self.disconnect_error is not None:
            raise self.disconnect_error
        await super().disconnect()


class ProbeClientWithStopNotify(ProbeClient):
    """Probe client variant that records stop_notify cleanup calls."""

    def __init__(self, frames: list[bytes]) -> None:
        super().__init__(frames)
        self.stop_notify_calls = 0

    async def stop_notify(self, characteristic: object) -> None:
        del characteristic
        self.stop_notify_calls += 1


@pytest.mark.asyncio
async def test_probe_success_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    status_frame: bytes,
    settings_frame: bytes,
    notification_builder,
) -> None:
    fake = ProbeClient([settings_frame, status_frame])

    async def establish(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )

    result = await client_module.async_probe_device(
        FakeHass(),  # type: ignore[arg-type]
        address="aa:bb:cc:dd:ee:ff",
        advertised_name="ALLPOWERS R600",
        timeout=0.1,
    )
    assert result.status.battery_percent == 73
    assert result.settings is not None

    mixed = ProbeClient([notification_builder(0x35, b"ProbeName"), status_frame])

    async def establish_mixed(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return mixed

    monkeypatch.setattr(client_module, "establish_connection", establish_mixed)
    result = await client_module.async_probe_device(
        FakeHass(),  # type: ignore[arg-type]
        address="aa:bb:cc:dd:ee:ff",
        advertised_name="ALLPOWERS R600",
        timeout=0.1,
    )
    assert result.status.battery_percent == 73
    assert result.model_support.verified
    assert fake.disconnect_calls == 1

    tolerant = ProbeClient([status_frame], disconnect_error=FakeBleakError("bye"))

    async def establish_tolerant(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return tolerant

    monkeypatch.setattr(client_module, "establish_connection", establish_tolerant)
    result = await client_module.async_probe_device(
        FakeHass(),  # type: ignore[arg-type]
        address="aa:bb:cc:dd:ee:ff",
        advertised_name="ALLPOWERS R600",
        timeout=0.1,
    )
    assert result.status.battery_percent == 73


@pytest.mark.asyncio
async def test_probe_rejects_model_route_gatt_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(UnsupportedDeviceError):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="AP S700",
            timeout=0.01,
        )

    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(DeviceNotFoundError):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.01,
        )

    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )
    invalid_gatt = FakeClient()
    invalid_gatt.services = Services(service=False)

    async def establish_invalid(*args: Any, **kwargs: Any) -> FakeClient:
        del args, kwargs
        return invalid_gatt

    monkeypatch.setattr(client_module, "establish_connection", establish_invalid)
    with pytest.raises(ProbeGattValidationError):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.01,
        )
    assert invalid_gatt.disconnect_calls == 1

    silent = ProbeClient([])

    async def establish_silent(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return silent

    monkeypatch.setattr(client_module, "establish_connection", establish_silent)
    with pytest.raises(TimeoutError):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.001,
        )
    assert silent.disconnect_calls == 1


@pytest.mark.asyncio
async def test_probe_reports_connection_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )

    async def establish_slow(*args: Any, **kwargs: Any) -> FakeClient:
        del args, kwargs
        await asyncio.sleep(0.02)
        return FakeClient()

    monkeypatch.setattr(client_module, "establish_connection", establish_slow)
    with pytest.raises(ProbeConnectionTimeoutError):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.001,
        )


@pytest.mark.asyncio
async def test_probe_reports_notification_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotifyFailClient(ProbeClient):
        async def start_notify(self, characteristic: object, callback: Any) -> None:
            del characteristic, callback
            raise FakeBleakError("notify failed")

    failing = NotifyFailClient([])

    async def establish_notify_fail(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return failing

    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )
    monkeypatch.setattr(client_module, "establish_connection", establish_notify_fail)

    with pytest.raises(ProbeNotificationSetupError):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.05,
        )
    assert failing.disconnect_calls == 1


@pytest.mark.asyncio
async def test_probe_reports_notification_setup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotifyTimeoutClient(ProbeClient):
        async def start_notify(self, characteristic: object, callback: Any) -> None:
            del characteristic, callback
            await asyncio.sleep(0.02)

    timing_out = NotifyTimeoutClient([])

    async def establish_notify_timeout(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return timing_out

    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )
    monkeypatch.setattr(client_module, "establish_connection", establish_notify_timeout)

    with pytest.raises(ProbeNotificationSetupError, match="timed out"):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.001,
        )
    assert timing_out.disconnect_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "ble_error"])
async def test_probe_reports_status_request_failures(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    class WriteFailureProbeClient(ProbeClient):
        async def write_gatt_char(
            self,
            characteristic: object,
            data: bytes,
            *,
            response: bool,
        ) -> None:
            del characteristic, data, response
            if mode == "timeout":
                await asyncio.sleep(0.02)
                return
            raise FakeBleakError("write failed")

    failing = WriteFailureProbeClient([])

    async def establish_write_failure(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return failing

    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )
    monkeypatch.setattr(client_module, "establish_connection", establish_write_failure)

    if mode == "timeout":
        error_match = "status request timed out"
    else:
        error_match = "status request failed"

    with pytest.raises(ProbeNotificationSetupError, match=error_match):
        await client_module.async_probe_device(
            FakeHass(),  # type: ignore[arg-type]
            address="AA:BB:CC:DD:EE:FF",
            advertised_name="ALLPOWERS R600",
            timeout=0.001,
        )
    assert failing.disconnect_calls == 1


@pytest.mark.asyncio
async def test_probe_cleanup_stops_notifications_when_available(
    monkeypatch: pytest.MonkeyPatch,
    status_frame: bytes,
) -> None:
    fake = ProbeClientWithStopNotify([status_frame])

    async def establish(*args: Any, **kwargs: Any) -> ProbeClient:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )

    result = await client_module.async_probe_device(
        FakeHass(),  # type: ignore[arg-type]
        address="aa:bb:cc:dd:ee:ff",
        advertised_name="ALLPOWERS R600",
        timeout=0.1,
    )

    assert result.status.battery_percent == 73
    assert fake.stop_notify_calls == 1
    assert fake.disconnect_calls == 1
