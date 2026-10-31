"""Real Home Assistant lifecycle tests using a stateful fake BLE transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from time import monotonic
from types import SimpleNamespace
from typing import Any
import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ADDRESS
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.frame import ReportBehavior

from custom_components.allpowers_ble import diagnostics
from custom_components.allpowers_ble.config_flow import (
    AllpowersConfigFlow,
    SECTION_ADVANCED_TIMING,
    SECTION_CONNECTION_HEALTH,
    SECTION_EXPERIMENTAL_CONTROLS,
)
from custom_components.allpowers_ble.client import ProbeStatusTimeoutError
from custom_components.allpowers_ble.const import (
    CONF_DEVICE_IDENTITY,
    CONF_DEVICE_IDENTITY_CONFLICT,
    CONF_DEVICE_NAME,
    DOMAIN,
    SERVICE_UUID,
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


@pytest.fixture(autouse=True)
def strict_reload_listener_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforce Core's announced reload/listener error before 2026.12 is released."""
    monkeypatch.setattr(
        config_entries,
        "report_usage",
        partial(
            config_entries.report_usage,
            custom_integration_behavior=ReportBehavior.ERROR,
        ),
    )


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

    options_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert options_result["type"] == "form"
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {
            SECTION_CONNECTION_HEALTH: {"status_interval": 15, "stale_timeout": 31},
            SECTION_ADVANCED_TIMING: {},
            SECTION_EXPERIMENTAL_CONTROLS: {},
        },
    )
    assert options_result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert any(call[0] == "apply_options" for call in first_client.calls)
    assert first_client.options.status_interval == 15
    assert first_client.options.stale_timeout == 31
    assert StatefulFakeClient.instances == [first_client]
    assert not first_client.stopped
    assert len(bluetooth_callbacks) == 1
    assert len(entry.update_listeners) == 1

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
    assert entry.update_listeners == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected_reload"),
    [
        ("address", True),
        ("display_name", True),
        ("model_identity", True),
        ("unchanged", False),
        ("failed", False),
        ("cancelled", False),
    ],
)
async def test_reconfigure_manages_one_lifecycle_update(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
    change: str,
    expected_reload: bool,
) -> None:
    """Drive the real flow manager, listener, reload, platforms, and cleanup."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module
    import custom_components.allpowers_ble.config_flow as flow_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    address = "AA:BB:CC:DD:EE:FF"
    target_address = "11:22:33:44:55:66" if change in {"address", "failed"} else address
    entry = _make_entry(address, title="ALLPOWERS R600 EEFF")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first_client = entry.runtime_data.client
    initial_data = dict(entry.data)
    initial_options = dict(entry.options)
    initial_title = entry.title
    initial_unique_id = entry.unique_id
    initial_keys = _keys_for_address(hass, address)
    name = "AP S300" if change == "model_identity" else "ALLPOWERS R600"
    discovery = SimpleNamespace(
        address=target_address, name=name, service_uuids=[SERVICE_UUID]
    )

    async def request_scan(_hass) -> None:
        pass

    async def probe(_hass, **kwargs) -> None:
        del kwargs
        if change == "failed":
            raise ProbeStatusTimeoutError("No valid status")

    monkeypatch.setattr(
        flow_module.bluetooth, "async_request_active_scan", request_scan
    )
    monkeypatch.setattr(
        flow_module.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [discovery],
    )
    monkeypatch.setattr(flow_module, "async_probe_device", probe)
    scheduled: list[str] = []
    original_schedule = hass.config_entries.async_schedule_reload

    def schedule(entry_id: str) -> None:
        scheduled.append(entry_id)
        original_schedule(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", schedule)
    updates: list[dict[str, Any]] = []
    original_update = hass.config_entries.async_update_entry

    def update(entry, **kwargs):
        updates.append(kwargs)
        return original_update(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", update)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["type"] == "form"
    if change == "cancelled":
        hass.config_entries.flow.async_abort(result["flow_id"])
    else:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ADDRESS: target_address,
                CONF_DEVICE_NAME: "Patio"
                if change == "display_name"
                else "ALLPOWERS R600",
            },
        )
        if change == "failed":
            assert result["type"] == "form"
            assert result["errors"] == {"base": "timeout"}
            hass.config_entries.flow.async_abort(result["flow_id"])
        else:
            assert result["type"] == "abort"
            assert result["reason"] == "reconfigure_successful"
            assert len(updates) == 1
    await hass.async_block_till_done()
    assert scheduled == ([entry.entry_id] if expected_reload else [])
    assert len(StatefulFakeClient.instances) == (2 if expected_reload else 1)
    assert len(bluetooth_callbacks) == 1
    assert len(entry.update_listeners) == 1
    active_client = entry.runtime_data.client
    assert active_client.address == (target_address if expected_reload else address)
    assert _keys_for_address(hass, active_client.address) == initial_keys
    assert all(
        _count_unique_id(hass, f"{active_client.address}_{key}", domain="switch") == 1
        for key in ("ac_output", "dc_output", "light")
    )
    if expected_reload:
        assert first_client.stopped
        assert first_client.callback is None
        assert active_client.snapshot().advertised_name == entry.data[CONF_DEVICE_NAME]
        assert (
            active_client.snapshot().model_identity == entry.data[CONF_DEVICE_IDENTITY]
        )
        assert entry.runtime_data.coordinator.name == entry.title
        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, target_address), entry.entry_id
        )
        assert device is not None
        assert device.name == entry.title
        callback, matcher = bluetooth_callbacks[0]
        assert matcher["address"] == target_address
        callback(discovery, object())
        await hass.async_block_till_done()
        assert active_client.advertisements == [discovery]
        assert first_client.advertisements == []
        assert scheduled == [entry.entry_id]
    else:
        assert active_client is first_client
        assert not first_client.stopped
        assert entry.data == initial_data
        assert entry.title == initial_title
        assert entry.unique_id == initial_unique_id
        if change in {"failed", "cancelled"}:
            assert updates == []
    assert entry.options == initial_options
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert bluetooth_callbacks == []
    assert entry.update_listeners == []
    assert active_client.callback is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        ("retry", ConfigEntryState.SETUP_RETRY),
        ("error", ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_reconfigure_recovers_setup_without_update_listener(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
    failure: str,
    expected_state: ConfigEntryState,
) -> None:
    """A successful reconfigure must restart entries with no setup listener."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module
    import custom_components.allpowers_ble.config_flow as flow_module
    from homeassistant.components import bluetooth

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    lookup_failure = True

    def find_ble_device(hass, address: str, *, connectable: bool):
        del hass, address, connectable
        if lookup_failure:
            if failure == "retry":
                return None
            raise RuntimeError("synthetic setup error")
        return object()

    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", find_ble_device)
    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 EEFF")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is expected_state
    assert entry.update_listeners == []

    lookup_failure = False
    target_address = "11:22:33:44:55:66"
    discovery = SimpleNamespace(
        address=target_address,
        name="ALLPOWERS R600",
        service_uuids=[SERVICE_UUID],
    )

    async def request_scan(_hass) -> None:
        pass

    async def probe(_hass, **kwargs) -> None:
        del kwargs

    monkeypatch.setattr(
        flow_module.bluetooth, "async_request_active_scan", request_scan
    )
    monkeypatch.setattr(
        flow_module.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [discovery],
    )
    monkeypatch.setattr(flow_module, "async_probe_device", probe)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ADDRESS: target_address, CONF_DEVICE_NAME: "Recovered station"},
    )
    assert result["type"] == "abort"
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_ADDRESS] == target_address
    assert entry.runtime_data.client.address == target_address
    assert entry.update_listeners
    assert len(StatefulFakeClient.instances) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert bluetooth_callbacks == []


