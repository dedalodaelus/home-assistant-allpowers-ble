"""Registry migration preflight, preservation, and replacement tests."""

from types import SimpleNamespace
from typing import Any

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.allpowers_ble import registry
from custom_components.allpowers_ble.const import (
    CONF_PREVIOUS_REGISTRY_ID,
    CONF_REGISTRY_ID,
    CONF_REPLACE_DEVICE,
    DOMAIN,
)
from tests.helpers import FakeHass
from tests.test_init import make_entry


class RegistryFake:
    def __init__(self) -> None:
        self.entities: dict[str, Any] = {}
        self.devices: dict[str, Any] = {}

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        return next(
            (
                entity.entity_id
                for entity in self.entities.values()
                if (entity.domain, entity.platform, entity.unique_id)
                == (domain, platform, unique_id)
            ),
            None,
        )

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        return next(
            (
                device
                for device in self.devices.values()
                if device.config_entry_id == config_entry_id
                and identifier in device.identifiers
            ),
            None,
        )

    def async_get_device_by_connection(self, connection, config_entry_id):
        return next(
            (
                device
                for device in self.devices.values()
                if device.config_entry_id == config_entry_id
                and connection in device.connections
            ),
            None,
        )

    def async_update_entity(self, entity_id: str, *, new_unique_id: str):
        self.entities[entity_id].unique_id = new_unique_id

    def async_update_device(self, device_id: str, **kwargs):
        device = self.devices[device_id]
        if "new_identifiers" in kwargs:
            device.identifiers = kwargs["new_identifiers"]
        if "new_connections" in kwargs:
            device.connections = kwargs["new_connections"]

    def async_remove(self, entity_id: str):
        del self.entities[entity_id]

    def async_remove_device(self, device_id: str):
        device = self.devices.pop(device_id)
        assert device.connections == set()
        assert device.identifiers == {(DOMAIN, f"retired_{device_id}")}


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch):
    fake = RegistryFake()
    monkeypatch.setattr(registry.er, "async_get", lambda hass: fake)
    monkeypatch.setattr(registry.dr, "async_get", lambda hass: fake)
    monkeypatch.setattr(
        registry.er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: [
            entity
            for entity in reg.entities.values()
            if entity.config_entry_id == entry_id
        ],
    )
    monkeypatch.setattr(
        registry.dr,
        "async_entries_for_config_entry",
        lambda reg, entry_id: [
            device
            for device in reg.devices.values()
            if device.config_entry_id == entry_id
        ],
    )
    return fake


def entity(unique_id: str, *, entry_id: str = "entry-id", platform: str = DOMAIN):
    return SimpleNamespace(
        entity_id=f"sensor.{unique_id}",
        unique_id=unique_id,
        domain="sensor",
        platform=platform,
        config_entry_id=entry_id,
    )


def device(identifier: str, *, address: str = "A", entry_id: str = "entry-id"):
    return SimpleNamespace(
        id=identifier,
        identifiers={(DOMAIN, identifier)},
        connections={("bluetooth", address)},
        config_entry_id=entry_id,
    )


@pytest.mark.parametrize(
    "collision",
    [
        "none",
        "entity",
        "foreign_platform",
        "stale_entity",
        "identifier",
        "connection",
        "extra_device",
    ],
)
def test_preflight_detects_conflicts_without_mutating_records(registries, collision):
    entry = make_entry()
    entry.data.update({CONF_PREVIOUS_REGISTRY_ID: "A", CONF_REGISTRY_ID: "B"})
    source = entity("A_battery")
    registries.entities[source.entity_id] = source
    source_device = device("A")
    registries.devices[source_device.id] = source_device
    if collision in ("entity", "foreign_platform", "stale_entity"):
        other = entity(
            "C_battery"
            if collision == "stale_entity"
            else "B_battery"
            if collision != "foreign_platform"
            else "other",
            entry_id="other-entry" if collision == "entity" else entry.entry_id,
            platform="other" if collision == "foreign_platform" else DOMAIN,
        )
        registries.entities[other.entity_id] = other
    if collision in ("identifier", "connection", "extra_device"):
        other_device = device(
            "B" if collision == "identifier" else "extra",
            address="B" if collision == "connection" else "extra",
        )
        registries.devices[other_device.id] = other_device
    assert registry.async_registry_transition_conflicts(
        FakeHass(), entry, "B", "B"
    ) == (collision not in ("none", "foreign_platform"))
    assert source.unique_id == "A_battery"
    assert source_device.connections == {("bluetooth", "A")}


