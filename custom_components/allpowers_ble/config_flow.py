"""Config and options flows for ALLPOWERS BLE."""

from __future__ import annotations

import logging
from typing import Any, override

from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS
import voluptuous as vol
from homeassistant import data_entry_flow

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .client import (
    DeviceNotFoundError,
    ProbeConnectionTimeoutError,
    ProbeGattValidationError,
    ProbeNotificationSetupError,
    ProbeStatusTimeoutError,
    UnsupportedDeviceError,
    async_probe_device,
)
from .const import (
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    CONF_DEVICE_NAME,
    CONF_ENABLE_CAR_CHARGER,
    CONF_RECONNECT_MAX_DELAY,
    CONF_SETTINGS_KEEPALIVE,
    CONF_SETTINGS_KEEPALIVE_INTERVAL,
    CONF_SETTINGS_STALE_TIMEOUT,
    CONF_STALE_TIMEOUT,
    CONF_STATUS_INTERVAL,
    CONF_WATCHDOG_TIMEOUT,
    DOMAIN,
    INITIAL_CONNECT_TIMEOUT,
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
    SERVICE_UUID,
)
from .model_support import identify_model
from .options import ConnectionOptions

_LOGGER = logging.getLogger(__name__)

SECTION_CONNECTION_HEALTH = "connection_health"
SECTION_ADVANCED_TIMING = "advanced_timing"
SECTION_EXPERIMENTAL_CONTROLS = "experimental_controls"


class AllpowersConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and setup of one ALLPOWERS BLE device."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._probe_error: str | None = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: ConfigEntry[Any],
    ) -> AllpowersOptionsFlow:
        """Return the options flow handler."""
        del config_entry
        return AllpowersOptionsFlow()

    @override
    async def async_step_bluetooth(
        self,
        discovery_info: BluetoothServiceInfoBleak,
    ) -> ConfigFlowResult:
        """Handle manifest-driven Bluetooth discovery."""
        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")

        support = identify_model(discovery_info.name)
        if not support.supported:
            return self.async_abort(reason="not_supported")

        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": _display_name(discovery_info),
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm and actively validate a discovered device."""
        assert self._discovery_info is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_probe_and_create(self._discovery_info)
            if result is not None:
                return result
            errors["base"] = self._probe_error or "unknown"

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"name": _display_name(self._discovery_info)},
        )

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allow manual setup by selecting a currently discovered device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            discovery_info = self._discovered_devices[user_input[CONF_ADDRESS]]
            address = discovery_info.address.upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            result = await self._async_probe_and_create(discovery_info)
            if result is not None:
                return result
            errors["base"] = self._probe_error or "unknown"

        await bluetooth.async_request_active_scan(self.hass)
        self._discover_candidates()
        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: _display_name(info)
                            for address, info in self._discovered_devices.items()
                        }
                    )
                }
            ),
            errors=errors,
        )

    @callback
    def _discover_candidates(self) -> None:
        current_ids = {
            str(unique_id).upper()
            for unique_id in self._async_current_ids(include_ignore=False)
        }
        for discovery_info in bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        ):
            address = discovery_info.address.upper()
            if address in current_ids or address in self._discovered_devices:
                continue
            if not _matches_device(discovery_info):
                continue
            if not identify_model(discovery_info.name).supported:
                continue
            self._discovered_devices[address] = discovery_info

    async def _async_probe_and_create(
        self,
        discovery_info: BluetoothServiceInfoBleak,
    ) -> ConfigFlowResult | None:
        address = discovery_info.address.upper()
        name = discovery_info.name or "ALLPOWERS"
        try:
            await async_probe_device(
                self.hass,
                address=address,
                advertised_name=name,
                timeout=INITIAL_CONNECT_TIMEOUT,
            )
        except UnsupportedDeviceError:
            return self.async_abort(reason="not_supported")
        except DeviceNotFoundError as ex:
            _LOGGER.debug("No connectable path during setup: %s", ex)
            self._probe_error = "cannot_connect"
            return None
        except ProbeConnectionTimeoutError as ex:
            _LOGGER.debug("Probe connection timeout: %s", ex, exc_info=True)
            self._probe_error = "connect_timeout"
            return None
        except ProbeGattValidationError as ex:
            _LOGGER.debug("Probe GATT validation failed: %s", ex, exc_info=True)
            self._probe_error = "gatt_unavailable"
            return None
        except ProbeNotificationSetupError as ex:
            _LOGGER.debug("Probe notification setup failed: %s", ex, exc_info=True)
            self._probe_error = "notify_failed"
            return None
        except ProbeStatusTimeoutError:
            self._probe_error = "timeout"
            return None
        except BLEAK_RETRY_EXCEPTIONS as ex:
            _LOGGER.debug("Bluetooth probe failed: %s", ex, exc_info=True)
            self._probe_error = "cannot_connect"
            return None
        except Exception:
            _LOGGER.exception("Unexpected error while probing ALLPOWERS BLE")
            self._probe_error = "unknown"
            return None

        self._probe_error = None

        await self.async_set_unique_id(address, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        title = _entry_title(discovery_info)
        return self.async_create_entry(
            title=title,
            data={
                CONF_ADDRESS: address,
                CONF_DEVICE_NAME: name,
            },
            options=ConnectionOptions().as_dict(),
        )


class AllpowersOptionsFlow(OptionsFlow):
    """Manage connection health and experimental settings."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show and validate runtime options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            flat_input = _flatten_options_input(user_input)
            options, errors = _validate_options_input(flat_input)
            if errors:
                _LOGGER.debug("Invalid ALLPOWERS options input: %s", errors)
            else:
                assert options is not None
                return self.async_create_entry(title="", data=options.as_dict())

        current = ConnectionOptions.from_mapping(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(SECTION_CONNECTION_HEALTH): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_STATUS_INTERVAL,
                                    default=current.status_interval,
                                ): vol.All(
                                    vol.Coerce(float),
                                    vol.Range(
                                        min=MIN_STATUS_INTERVAL,
                                        max=MAX_STATUS_INTERVAL,
                                    ),
                                ),
                                vol.Required(
                                    CONF_STALE_TIMEOUT,
                                    default=current.stale_timeout,
                                ): vol.All(
                                    vol.Coerce(float),
                                    vol.Range(
                                        min=MIN_STALE_TIMEOUT,
                                        max=MAX_STALE_TIMEOUT,
                                    ),
                                ),
                                vol.Required(
                                    CONF_WATCHDOG_TIMEOUT,
                                    default=current.watchdog_timeout,
                                ): vol.All(
                                    vol.Coerce(float),
                                    vol.Range(
                                        min=MIN_WATCHDOG_TIMEOUT,
                                        max=MAX_WATCHDOG_TIMEOUT,
                                    ),
                                ),
                            }
                        ),
                    ),
                    vol.Required(SECTION_ADVANCED_TIMING): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_RECONNECT_MAX_DELAY,
                                    default=current.reconnect_max_delay,
                                ): vol.All(
                                    vol.Coerce(float),
                                    vol.Range(
                                        min=MIN_RECONNECT_MAX_DELAY,
                                        max=MAX_RECONNECT_MAX_DELAY,
                                    ),
                                ),
                                vol.Required(
                                    CONF_SETTINGS_STALE_TIMEOUT,
                                    default=current.settings_stale_timeout,
                                ): vol.All(
                                    vol.Coerce(float),
                                    vol.Range(
                                        min=MIN_SETTINGS_STALE_TIMEOUT,
                                        max=MAX_SETTINGS_STALE_TIMEOUT,
                                    ),
                                ),
                                vol.Required(
                                    CONF_SETTINGS_KEEPALIVE,
                                    default=current.settings_keepalive,
                                ): bool,
                                vol.Required(
                                    CONF_SETTINGS_KEEPALIVE_INTERVAL,
                                    default=current.settings_keepalive_interval,
                                ): vol.All(
                                    vol.Coerce(float),
                                    vol.Range(
                                        min=MIN_SETTINGS_KEEPALIVE_INTERVAL,
                                        max=MAX_SETTINGS_KEEPALIVE_INTERVAL,
                                    ),
                                ),
                            }
                        ),
                        {"collapsed": True},
                    ),
                    vol.Required(
                        SECTION_EXPERIMENTAL_CONTROLS
                    ): data_entry_flow.section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_ENABLE_CAR_CHARGER,
                                    default=current.enable_car_charger,
                                ): bool,
                            }
                        ),
                        {"collapsed": True},
                    ),
                }
            ),
            errors=errors,
        )


def _flatten_options_input(values: dict[str, Any]) -> dict[str, Any]:
    """Normalize both flat and sectioned options-flow payloads."""
    flat: dict[str, Any] = dict(values)
    for section in (
        SECTION_CONNECTION_HEALTH,
        SECTION_ADVANCED_TIMING,
        SECTION_EXPERIMENTAL_CONTROLS,
    ):
        section_values = values.get(section)
        if isinstance(section_values, dict):
            flat.update(section_values)
    return flat


