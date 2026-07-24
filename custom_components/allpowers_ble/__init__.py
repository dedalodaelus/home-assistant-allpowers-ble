"""The ALLPOWERS BLE Home Assistant integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import Platform

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import AllpowersConfigEntry

PLATFORMS: tuple[Platform, ...] = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.NUMBER,
)


async def async_setup_entry(hass: HomeAssistant, entry: AllpowersConfigEntry) -> bool:
    """Set up an ALLPOWERS BLE device from a config entry."""
    from homeassistant.components import bluetooth
    from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
    from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
    from homeassistant.exceptions import ConfigEntryNotReady

    from .client import AllpowersBLEClient
    from .const import CONF_DEVICE_NAME, INITIAL_DATA_TIMEOUT
    from .coordinator import AllpowersCoordinator, AllpowersRuntimeData
    from .options import ConnectionOptions

    address = str(entry.data[CONF_ADDRESS]).upper()
    options = ConnectionOptions.from_mapping(entry.options)
    initial_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if initial_device is None:
        raise ConfigEntryNotReady(f"No connectable Bluetooth path found for {address}")

    client = AllpowersBLEClient(
        hass=hass,
        address=address,
        advertised_name=str(entry.data.get(CONF_DEVICE_NAME, entry.title)),
        options=options,
    )
    coordinator = AllpowersCoordinator(hass, entry, client)

    def _advertisement_callback(service_info: Any, change: Any) -> None:
        del change
        client.update_advertisement(service_info)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _advertisement_callback,
            BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    await coordinator.async_start()
    try:
        await coordinator.async_wait_ready(INITIAL_DATA_TIMEOUT)
    except TimeoutError as ex:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(
            "Connected path exists, but no valid status frame was received "
            f"from {address}"
        ) from ex

    entry.runtime_data = AllpowersRuntimeData(client=client, coordinator=coordinator)

    async def _async_stop(_event: Any) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> None:
    """Apply option changes without reloading the Bluetooth connection."""
    from .options import ConnectionOptions

    await entry.runtime_data.coordinator.async_apply_options(
        ConnectionOptions.from_mapping(entry.options)
    )


async def async_unload_entry(hass: HomeAssistant, entry: AllpowersConfigEntry) -> bool:
    """Unload an ALLPOWERS BLE config entry and release its connection slot."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.coordinator.async_shutdown()
    return unloaded
