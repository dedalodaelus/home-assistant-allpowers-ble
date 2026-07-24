"""Tests for runtime option validation."""

from __future__ import annotations

import pytest

from custom_components.allpowers_ble.const import (
    CONF_ENABLE_CAR_CHARGER,
    CONF_RECONNECT_MAX_DELAY,
    CONF_SETTINGS_KEEPALIVE,
    CONF_SETTINGS_KEEPALIVE_INTERVAL,
    CONF_SETTINGS_STALE_TIMEOUT,
    CONF_STALE_TIMEOUT,
    CONF_STATUS_INTERVAL,
    CONF_WATCHDOG_TIMEOUT,
)
from custom_components.allpowers_ble.options import ConnectionOptions


def test_defaults_are_valid_and_serializable() -> None:
    options = ConnectionOptions.from_mapping({})

    assert options == ConnectionOptions()
    assert ConnectionOptions.from_mapping(options.as_dict()) == options


def test_custom_mapping() -> None:
    values = {
        CONF_STATUS_INTERVAL: 15,
        CONF_STALE_TIMEOUT: 31,
        CONF_WATCHDOG_TIMEOUT: 50,
        CONF_RECONNECT_MAX_DELAY: 30,
        CONF_SETTINGS_STALE_TIMEOUT: 900,
        CONF_SETTINGS_KEEPALIVE: True,
        CONF_SETTINGS_KEEPALIVE_INTERVAL: 480,
        CONF_ENABLE_CAR_CHARGER: True,
    }

    options = ConnectionOptions.from_mapping(values)

    assert options.status_interval == 15
    assert options.settings_keepalive is True
    assert options.enable_car_charger is True
    assert options.as_dict() == {
        key: (
            float(value)
            if isinstance(value, int) and not isinstance(value, bool)
            else value
        )
        for key, value in values.items()
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_STATUS_INTERVAL, 9),
        (CONF_STATUS_INTERVAL, 121),
        (CONF_STALE_TIMEOUT, 14),
        (CONF_STALE_TIMEOUT, 301),
        (CONF_WATCHDOG_TIMEOUT, 24),
        (CONF_WATCHDOG_TIMEOUT, 601),
        (CONF_RECONNECT_MAX_DELAY, 4),
        (CONF_RECONNECT_MAX_DELAY, 301),
        (CONF_SETTINGS_KEEPALIVE_INTERVAL, 59),
        (CONF_SETTINGS_KEEPALIVE_INTERVAL, 1801),
    ],
)
def test_range_validation(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        ConnectionOptions.from_mapping({field: value})


def test_stale_must_exceed_status_interval() -> None:
    with pytest.raises(ValueError, match="stale_timeout"):
        ConnectionOptions.from_mapping(
            {
                CONF_STATUS_INTERVAL: 30,
                CONF_STALE_TIMEOUT: 30,
                CONF_WATCHDOG_TIMEOUT: 45,
            }
        )


def test_watchdog_must_exceed_stale_timeout() -> None:
    with pytest.raises(ValueError, match="watchdog_timeout"):
        ConnectionOptions.from_mapping(
            {
                CONF_STATUS_INTERVAL: 20,
                CONF_STALE_TIMEOUT: 45,
                CONF_WATCHDOG_TIMEOUT: 45,
            }
        )


def test_settings_stale_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="settings_stale_timeout"):
        ConnectionOptions.from_mapping({CONF_SETTINGS_STALE_TIMEOUT: 0})
