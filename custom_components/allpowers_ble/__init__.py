"""The ALLPOWERS BLE Home Assistant integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_ADDRESS, Platform

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import AllpowersConfigEntry

from .const import (
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    CONF_DEVICE_IDENTITY,
    CONF_DEVICE_IDENTITY_CONFLICT,
    CONF_DEVICE_NAME,
    CONF_ENTITY_ID_PREFIX,
    CONF_PREVIOUS_REGISTRY_ID,
    CONF_REGISTRY_ID,
    CONF_REPLACE_DEVICE,
    DOMAIN,
)
from .model_support import (
    canonical_model_identity,
    has_specific_model_identity,
    identify_model,
)
from .options import ConnectionOptions
from .repairs import (
    ISSUE_INVALID_MIGRATION,
    AllpowersRepairsManager,
    async_create_unload_failed_issue,
    async_delete_unload_failed_issue,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.NUMBER,
)


def _migrated_identity_from_display_name(name: str) -> str:
    """Trust only specific legacy names from protocol families not rejected here."""
    if has_specific_model_identity(name) and identify_model(name).supported:
        return name.strip()
    return ""


async def async_migrate_entry(hass: HomeAssistant, entry: AllpowersConfigEntry) -> bool:
    """Migrate old config entries to the latest supported schema."""
    if entry.version > CONFIG_ENTRY_VERSION or (
        entry.version == CONFIG_ENTRY_VERSION
        and entry.minor_version > CONFIG_ENTRY_MINOR_VERSION
    ):
        _LOGGER.error(
            "Cannot migrate config entry %s: unsupported future version %s.%s",
            entry.entry_id,
            entry.version,
            entry.minor_version,
        )
        return False

    if (
        entry.version == CONFIG_ENTRY_VERSION
        and entry.minor_version == CONFIG_ENTRY_MINOR_VERSION
    ):
        _delete_migration_issue(hass, entry)
        return True

    if entry.version == CONFIG_ENTRY_VERSION and entry.minor_version == 1:
        migrated_data = dict(entry.data)
        stored_name_value = migrated_data.get(CONF_DEVICE_NAME)
        stored_name = stored_name_value if isinstance(stored_name_value, str) else ""
        migrated_data[CONF_DEVICE_IDENTITY] = _migrated_identity_from_display_name(
            stored_name
        )
        hass.config_entries.async_update_entry(
            entry,
            data=migrated_data,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
        _LOGGER.info(
            "Migrated config entry %s from version 1.1 to %s.%s",
            entry.entry_id,
            CONFIG_ENTRY_VERSION,
            CONFIG_ENTRY_MINOR_VERSION,
        )
        _delete_migration_issue(hass, entry)
        return True

    if entry.version == 1 and entry.minor_version == 0:
        try:
            migrated_options = ConnectionOptions.from_mapping(entry.options).as_dict()
        except (TypeError, ValueError) as ex:
            _LOGGER.error(
                "Cannot migrate config entry %s from version 1.0: invalid options (%s)",
                entry.entry_id,
                ex,
            )
            _create_migration_issue(hass, entry)
            return False

        migrated_data = dict(entry.data)
        if CONF_ADDRESS in migrated_data:
            migrated_data[CONF_ADDRESS] = str(migrated_data[CONF_ADDRESS]).upper()
        if CONF_DEVICE_NAME in migrated_data:
            migrated_data[CONF_DEVICE_NAME] = (
                str(migrated_data[CONF_DEVICE_NAME])
                if isinstance(migrated_data[CONF_DEVICE_NAME], str)
                else ""
            )
        stored_name_value = migrated_data.get(CONF_DEVICE_NAME)
        stored_name = stored_name_value if isinstance(stored_name_value, str) else ""
        migrated_data[CONF_DEVICE_IDENTITY] = _migrated_identity_from_display_name(
            stored_name
        )

        hass.config_entries.async_update_entry(
            entry,
            data=migrated_data,
            options=migrated_options,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
        _LOGGER.info(
            "Migrated config entry %s from version 1.0 to %s.%s",
            entry.entry_id,
            CONFIG_ENTRY_VERSION,
            CONFIG_ENTRY_MINOR_VERSION,
        )
        _delete_migration_issue(hass, entry)
        return True

    _LOGGER.error(
        "Cannot migrate config entry %s: unsupported version %s.%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: AllpowersConfigEntry) -> bool:
    """Set up an ALLPOWERS BLE device from a config entry."""
    from homeassistant.components import bluetooth
    from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.exceptions import ConfigEntryNotReady

    from .client import AllpowersBLEClient
    from .const import CONF_DEVICE_NAME, INITIAL_DATA_TIMEOUT
    from .coordinator import AllpowersCoordinator, AllpowersRuntimeData
    from .options import ConnectionOptions

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    advertisement_unsubscribe: Callable[[], None] | None = None

    def _unsubscribe_advertisement() -> None:
        nonlocal advertisement_unsubscribe
        if advertisement_unsubscribe is not None:
            advertisement_unsubscribe()
            advertisement_unsubscribe = None

    entry.async_on_unload(_unsubscribe_advertisement)

    while True:
        address = str(entry.data[CONF_ADDRESS]).upper()
        config_data = dict(entry.data)
        config_title = entry.title
        config_options = dict(entry.options)
        options = ConnectionOptions.from_mapping(config_options)
        stored_name = entry.data.get(CONF_DEVICE_NAME)
        stored_identity = entry.data.get(CONF_DEVICE_IDENTITY)
        stored_identity = stored_identity if isinstance(stored_identity, str) else ""
        stored_model_identity = canonical_model_identity(stored_identity)
        rejected_stored_identity = bool(
            stored_model_identity
            and not identify_model(stored_model_identity).supported
        )

        def _persist_model_identity(
            identity: str,
            *,
            persisted_data: dict[str, Any] = config_data,
            persisted_title: str = config_title,
        ) -> None:
            if entry.data != persisted_data or entry.title != persisted_title:
                return
            if entry.data.get(CONF_DEVICE_IDENTITY) == identity:
                return
            persisted_data[CONF_DEVICE_IDENTITY] = identity
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_DEVICE_IDENTITY: identity},
            )

        def _persist_identity_conflict(
            conflicted: bool,
            *,
            persisted_data: dict[str, Any] = config_data,
            persisted_title: str = config_title,
        ) -> None:
            if entry.data != persisted_data or entry.title != persisted_title:
                return
            if entry.data.get(CONF_DEVICE_IDENTITY_CONFLICT, False) == conflicted:
                return
            persisted_data[CONF_DEVICE_IDENTITY_CONFLICT] = conflicted
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_DEVICE_IDENTITY_CONFLICT: conflicted},
            )

        if rejected_stored_identity:
            _persist_identity_conflict(True)

        initial_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if initial_device is None:
            raise ConfigEntryNotReady(
                f"No connectable Bluetooth path found for {address}"
            )

        client = AllpowersBLEClient(
            hass=hass,
            address=address,
            advertised_name=str(stored_name or entry.title),
            device_identity=stored_identity,
            identity_conflicted=bool(
                entry.data.get(CONF_DEVICE_IDENTITY_CONFLICT, False)
                or rejected_stored_identity
            ),
            model_identity_callback=_persist_model_identity,
            identity_conflict_callback=_persist_identity_conflict,
            options=options,
        )
        coordinator = AllpowersCoordinator(hass, entry, client)
        initial_snapshot = client.snapshot()
        repairs = AllpowersRepairsManager(
            hass,
            entry_id=entry.entry_id,
            entry_title=entry.title,
            initial_snapshot=initial_snapshot,
        )
        coordinator.set_repairs_manager(repairs)
        repairs.evaluate(initial_snapshot, status_is_fresh=initial_snapshot.connected)

        def _advertisement_callback(
            service_info: Any,
            change: Any,
            *,
            active_client: AllpowersBLEClient = client,
        ) -> None:
            del change
            active_client.update_advertisement(service_info)

        advertisement_unsubscribe = bluetooth.async_register_callback(
            hass,
            _advertisement_callback,
            BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

        await coordinator.async_start()
        try:
            await coordinator.async_wait_ready(INITIAL_DATA_TIMEOUT)
        except TimeoutError as ex:
            await coordinator.async_shutdown()
            if entry.data != config_data or entry.title != config_title:
                _unsubscribe_advertisement()
                continue
            raise ConfigEntryNotReady(
                "Connected path exists, but no valid status frame was received "
                f"from {address}"
            ) from ex

        while entry.options != config_options:
            config_options = dict(entry.options)
            await coordinator.async_apply_options(
                ConnectionOptions.from_mapping(config_options)
            )

        if entry.data != config_data or entry.title != config_title:
            await coordinator.async_shutdown()
            _unsubscribe_advertisement()
            continue
        break

    if CONF_PREVIOUS_REGISTRY_ID in entry.data:
        from .registry import async_apply_registry_transition

        try:
            async_apply_registry_transition(hass, entry)
        except Exception:
            await coordinator.async_shutdown()
            raise
        if config_data[CONF_REPLACE_DEVICE]:
            config_data[CONF_ENTITY_ID_PREFIX] = config_data[CONF_REGISTRY_ID]
        config_data.pop(CONF_PREVIOUS_REGISTRY_ID)
        config_data.pop(CONF_REPLACE_DEVICE)
        hass.config_entries.async_update_entry(entry, data=dict(config_data))

    entry.runtime_data = AllpowersRuntimeData(
        client=client,
        coordinator=coordinator,
        config_data=config_data,
        config_title=config_title,
    )

    async def _async_stop(_event: Any) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_delete_unload_failed_issue(hass, entry)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> None:
    """Apply options live, or schedule one reload for changed runtime config."""
    from homeassistant.config_entries import ConfigEntryState

    from .options import ConnectionOptions

    if entry.state is ConfigEntryState.FAILED_UNLOAD:
        async_create_unload_failed_issue(hass, entry)
        return
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
            hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    if runtime.reload_pending:
        return
    if entry.data != runtime.config_data or entry.title != runtime.config_title:
        runtime.reload_pending = True
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    await runtime.coordinator.async_apply_options(
        ConnectionOptions.from_mapping(entry.options)
    )


async def async_unload_entry(hass: HomeAssistant, entry: AllpowersConfigEntry) -> bool:
    """Unload an ALLPOWERS BLE config entry and release its connection slot."""
    try:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unloaded:
            await entry.runtime_data.coordinator.async_shutdown()
    except Exception:
        async_create_unload_failed_issue(hass, entry)
        raise
    if not unloaded:
        _LOGGER.error(
            "Platform unload failed for config entry %s; restart Home Assistant "
            "before reloading or reconfiguring this entry",
            entry.entry_id,
        )
        async_create_unload_failed_issue(hass, entry)
    return unloaded


def _migration_issue_id(entry: AllpowersConfigEntry) -> str:
    return f"{entry.entry_id}_{ISSUE_INVALID_MIGRATION}"


def _create_migration_issue(hass: HomeAssistant, entry: AllpowersConfigEntry) -> None:
    try:
        from homeassistant.helpers import issue_registry as ir
    except ModuleNotFoundError:
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        _migration_issue_id(entry),
        issue_domain=DOMAIN,
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_INVALID_MIGRATION,
        translation_placeholders={"entry_title": entry.title},
        learn_more_url=(
            "https://github.com/dedalodaelus/home-assistant-allpowers-ble"
            "/blob/main/docs/troubleshooting.md#home-assistant-repairs"
        ),
    )


def _delete_migration_issue(hass: HomeAssistant, entry: AllpowersConfigEntry) -> None:
    try:
        from homeassistant.helpers import issue_registry as ir
    except ModuleNotFoundError:
        return

    ir.async_delete_issue(hass, DOMAIN, _migration_issue_id(entry))
