"""Discover ALLPOWERS devices by registry ownership, never translated names."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
from typing import Any

from .api import HAAPIError

DOMAIN = "allpowers_ble"
KNOWN_MODELS = {"R600", "VOLIX P1800", "BLE power station", "S500", "S700"}
CONFIG_ENTRY_STATES = frozenset(
    {
        "loaded",
        "not_loaded",
        "setup_error",
        "setup_retry",
        "migration_error",
        "failed_unload",
        "setup_in_progress",
        "unload_in_progress",
    }
)
SENSOR_KEYS = (
    "battery",
    "input_power",
    "output_power",
    "remaining_time",
    "hardware_version",
    "firmware_version",
)
BINARY_KEYS = ("connected", "telemetry_available")
CONTROL_DOMAINS = {
    "ac_output": "switch",
    "dc_output": "switch",
    "light": "switch",
    "eco_mode": "switch",
    "car_charger": "switch",
    "work_mode": "select",
    "eco_timeout": "select",
}


@dataclass(slots=True)
class Device:
    """Transient registry selection; HA IDs never enter report serialization."""

    device_id: str
    model: str
    hardware: str | None
    firmware: str | None
    state_entities: dict[str, str]
    entry_state: str | None = None
    control_entities: dict[str, str] = field(default_factory=dict)
    config_entry_id: str | None = None
    refresh_entity: str | None = None


def safe_model(value: Any) -> str:
    """Permit only integration-generated model families in a public report."""
    if not isinstance(value, str):
        return "unknown"
    if value in KNOWN_MODELS or re.fullmatch(r"S[0-9]{3,4}", value):
        return value
    return "unknown"


def safe_version(value: Any) -> str | None:
    """Reject arbitrary device-provided strings (could contain identifiers)."""
    if isinstance(value, str):
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            return None
        if re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,3}", value):
            return value
        if re.fullmatch(r"0[xX][0-9a-fA-F]{1,4}", value):
            return "0x" + value[2:].upper()
    return None


def _matches_entry(device: dict[str, Any], entry_ids: set[str]) -> bool:
    entry_id = device.get("config_entry_id")
    if isinstance(entry_id, str) and entry_id in entry_ids:
        return True
    older = device.get("config_entries")
    return isinstance(older, list) and any(x in entry_ids for x in older)


def _owned_identifiers(device: dict[str, Any]) -> bool:
    identifiers = device.get("identifiers")
    return isinstance(identifiers, list) and any(
        isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == DOMAIN
        for item in identifiers
    )


def _entry_state(device: dict[str, Any], entries: list[Any] | None) -> str | None:
    """Inspect only linked config entries, without retaining their IDs.

    A missing, inaccessible or ambiguous entry state is *not* proof of a loaded
    integration. The list comes from Home Assistant, not arbitrary device names.
    """
    if entries is None:
        return None
    ids: set[str] = set()
    main_id = device.get("config_entry_id")
    if isinstance(main_id, str):
        ids.add(main_id)
    old_ids = device.get("config_entries")
    if isinstance(old_ids, list):
        ids.update(value for value in old_ids if isinstance(value, str))
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("domain") == DOMAIN
        and isinstance(entry.get("entry_id"), str)
        and entry["entry_id"] in ids
    ]
    if len(matches) != 1:
        return None
    entry = matches[0]
    if entry.get("disabled_by") is not None:
        return "disabled"
    state = entry.get("state")
    return state if isinstance(state, str) and state in CONFIG_ENTRY_STATES else None


def _linked_entry_ids(device: dict[str, Any]) -> set[str]:
    """Return transient config-entry links from one validated registry device."""
    linked: set[str] = set()
    main_id = device.get("config_entry_id")
    if isinstance(main_id, str):
        linked.add(main_id)
    older = device.get("config_entries")
    if isinstance(older, list):
        linked.update(value for value in older if isinstance(value, str))
    return linked


def _loaded_entry_id(device: dict[str, Any], entries: list[Any] | None) -> str | None:
    """Return one loaded ALLPOWERS config-entry ID for a transient HIL request."""
    if entries is None:
        return None
    matches = [
        value
        for value in entries
        if isinstance(value, dict)
        and value.get("domain") == DOMAIN
        and value.get("state") == "loaded"
        and value.get("disabled_by") is None
        and isinstance(value.get("entry_id"), str)
        and value["entry_id"] in _linked_entry_ids(device)
    ]
    return matches[0]["entry_id"] if len(matches) == 1 else None


def _entity_key(entry: dict[str, Any]) -> str | None:
    """Resolve based on platform and integration-owned unique-id suffix."""
    unique_id = entry.get("unique_id")
    entity_id = entry.get("entity_id")
    if not isinstance(unique_id, str) or not isinstance(entity_id, str):
        return None
    for key in SENSOR_KEYS + BINARY_KEYS:
        prefix = "sensor." if key in SENSOR_KEYS else "binary_sensor."
        if unique_id.endswith("_" + key) and entity_id.startswith(prefix):
            return key
    return None


def _control_key(entry: dict[str, Any]) -> str | None:
    """Resolve only integration-owned control suffixes on their fixed domain."""
    unique_id = entry.get("unique_id")
    entity_id = entry.get("entity_id")
    if not isinstance(unique_id, str) or not isinstance(entity_id, str):
        return None
    for key, domain in CONTROL_DOMAINS.items():
        if unique_id.endswith("_" + key) and entity_id.startswith(domain + "."):
            return key
    return None


def _refresh_entity(entry: dict[str, Any]) -> str | None:
    """Resolve only the integration-owned read-only refresh button."""
    unique_id = entry.get("unique_id")
    entity_id = entry.get("entity_id")
    if (
        isinstance(unique_id, str)
        and unique_id.endswith("_refresh")
        and isinstance(entity_id, str)
        and entity_id.startswith("button.")
    ):
        return entity_id
    return None


def find_devices(
    entities: list[Any],
    devices: list[Any],
    entries: list[Any] | None,
) -> list[Device]:
    """Find devices from the owning integration, never manufacturer alone."""
    entry_ids = {
        value["entry_id"]
        for value in (entries or [])
        if isinstance(value, dict)
        and value.get("domain") == DOMAIN
        and isinstance(value.get("entry_id"), str)
    }
    platforms: dict[str, list[dict[str, Any]]] = {}
    for raw_entity in entities:
        if not isinstance(raw_entity, dict) or raw_entity.get("platform") != DOMAIN:
            continue
        device_id = raw_entity.get("device_id")
        if isinstance(device_id, str):
            platforms.setdefault(device_id, []).append(raw_entity)

    results: list[Device] = []
    for raw_device in devices:
        if not isinstance(raw_device, dict):
            continue
        device_id = raw_device.get("id")
        if not isinstance(device_id, str):
            continue
        if not (
            device_id in platforms
            or _matches_entry(raw_device, entry_ids)
            or _owned_identifiers(raw_device)
        ):
            continue
        # Fail closed if a config-entry list explicitly contradicts other hints.
        if entry_ids and not _matches_entry(raw_device, entry_ids):
            continue
        mapping: dict[str, str] = {}
        control_mapping: dict[str, str] = {}
        refresh_entity: str | None = None
        ambiguous_refresh = False
        ambiguous: set[str] = set()
        ambiguous_controls: set[str] = set()
        for entity in platforms.get(device_id, []):
            if entity.get("disabled_by") is not None:
                continue
            key = _entity_key(entity)
            entity_id = entity.get("entity_id")
            if key is not None and isinstance(entity_id, str):
                if key in mapping:
                    ambiguous.add(key)
                mapping[key] = entity_id
            control_key = _control_key(entity)
            refresh = _refresh_entity(entity)
            if refresh is not None:
                config_entry_id = entity.get("config_entry_id")
                linked_entries = _linked_entry_ids(raw_device)
                if config_entry_id is None or (
                    linked_entries and config_entry_id not in linked_entries
                ):
                    continue
                if refresh_entity is not None:
                    ambiguous_refresh = True
                refresh_entity = refresh
            expected_domain = CONTROL_DOMAINS.get(control_key or "")
            if (
                control_key is not None
                and expected_domain is not None
                and isinstance(entity_id, str)
                and entity_id.startswith(expected_domain + ".")
            ):
                config_entry_id = entity.get("config_entry_id")
                linked_entries = _linked_entry_ids(raw_device)
                if (
                    config_entry_id is not None
                    and linked_entries
                    and config_entry_id not in linked_entries
                ):
                    continue
                if control_key in control_mapping:
                    ambiguous_controls.add(control_key)
                control_mapping[control_key] = entity_id
        for key in ambiguous:
            mapping.pop(key, None)
        for key in ambiguous_controls:
            control_mapping.pop(key, None)
        if ambiguous_refresh:
            refresh_entity = None
        results.append(
            Device(
                device_id=device_id,
                model=safe_model(raw_device.get("model")),
                hardware=safe_version(raw_device.get("hw_version")),
                firmware=safe_version(raw_device.get("sw_version")),
                state_entities=mapping,
                entry_state=_entry_state(raw_device, entries),
                control_entities=control_mapping,
                config_entry_id=_loaded_entry_id(raw_device, entries),
                refresh_entity=refresh_entity,
            )
        )
    return sorted(
        results,
        key=lambda device: (
            device.model,
            device.hardware or "",
            device.firmware or "",
            device.device_id,
        ),
    )


def choose_device(
    devices: list[Device], selector: str | None, *, non_interactive: bool
) -> Device:
    """Select by a short 1-based index or unambiguous model, never entity ID."""
    if not devices:
        raise HAAPIError("No ALLPOWERS devices found in the HA registries")
    if selector:
        if selector.isdecimal():
            index = int(selector)
            if 1 <= index <= len(devices):
                return devices[index - 1]
        else:
            matches = [
                dev for dev in devices if dev.model.casefold() == selector.casefold()
            ]
            if len(matches) == 1:
                return matches[0]
        raise HAAPIError(
            "Selected device is missing or ambiguous; choose its list index"
        )
    if len(devices) == 1:
        return devices[0]
    if non_interactive:
        raise HAAPIError("Multiple devices; set --device to a displayed list index")
    while True:
        choice = input("Select device number: ").strip()
        if choice.isdecimal() and 1 <= int(choice) <= len(devices):
            return devices[int(choice) - 1]
        print("Invalid selection; enter a number from the device list")