@pytest.mark.asyncio
async def test_setup_reconciles_options_changed_during_initialization(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Setup restarts its client with options persisted while it awaited startup."""
    del enable_custom_integrations, bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    first_wait_started = asyncio.Event()
    allow_first_wait = asyncio.Event()

    async def wait_ready(client, timeout: float) -> None:
        del timeout
        if client is StatefulFakeClient.instances[0]:
            first_wait_started.set()
            await allow_first_wait.wait()

    monkeypatch.setattr(StatefulFakeClient, "async_wait_ready", wait_ready)
    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 EEFF")
    entry.add_to_hass(hass)

    setup_task = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    await first_wait_started.wait()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            SECTION_CONNECTION_HEALTH: {"status_interval": 15, "stale_timeout": 31},
            SECTION_ADVANCED_TIMING: {},
            SECTION_EXPERIMENTAL_CONTROLS: {},
        },
    )
    assert result["type"] == "create_entry"
    allow_first_wait.set()

    assert await setup_task
    await hass.async_block_till_done()
    assert len(StatefulFakeClient.instances) == 2
    first_client, active_client = StatefulFakeClient.instances
    assert first_client.stopped
    assert active_client.options.status_interval == 15
    assert active_client.options.stale_timeout == 31
    assert entry.runtime_data.client is active_client
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_setup_reconciles_config_changed_during_pending_reload(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """A second reconfigure during reload setup wins before the new client is ready."""
    del enable_custom_integrations
    import custom_components.allpowers_ble.client as client_module
    import custom_components.allpowers_ble.config_flow as flow_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    first_address = "AA:BB:CC:DD:EE:FF"
    pending_address = "11:22:33:44:55:66"
    latest_address = "22:33:44:55:66:77"
    entry = _make_entry(first_address, title="ALLPOWERS R600 EEFF")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    replacement_started = asyncio.Event()
    allow_replacement = asyncio.Event()

    async def wait_ready(client, timeout: float) -> None:
        del timeout
        if client.address == pending_address:
            replacement_started.set()
            await allow_replacement.wait()

    monkeypatch.setattr(StatefulFakeClient, "async_wait_ready", wait_ready)

    async def request_scan(_hass) -> None:
        pass

    async def probe(_hass, **kwargs) -> None:
        del kwargs

    monkeypatch.setattr(
        flow_module.bluetooth, "async_request_active_scan", request_scan
    )
    monkeypatch.setattr(flow_module, "async_probe_device", probe)
    discovered_address = pending_address
    monkeypatch.setattr(
        flow_module.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [
            SimpleNamespace(
                address=discovered_address,
                name="ALLPOWERS R600",
                service_uuids=[SERVICE_UUID],
            )
        ],
    )

    async def reconfigure(address: str, name: str) -> None:
        nonlocal discovered_address
        discovered_address = address
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: address, CONF_DEVICE_NAME: name},
        )
        assert result["type"] == "abort"

    await reconfigure(pending_address, "Pending station")
    await replacement_started.wait()
    assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS

    await reconfigure(latest_address, "Latest station")
    allow_replacement.set()
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_ADDRESS] == latest_address
    assert entry.data[CONF_DEVICE_NAME] == "Latest station"
    assert entry.runtime_data.client.address == latest_address
    assert [client.address for client in StatefulFakeClient.instances] == [
        first_address,
        pending_address,
        latest_address,
    ]
    assert StatefulFakeClient.instances[0].stopped
    assert StatefulFakeClient.instances[1].stopped
    assert len(bluetooth_callbacks) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert bluetooth_callbacks == []


@pytest.mark.asyncio
async def test_strict_contract_rejects_old_reloading_helper(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
) -> None:
    """Negative control: the real Core report path rejects the previous helper."""
    del enable_custom_integrations, bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", StatefulFakeClient)
    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 EEFF")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flow = AllpowersConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    with pytest.raises(RuntimeError, match="has an update listener"):
        flow.async_update_reload_and_abort(entry)


@pytest.mark.parametrize("r600_name", ["AP R600 V2.0", "AP R600 V2.0 CE3A"])
@pytest.mark.asyncio
async def test_production_client_identity_survives_proxy_advertisements_and_reload(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks: list[tuple[Any, Any]],
    notification_builder,
    r600_name: str,
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
            "device_name": r600_name,
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
    first_callback(SimpleNamespace(name=r600_name, rssi=-50), object())
    await hass.async_block_till_done()
    assert entry.runtime_data.client is first_client
    assert first_client.snapshot().advertised_name == r600_name
    assert first_client.snapshot().model_identity == "R600"
    assert entry.data["device_identity"] == "R600"
    first_callback(SimpleNamespace(name=r600_name, rssi=-50), object())
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
    assert entry.runtime_data.client is reloaded_client
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
    contradictory_callback(SimpleNamespace(name=r600_name, rssi=-48), object())
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
