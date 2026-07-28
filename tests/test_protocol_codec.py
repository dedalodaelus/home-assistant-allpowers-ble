"""Unit tests for protocol encoding and decoding."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from custom_components.allpowers_ble.protocol import (
    DeviceNameData,
    FrameTooShortError,
    InvalidChecksumError,
    InvalidHeaderError,
    InvalidLengthError,
    InvalidPayloadError,
    SettingsData,
    StatusData,
    UnknownPacket,
    WorkMode,
    decode_notification,
    encode_output_control,
    encode_settings_control,
    encode_status_request,
    format_version,
    updated_settings,
    xor_checksum,
)
from custom_components.allpowers_ble.protocol.codec import append_xor_checksum


def test_decode_status(status_frame: bytes) -> None:
    packet = decode_notification(status_frame)

    assert packet == StatusData(
        dc_enabled=True,
        ac_enabled=True,
        light_enabled=True,
        battery_percent=73,
        input_power_w=300,
        output_power_w=150,
        remaining_minutes=120,
        raw_flags=0x13,
    )


def test_decode_settings(settings_frame: bytes) -> None:
    packet = decode_notification(settings_frame)

    assert packet == SettingsData(
        eco_enabled=True,
        work_mode=WorkMode.FAST,
        car_charger_enabled=True,
        eco_timeout_hours=4,
        hardware_version="0.3",
        firmware_version="0xAF",
        raw_flags=0xB5,
        raw_hardware_version=0x03,
        raw_firmware_version=0xAF,
    )


def test_decode_reserved_work_mode(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(
        notification_builder(0x03, bytes((0x06, 2, 0, 0, 0x10, 0x21)))
    )

    assert isinstance(packet, SettingsData)
    assert packet.work_mode is None
    assert packet.raw_flags == 0x06


def test_decode_device_name(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(notification_builder(0x35, b"R600 Kitchen\x00\x00"))

    assert packet == DeviceNameData(name="R600 Kitchen")


def test_decode_device_name_trims_and_strips_controls(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(
        notification_builder(0x35, "  Sala\n\tR600\x00 ".encode("utf-8"))
    )

    assert packet == DeviceNameData(name="SalaR600")


def test_decode_device_name_keeps_valid_unicode(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(
        notification_builder(0x35, "  Ático estación  ".encode("utf-8"))
    )

    assert packet == DeviceNameData(name="Ático estación")


def test_decode_device_name_whitespace_only_becomes_empty(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(notification_builder(0x35, b"  \t\n\x00"))

    assert packet == DeviceNameData(name="")


def test_decode_device_name_is_capped(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(notification_builder(0x35, b"X" * 100))

    assert packet == DeviceNameData(name="X" * 64)


def test_decode_unknown_command(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    packet = decode_notification(notification_builder(0x7E, b"\x01\x02"))

    assert packet == UnknownPacket(command=0x7E, payload=b"\x01\x02")


def test_reject_too_short() -> None:
    with pytest.raises(FrameTooShortError):
        decode_notification(b"\xa5\x65")


def test_reject_header(status_frame: bytes) -> None:
    corrupted = bytes((0x00,)) + status_frame[1:]

    with pytest.raises(InvalidHeaderError):
        decode_notification(corrupted)


def test_reject_encoded_length(status_frame: bytes) -> None:
    corrupted = bytearray(status_frame)
    corrupted[5] += 1

    with pytest.raises(InvalidLengthError):
        decode_notification(corrupted)


def test_reject_checksum(status_frame: bytes) -> None:
    corrupted = bytearray(status_frame)
    corrupted[-1] ^= 0x80

    with pytest.raises(InvalidChecksumError):
        decode_notification(corrupted)


def test_reject_short_status_payload(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    with pytest.raises(InvalidPayloadError):
        decode_notification(notification_builder(0x01, b"\x00" * 7))


def test_reject_invalid_battery(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    payload = bytes((0, 101, 0, 0, 0, 0, 0, 0))

    with pytest.raises(InvalidPayloadError, match="Battery"):
        decode_notification(notification_builder(0x01, payload))


def test_reject_short_settings_payload(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    with pytest.raises(InvalidPayloadError):
        decode_notification(notification_builder(0x03, b"\x00" * 5))


def test_reject_invalid_utf8_name(
    notification_builder: Callable[[int, bytes], bytes],
) -> None:
    with pytest.raises(InvalidPayloadError, match="UTF-8"):
        decode_notification(notification_builder(0x35, b"\xff"))


def test_status_request_is_exact_vendor_vector() -> None:
    assert encode_status_request() == bytes.fromhex(
        "A5 65 B1 00 01 06 01 00 00 00 00 00"
    )


@pytest.mark.parametrize(
    ("dc", "ac", "light", "expected_flags", "expected_checksum"),
    [
        (False, False, False, 0x00, 0x71),
        (True, False, False, 0x01, 0x70),
        (False, True, False, 0x02, 0x73),
        (True, True, False, 0x03, 0x72),
        (False, False, True, 0x20, 0x51),
        (True, True, True, 0x23, 0x52),
    ],
)
def test_encode_output_vectors(
    dc: bool,
    ac: bool,
    light: bool,
    expected_flags: int,
    expected_checksum: int,
) -> None:
    frame = encode_output_control(dc=dc, ac=ac, light=light)

    assert frame == bytes(
        (0xA5, 0x65, 0x00, 0xB1, 0x01, 0x01, 0x00, expected_flags, expected_checksum)
    )
    assert xor_checksum(frame) == 0


def test_settings_update_preserves_unknown_bits(settings_frame: bytes) -> None:
    current = decode_notification(settings_frame)
    assert isinstance(current, SettingsData)

    target = updated_settings(
        current,
        eco_enabled=False,
        work_mode=WorkMode.MUTE,
        car_charger_enabled=False,
        eco_timeout_hours=6,
    )

    assert target.raw_flags == 0xA0
    assert target.eco_enabled is False
    assert target.work_mode is WorkMode.MUTE
    assert target.car_charger_enabled is False
    assert target.eco_timeout_hours == 6
    assert target.raw_hardware_version == current.raw_hardware_version
    assert target.raw_firmware_version == current.raw_firmware_version


def test_settings_update_only_one_field(settings_frame: bytes) -> None:
    current = decode_notification(settings_frame)
    assert isinstance(current, SettingsData)

    target = updated_settings(current, eco_timeout_hours=1)

    assert target.raw_flags == current.raw_flags
    assert target.eco_timeout_hours == 1


def test_settings_update_can_enable_masks(settings_frame: bytes) -> None:
    current = decode_notification(settings_frame)
    assert isinstance(current, SettingsData)
    cleared = updated_settings(
        current,
        eco_enabled=False,
        car_charger_enabled=False,
    )

    target = updated_settings(
        cleared,
        eco_enabled=True,
        car_charger_enabled=True,
        work_mode=WorkMode.STANDARD,
    )

    assert target.raw_flags & 0x01
    assert target.raw_flags & 0x10
    assert target.work_mode is WorkMode.STANDARD
    assert target.raw_flags & 0xA0 == 0xA0


def test_settings_update_rejects_timeout(settings_frame: bytes) -> None:
    current = decode_notification(settings_frame)
    assert isinstance(current, SettingsData)

    with pytest.raises(ValueError, match="Unsupported ECO timeout"):
        updated_settings(current, eco_timeout_hours=3)


def test_encode_settings_vector(settings_frame: bytes) -> None:
    current = decode_notification(settings_frame)
    assert isinstance(current, SettingsData)
    target = updated_settings(current, eco_timeout_hours=6)

    frame = encode_settings_control(target)

    assert frame[:-1] == bytes.fromhex("A5 65 00 B1 01 02 02 B5 06")
    assert xor_checksum(frame) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0x00, "0.0"),
        (0x12, "1.2"),
        (0x99, "9.9"),
        (0xA0, "0xA0"),
        (0x0F, "0x0F"),
    ],
)
def test_format_version(raw: int, expected: str) -> None:
    assert format_version(raw) == expected


def test_xor_checksum_and_append() -> None:
    raw = bytes.fromhex("A5 65 00 B1 01 01 00 03")
    frame = append_xor_checksum(raw)

    assert frame[-1] == xor_checksum(raw)
    assert xor_checksum(frame) == 0
