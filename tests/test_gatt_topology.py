"""Regression tests for GATT service membership (issue #225)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import (
    ProbeGattValidationError,
    UnsupportedDeviceError,
)
from tests.ha_stubs import FakeCharacteristic, FakeClient, FakeDevice
from tests.test_client_runtime import (
    ProbeClientWithStopNotify,
    _probe_for_cleanup_test,
    make_client,
)


class GattServices:
    """Model service hierarchy and deliberately misleading global UUID lookup."""

    def __init__(self, required: tuple[str, ...]) -> None:
        self.notify = FakeCharacteristic(("notify",))
        self.write = FakeCharacteristic(("write", "write-without-response"))
        self.other_notify = FakeCharacteristic(("notify",))
        self.other_write = FakeCharacteristic(("write",))
        self.required = SimpleNamespace(
            get_characteristic=lambda uuid: {
                client_module.NOTIFY_UUID: self.notify if "notify" in required else None,
                client_module.WRITE_UUID: self.write if "write" in required else None,
            }.get(uuid)
        )

    def get_service(self, uuid: str) -> object | None:
        return self.required if uuid == client_module.SERVICE_UUID else None

    def get_characteristic(self, uuid: str) -> object | None:
        # Simulate the global lookup returning a valid but misplaced characteristic.
        return {
            client_module.NOTIFY_UUID: self.other_notify,
            client_module.WRITE_UUID: self.other_write,
        }.get(uuid)


@pytest.mark.parametrize("members", [(), ("notify",), ("write",)])
def test_misplaced_characteristics_are_rejected(members: tuple[str, ...]) -> None:
    fake = FakeClient()
    fake.services = GattServices(members)
    with pytest.raises(UnsupportedDeviceError, match="characteristics"):
        client_module._required_characteristics(fake)


def test_duplicate_uuids_bind_only_to_fff0() -> None:
    fake = FakeClient()
    fake.services = GattServices(("notify", "write"))
    assert client_module._required_characteristics(fake) == (
        fake.services.notify,
        fake.services.write,
    )
    assert client_module._write_with_response(fake.services.write)


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
