"""Home Assistant Repairs behavior for persistent actionable failures."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ADDRESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from custom_components.allpowers_ble.const import DOMAIN
from custom_components.allpowers_ble.options import ConnectionOptions

from tests.helpers import snapshot

pytestmark = pytest.mark.homeassistant


class RepairFakeClient:
    """Integration-facing fake BLE client for Repairs state transitions."""

    instances: list[RepairFakeClient] = []
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
        self.address = address
        self.options = options
        self._snapshot = replace(
            RepairFakeClient.default_state,
            advertised_name=advertised_name,
        )
        self.callback: Callable[[], None] | None = None
        RepairFakeClient.instances.append(self)

    def set_update_callback(self, callback: Callable[[], None] | None) -> None:
        self.callback = callback

    def snapshot(self):
        return self._snapshot

    def set_snapshot(self, value) -> None:
        self._snapshot = value
        if self.callback is not None:
            self.callback()

    def update_advertisement(self, service_info: Any) -> None:
        del service_info

    async def async_start(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None

    async def async_wait_ready(self, timeout: float) -> None:
        del timeout

    async def async_apply_options(self, options: ConnectionOptions) -> None:
        self.options = options


@pytest.fixture(autouse=True)
def reset_repair_client() -> None:
    RepairFakeClient.instances.clear()
    RepairFakeClient.default_state = snapshot()


@pytest.fixture
def bluetooth_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch Bluetooth APIs used by setup_entry."""

    from homeassistant.components import bluetooth

    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable: object(),
    )
    monkeypatch.setattr(
        bluetooth,
        "async_register_callback",
        lambda *args, **kwargs: lambda: None,
    )


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


@pytest.mark.asyncio
async def test_persistent_no_route_repair_deduplicates_and_dismisses(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks,
) -> None:
    """No-route Repair appears only after persistence and dismisses on recovery."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", RepairFakeClient)

    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = RepairFakeClient.instances[-1]
    issue_id = f"{entry.entry_id}_persistent_no_route"
    registry = ir.async_get(hass)

    disconnected = replace(
        client.snapshot(),
        connected=False,
        last_error=(
            "DeviceNotFoundError: No connectable Bluetooth adapter or proxy can "
            "currently reach AA:BB:CC:DD:EE:FF"
        ),
    )
    client.set_snapshot(disconnected)
    client.set_snapshot(disconnected)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    client.set_snapshot(disconnected)
    await hass.async_block_till_done()
    first_issue = registry.async_get_issue(DOMAIN, issue_id)
    assert first_issue is not None

    client.set_snapshot(disconnected)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is first_issue

    recovered = replace(disconnected, connected=True, last_error=None)
    client.set_snapshot(recovered)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_watchdog_repair_persists_then_dismisses_when_fresh(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks,
) -> None:
    """Repeated watchdog resets raise a Repair that clears once status is fresh."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", RepairFakeClient)

    entry = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 AABB")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = RepairFakeClient.instances[-1]
    issue_id = f"{entry.entry_id}_repeated_watchdog_resets"
    registry = ir.async_get(hass)

    unstable = replace(
        client.snapshot(),
        connected=False,
        statistics=replace(client.snapshot().statistics, watchdog_resets=4),
    )
    client.set_snapshot(unstable)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    stable = replace(unstable, connected=True)
    client.set_snapshot(stable)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_repairs_cleanup_on_reload_unload_and_multiple_entries(
    hass,
    enable_custom_integrations,
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_callbacks,
) -> None:
    """Repairs are entry-scoped, survive dedupe, and are cleaned on reload/unload."""
    del enable_custom_integrations
    del bluetooth_callbacks
    import custom_components.allpowers_ble.client as client_module

    monkeypatch.setattr(client_module, "AllpowersBLEClient", RepairFakeClient)

    entry_a = _make_entry("AA:BB:CC:DD:EE:FF", title="ALLPOWERS R600 AABB")
    entry_b = _make_entry("11:22:33:44:55:66", title="ALLPOWERS S300 5566")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    if entry_b.state is ConfigEntryState.NOT_LOADED:
        assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    by_address = {
        client.address.upper(): client for client in RepairFakeClient.instances
    }
    issue_a = f"{entry_a.entry_id}_persistent_no_route"
    issue_b = f"{entry_b.entry_id}_persistent_no_route"
    registry = ir.async_get(hass)

    disconnected_a = replace(
        by_address["AA:BB:CC:DD:EE:FF"].snapshot(),
        connected=False,
        last_error="DeviceNotFoundError: no route",
    )
    disconnected_b = replace(
        by_address["11:22:33:44:55:66"].snapshot(),
        connected=False,
        last_error="DeviceNotFoundError: no route",
    )

    for _ in range(3):
        by_address["AA:BB:CC:DD:EE:FF"].set_snapshot(disconnected_a)
        by_address["11:22:33:44:55:66"].set_snapshot(disconnected_b)
    await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, issue_a) is not None
    assert registry.async_get_issue(DOMAIN, issue_b) is not None

    assert await hass.config_entries.async_reload(entry_a.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_a) is None
    assert registry.async_get_issue(DOMAIN, issue_b) is not None

    assert await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_b) is None
