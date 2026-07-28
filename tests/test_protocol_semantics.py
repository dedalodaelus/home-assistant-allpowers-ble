"""Tests for profile-driven semantic write validation."""

from custom_components.allpowers_ble.protocol import SettingsData, StatusData, WorkMode
from custom_components.allpowers_ble.protocol.semantics import (
    settings_write_validation_errors,
    status_write_validation_errors,
)


def _status(**changes: object) -> StatusData:
    values: dict[str, object] = {
        "dc_enabled": False,
        "ac_enabled": False,
        "light_enabled": False,
        "battery_percent": 50,
        "input_power_w": 100,
        "output_power_w": 50,
        "remaining_minutes": 120,
        "raw_flags": 0x00,
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
        "raw_flags": 0x00,
        "raw_hardware_version": 0x03,
        "raw_firmware_version": 0x11,
    }
    values.update(changes)
    return SettingsData(**values)


def test_verified_profile_rejects_unknown_status_bits() -> None:
    errors = status_write_validation_errors("r600-hw-0.3", _status(raw_flags=0x14))

    assert errors
    assert "unknown flag bits" in errors[0]


def test_verified_profile_rejects_reserved_settings_mode() -> None:
    errors = settings_write_validation_errors("r600-hw-0.3", _settings(work_mode=None))

    assert errors
    assert "reserved work mode" in errors[0]


def test_verified_profile_rejects_unsupported_eco_timeout() -> None:
    errors = settings_write_validation_errors(
        "r600-hw-0.3",
        _settings(eco_timeout_hours=9),
    )

    assert errors
    assert "unsupported ECO timeout" in errors[0]


def test_unverified_profiles_do_not_apply_r600_semantic_rules() -> None:
    status_errors = status_write_validation_errors(
        "r600-unverified-revision",
        _status(raw_flags=0xFF),
    )
    settings_errors = settings_write_validation_errors(
        "r600-unverified-revision",
        _settings(work_mode=None, eco_timeout_hours=9),
    )

    assert status_errors == ()
    assert settings_errors == ()