@pytest.mark.parametrize("replace_device", [False, True])
def test_transition_preserves_or_discards_only_owned_records(
    registries, replace_device
):
    entry = make_entry()
    entry.data.update(
        {
            "address": "B",
            CONF_REGISTRY_ID: "B",
            CONF_PREVIOUS_REGISTRY_ID: "A",
            CONF_REPLACE_DEVICE: replace_device,
        }
    )
    source = entity("A_battery")
    other = entity("other_battery", entry_id="other")
    registries.entities = {source.entity_id: source, other.entity_id: other}
    source_device = device("A")
    other_device = device("other", entry_id="other")
    registries.devices = {"A": source_device, "other": other_device}
    registry.async_apply_registry_transition(FakeHass(), entry)
    assert registries.entities[other.entity_id] is other
    assert registries.devices["other"] is other_device
    if replace_device:
        assert source.entity_id not in registries.entities
        assert "A" not in registries.devices
    else:
        assert source.unique_id == "B_battery"
        assert source_device.identifiers == {(DOMAIN, "B")}
        assert source_device.connections == {("bluetooth", "B")}


def test_transition_rechecks_collisions_before_any_mutation(registries):
    entry = make_entry()
    entry.data.update(
        {
            "address": "B",
            CONF_REGISTRY_ID: "B",
            CONF_PREVIOUS_REGISTRY_ID: "A",
            CONF_REPLACE_DEVICE: False,
        }
    )
    source = entity("A_battery")
    conflicting = entity("B_battery", entry_id="other")
    registries.entities = {source.entity_id: source, conflicting.entity_id: conflicting}
    with pytest.raises(HomeAssistantError, match="conflicts"):
        registry.async_apply_registry_transition(FakeHass(), entry)
    assert source.unique_id == "A_battery"


def test_transition_keeps_stable_ids_and_unrelated_device_metadata(registries):
    entry = make_entry()
    entry.data.update(
        {
            "address": "B",
            CONF_REGISTRY_ID: "A",
            CONF_PREVIOUS_REGISTRY_ID: "A",
            CONF_REPLACE_DEVICE: False,
        }
    )
    stable = entity("A_battery")
    foreign = entity("foreign", platform="other")
    source_device = device("A")
    source_device.identifiers.add(("other", "extra"))
    source_device.connections.add(("mac", "other"))
    registries.entities = {stable.entity_id: stable, foreign.entity_id: foreign}
    registries.devices = {"A": source_device}
    registry.async_apply_registry_transition(FakeHass(), entry)
    assert stable.unique_id == "A_battery"
    assert foreign.unique_id == "foreign"
    assert source_device.identifiers == {(DOMAIN, "A"), ("other", "extra")}
    assert source_device.connections == {("bluetooth", "B"), ("mac", "other")}


@pytest.mark.parametrize(
    "connections",
    [None, set(), {("bluetooth", "A")}, {("bluetooth", "A"), ("bluetooth", "B")}],
)
def test_source_address_only_identifies_an_intact_unambiguous_device(
    registries, connections
):
    entry = make_entry()
    entry.data[CONF_PREVIOUS_REGISTRY_ID] = "source"
    if connections is not None:
        source = device("source")
        source.connections = connections | {("mac", "unrelated")}
        registries.devices[source.id] = source
    assert registry.async_registry_source_address(FakeHass(), entry) == (
        "A" if connections == {("bluetooth", "A")} else None
    )


@pytest.mark.parametrize("device_migrated", [False, True])
def test_partially_migrated_records_are_idempotently_retryable(
    registries, device_migrated
):
    entry = make_entry()
    entry.data.update(
        {
            "address": "B",
            CONF_REGISTRY_ID: "target",
            CONF_PREVIOUS_REGISTRY_ID: "source",
            CONF_REPLACE_DEVICE: False,
        }
    )
    original = entity("source_battery")
    migrated = entity("target_temperature")
    registries.entities = {original.entity_id: original, migrated.entity_id: migrated}
    record = device(
        "target" if device_migrated else "source",
        address="B" if device_migrated else "A",
    )
    registries.devices[record.id] = record
    assert not registry.async_registry_transition_conflicts(
        FakeHass(), entry, "target", "B"
    )
    registry.async_apply_registry_transition(FakeHass(), entry)
    registry.async_apply_registry_transition(FakeHass(), entry)
    assert original.unique_id == "target_battery"
    assert migrated.unique_id == "target_temperature"
    assert record.identifiers == {(DOMAIN, "target")}
    assert record.connections == {("bluetooth", "B")}


def test_partial_migration_rejects_duplicate_logical_entities(registries):
    entry = make_entry()
    entry.data.update({CONF_PREVIOUS_REGISTRY_ID: "source", CONF_REGISTRY_ID: "target"})
    source = entity("source_battery")
    duplicate = entity("target_battery")
    registries.entities = {source.entity_id: source, duplicate.entity_id: duplicate}
    assert registry.async_registry_transition_conflicts(
        FakeHass(), entry, "target", "B"
    )
