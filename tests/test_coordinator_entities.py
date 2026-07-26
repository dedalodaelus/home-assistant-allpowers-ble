"""Coordinator and Home Assistant entity adapter tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.allpowers_ble import (
    binary_sensor,
    button,
    coordinator as coordinator_module,
    number,
    select,
    sensor,
    switch,
)
from custom_components.allpowers_ble.client import NotConnectedError
from custom_components.allpowers_ble.options import ConnectionOptions
from custom_components.allpowers_ble.protocol import StateUnavailableError, WorkMode

from tests.helpers import (
    ADDRESS,
    configured_entry,
    disconnected_snapshot,
    settings,
    snapshot,
    status,
)


def assert_command_error(
    error_info: pytest.ExceptionInfo[HomeAssistantError],
    *,
    key: str,
    cause: type[Exception],
) -> None:
    """Assert that a writable entity exposes a translated command error."""
    error = error_info.value
    assert getattr(error, "translation_domain", None) == "allpowers_ble"
    assert getattr(error, "translation_key", None) == key
    assert isinstance(error.__cause__, cause)


class _RegistryDevice:
    """Tiny mutable record emulating Home Assistant device entries."""

    def __init__(
        self, device_id: str, *, hw_version: str | None, sw_version: str | None
    ) -> None:
        self.id = device_id
        self.hw_version = hw_version
        self.sw_version = sw_version


class _RegistryStub:
    """Small device-registry stub supporting the coordinator contract."""

    def __init__(self, devices_by_address: dict[str, _RegistryDevice]) -> None:
        self._devices_by_address = devices_by_address
        self.updates: list[tuple[str, dict[str, str]]] = []

    def async_get_device(
        self,
        *,
        identifiers: set[tuple[str, str]],
        connections: set[tuple[str, str]],
    ) -> _RegistryDevice | None:
        del connections
        _, address = next(iter(identifiers))
        return self._devices_by_address.get(address)

    def async_update_device(self, device_id: str, **kwargs: str) -> _RegistryDevice:
        self.updates.append((device_id, dict(kwargs)))
        for device in self._devices_by_address.values():
            if device.id != device_id:
                continue
            if "hw_version" in kwargs:
                device.hw_version = kwargs["hw_version"]
            if "sw_version" in kwargs:
                device.sw_version = kwargs["sw_version"]
            return device
        raise AssertionError(f"Unexpected device_id update: {device_id}")


@pytest.mark.asyncio
async def test_coordinator_lifecycle_and_push_updates() -> None:
    entry, client, coordinator, _ = configured_entry()

    assert coordinator.data == client.snapshot()
    assert coordinator.connected
    assert coordinator.status_is_fresh
    assert coordinator.settings_are_fresh
    assert coordinator.controls_available
    assert coordinator.settings_controls_available

    await coordinator.async_start()
    await coordinator.async_wait_ready(12)
    options = ConnectionOptions(status_interval=15, stale_timeout=31)
    await coordinator.async_apply_options(options)
    assert client.options == options
    assert ("wait_ready", 12) in client.calls

    new_state = replace(client.snapshot(), rssi=-70)
    previous_updates = coordinator.update_count
    client.set_snapshot(new_state)
    assert coordinator.data.rssi == -70
    assert coordinator.update_count == previous_updates + 1
    assert await coordinator._async_update_data() == new_state

    await coordinator.async_shutdown()
    assert client.stopped
    assert client.callback is None
    assert entry.runtime_data.coordinator is coordinator


def test_validated_registry_version_accepts_only_bcd_and_non_zero() -> None:
    assert coordinator_module._validated_registry_version("1.2", 0x12) == "1.2"
    assert coordinator_module._validated_registry_version(" 3.4 ", 0x34) == "3.4"
    assert coordinator_module._validated_registry_version("", 0x12) is None
    assert coordinator_module._validated_registry_version(None, 0x12) is None
    assert coordinator_module._validated_registry_version("0xAF", 0xAF) is None
    assert coordinator_module._validated_registry_version("0.0", 0x00) is None


def test_coordinator_registry_refresh_returns_early_without_address_or_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, client, coordinator, _ = configured_entry()

    entry.data["address"] = ""
    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(),
            settings_monotonic=monotonic(),
        )
    )

    registry = _RegistryStub({})
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_get",
        lambda hass: registry,
        raising=False,
    )
    entry.data["address"] = ADDRESS
    coordinator._async_refresh_device_registry_metadata()
    assert registry.updates == []


def test_coordinator_refreshes_registry_metadata_once_per_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = replace(snapshot(), settings=None, settings_monotonic=None)
    entry, client, _, _ = configured_entry(state=start)
    registry = _RegistryStub(
        {
            ADDRESS: _RegistryDevice("dev-1", hw_version=None, sw_version=None),
        }
    )
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_get",
        lambda hass: registry,
        raising=False,
    )

    client.set_snapshot(
        replace(
            start,
            settings=settings(),
            settings_monotonic=monotonic(),
        )
    )
    assert registry.updates == [("dev-1", {"hw_version": "1.2", "sw_version": "3.4"})]

    client.set_snapshot(
        replace(
            client.snapshot(),
            rssi=-70,
        )
    )
    assert len(registry.updates) == 1

    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(
                hardware_version="1.3",
                firmware_version="3.5",
                raw_hardware_version=0x13,
                raw_firmware_version=0x35,
            ),
            settings_monotonic=monotonic(),
        )
    )
    assert registry.updates[-1] == (
        "dev-1",
        {"hw_version": "1.3", "sw_version": "3.5"},
    )


def test_coordinator_does_not_overwrite_valid_registry_values_with_invalid_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, client, _, _ = configured_entry()
    registry = _RegistryStub(
        {
            ADDRESS: _RegistryDevice("dev-1", hw_version="1.2", sw_version="3.4"),
        }
    )
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_get",
        lambda hass: registry,
        raising=False,
    )

    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(
                hardware_version="0xAF",
                firmware_version="0xFF",
                raw_hardware_version=0xAF,
                raw_firmware_version=0xFF,
            ),
            settings_monotonic=monotonic(),
        )
    )
    assert registry.updates == []

    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(
                hardware_version="0xAF",
                firmware_version="3.5",
                raw_hardware_version=0xAF,
                raw_firmware_version=0x35,
            ),
            settings_monotonic=monotonic(),
        )
    )
    assert registry.updates == [("dev-1", {"sw_version": "3.5"})]


def test_coordinator_registry_refresh_is_isolated_per_entry_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_a, client_a, _, _ = configured_entry()
    entry_b, client_b, _, _ = configured_entry()
    entry_b.data["address"] = "11:22:33:44:55:66"
    registry = _RegistryStub(
        {
            ADDRESS: _RegistryDevice("dev-a", hw_version=None, sw_version=None),
            "11:22:33:44:55:66": _RegistryDevice(
                "dev-b", hw_version=None, sw_version=None
            ),
        }
    )
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_get",
        lambda hass: registry,
        raising=False,
    )

    client_a.set_snapshot(replace(client_a.snapshot(), settings=settings()))
    assert registry.updates == [("dev-a", {"hw_version": "1.2", "sw_version": "3.4"})]

    client_b.set_snapshot(
        replace(
            client_b.snapshot(),
            settings=settings(
                hardware_version="2.1",
                firmware_version="4.0",
                raw_hardware_version=0x21,
                raw_firmware_version=0x40,
            ),
        )
    )
    assert registry.updates[-1] == (
        "dev-b",
        {"hw_version": "2.1", "sw_version": "4.0"},
    )


def test_coordinator_rejects_cached_or_stale_state() -> None:
    _, client, coordinator, _ = configured_entry(state=disconnected_snapshot())
    assert not coordinator.status_is_fresh
    assert not coordinator.settings_are_fresh
    assert not coordinator.controls_available
    assert not coordinator.settings_controls_available

    stale = replace(
        snapshot(),
        status_monotonic=monotonic() - client.options.stale_timeout - 1,
        settings_monotonic=(monotonic() - client.options.settings_stale_timeout - 1),
    )
    client.set_snapshot(stale)
    assert not coordinator.status_is_fresh
    assert not coordinator.settings_are_fresh


@pytest.mark.asyncio
async def test_entity_setup_and_state_values() -> None:
    entry, _, coordinator, _ = configured_entry()

    sensor_entities: list[Any] = []
    await sensor.async_setup_entry(
        None, entry, lambda entities: sensor_entities.extend(entities)
    )
    assert len(sensor_entities) == len(sensor.SENSOR_DESCRIPTIONS)
    sensor_values = {
        entity.entity_description.key: entity.native_value for entity in sensor_entities
    }
    assert sensor_values == {
        "battery": 73,
        "input_power": 300,
        "output_power": 150,
        "remaining_time": 120,
        "rssi": -61,
        "hardware_version": "1.2",
        "firmware_version": "3.4",
        "reconnects": 1,
        "parser_discards": 3,
        "protocol_errors": 1,
        "watchdog_resets": 1,
    }
    assert all(entity.available for entity in sensor_entities)
    ordinary = next(
        entity
        for entity in sensor_entities
        if entity.entity_description.key == "battery"
    )
    diagnostic = next(
        entity
        for entity in sensor_entities
        if entity.entity_description.key == "protocol_errors"
    )
    assert getattr(ordinary, "extra_state_attributes", None) is None
    assert getattr(diagnostic, "extra_state_attributes", None) is None
    for key in {
        "reconnects",
        "protocol_errors",
        "parser_discards",
        "watchdog_resets",
    }:
        assert (
            next(
                entity
                for entity in sensor_entities
                if entity.entity_description.key == key
            ).entity_description.state_class
            is None
        )

    binary_entities: list[Any] = []
    await binary_sensor.async_setup_entry(
        None, entry, lambda entities: binary_entities.extend(entities)
    )
    assert len(binary_entities) == len(binary_sensor.BINARY_SENSOR_DESCRIPTIONS)
    binary_values = {
        entity.entity_description.key: entity.is_on for entity in binary_entities
    }
    assert binary_values == {
        "connected": True,
        "telemetry_available": True,
        "settings_available": True,
        "charging": True,
        "discharging": True,
        "ac_output": False,
        "dc_output": True,
        "light_output": True,
    }
    assert all(entity.available for entity in binary_entities)

    device_info = ordinary.device_info
    assert device_info.identifiers == {("allpowers_ble", ADDRESS)}
    assert device_info.connections == {("bluetooth", ADDRESS)}
    assert device_info.model == "R600"
    assert device_info.hw_version == "1.2"
    assert device_info.sw_version == "3.4"
    assert coordinator.data.status is not None


@pytest.mark.asyncio
async def test_entities_become_unavailable_when_data_is_stale() -> None:
    state = snapshot(
        status_age=ConnectionOptions().stale_timeout + 1,
        settings_age=ConnectionOptions().settings_stale_timeout + 1,
    )
    entry, _, _, _ = configured_entry(state=state)

    sensor_entities: list[Any] = []
    await sensor.async_setup_entry(
        None, entry, lambda entities: sensor_entities.extend(entities)
    )
    availability = {
        entity.entity_description.key: entity.available for entity in sensor_entities
    }
    assert not availability["battery"]
    assert not availability["hardware_version"]
    assert availability["rssi"]
    assert availability["reconnects"]

    binary_entities: list[Any] = []
    await binary_sensor.async_setup_entry(
        None, entry, lambda entities: binary_entities.extend(entities)
    )
    states = {entity.entity_description.key: entity.is_on for entity in binary_entities}
    assert states["connected"] is True
    assert states["telemetry_available"] is False
    assert states["settings_available"] is False
    charging = next(
        entity
        for entity in binary_entities
        if entity.entity_description.key == "charging"
    )
    assert not charging.available


@pytest.mark.asyncio
async def test_binary_input_output_active_semantics_edge_cases() -> None:
    cases = [
        (0, 0, False, False),
        (1, 0, True, False),
        (0, 1, False, True),
        (250, 120, True, True),
    ]

    for (
        input_power,
        output_power,
        expected_input_active,
        expected_output_active,
    ) in cases:
        entry, _, _, _ = configured_entry(
            state=snapshot(
                status_data=status(
                    input_power_w=input_power,
                    output_power_w=output_power,
                )
            )
        )
        binary_entities: list[Any] = []
        await binary_sensor.async_setup_entry(
            None, entry, lambda entities: binary_entities.extend(entities)
        )
        by_key = {entity.entity_description.key: entity for entity in binary_entities}
        assert by_key["charging"].is_on is expected_input_active
        assert by_key["discharging"].is_on is expected_output_active
        assert by_key["charging"].available
        assert by_key["discharging"].available

    entry, _, _, _ = configured_entry(
        state=replace(snapshot(), status=None, status_monotonic=None)
    )
    binary_entities = []
    await binary_sensor.async_setup_entry(
        None, entry, lambda entities: binary_entities.extend(entities)
    )
    by_key = {entity.entity_description.key: entity for entity in binary_entities}
    assert by_key["charging"].is_on is None
    assert by_key["discharging"].is_on is None
    assert not by_key["charging"].available
    assert not by_key["discharging"].available


@pytest.mark.asyncio
async def test_switches_send_commands_and_wrap_safety_errors() -> None:
    entry, client, _, _ = configured_entry()
    entities: list[Any] = []
    await switch.async_setup_entry(None, entry, lambda values: entities.extend(values))
    by_key = {entity.entity_description.key: entity for entity in entities}
    assert set(by_key) == {"ac_output", "dc_output", "light", "eco_mode", "car_charger"}
    assert by_key["dc_output"].is_on is True
    assert by_key["ac_output"].is_on is False
    assert by_key["eco_mode"].is_on is True
    assert not by_key["car_charger"].available

    await by_key["ac_output"].async_turn_on(source="test")
    await by_key["dc_output"].async_turn_off()
    await by_key["light"].async_turn_off()
    await by_key["eco_mode"].async_turn_off()
    assert ("set_ac", True) in client.calls
    assert ("set_dc", False) in client.calls
    assert ("set_light", False) in client.calls
    assert ("set_eco", False) in client.calls

    enabled = ConnectionOptions(enable_car_charger=True)
    await entry.runtime_data.coordinator.async_apply_options(enabled)
    assert by_key["car_charger"].available
    await by_key["car_charger"].async_turn_on()
    assert ("set_car_charger", True) in client.calls

    client.errors["set_ac"] = StateUnavailableError("fresh state required")
    with pytest.raises(HomeAssistantError) as error_info:
        await by_key["ac_output"].async_turn_on()
    assert_command_error(
        error_info,
        key="command_stale_state",
        cause=StateUnavailableError,
    )

    client.errors["set_eco"] = NotConnectedError("offline")
    with pytest.raises(HomeAssistantError) as error_info:
        await by_key["eco_mode"].async_turn_on()
    assert_command_error(
        error_info,
        key="command_disconnected",
        cause=NotConnectedError,
    )

    client.errors["set_dc"] = StateUnavailableError(
        "Output command confirmation timed out; wait for a fresh status update"
    )
    with pytest.raises(HomeAssistantError) as error_info:
        await by_key["dc_output"].async_turn_on()
    assert_command_error(
        error_info,
        key="command_unconfirmed",
        cause=StateUnavailableError,
    )

    client.errors["set_light"] = TimeoutError("write timeout")
    with pytest.raises(HomeAssistantError) as error_info:
        await by_key["light"].async_turn_on()
    assert_command_error(error_info, key="command_timeout", cause=TimeoutError)

    client.errors["set_car_charger"] = RuntimeError("transport exploded")
    with pytest.raises(HomeAssistantError) as error_info:
        await by_key["car_charger"].async_turn_off()
    assert_command_error(error_info, key="command_transport", cause=RuntimeError)


@pytest.mark.asyncio
async def test_selects_handle_known_unknown_and_invalid_values() -> None:
    entry, client, _, _ = configured_entry()
    entities: list[Any] = []
    await select.async_setup_entry(None, entry, lambda values: entities.extend(values))
    work_mode, eco_timeout = entities
    assert work_mode.current_option == "standard"
    assert eco_timeout.current_option == "four_hours"

    await work_mode.async_select_option("fast")
    await eco_timeout.async_select_option("six_hours")
    assert ("set_work_mode", WorkMode.FAST) in client.calls
    assert ("set_eco_timeout", 6) in client.calls

    with pytest.raises(HomeAssistantError) as error_info:
        await work_mode.async_select_option("turbo")
    assert_command_error(error_info, key="command_unsupported", cause=KeyError)

    with pytest.raises(HomeAssistantError) as error_info:
        await eco_timeout.async_select_option("forever")
    assert_command_error(error_info, key="command_unsupported", cause=KeyError)

    client.errors["set_work_mode"] = NotConnectedError("offline")
    with pytest.raises(HomeAssistantError) as error_info:
        await work_mode.async_select_option("mute")
    assert_command_error(
        error_info,
        key="command_disconnected",
        cause=NotConnectedError,
    )

    client.errors["set_eco_timeout"] = StateUnavailableError(
        "Settings writes are blocked by semantic validation: unsupported ECO timeout"
    )
    with pytest.raises(HomeAssistantError) as error_info:
        await eco_timeout.async_select_option("one_hour")
    assert_command_error(
        error_info,
        key="command_unsupported",
        cause=StateUnavailableError,
    )

    unknown = replace(
        client.snapshot(),
        settings=settings(work_mode=None, eco_timeout_hours=3),
    )
    client.set_snapshot(unknown)
    assert work_mode.current_option is None
    assert eco_timeout.current_option is None

    no_settings = replace(client.snapshot(), settings=None, settings_monotonic=None)
    client.set_snapshot(no_settings)
    assert work_mode.current_option is None
    assert eco_timeout.current_option is None


@pytest.mark.asyncio
async def test_experimental_profiles_expose_no_writable_entities() -> None:
    experimental_state = replace(
        snapshot(),
        advertised_name="AP S300",
    )
    entry, _, _, _ = configured_entry(state=experimental_state)

    switch_entities: list[Any] = []
    await switch.async_setup_entry(
        None, entry, lambda values: switch_entities.extend(values)
    )
    assert switch_entities == []

    select_entities: list[Any] = []
    await select.async_setup_entry(
        None, entry, lambda values: select_entities.extend(values)
    )
    assert select_entities == []

    button_entities: list[Any] = []
    await button.async_setup_entry(
        None, entry, lambda values: button_entities.extend(values)
    )
    assert len(button_entities) == 2
    assert {entity._attr_translation_key for entity in button_entities} == {
        "refresh",
        "reconnect",
    }


@pytest.mark.asyncio
async def test_buttons_send_commands_and_wrap_errors() -> None:
    entry, client, _, _ = configured_entry()
    entities: list[Any] = []
    await button.async_setup_entry(None, entry, lambda values: entities.extend(values))
    refresh, reconnect, keepalive = entities
    assert refresh.available

    await refresh.async_press()
    await reconnect.async_press()
    await keepalive.async_press()
    assert ("request_status", None) in client.calls
    assert ("reconnect", "Reconnect requested by user") in client.calls
    assert ("settings_keepalive", None) in client.calls

    client.errors["request_status"] = NotConnectedError("offline")
    with pytest.raises(HomeAssistantError) as error_info:
        await refresh.async_press()
    assert_command_error(
        error_info,
        key="command_disconnected",
        cause=NotConnectedError,
    )

    client.errors["settings_keepalive"] = StateUnavailableError("settings stale")
    with pytest.raises(HomeAssistantError) as error_info:
        await keepalive.async_press()
    assert_command_error(
        error_info,
        key="command_stale_state",
        cause=StateUnavailableError,
    )

    client.errors["reconnect"] = RuntimeError("adapter failure")
    with pytest.raises(HomeAssistantError) as error_info:
        await reconnect.async_press()
    assert_command_error(error_info, key="command_transport", cause=RuntimeError)

    client.set_snapshot(disconnected_snapshot())
    assert not refresh.available


@pytest.mark.asyncio
async def test_option_numbers_validate_and_persist_complete_options() -> None:
    entry, _, _, hass = configured_entry()
    entities: list[Any] = []
    await number.async_setup_entry(None, entry, lambda values: entities.extend(values))
    status_interval, keepalive_interval = entities
    assert status_interval.native_value == 20
    assert keepalive_interval.native_value == 9

    await status_interval.async_set_native_value(15)
    assert hass.config_entries.updates[-1][1]["status_interval"] == 15
    await keepalive_interval.async_set_native_value(8)
    assert hass.config_entries.updates[-1][1]["settings_keepalive_interval"] == 480

    with pytest.raises(HomeAssistantError, match="stale_timeout"):
        await status_interval.async_set_native_value(120)


def test_command_exception_translations_are_present() -> None:
    expected = {
        "command_disconnected",
        "command_timeout",
        "command_stale_state",
        "command_unsupported",
        "command_transport",
        "command_unconfirmed",
    }
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "custom_components/allpowers_ble/strings.json",
        "custom_components/allpowers_ble/translations/en.json",
        "custom_components/allpowers_ble/translations/es.json",
    ):
        payload = json.loads((root / relative).read_text(encoding="utf-8"))
        exceptions = payload.get("exceptions", {})
        assert expected.issubset(exceptions)
        assert all(exceptions[key].get("message") for key in expected)
