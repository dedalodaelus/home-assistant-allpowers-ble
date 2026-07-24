"""Validated runtime options independent from Home Assistant internals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .const import (
    CONF_ENABLE_CAR_CHARGER,
    CONF_RECONNECT_MAX_DELAY,
    CONF_SETTINGS_KEEPALIVE,
    CONF_SETTINGS_KEEPALIVE_INTERVAL,
    CONF_SETTINGS_STALE_TIMEOUT,
    CONF_STALE_TIMEOUT,
    CONF_STATUS_INTERVAL,
    CONF_WATCHDOG_TIMEOUT,
    DEFAULT_ENABLE_CAR_CHARGER,
    DEFAULT_RECONNECT_MAX_DELAY,
    DEFAULT_SETTINGS_KEEPALIVE,
    DEFAULT_SETTINGS_KEEPALIVE_INTERVAL,
    DEFAULT_SETTINGS_STALE_TIMEOUT,
    DEFAULT_STALE_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_TIMEOUT,
    MAX_RECONNECT_MAX_DELAY,
    MAX_SETTINGS_KEEPALIVE_INTERVAL,
    MAX_SETTINGS_STALE_TIMEOUT,
    MAX_STALE_TIMEOUT,
    MAX_STATUS_INTERVAL,
    MAX_WATCHDOG_TIMEOUT,
    MIN_RECONNECT_MAX_DELAY,
    MIN_SETTINGS_KEEPALIVE_INTERVAL,
    MIN_SETTINGS_STALE_TIMEOUT,
    MIN_STALE_TIMEOUT,
    MIN_STATUS_INTERVAL,
    MIN_WATCHDOG_TIMEOUT,
)


@dataclass(frozen=True, slots=True)
class ConnectionOptions:
    """Runtime tuning options with safety validation."""

    status_interval: float = DEFAULT_STATUS_INTERVAL
    stale_timeout: float = DEFAULT_STALE_TIMEOUT
    watchdog_timeout: float = DEFAULT_WATCHDOG_TIMEOUT
    reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY
    settings_stale_timeout: float = DEFAULT_SETTINGS_STALE_TIMEOUT
    settings_keepalive: bool = DEFAULT_SETTINGS_KEEPALIVE
    settings_keepalive_interval: float = DEFAULT_SETTINGS_KEEPALIVE_INTERVAL
    enable_car_charger: bool = DEFAULT_ENABLE_CAR_CHARGER

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ConnectionOptions:
        """Build and validate options from a config-entry options mapping."""
        options = cls(
            status_interval=float(
                values.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL)
            ),
            stale_timeout=float(values.get(CONF_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT)),
            watchdog_timeout=float(
                values.get(CONF_WATCHDOG_TIMEOUT, DEFAULT_WATCHDOG_TIMEOUT)
            ),
            reconnect_max_delay=float(
                values.get(CONF_RECONNECT_MAX_DELAY, DEFAULT_RECONNECT_MAX_DELAY)
            ),
            settings_stale_timeout=float(
                values.get(CONF_SETTINGS_STALE_TIMEOUT, DEFAULT_SETTINGS_STALE_TIMEOUT)
            ),
            settings_keepalive=_boolean(
                CONF_SETTINGS_KEEPALIVE,
                values.get(CONF_SETTINGS_KEEPALIVE, DEFAULT_SETTINGS_KEEPALIVE),
            ),
            settings_keepalive_interval=float(
                values.get(
                    CONF_SETTINGS_KEEPALIVE_INTERVAL,
                    DEFAULT_SETTINGS_KEEPALIVE_INTERVAL,
                )
            ),
            enable_car_charger=_boolean(
                CONF_ENABLE_CAR_CHARGER,
                values.get(CONF_ENABLE_CAR_CHARGER, DEFAULT_ENABLE_CAR_CHARGER),
            ),
        )
        options.validate()
        return options

    def validate(self) -> None:
        """Validate ranges and relationships that protect connection health."""
        _range(
            CONF_STATUS_INTERVAL,
            self.status_interval,
            MIN_STATUS_INTERVAL,
            MAX_STATUS_INTERVAL,
        )
        _range(
            CONF_STALE_TIMEOUT,
            self.stale_timeout,
            MIN_STALE_TIMEOUT,
            MAX_STALE_TIMEOUT,
        )
        _range(
            CONF_WATCHDOG_TIMEOUT,
            self.watchdog_timeout,
            MIN_WATCHDOG_TIMEOUT,
            MAX_WATCHDOG_TIMEOUT,
        )
        _range(
            CONF_RECONNECT_MAX_DELAY,
            self.reconnect_max_delay,
            MIN_RECONNECT_MAX_DELAY,
            MAX_RECONNECT_MAX_DELAY,
        )
        _range(
            CONF_SETTINGS_STALE_TIMEOUT,
            self.settings_stale_timeout,
            MIN_SETTINGS_STALE_TIMEOUT,
            MAX_SETTINGS_STALE_TIMEOUT,
        )
        _range(
            CONF_SETTINGS_KEEPALIVE_INTERVAL,
            self.settings_keepalive_interval,
            MIN_SETTINGS_KEEPALIVE_INTERVAL,
            MAX_SETTINGS_KEEPALIVE_INTERVAL,
        )
        if self.stale_timeout <= self.status_interval:
            raise ValueError("stale_timeout must be greater than status_interval")
        if self.watchdog_timeout <= self.stale_timeout:
            raise ValueError("watchdog_timeout must be greater than stale_timeout")
        if (
            self.settings_keepalive
            and self.settings_stale_timeout <= self.settings_keepalive_interval
        ):
            raise ValueError(
                "settings_stale_timeout must be greater than "
                "settings_keepalive_interval when keepalive is enabled"
            )

    def as_dict(self) -> dict[str, bool | float]:
        """Return a JSON-serializable representation for diagnostics."""
        return {
            CONF_STATUS_INTERVAL: self.status_interval,
            CONF_STALE_TIMEOUT: self.stale_timeout,
            CONF_WATCHDOG_TIMEOUT: self.watchdog_timeout,
            CONF_RECONNECT_MAX_DELAY: self.reconnect_max_delay,
            CONF_SETTINGS_STALE_TIMEOUT: self.settings_stale_timeout,
            CONF_SETTINGS_KEEPALIVE: self.settings_keepalive,
            CONF_SETTINGS_KEEPALIVE_INTERVAL: self.settings_keepalive_interval,
            CONF_ENABLE_CAR_CHARGER: self.enable_car_charger,
        }


def _range(name: str, value: float, minimum: float, maximum: float) -> None:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")


def _boolean(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{name} must be a boolean")
