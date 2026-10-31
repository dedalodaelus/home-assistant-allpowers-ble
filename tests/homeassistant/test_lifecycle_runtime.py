"""Real Home Assistant lifecycle tests using a stateful fake BLE transport."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from typing import Any
import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ADDRESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.allpowers_ble import diagnostics
from custom_components.allpowers_ble.const import (
    CONF_DEVICE_IDENTITY,
    CONF_DEVICE_IDENTITY_CONFLICT,
    DOMAIN,
)
from custom_components.allpowers_ble.options import ConnectionOptions

from tests.helpers import settings, snapshot, status

pytestmark = pytest.mark.homeassistant


class StatefulFakeClient:
    """Integration-facing fake BLE transport used for real HA lifecycle tests."""

    instances: list[StatefulFakeClient] = []
    default_state = snapshot()

    def __init__(
        self,
        *,
        hass: Any,
        address: str,
        advertised_name: str,
        device_identity: str,
        options: ConnectionOptions,
        identity_conflicted: bool = False,
        model_identity_callback: Callable[[str], None] | None = None,
        identity_conflict_callback: Callable[[bool], None] | None = None,
    ) -> None:
        del hass
        self.address = address.upper()
        self.options = options
        self.identity_conflicted = identity_conflicted
        self.model_identity_callback = model_identity_callback
        self.identity_conflict_callback = identity_conflict_callback
        self.callback: Callable[[], None] | None = None
        self.advertisements: list[Any] = []
        self.calls: list[tuple[str, Any]] = []
        self.errors: dict[str, Exception] = {}
        self.started = False
        self.stopped = False
        self._snapshot = replace(
            StatefulFakeClient.default_state,
            advertised_name=advertised_name,
            model_identity=device_identity,
        )
        StatefulFakeClient.instances.append(self)

    def set_update_callback(self, callback: Callable[[], None] | None) -> None:
        self.callback = callback

    def snapshot(self):
        return self._snapshot

    def set_snapshot(self, value) -> None:
        self._snapshot = value
        if self.callback is not None:
            self.callback()

    def update_advertisement(self, service_info: Any) -> None:
        self.advertisements.append(service_info)
        advertised_name = service_info.name or self._snapshot.advertised_name
        model_identity = self._snapshot.model_identity
        if service_info.name and "R600" in service_info.name.upper():
            if service_info.name != model_identity:
                model_identity = service_info.name
            if (
                model_identity == service_info.name
                and service_info.name != self._snapshot.model_identity
                and self.model_identity_callback is not None
            ):
                self.model_identity_callback(model_identity)
        self._snapshot = replace(
            self._snapshot,
            advertised_name=advertised_name,
            model_identity=model_identity,
            rssi=getattr(service_info, "rssi", self._snapshot.rssi),
        )
        if self.callback is not None:
            self.callback()

    async def async_start(self) -> None:
        self.calls.append(("start", None))
        self.started = True

    async def async_stop(self) -> None:
        self.calls.append(("stop", None))
        self.stopped = True

    async def async_wait_ready(self, timeout: float) -> None:
        self.calls.append(("wait_ready", timeout))

    async def async_apply_options(self, options: ConnectionOptions) -> None:
        self.calls.append(("apply_options", options))
        self.options = options

    async def async_set_ac(self, enabled: bool) -> None:
        self.calls.append(("set_ac", enabled))
        if error := self.errors.get("set_ac"):
            raise error

    async def async_set_dc(self, enabled: bool) -> None:
        self.calls.append(("set_dc", enabled))

    async def async_set_light(self, enabled: bool) -> None:
        self.calls.append(("set_light", enabled))

    async def async_set_eco(self, enabled: bool) -> None:
        self.calls.append(("set_eco", enabled))

    async def async_set_car_charger(self, enabled: bool) -> None:
        self.calls.append(("set_car_charger", enabled))


@pytest.fixture(autouse=True)
def reset_stateful_client() -> None:
    StatefulFakeClient.instances.clear()
    StatefulFakeClient.default_state = snapshot()


@pytest.fixture
def bluetooth_callbacks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Patch Bluetooth APIs used by setup_entry and capture passive callbacks."""

    from homeassistant.components import bluetooth

    callbacks: list[tuple[Any, Any]] = []

    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable: object(),
    )

    def register_callback(hass: Any, callback: Any, matcher: Any, mode: Any):
        del hass, mode
        callbacks.append((callback, matcher))

        def unsubscribe() -> None:
            callbacks.remove((callback, matcher))

        return unsubscribe

    monkeypatch.setattr(bluetooth, "async_register_callback", register_callback)
    return callbacks


