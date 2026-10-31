"""Entry-scoped registry transitions for confirmed address reconfiguration."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_PREVIOUS_REGISTRY_ID,
    CONF_REGISTRY_ID,
    CONF_REPLACE_DEVICE,
    DOMAIN,
)
from .coordinator import AllpowersConfigEntry


@callback
def async_registry_transition_conflicts(
    hass: HomeAssistant,
    entry: AllpowersConfigEntry,
    registry_id: str,
    address: str,
) -> bool:
    """Reject target identities already occupied outside the source device."""
    entity_registry = er.async_get(hass)
    source = str(
        entry.data.get(
            CONF_PREVIOUS_REGISTRY_ID,
            entry.data.get(CONF_REGISTRY_ID, entry.data[CONF_ADDRESS]),
        )
    )
    prefix = f"{source}_"
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.platform != DOMAIN:
            continue
        if not entity.unique_id.startswith(prefix):
            return True
        unique_id = f"{registry_id}_{entity.unique_id[len(prefix) :]}"
        existing = entity_registry.async_get_entity_id(entity.domain, DOMAIN, unique_id)
        if existing is not None and existing != entity.entity_id:
            return True
    device_registry = dr.async_get(hass)
    target = device_registry.async_get_device_by_identifier(
        (DOMAIN, registry_id), entry.entry_id
    )
    source_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, source), entry.entry_id
    )
    connection_device = device_registry.async_get_device_by_connection(
        (dr.CONNECTION_BLUETOOTH, address), entry.entry_id
    )
    return (
        (target is not None and target != source_device)
        or (connection_device is not None and connection_device != source_device)
        or any(
            device != source_device
            for device in dr.async_entries_for_config_entry(
                device_registry, entry.entry_id
            )
        )
    )


@callback
def async_apply_registry_transition(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> None:
    """Migrate the same device or discard this entry's replaced device records."""
    source = str(entry.data[CONF_PREVIOUS_REGISTRY_ID])
    registry_id = str(entry.data[CONF_REGISTRY_ID])
    if not entry.data[CONF_REPLACE_DEVICE] and async_registry_transition_conflicts(
        hass, entry, registry_id, str(entry.data[CONF_ADDRESS]).upper()
    ):
        raise HomeAssistantError(
            "Registry transition conflicts with existing records; "
            "reconfigure explicitly as a replacement to remove this entry's old records"
        )
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    if entry.data[CONF_REPLACE_DEVICE]:
        for entity in entities:
            entity_registry.async_remove(entity.entity_id)
        for device in devices:
            # Deleted records can be restored by Bluetooth address. Retire their
            # lookup keys first so a replacement cannot inherit customizations.
            device_registry.async_update_device(
                device.id,
                new_identifiers={(DOMAIN, f"retired_{device.id}")},
                new_connections=set(),
            )
            device_registry.async_remove_device(device.id)
        return

    prefix = f"{source}_"
    for entity in entities:
        if entity.platform == DOMAIN and entity.unique_id.startswith(prefix):
            unique_id = f"{registry_id}_{entity.unique_id[len(prefix) :]}"
            if unique_id != entity.unique_id:
                entity_registry.async_update_entity(
                    entity.entity_id, new_unique_id=unique_id
                )
    for device in devices:
        device_registry.async_update_device(
            device.id,
            new_identifiers=(device.identifiers - {(DOMAIN, source)})
            | {(DOMAIN, registry_id)},
            new_connections={
                connection
                for connection in device.connections
                if connection[0] != dr.CONNECTION_BLUETOOTH
            }
            | {(dr.CONNECTION_BLUETOOTH, str(entry.data[CONF_ADDRESS]).upper())},
        )
