"""Coordinator and Home Assistant entity adapter tests."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic
from typing import Any

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.allpowers_ble import (
    binary_sensor,
    button,
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
)


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
    assert ordinary.extra_state_attributes is None
    assert diagnostic.extra_state_attributes == {"last_error": "synthetic error"}

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
    with pytest.raises(HomeAssistantError, match="fresh state"):
        await by_key["ac_output"].async_turn_on()

    client.errors["set_eco"] = NotConnectedError("offline")
    with pytest.raises(HomeAssistantError, match="offline"):
        await by_key["eco_mode"].async_turn_on()


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

    with pytest.raises(HomeAssistantError, match="Unsupported work mode"):
        await work_mode.async_select_option("turbo")
    with pytest.raises(HomeAssistantError, match="Unsupported ECO timeout"):
        await eco_timeout.async_select_option("forever")

    client.errors["set_work_mode"] = NotConnectedError("offline")
    with pytest.raises(HomeAssistantError, match="offline"):
        await work_mode.async_select_option("mute")

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
    with pytest.raises(HomeAssistantError, match="offline"):
        await refresh.async_press()
    client.errors["settings_keepalive"] = StateUnavailableError("settings stale")
    with pytest.raises(HomeAssistantError, match="settings stale"):
        await keepalive.async_press()

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
