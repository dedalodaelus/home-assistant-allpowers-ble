"""Hardware-free GATT hierarchy integration tests using the installed real Bleak."""

from __future__ import annotations

from importlib.metadata import version
from types import SimpleNamespace

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection
from bleak.exc import BleakError
import pytest

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.const import (
    NOTIFY_UUID,
    SERVICE_UUID,
    WRITE_UUID,
)

pytestmark = pytest.mark.homeassistant

OTHER_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"


def _real_gatt_collection(
    *,
    fff0_present: bool = True,
    fff0_notify: tuple[str, ...] | None = ("notify",),
    fff0_write: tuple[str, ...] | None = ("write",),
    other_notify: tuple[str, ...] | None = None,
    other_write: tuple[str, ...] | None = None,
) -> tuple[BleakGATTServiceCollection, dict[str, BleakGATTCharacteristic]]:
    """Construct actual Bleak GATT metadata without connecting to any adapter."""
    collection = BleakGATTServiceCollection()
    required = BleakGATTService(obj=None, handle=1, uuid=SERVICE_UUID)
    other = BleakGATTService(obj=None, handle=10, uuid=OTHER_SERVICE_UUID)
    if fff0_present:
        collection.add_service(required)
    collection.add_service(other)

    result: dict[str, BleakGATTCharacteristic] = {}

    def add(
        name: str,
        service: BleakGATTService,
        uuid: str,
        properties: tuple[str, ...] | None,
        handle: int,
    ) -> None:
        if properties is None:
            return
        characteristic = BleakGATTCharacteristic(
            obj=None,
            handle=handle,
            uuid=uuid,
            properties=list(properties),
            max_write_without_response_size=lambda: 20,
            service=service,
        )
        collection.add_characteristic(characteristic)
        result[name] = characteristic

    if fff0_present:
        add("fff0_notify", required, NOTIFY_UUID, fff0_notify, 2)
        add("fff0_write", required, WRITE_UUID, fff0_write, 3)
    add("other_notify", other, NOTIFY_UUID, other_notify, 11)
    add("other_write", other, WRITE_UUID, other_write, 12)

    return collection, result


def _resolve(
    collection: BleakGATTServiceCollection,
) -> tuple[BleakGATTCharacteristic, BleakGATTCharacteristic]:
    """Exercise the production validator with a client exposing real services."""
    return client_module._required_characteristics(SimpleNamespace(services=collection))


def test_real_bleak_classes_are_installed_not_test_stubs() -> None:
    assert version("bleak")
    assert BleakGATTServiceCollection.__module__ == "bleak.backends.service"
    assert BleakGATTService.__module__ == "bleak.backends.service"
    assert BleakGATTCharacteristic.__module__ == "bleak.backends.characteristic"


@pytest.mark.parametrize(
    ("notify_properties", "write_properties", "expected_response"),
    [
        (("notify",), ("write",), True),
        (("indicate",), ("write-without-response",), False),
        (("notify", "indicate"), ("write", "write-without-response"), True),
    ],
)
def test_real_bleak_valid_fff0_and_write_mode(
    notify_properties: tuple[str, ...],
    write_properties: tuple[str, ...],
    expected_response: bool,
) -> None:
    collection, chars = _real_gatt_collection(
        fff0_notify=notify_properties,
        fff0_write=write_properties,
    )
    notify, write = _resolve(collection)
    assert notify is chars["fff0_notify"]
    assert write is chars["fff0_write"]
    assert notify.service_uuid == write.service_uuid == SERVICE_UUID
    assert notify.service_handle == write.service_handle == 1
    assert client_module._write_with_response(write) is expected_response


@pytest.mark.parametrize(
    ("notify_misplaced", "write_misplaced"),
    [(True, True), (True, False), (False, True)],
)
def test_real_bleak_rejects_misplaced_and_partial_layouts(
    notify_misplaced: bool, write_misplaced: bool
) -> None:
    collection, chars = _real_gatt_collection(
        fff0_notify=None if notify_misplaced else ("notify",),
        fff0_write=None if write_misplaced else ("write",),
        other_notify=("notify",) if notify_misplaced else None,
        other_write=("write",) if write_misplaced else None,
    )
    # The old global lookups would have found a valid FFF1 and FFF2 here.
    global_notify = collection.get_characteristic(NOTIFY_UUID)
    global_write = collection.get_characteristic(WRITE_UUID)
    expected_notify_key = "other_notify" if notify_misplaced else "fff0_notify"
    expected_write_key = "other_write" if write_misplaced else "fff0_write"
    assert global_notify is chars[expected_notify_key]
    assert global_write is chars[expected_write_key]
    with pytest.raises(client_module.UnsupportedDeviceError, match="characteristics"):
        _resolve(collection)


@pytest.mark.parametrize(
    "duplicated",
    [("notify",), ("write",), ("notify", "write")],
)
def test_real_bleak_duplicate_uuids_are_resolved_within_fff0(
    duplicated: tuple[str, ...],
) -> None:
    collection, chars = _real_gatt_collection(
        other_notify=("notify",) if "notify" in duplicated else None,
        other_write=("write",) if "write" in duplicated else None,
    )
    for name, uuid in (("notify", NOTIFY_UUID), ("write", WRITE_UUID)):
        if name in duplicated:
            with pytest.raises(BleakError, match="Multiple Characteristics"):
                collection.get_characteristic(uuid)

    notify, write = _resolve(collection)
    assert notify is chars["fff0_notify"]
    assert write is chars["fff0_write"]
    assert notify.service_handle == write.service_handle == 1


@pytest.mark.parametrize(
    ("notify_properties", "write_properties", "error"),
    [
        (("read",), ("write",), "notify or indicate"),
        ((), ("write",), "notify or indicate"),
        (("notify",), ("read",), "write-without-response"),
        (("notify",), (), "write-without-response"),
    ],
)
def test_real_bleak_rejects_invalid_fff0_properties(
    notify_properties: tuple[str, ...],
    write_properties: tuple[str, ...],
    error: str,
) -> None:
    collection, _ = _real_gatt_collection(
        fff0_notify=notify_properties,
        fff0_write=write_properties,
    )
    with pytest.raises(client_module.UnsupportedDeviceError, match=error):
        _resolve(collection)


def test_real_bleak_missing_fff0_service_is_rejected() -> None:
    collection, _ = _real_gatt_collection(
        fff0_present=False,
        other_notify=("notify",),
        other_write=("write",),
    )
    assert collection.get_characteristic(NOTIFY_UUID) is not None
    assert collection.get_characteristic(WRITE_UUID) is not None
    with pytest.raises(client_module.UnsupportedDeviceError, match="service"):
        _resolve(collection)
