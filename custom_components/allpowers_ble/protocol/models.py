"""Data models for the ALLPOWERS BLE protocol."""

from dataclasses import dataclass
from enum import IntEnum


class WorkMode(IntEnum):
    """Supported ALLPOWERS charging work modes."""

    MUTE = 0
    STANDARD = 1
    FAST = 2


@dataclass(frozen=True, slots=True)
class StatusData:
    """Decoded status notification."""

    dc_enabled: bool
    ac_enabled: bool
    light_enabled: bool
    battery_percent: int
    input_power_w: int
    output_power_w: int
    remaining_minutes: int
    raw_flags: int


@dataclass(frozen=True, slots=True)
class SettingsData:
    """Decoded settings notification, including raw values for safe writes."""

    eco_enabled: bool
    work_mode: WorkMode | None
    car_charger_enabled: bool
    eco_timeout_hours: int
    hardware_version: str
    firmware_version: str
    raw_flags: int
    raw_hardware_version: int
    raw_firmware_version: int


@dataclass(frozen=True, slots=True)
class DeviceNameData:
    """Decoded optional device-name notification."""

    name: str


@dataclass(frozen=True, slots=True)
class UnknownPacket:
    """A valid frame whose command is not implemented."""

    command: int
    payload: bytes


type ProtocolPacket = StatusData | SettingsData | DeviceNameData | UnknownPacket
