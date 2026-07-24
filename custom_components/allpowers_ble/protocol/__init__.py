"""Public protocol API for ALLPOWERS BLE devices."""

from .codec import (
    NotificationStreamDecoder,
    VALID_ECO_TIMEOUTS,
    decode_notification,
    encode_output_control,
    encode_settings_control,
    encode_status_request,
    format_version,
    updated_settings,
    xor_checksum,
)
from .errors import (
    FrameTooShortError,
    InvalidChecksumError,
    InvalidHeaderError,
    InvalidLengthError,
    InvalidPayloadError,
    ProtocolError,
    StateUnavailableError,
)
from .models import (
    DeviceNameData,
    ProtocolPacket,
    SettingsData,
    StatusData,
    UnknownPacket,
    WorkMode,
)

__all__ = [
    "DeviceNameData",
    "FrameTooShortError",
    "InvalidChecksumError",
    "InvalidHeaderError",
    "InvalidLengthError",
    "InvalidPayloadError",
    "NotificationStreamDecoder",
    "ProtocolError",
    "ProtocolPacket",
    "SettingsData",
    "StateUnavailableError",
    "StatusData",
    "UnknownPacket",
    "VALID_ECO_TIMEOUTS",
    "WorkMode",
    "decode_notification",
    "encode_output_control",
    "encode_settings_control",
    "encode_status_request",
    "format_version",
    "updated_settings",
    "xor_checksum",
]