def _validate_options_input(
    values: dict[str, Any],
) -> tuple[ConnectionOptions, dict[str, str]] | tuple[None, dict[str, str]]:
    """Validate and attribute options errors to specific fields."""
    errors: dict[str, str] = {}

    status_interval = _parse_float(values, CONF_STATUS_INTERVAL, errors)
    stale_timeout = _parse_float(values, CONF_STALE_TIMEOUT, errors)
    watchdog_timeout = _parse_float(values, CONF_WATCHDOG_TIMEOUT, errors)
    reconnect_max_delay = _parse_float(values, CONF_RECONNECT_MAX_DELAY, errors)
    settings_stale_timeout = _parse_float(values, CONF_SETTINGS_STALE_TIMEOUT, errors)
    settings_keepalive_interval = _parse_float(
        values,
        CONF_SETTINGS_KEEPALIVE_INTERVAL,
        errors,
    )
    settings_keepalive = _parse_bool(values, CONF_SETTINGS_KEEPALIVE, errors)
    enable_car_charger = _parse_bool(values, CONF_ENABLE_CAR_CHARGER, errors)

    if errors:
        return None, errors

    assert status_interval is not None
    assert stale_timeout is not None
    assert watchdog_timeout is not None
    assert reconnect_max_delay is not None
    assert settings_stale_timeout is not None
    assert settings_keepalive_interval is not None
    assert settings_keepalive is not None
    assert enable_car_charger is not None

    _validate_range(
        CONF_STATUS_INTERVAL,
        status_interval,
        MIN_STATUS_INTERVAL,
        MAX_STATUS_INTERVAL,
        errors,
    )
    _validate_range(
        CONF_STALE_TIMEOUT,
        stale_timeout,
        MIN_STALE_TIMEOUT,
        MAX_STALE_TIMEOUT,
        errors,
    )
    _validate_range(
        CONF_WATCHDOG_TIMEOUT,
        watchdog_timeout,
        MIN_WATCHDOG_TIMEOUT,
        MAX_WATCHDOG_TIMEOUT,
        errors,
    )
    _validate_range(
        CONF_RECONNECT_MAX_DELAY,
        reconnect_max_delay,
        MIN_RECONNECT_MAX_DELAY,
        MAX_RECONNECT_MAX_DELAY,
        errors,
    )
    _validate_range(
        CONF_SETTINGS_STALE_TIMEOUT,
        settings_stale_timeout,
        MIN_SETTINGS_STALE_TIMEOUT,
        MAX_SETTINGS_STALE_TIMEOUT,
        errors,
    )
    _validate_range(
        CONF_SETTINGS_KEEPALIVE_INTERVAL,
        settings_keepalive_interval,
        MIN_SETTINGS_KEEPALIVE_INTERVAL,
        MAX_SETTINGS_KEEPALIVE_INTERVAL,
        errors,
    )

    if stale_timeout <= status_interval:
        errors[CONF_STALE_TIMEOUT] = "stale_timeout_must_exceed_status_interval"
    if watchdog_timeout <= stale_timeout:
        errors[CONF_WATCHDOG_TIMEOUT] = "watchdog_timeout_must_exceed_stale_timeout"
    if settings_keepalive and settings_stale_timeout <= settings_keepalive_interval:
        errors[CONF_SETTINGS_STALE_TIMEOUT] = (
            "settings_stale_timeout_must_exceed_keepalive_interval"
        )

    if errors:
        return None, errors

    options = ConnectionOptions(
        status_interval=status_interval,
        stale_timeout=stale_timeout,
        watchdog_timeout=watchdog_timeout,
        reconnect_max_delay=reconnect_max_delay,
        settings_stale_timeout=settings_stale_timeout,
        settings_keepalive=settings_keepalive,
        settings_keepalive_interval=settings_keepalive_interval,
        enable_car_charger=enable_car_charger,
    )
    return options, errors


def _parse_float(
    values: dict[str, Any],
    key: str,
    errors: dict[str, str],
) -> float | None:
    raw = values.get(key)
    if raw is None:
        errors[key] = "invalid_number"
        return None
    try:
        return float(raw)
    except TypeError, ValueError:
        errors[key] = "invalid_number"
        return None


def _parse_bool(
    values: dict[str, Any],
    key: str,
    errors: dict[str, str],
) -> bool | None:
    raw = values.get(key)
    if isinstance(raw, bool):
        return raw
    errors[key] = "invalid_boolean"
    return None


def _validate_range(
    key: str,
    value: float,
    minimum: float,
    maximum: float,
    errors: dict[str, str],
) -> None:
    if not minimum <= value <= maximum:
        errors[key] = "out_of_range"


def _matches_device(discovery_info: BluetoothServiceInfoBleak) -> bool:
    name = (discovery_info.name or "").upper()
    service_uuids = {uuid.lower() for uuid in discovery_info.service_uuids}
    return (
        name.startswith("R600")
        or name.startswith("AP R")
        or name.startswith("AP S")
        or name.startswith("ALLPOWERS")
        or SERVICE_UUID in service_uuids
    )


def _display_name(discovery_info: BluetoothServiceInfoBleak) -> str:
    name = discovery_info.name or "ALLPOWERS"
    return f"{name} ({discovery_info.address.upper()})"


def _entry_title(discovery_info: BluetoothServiceInfoBleak) -> str:
    name = discovery_info.name or "ALLPOWERS"
    compact = discovery_info.address.replace(":", "").replace("-", "")
    return f"{name} {compact[-4:].upper()}"
