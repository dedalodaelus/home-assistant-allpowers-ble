"""Regression tests for GATT service membership (issue #225)."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import (
    ProbeGattValidationError,
    UnsupportedDeviceError,
)
from tests.ha_stubs import FakeBleakError, FakeCharacteristic, FakeClient, FakeDevice
from tests.test_client_runtime import (
    ProbeClientWithStopNotify,
    _probe_for_cleanup_test,
    make_client,
)


class GattService:
    """Expose only the characteristics belonging to this particular service."""

    def __init__(self, characteristics: dict[str, FakeCharacteristic]) -> None:
        self._characteristics = characteristics

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return self._characteristics.get(uuid)


class GattServices:
    """Model a GATT collection with two independent services.

    Global UUID lookup is unambiguous for misplaced-only cases, but raises
    on duplicate UUIDs as BleakGATTServiceCollection does in production.
    """

    def __init__(
        self, required: tuple[str, ...], *, duplicate_uuids: bool = False
    ) -> None:
        self.notify = FakeCharacteristic(("notify",))
        self.write = FakeCharacteristic(("write", "write-without-response"))
        self.other_notify = FakeCharacteristic(("notify",))
        self.other_write = FakeCharacteristic(("write",))

        required_members: dict[str, FakeCharacteristic] = {}
        other_members: dict[str, FakeCharacteristic] = {}
        if "notify" in required:
            required_members[client_module.NOTIFY_UUID] = self.notify
        if "write" in required:
            required_members[client_module.WRITE_UUID] = self.write
        if duplicate_uuids or "notify" not in required:
            other_members[client_module.NOTIFY_UUID] = self.other_notify
        if duplicate_uuids or "write" not in required:
            other_members[client_module.WRITE_UUID] = self.other_write

        self.required = GattService(required_members)
        self.other = GattService(other_members)

    def get_service(self, uuid: str) -> GattService | None:
        return self.required if uuid == client_module.SERVICE_UUID else None

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        # Match Bleak's collection-wide lookup: ambiguity raises BleakError.
        matches: list[FakeCharacteristic] = []
        for service in (self.required, self.other):
            characteristic = service.get_characteristic(uuid)
            if characteristic is not None:
                matches.append(characteristic)
        if len(matches) > 1:
            raise FakeBleakError("Multiple Characteristics with this UUID")
        return matches[0] if matches else None


@pytest.mark.parametrize("members", [(), ("notify",), ("write",)])
def test_misplaced_characteristics_are_rejected(members: tuple[str, ...]) -> None:
    fake = FakeClient()
    fake.services = GattServices(members)
    # The former global-lookup implementation would have accepted these
    # characteristics despite at least one belonging to another service.
    assert fake.services.get_characteristic(client_module.NOTIFY_UUID) is (
        fake.services.notify if "notify" in members else fake.services.other_notify
    )
    assert fake.services.get_characteristic(client_module.WRITE_UUID) is (
        fake.services.write if "write" in members else fake.services.other_write
    )
    with pytest.raises(UnsupportedDeviceError, match="characteristics"):
        client_module._required_characteristics(fake)


def test_duplicate_uuids_bind_only_to_fff0() -> None:
    fake = FakeClient()
    fake.services = GattServices(("notify", "write"), duplicate_uuids=True)
    for uuid in (client_module.NOTIFY_UUID, client_module.WRITE_UUID):
        with pytest.raises(FakeBleakError, match="Multiple Characteristics"):
            fake.services.get_characteristic(uuid)

    # Scoped lookups must succeed without invoking the ambiguous global one.
    assert client_module._required_characteristics(fake) == (
        fake.services.notify,
        fake.services.write,
    )
    assert client_module._write_with_response(fake.services.write)


def test_old_global_lookup_would_accept_misplaced_characteristics() -> None:
    """Show the old code's false positive without replacing the production code."""
    fake = FakeClient()
    fake.services = GattServices(())

    # This mirrors the original pre-fix global-UUID resolution path.
    assert fake.services.get_service(client_module.SERVICE_UUID) is not None
    notify = fake.services.get_characteristic(client_module.NOTIFY_UUID)
    write = fake.services.get_characteristic(client_module.WRITE_UUID)
    assert notify is fake.services.other_notify
    assert write is fake.services.other_write
    assert set(notify.properties).intersection({"notify", "indicate"})
    assert client_module._write_with_response(write)

    # The corrected implementation must reject this same GATT hierarchy.
    with pytest.raises(UnsupportedDeviceError, match="characteristics"):
        client_module._required_characteristics(fake)


