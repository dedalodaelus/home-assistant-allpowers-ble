"""Config and options flows for ALLPOWERS BLE."""

from __future__ import annotations

import logging
from typing import Any, override

from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS
import voluptuous as vol

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


class AllpowersConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle discovery and setup of one ALLPOWERS BLE device."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

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
            errors["base"] = self.context.pop("probe_error", "unknown")

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
            errors["base"] = self.context.pop("probe_error", "unknown")

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
            self.context["probe_error"] = "cannot_connect"
            return None
        except ProbeConnectionTimeoutError as ex:
            _LOGGER.debug("Probe connection timeout: %s", ex, exc_info=True)
            self.context["probe_error"] = "connect_timeout"
            return None
        except ProbeGattValidationError as ex:
            _LOGGER.debug("Probe GATT validation failed: %s", ex, exc_info=True)
            self.context["probe_error"] = "gatt_unavailable"
            return None
        except ProbeNotificationSetupError as ex:
            _LOGGER.debug("Probe notification setup failed: %s", ex, exc_info=True)
            self.context["probe_error"] = "notify_failed"
            return None
        except ProbeStatusTimeoutError:
            self.context["probe_error"] = "timeout"
            return None
        except BLEAK_RETRY_EXCEPTIONS as ex:
            _LOGGER.debug("Bluetooth probe failed: %s", ex, exc_info=True)
            self.context["probe_error"] = "cannot_connect"
            return None
        except Exception:
            _LOGGER.exception("Unexpected error while probing ALLPOWERS BLE")
            self.context["probe_error"] = "unknown"
            return None

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

    @override
    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show and validate runtime options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = ConnectionOptions.from_mapping(user_input)
            except (TypeError, ValueError) as ex:
                _LOGGER.debug("Invalid ALLPOWERS options: %s", ex)
                errors["base"] = "invalid_options"
            else:
                return self.async_create_entry(title="", data=options.as_dict())

        current = ConnectionOptions.from_mapping(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_STATUS_INTERVAL,
                        default=current.status_interval,
                    ): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=MIN_STATUS_INTERVAL, max=MAX_STATUS_INTERVAL),
                    ),
                    vol.Required(
                        CONF_STALE_TIMEOUT,
                        default=current.stale_timeout,
                    ): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=MIN_STALE_TIMEOUT, max=MAX_STALE_TIMEOUT),
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
                    vol.Required(
                        CONF_ENABLE_CAR_CHARGER,
                        default=current.enable_car_charger,
                    ): bool,
                }
            ),
            errors=errors,
        )


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