def _make_entry(address: str, *, title: str) -> MockConfigEntry:
    device_name = re.sub(r"\s+[0-9A-Fa-f]{4}$", "", title)
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        version=1,
        minor_version=1,
        title=title,
        data={CONF_ADDRESS: address, "device_name": device_name},
        options=ConnectionOptions().as_dict(),
    )


def _keys_for_address(hass, address: str) -> set[str]:
    registry = er.async_get(hass)
    prefix = f"{address.upper()}_"
    return {
        entry.unique_id[len(prefix) :]
        for entry in registry.entities.values()
        if entry.unique_id.startswith(prefix)
    }


def _count_unique_id(hass, unique_id: str, *, domain: str) -> int:
    registry = er.async_get(hass)
    prefix = f"{domain}."
    return sum(
        1
        for entry in registry.entities.values()
        if entry.unique_id == unique_id and entry.entity_id.startswith(prefix)
    )


@pytest.mark.asyncio
async def test_setup_options_reload_unload_and_diagnostics(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Exercise setup lifecycle and verify no leaked client callbacks."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)

    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    first_client = StatefulFakeClient.instances[-1]
    assert first_client.started
    assert any(call[0] == "wait_ready" for call in first_client.calls)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert devices
    assert devices[0].hw_version == "0.3"
    assert devices[0].sw_version == "3.4"

    callback, _ = bluetooth_callbacks[0]
    callback(SimpleNamespace(name="ALLPOWERS R600 Updated", rssi=-48), object())
    assert first_client.advertisements

    hass.config_entries.async_update_entry(
        entry,
        options=ConnectionOptions(status_interval=15, stale_timeout=31).as_dict(),
    )
    await hass.async_block_till_done()
    assert any(call[0] == "apply_options" for call in first_client.calls)

    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert payload["entry"]["data"]["address"] == "**REDACTED**"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded_client = StatefulFakeClient.instances[-1]
    assert reloaded_client is not first_client
    assert first_client.stopped
    assert first_client.callback is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert reloaded_client.stopped
    assert reloaded_client.callback is None
    assert bluetooth_callbacks == []


@pytest.mark.asyncio
async def test_production_client_identity_survives_proxy_advertisements_and_reload(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
    notification_builder,
) -> None:
    """Use the production client for identity, entity, and reload behavior."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module
    import custom_components.allpowers_ble.coordinator as coordinator_module

    async def start_with_telemetry(client) -> None:
        client._connected = True
        client._settings = settings()
        client._status = status()
        client._settings_monotonic = monotonic()
        client._status_monotonic = monotonic()
        client._ready_event.set()
        client._emit_update()

    async def wait_ready(client, timeout: float) -> None:
        del client, timeout

    async def stop(client) -> None:
        client._connected = False

    monkeypatch.setattr(
        client_module.AllpowersBLEClient, "async_start", start_with_telemetry
    )
    monkeypatch.setattr(
        client_module.AllpowersBLEClient, "async_wait_ready", wait_ready
    )
    monkeypatch.setattr(client_module.AllpowersBLEClient, "async_stop", stop)

    address = "AA:BB:CC:DD:EE:FF"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        version=1,
        minor_version=2,
        title="Cabin Power",
        data={
            CONF_ADDRESS: address,
            "device_name": "AP R600 V2.0",
        },
        options=ConnectionOptions().as_dict(),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    first_client = entry.runtime_data.client
    first_callback, _ = bluetooth_callbacks[0]
    first_client._rssi = -50
    entry_updates: list[dict[str, Any]] = []
    original_update_entry = hass.config_entries.async_update_entry

    def track_update_entry(config_entry: Any, **kwargs: Any) -> None:
        if "device_identity" in kwargs.get("data", {}):
            entry_updates.append(dict(kwargs["data"]))
        original_update_entry(config_entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", track_update_entry)
    first_callback(SimpleNamespace(name=None, rssi=-50), object())
    assert first_client.snapshot().model_identity == ""
    assert not first_client._runtime_model_support().capabilities.write_output_controls
    first_callback(SimpleNamespace(name="AP R600 V2.0", rssi=-50), object())
    await hass.async_block_till_done()
    assert first_client.snapshot().advertised_name == "AP R600 V2.0"
    assert first_client.snapshot().model_identity == "R600"
    assert entry.data["device_identity"] == "R600"
    first_callback(SimpleNamespace(name="AP R600 V2.0", rssi=-50), object())
    assert len(entry_updates) == 1
    assert first_client._runtime_model_support().capabilities.write_output_controls

    first_callback(SimpleNamespace(name=address, rssi=-50), object())
    assert first_client.snapshot().advertised_name == address
    assert first_client.snapshot().model_identity == "R600"

    registry = er.async_get(hass)
    entity_ids = {
        key: registry.async_get_entity_id("switch", DOMAIN, f"{address}_{key}")
        for key in ("ac_output", "dc_output", "light")
    }
    settings_entity_ids = {
        "eco_mode": registry.async_get_entity_id(
            "switch", DOMAIN, f"{address}_eco_mode"
        ),
        "work_mode": registry.async_get_entity_id(
            "select", DOMAIN, f"{address}_work_mode"
        ),
    }
    assert all(entity_ids.values())
    assert all(settings_entity_ids.values())
    for entity_id in entity_ids.values():
        assert hass.states.get(entity_id).state != "unavailable"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    reloaded_client = entry.runtime_data.client
    reload_callback, _ = bluetooth_callbacks[0]
    reload_callback(SimpleNamespace(name=address, rssi=-49), object())
    assert reloaded_client.snapshot().model_identity == "R600"
    assert reloaded_client._runtime_model_support().capabilities.write_output_controls
    assert entry.data.get("device_identity_conflict", False) is False
    assert all(
        _count_unique_id(hass, f"{address}_{key}", domain="switch") == 1
        for key in ("ac_output", "dc_output", "light")
    )

    reload_callback(SimpleNamespace(name="AP S300", rssi=-49), object())
    await hass.async_block_till_done()
    assert reloaded_client.snapshot().model_identity == "R600"
    assert entry.data["device_identity"] == "R600"
    assert entry.data["device_identity_conflict"] is True
    assert reloaded_client.snapshot().identity_conflicted
    assert (
        not reloaded_client._runtime_model_support().capabilities.write_output_controls
    )
    conflict_diagnostics = await diagnostics.async_get_config_entry_diagnostics(
        hass, entry
    )
    assert conflict_diagnostics["model_support"]["classification"] == (
        "identity_conflict"
    )
    await hass.async_block_till_done()
    for entity_id in (*entity_ids.values(), *settings_entity_ids.values()):
        assert hass.states.get(entity_id).state == "unavailable"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    contradictory_client = entry.runtime_data.client
    contradictory_callback, _ = bluetooth_callbacks[0]
    contradictory_callback(SimpleNamespace(name=address, rssi=-48), object())
    assert contradictory_client.snapshot().model_identity == "R600"
    assert contradictory_client._identity_conflicted
    assert not contradictory_client._runtime_model_support().capabilities.write_output_controls

    contradictory_client._settings_monotonic = monotonic() - 1000
    contradictory_client._status_monotonic = monotonic() - 1000
    contradictory_client._emit_update()
    await hass.async_block_till_done()
    for entity_id in entity_ids.values():
        assert hass.states.get(entity_id).state == "unavailable"

    contradictory_client._settings = None
    contradictory_client._status = None
    contradictory_client._settings_monotonic = None
    contradictory_client._status_monotonic = None
    contradictory_client._emit_update()
    await hass.async_block_till_done()
    assert hass.states.get(entity_ids["ac_output"]).state == "unavailable"

    contradictory_client._settings = settings()
    contradictory_client._status = status()
    contradictory_client._settings_monotonic = monotonic()
    contradictory_client._status_monotonic = monotonic()
    contradictory_callback(SimpleNamespace(name="AP R600 V2.0", rssi=-48), object())
    await hass.async_block_till_done()
    assert not contradictory_client._identity_conflicted
    assert entry.data["device_identity_conflict"] is False
    assert (
        contradictory_client._runtime_model_support().capabilities.write_output_controls
    )
    await hass.async_block_till_done()
    for entity_id in (*entity_ids.values(), *settings_entity_ids.values()):
        assert hass.states.get(entity_id).state != "unavailable"

    clock = {"now": monotonic()}
    contradictory_client._loop_time = lambda: clock["now"]  # type: ignore[method-assign]
    monkeypatch.setattr(coordinator_module, "monotonic", lambda: clock["now"])
    settings_frame = notification_builder(0x03, bytes((0xA1, 2, 0, 0, 0x03, 0x11)))
    status_frame = notification_builder(0x01, bytes((0x10, 50, 0, 100, 0, 25, 0, 90)))
    contradictory_client._notification_handler(object(), bytearray(settings_frame))
    contradictory_client._notification_handler(object(), bytearray(status_frame))
    for _ in range(61):
        clock["now"] += 20
        contradictory_client._notification_handler(object(), bytearray(status_frame))
    await hass.async_block_till_done()

    assert clock["now"] - contradictory_client.snapshot().settings_monotonic > 20 * 60
    assert not contradictory_client._settings_are_fresh()
    assert (
        contradictory_client._runtime_model_support().capabilities.write_output_controls
    )
    assert contradictory_client._safe_output_snapshot() == (False, False, True)
    for entity_id in entity_ids.values():
        assert hass.states.get(entity_id).state != "unavailable"
    for entity_id in settings_entity_ids.values():
        assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
async def test_entry_title_and_hardware_revision_do_not_establish_identity(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """A title plus verified revision cannot authorize model-specific writes."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module

    async def start_with_telemetry(client) -> None:
        client._connected = True
        client._settings = settings()
        client._status = status()
        client._settings_monotonic = monotonic()
        client._status_monotonic = monotonic()

    async def wait_ready(client, timeout: float) -> None:
        del client, timeout

    async def stop(client) -> None:
        client._connected = False

    monkeypatch.setattr(
        client_module.AllpowersBLEClient, "async_start", start_with_telemetry
    )
    monkeypatch.setattr(
        client_module.AllpowersBLEClient, "async_wait_ready", wait_ready
    )
    monkeypatch.setattr(client_module.AllpowersBLEClient, "async_stop", stop)

    address = "AA:BB:CC:DD:EE:FF"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        version=1,
        minor_version=2,
        title="AP R600 V2.0",
        data={CONF_ADDRESS: address, "device_name": "AP R600 V2.0"},
        options=ConnectionOptions().as_dict(),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data.client
    assert not client.snapshot().model_identity
    assert not client._runtime_model_support().capabilities.write_output_controls
    output_keys = ("ac_output", "dc_output", "light")
    assert all(
        _count_unique_id(hass, f"{address}_{key}", domain="switch") == 0
        for key in output_keys
    )
    callback, _ = bluetooth_callbacks[0]
    client._settings = replace(
        settings(), hardware_version="0.4", raw_hardware_version=0x04
    )
    callback(SimpleNamespace(name="AP R600 V2.0", rssi=-50), object())
    await hass.async_block_till_done()
    assert not client._runtime_model_support().capabilities.write_output_controls
    assert all(
        _count_unique_id(hass, f"{address}_{key}", domain="switch") == 0
        for key in output_keys
    )

    client._settings = settings()
    client._emit_update()
    await hass.async_block_till_done()
    assert client._runtime_model_support().capabilities.write_output_controls
    registry = er.async_get(hass)
    for key in output_keys:
        assert _count_unique_id(hass, f"{address}_{key}", domain="switch") == 1
        entity_id = registry.async_get_entity_id("switch", DOMAIN, f"{address}_{key}")
        assert entity_id is not None
        assert hass.states.get(entity_id).state != "unavailable"


@pytest.mark.asyncio
async def test_first_rejected_identity_restarts_read_only_and_recovers(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """An initial S500/S700 label must not persist a reconnect-blocking identity."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module

    async def start_with_telemetry(client) -> None:
        client._connected = True
        client._settings = settings()
        client._status = status()
        client._settings_monotonic = monotonic()
        client._status_monotonic = monotonic()
        client._ready_event.set()
        client._emit_update()

    async def wait_ready(client, timeout: float) -> None:
        del client, timeout

    async def stop(client) -> None:
        client._connected = False

    monkeypatch.setattr(
        client_module.AllpowersBLEClient, "async_start", start_with_telemetry
    )
    monkeypatch.setattr(
        client_module.AllpowersBLEClient, "async_wait_ready", wait_ready
    )
    monkeypatch.setattr(client_module.AllpowersBLEClient, "async_stop", stop)

    address = "AA:BB:CC:DD:EE:FF"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        version=1,
        minor_version=2,
        title="Power station",
        data={CONF_ADDRESS: address, "device_name": "ALLPOWERS power station"},
        options=ConnectionOptions().as_dict(),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first_client = entry.runtime_data.client
    callback, _ = bluetooth_callbacks[0]
    callback(SimpleNamespace(name="AP S500", rssi=-50), object())
    await hass.async_block_till_done()

    assert entry.data.get(CONF_DEVICE_IDENTITY, "") == ""
    assert entry.data[CONF_DEVICE_IDENTITY_CONFLICT] is True
    assert first_client.snapshot().identity_conflicted
    assert not first_client._runtime_model_support().capabilities.write_output_controls

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restarted_client = entry.runtime_data.client
    callback, _ = bluetooth_callbacks[0]
    callback(SimpleNamespace(name=address, rssi=-49), object())
    assert restarted_client.snapshot().model_identity == ""
    assert restarted_client.snapshot().identity_conflicted
    assert (
        not restarted_client._runtime_model_support().capabilities.write_output_controls
    )

    callback(SimpleNamespace(name="AP R600 V2.0", rssi=-49), object())
    await hass.async_block_till_done()
    assert entry.data[CONF_DEVICE_IDENTITY] == "R600"
    assert entry.data[CONF_DEVICE_IDENTITY_CONFLICT] is False
    assert not restarted_client.snapshot().identity_conflicted

    registry = er.async_get(hass)
    for key in ("ac_output", "dc_output", "light"):
        entity_id = registry.async_get_entity_id("switch", DOMAIN, f"{address}_{key}")
        assert entity_id is not None
        assert hass.states.get(entity_id).state != "unavailable"
        assert _count_unique_id(hass, f"{address}_{key}", domain="switch") == 1


@pytest.mark.asyncio
async def test_multiple_entries_are_isolated(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Each config entry should retain an isolated client and callback route."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)

    entry_a = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 AABB")
    entry_b = _make_entry("11:22:33:44:55:66", title="ALLPOWERS S300 5566")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry_a.entry_id)
    if entry_b.state is ConfigEntryState.NOT_LOADED:
        assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    clients_by_address = {
        client.address: client for client in StatefulFakeClient.instances
    }
    client_a = clients_by_address["AA:BB:CC:DD:EE:FF"]
    client_b = clients_by_address["11:22:33:44:55:66"]

    for callback, matcher in list(bluetooth_callbacks):
        address = str(
            getattr(matcher, "get", lambda _key, default=None: default)("address", "")
        ).upper()
        service_info = SimpleNamespace(name=f"notify-{address}", rssi=-60)
        callback(service_info, object())

    assert len(client_a.advertisements) == 1
    assert len(client_b.advertisements) == 1

    assert await hass.config_entries.async_unload(entry_a.entry_id)
    assert await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()
    assert client_a.stopped
    assert client_b.stopped


@pytest.mark.asyncio
async def test_command_errors_surface_via_real_service_call(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Writable entity commands should raise translated Home Assistant errors."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)

    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = StatefulFakeClient.instances[-1]
    client.errors["set_ac"] = TimeoutError("synthetic timeout")

    entity_id = er.async_get(hass).async_get_entity_id(
        "switch",
        DOMAIN,
        "AA:BB:CC:DD:EE:FF_ac_output",
    )
    assert entity_id is not None

    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": entity_id},
            blocking=True,
        )

    assert getattr(error.value, "translation_domain", None) == DOMAIN
    assert getattr(error.value, "translation_key", None) == "command_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["output", "settings", "keepalive"])
async def test_pending_transaction_blocks_real_service_until_notification(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
    notification_builder,
    group: str,
) -> None:
    """Use the production transaction client under HA with only BLE replaced."""
    del enable_custom_integrations, bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module
    from custom_components.allpowers_ble.protocol import WorkMode
    from tests.ha_stubs import FakeCharacteristic, FakeClient

    real_client_type = client_module.AllpowersBLEClient
    clients = []
    transports = []

    def create_client(**kwargs):
        client = real_client_type(**kwargs)
        transport = FakeClient()

        async def start() -> None:
            now = monotonic()
            client._client = transport
            client._write_characteristic = FakeCharacteristic()
            client._activate_session_generation()
            client._connected = True
            client._status = status(dc_enabled=False, raw_flags=0x10)
            client._status_monotonic = now
            client._settings = settings(
                eco_enabled=False, work_mode=WorkMode.MUTE, raw_flags=0xA0
            )
            client._settings_monotonic = now
            client._ready_event.set()
            client._emit_update()

        monkeypatch.setattr(client, "async_start", start)
        monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
        clients.append(client)
        transports.append(transport)
        return client

    monkeypatch.setattr(client_module, "AllpowersBLEClient", create_client)
    address = "AA:BB:CC:DD:EE:FF"
    entry = _make_entry(address, title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)
    if group == "keepalive":
        er.async_get(hass).async_get_or_create(
            "button", DOMAIN, f"{address}_settings_keepalive", config_entry=entry
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = clients[0]
    transport = transports[0]
    registry = er.async_get(hass)
    first_key = "ac_output" if group == "output" else "eco_mode"
    first_id = registry.async_get_entity_id("switch", DOMAIN, f"{address}_{first_key}")
    assert first_id is not None
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": first_id}, blocking=True
    )
    assert len(transport.writes) == 1
    assert client.snapshot().status.ac_enabled is False
    assert client.snapshot().settings.eco_enabled is False

    second_domain = "button" if group == "keepalive" else "switch"
    second_key = (
        "dc_output"
        if group == "output"
        else "settings_keepalive"
        if group == "keepalive"
        else "eco_mode"
    )
    second_service = (
        "press"
        if group == "keepalive"
        else "turn_off"
        if group == "settings"
        else "turn_on"
    )
    second_id = registry.async_get_entity_id(
        second_domain, DOMAIN, f"{address}_{second_key}"
    )
    assert second_id is not None
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            second_domain, second_service, {"entity_id": second_id}, blocking=True
        )
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == "command_unconfirmed"
    assert len(transport.writes) == 1

    notification = notification_builder(
        0x01 if group == "output" else 0x03,
        bytes((0x12, 73, 1, 44, 0, 150, 0, 120))
        if group == "output"
        else bytes((0xA1, 4, 0, 0, 0x03, 0x34)),
    )
    client._notification_handler(FakeCharacteristic(), bytearray(notification))
    await hass.async_block_till_done()
    await hass.services.async_call(
        second_domain, second_service, {"entity_id": second_id}, blocking=True
    )
    assert len(transport.writes) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert client._pending_output_transaction is None
    assert client._pending_settings_transaction is None


@pytest.mark.asyncio
async def test_status_before_settings_adds_controls_once_without_reload(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Control platforms should be added exactly once when verified settings arrive."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    StatefulFakeClient.default_state = replace(
        snapshot(),
        settings=None,
        settings_monotonic=None,
    )

    address = "AA:BB:CC:DD:EE:FF"
    entry = _make_entry(address, title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{address}_battery") is not None
    )
    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{address}_ac_output") is None
    )
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{address}_work_mode") is None
    )
    assert (
        registry.async_get_entity_id("button", DOMAIN, f"{address}_settings_keepalive")
        is None
    )

    client = StatefulFakeClient.instances[-1]
    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(),
            settings_monotonic=monotonic(),
        )
    )
    await hass.async_block_till_done()

    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{address}_ac_output")
        is not None
    )
    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{address}_eco_mode")
        is not None
    )
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{address}_work_mode")
        is not None
    )
    assert (
        registry.async_get_entity_id("button", DOMAIN, f"{address}_settings_keepalive")
        is not None
    )

    client.set_snapshot(replace(client.snapshot(), rssi=-48))
    client.set_snapshot(replace(client.snapshot(), connected=False))
    client.set_snapshot(
        replace(
            client.snapshot(),
            connected=True,
            settings=settings(firmware_version="3.5", raw_firmware_version=0x35),
            settings_monotonic=monotonic(),
        )
    )
    await hass.async_block_till_done()

    assert _count_unique_id(hass, f"{address}_ac_output", domain="switch") == 1
    assert _count_unique_id(hass, f"{address}_work_mode", domain="select") == 1
    assert (
        _count_unique_id(
            hass,
            f"{address}_settings_keepalive",
            domain="button",
        )
        == 1
    )


@pytest.mark.asyncio
async def test_settings_before_status_and_status_before_settings_converge_to_same_entities(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Entity lifecycle should converge regardless of settings timing."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)

    delayed_address = "AA:BB:CC:DD:EE:FF"
    delayed_entry = _make_entry(delayed_address, title="ALLPOWERS R600 AABB")
    delayed_entry.add_to_hass(hass)
    StatefulFakeClient.default_state = replace(
        snapshot(),
        settings=None,
        settings_monotonic=None,
    )

    assert await hass.config_entries.async_setup(delayed_entry.entry_id)
    await hass.async_block_till_done()

    delayed_client = StatefulFakeClient.instances[-1]
    delayed_client.set_snapshot(
        replace(
            delayed_client.snapshot(),
            settings=settings(),
            settings_monotonic=monotonic(),
        )
    )
    await hass.async_block_till_done()

    immediate_address = "11:22:33:44:55:66"
    immediate_entry = _make_entry(immediate_address, title="ALLPOWERS R600 5566")
    immediate_entry.add_to_hass(hass)
    StatefulFakeClient.default_state = snapshot()

    assert await hass.config_entries.async_setup(immediate_entry.entry_id)
    await hass.async_block_till_done()

    assert _keys_for_address(hass, delayed_address) == _keys_for_address(
        hass, immediate_address
    )


@pytest.mark.asyncio
async def test_settings_never_arrive_keeps_controls_uncreated(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Unverified sessions should keep writable entities absent."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    StatefulFakeClient.default_state = replace(
        snapshot(),
        settings=None,
        settings_monotonic=None,
    )

    address = "AA:BB:CC:DD:EE:FF"
    entry = _make_entry(address, title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = StatefulFakeClient.instances[-1]
    client.set_snapshot(replace(client.snapshot(), rssi=-49))
    client.set_snapshot(replace(client.snapshot(), rssi=-52))
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{address}_ac_output") is None
    )
    assert (
        registry.async_get_entity_id("select", DOMAIN, f"{address}_work_mode") is None
    )
    assert (
        registry.async_get_entity_id("button", DOMAIN, f"{address}_settings_keepalive")
        is None
    )


@pytest.mark.asyncio
async def test_capability_downgrade_marks_existing_controls_unavailable(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Created controls must remain registered but become unavailable after downgrade."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)

    address = "AA:BB:CC:DD:EE:FF"
    entry = _make_entry(address, title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    ac_entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{address}_ac_output"
    )
    mode_entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{address}_work_mode"
    )
    assert ac_entity_id is not None
    assert mode_entity_id is not None

    client = StatefulFakeClient.instances[-1]
    client.set_snapshot(
        replace(
            client.snapshot(),
            settings=settings(
                hardware_version="9.9",
                raw_hardware_version=0x99,
            ),
            settings_monotonic=monotonic(),
        )
    )
    await hass.async_block_till_done()

    assert hass.states.get(ac_entity_id) is not None
    assert hass.states.get(mode_entity_id) is not None
    assert hass.states.get(ac_entity_id).state == "unavailable"
    assert hass.states.get(mode_entity_id).state == "unavailable"
    assert _count_unique_id(hass, f"{address}_ac_output", domain="switch") == 1
    assert _count_unique_id(hass, f"{address}_work_mode", domain="select") == 1
