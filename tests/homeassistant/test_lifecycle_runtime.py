"""Real Home Assistant lifecycle tests using a stateful fake BLE transport."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ADDRESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.allpowers_ble import diagnostics
from custom_components.allpowers_ble.const import DOMAIN
from custom_components.allpowers_ble.options import ConnectionOptions

from tests.helpers import settings, snapshot

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
        options: ConnectionOptions,
    ) -> None:
        del hass
        self.address = address.upper()
        self.options = options
        self.callback: Callable[[], None] | None = None
        self.advertisements: list[Any] = []
        self.calls: list[tuple[str, Any]] = []
        self.errors: dict[str, Exception] = {}
        self.started = False
        self.stopped = False
        self._snapshot = replace(
            StatefulFakeClient.default_state,
            advertised_name=advertised_name,
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
        self._snapshot = replace(
            self._snapshot,
            advertised_name=advertised_name,
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
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        version=1,
        minor_version=1,
        title=title,
        data={CONF_ADDRESS: address, "device_name": title.replace(" AABB", "")},
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

    devices = dr.async_get(hass).devices.get_devices_for_config_entry_id(entry.entry_id)
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
