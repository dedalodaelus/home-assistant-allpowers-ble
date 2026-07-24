"""Pure Python encoder and decoder for the ALLPOWERS BLE protocol."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .errors import (
    FrameTooShortError,
    InvalidChecksumError,
    InvalidHeaderError,
    InvalidLengthError,
    InvalidPayloadError,
)
from .models import (
    DeviceNameData,
    ProtocolPacket,
    SettingsData,
    StatusData,
    UnknownPacket,
    WorkMode,
)

HEADER = bytes((0xA5, 0x65))
STATUS_COMMAND = 0x01
SETTINGS_WRITE_COMMAND = 0x02
SETTINGS_NOTIFICATION_COMMAND = 0x03
DEVICE_NAME_COMMAND = 0x35
MAX_NOTIFICATION_PAYLOAD = 128

STATUS_REQUEST = bytes((0xA5, 0x65, 0xB1, 0x00, 0x01, 0x06, 0x01, 0, 0, 0, 0, 0))

STATUS_DC_MASK = 0x01
STATUS_AC_MASK = 0x02
STATUS_LIGHT_MASK = 0x10

OUTPUT_DC_MASK = 0x01
OUTPUT_AC_MASK = 0x02
OUTPUT_LIGHT_MASK = 0x20

SETTINGS_ECO_MASK = 0x01
SETTINGS_WORK_MODE_MASK = 0x06
SETTINGS_CAR_CHARGER_MASK = 0x10

VALID_ECO_TIMEOUTS = frozenset((1, 2, 4, 6))


def xor_checksum(data: Iterable[int]) -> int:
    """Return the XOR checksum for an iterable of bytes."""
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum


def append_xor_checksum(data: bytes | bytearray) -> bytes:
    """Append a byte that makes the XOR of the complete frame equal zero."""
    raw = bytes(data)
    return raw + bytes((xor_checksum(raw),))


def format_version(raw: int) -> str:
    """Format the version byte used by ALLPOWERS settings notifications."""
    high = raw >> 4
    low = raw & 0x0F
    if high <= 9 and low <= 9:
        return f"{high}.{low}"
    return f"0x{raw:02X}"


def decode_notification(data: bytes | bytearray | memoryview) -> ProtocolPacket:
    """Decode and validate one complete GATT notification frame."""
    frame = bytes(data)
    if len(frame) < 8:
        raise FrameTooShortError(
            f"Notification has {len(frame)} bytes; expected at least 8"
        )
    if frame[:2] != HEADER:
        raise InvalidHeaderError(f"Unexpected header: {frame[:2].hex(' ')}")

    payload_length = frame[5]
    expected_length = 8 + payload_length
    if len(frame) != expected_length:
        raise InvalidLengthError(
            "Encoded payload length "
            f"{payload_length} requires {expected_length} bytes; "
            f"received {len(frame)}"
        )
    if xor_checksum(frame) != 0:
        raise InvalidChecksumError("Notification XOR checksum is invalid")

    command = frame[6]
    payload = frame[7:-1]
    if command == STATUS_COMMAND:
        return _decode_status(payload)
    if command == SETTINGS_NOTIFICATION_COMMAND:
        return _decode_settings(payload)
    if command == DEVICE_NAME_COMMAND:
        return _decode_device_name(payload)
    return UnknownPacket(command=command, payload=payload)


def _decode_status(payload: bytes) -> StatusData:
    if len(payload) < 8:
        raise InvalidPayloadError(
            f"Status payload has {len(payload)} bytes; expected at least 8"
        )

    flags = payload[0]
    battery = payload[1]
    if battery > 100:
        raise InvalidPayloadError(f"Battery percentage is outside 0..100: {battery}")

    return StatusData(
        dc_enabled=bool(flags & STATUS_DC_MASK),
        ac_enabled=bool(flags & STATUS_AC_MASK),
        light_enabled=bool(flags & STATUS_LIGHT_MASK),
        battery_percent=battery,
        input_power_w=int.from_bytes(payload[2:4], "big"),
        output_power_w=int.from_bytes(payload[4:6], "big"),
        remaining_minutes=int.from_bytes(payload[6:8], "big"),
        raw_flags=flags,
    )


def _decode_settings(payload: bytes) -> SettingsData:
    if len(payload) < 6:
        raise InvalidPayloadError(
            f"Settings payload has {len(payload)} bytes; expected at least 6"
        )

    flags = payload[0]
    mode_value = (flags & SETTINGS_WORK_MODE_MASK) >> 1
    try:
        mode = WorkMode(mode_value)
    except ValueError:
        mode = None

    hardware_raw = payload[4]
    firmware_raw = payload[5]
    return SettingsData(
        eco_enabled=bool(flags & SETTINGS_ECO_MASK),
        work_mode=mode,
        car_charger_enabled=bool(flags & SETTINGS_CAR_CHARGER_MASK),
        eco_timeout_hours=payload[1],
        hardware_version=format_version(hardware_raw),
        firmware_version=format_version(firmware_raw),
        raw_flags=flags,
        raw_hardware_version=hardware_raw,
        raw_firmware_version=firmware_raw,
    )


def _decode_device_name(payload: bytes) -> DeviceNameData:
    try:
        name = payload.rstrip(b"\x00").decode("utf-8")
    except UnicodeDecodeError as ex:
        raise InvalidPayloadError("Device name is not valid UTF-8") from ex
    return DeviceNameData(name=name)


def encode_status_request() -> bytes:
    """Return the vendor status request exactly as observed on supported units."""
    return STATUS_REQUEST


def encode_output_control(*, dc: bool, ac: bool, light: bool) -> bytes:
    """Encode one combined AC/DC/light output-control command."""
    flags = 0
    if dc:
        flags |= OUTPUT_DC_MASK
    if ac:
        flags |= OUTPUT_AC_MASK
    if light:
        flags |= OUTPUT_LIGHT_MASK

    return append_xor_checksum(
        bytes((0xA5, 0x65, 0x00, 0xB1, 0x01, STATUS_COMMAND, 0x00, flags))
    )


def updated_settings(
    current: SettingsData,
    *,
    eco_enabled: bool | None = None,
    work_mode: WorkMode | None = None,
    car_charger_enabled: bool | None = None,
    eco_timeout_hours: int | None = None,
) -> SettingsData:
    """Return settings with requested fields changed and unknown bits preserved."""
    flags = current.raw_flags

    if eco_enabled is not None:
        flags = _set_mask(flags, SETTINGS_ECO_MASK, eco_enabled)
    if work_mode is not None:
        flags = (flags & ~SETTINGS_WORK_MODE_MASK) | (
            (int(work_mode) << 1) & SETTINGS_WORK_MODE_MASK
        )
    if car_charger_enabled is not None:
        flags = _set_mask(flags, SETTINGS_CAR_CHARGER_MASK, car_charger_enabled)

    timeout = current.eco_timeout_hours
    if eco_timeout_hours is not None:
        if eco_timeout_hours not in VALID_ECO_TIMEOUTS:
            raise ValueError(
                f"Unsupported ECO timeout {eco_timeout_hours}; expected one of "
                f"{sorted(VALID_ECO_TIMEOUTS)}"
            )
        timeout = eco_timeout_hours

    mode_value = (flags & SETTINGS_WORK_MODE_MASK) >> 1
    try:
        mode = WorkMode(mode_value)
    except ValueError:
        mode = None

    return replace(
        current,
        eco_enabled=bool(flags & SETTINGS_ECO_MASK),
        work_mode=mode,
        car_charger_enabled=bool(flags & SETTINGS_CAR_CHARGER_MASK),
        eco_timeout_hours=timeout,
        raw_flags=flags,
    )


def encode_settings_control(settings: SettingsData) -> bytes:
    """Encode a settings command from a fully materialized safe snapshot."""
    return append_xor_checksum(
        bytes(
            (
                0xA5,
                0x65,
                0x00,
                0xB1,
                0x01,
                SETTINGS_WRITE_COMMAND,
                0x02,
                settings.raw_flags,
                settings.eco_timeout_hours,
            )
        )
    )


def _set_mask(value: int, mask: int, enabled: bool) -> int:
    if enabled:
        return value | mask
    return value & ~mask


class NotificationStreamDecoder:
    """Incrementally recover complete frames from fragmented or noisy input."""

    __slots__ = ("_buffer", "_discarded_frames")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._discarded_frames = 0

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes waiting for a complete frame."""
        return len(self._buffer)

    @property
    def discarded_frames(self) -> int:
        """Return the number of invalid candidate frames discarded."""
        return self._discarded_frames

    def reset(self) -> None:
        """Discard all buffered bytes and error counters."""
        self._buffer.clear()
        self._discarded_frames = 0

    def feed(self, data: bytes | bytearray | memoryview) -> list[ProtocolPacket]:
        """Feed bytes and return every complete, valid packet recovered."""
        self._buffer.extend(data)
        packets: list[ProtocolPacket] = []

        while True:
            header_index = self._buffer.find(HEADER)
            if header_index < 0:
                if self._buffer.endswith(HEADER[:1]):
                    del self._buffer[:-1]
                else:
                    self._buffer.clear()
                break
            if header_index:
                del self._buffer[:header_index]

            if len(self._buffer) < 7:
                break

            payload_length = self._buffer[5]
            if payload_length > MAX_NOTIFICATION_PAYLOAD:
                del self._buffer[0]
                self._discarded_frames += 1
                continue

            total_length = 8 + payload_length
            if len(self._buffer) < total_length:
                break

            candidate = bytes(self._buffer[:total_length])
            try:
                packet = decode_notification(candidate)
            except (InvalidChecksumError, InvalidLengthError, InvalidPayloadError):
                del self._buffer[0]
                self._discarded_frames += 1
                continue

            del self._buffer[:total_length]
            packets.append(packet)

        return packets