class RecordingRuntimeClient(FakeClient):
    """Record actual characteristic instances used for BLE operations."""

    def __init__(self) -> None:
        super().__init__()
        self.notify_target: object | None = None
        self.write_target: object | None = None

    async def start_notify(self, characteristic: object, callback: Any) -> None:
        self.notify_target = characteristic
        await super().start_notify(characteristic, callback)

    async def write_gatt_char(
        self, characteristic: object, data: bytes, *, response: bool
    ) -> None:
        self.write_target = characteristic
        await super().write_gatt_char(characteristic, data, response=response)


class RecordingProbeClient(ProbeClientWithStopNotify):
    """Record probe characteristic instances without bypassing notifications."""

    def __init__(self, frames: list[bytes]) -> None:
        super().__init__(frames)
        self.notify_target: object | None = None
        self.write_target: object | None = None

    async def start_notify(self, characteristic: object, callback: Any) -> None:
        self.notify_target = characteristic
        await super().start_notify(characteristic, callback)

    async def write_gatt_char(
        self, characteristic: object, data: bytes, *, response: bool
    ) -> None:
        self.write_target = characteristic
        await super().write_gatt_char(characteristic, data, response=response)


@pytest.mark.asyncio
async def test_runtime_uses_fff0_characteristic_instances_with_duplicate_uuids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RecordingRuntimeClient()
    fake.services = GattServices(("notify", "write"), duplicate_uuids=True)
    client = make_client()

    async def establish(*args: Any, **kwargs: Any) -> RecordingRuntimeClient:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())

    await client._connect_once()

    assert fake.notify_target is fake.services.notify
    assert fake.write_target is fake.services.write
    assert fake.write_responses == [True]
    assert fake.notification_callback is not None
    await client._disconnect_client()
    assert fake.disconnect_calls == 1


@pytest.mark.asyncio
async def test_probe_uses_fff0_characteristic_instances_with_duplicate_uuids(
    monkeypatch: pytest.MonkeyPatch,
    status_frame: bytes,
) -> None:
    fake = RecordingProbeClient([status_frame])
    fake.services = GattServices(("notify", "write"), duplicate_uuids=True)

    result = await _probe_for_cleanup_test(monkeypatch, fake, timeout=0.1)

    assert result.status.battery_percent == 73
    assert fake.notify_target is fake.services.notify
    assert fake.write_target is fake.services.write
    assert fake.write_responses == [True]
    assert fake.stop_notify_calls == 1
    assert fake.disconnect_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [(), ("notify",), ("write",)])
async def test_runtime_rejects_misplaced_characteristics_before_io(
    monkeypatch: pytest.MonkeyPatch, members: tuple[str, ...]
) -> None:
    fake = FakeClient()
    fake.services = GattServices(members)
    client = make_client()

    async def establish(*args: Any, **kwargs: Any) -> FakeClient:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    with pytest.raises(UnsupportedDeviceError, match="characteristics"):
        await client._connect_once()
    assert fake.notification_callback is None
    assert fake.writes == []
    assert fake.disconnect_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [(), ("notify",), ("write",)])
async def test_probe_rejects_misplaced_characteristics_before_io(
    monkeypatch: pytest.MonkeyPatch, members: tuple[str, ...]
) -> None:
    fake = ProbeClientWithStopNotify([])
    fake.services = GattServices(members)
    with pytest.raises(ProbeGattValidationError, match="characteristics"):
        await _probe_for_cleanup_test(monkeypatch, fake, timeout=0.1)
    assert fake.notification_callback is None
    assert fake.writes == []
    assert fake.stop_notify_calls == 0
    assert fake.disconnect_calls == 1
