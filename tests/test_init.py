"""Integration setup, option update, shutdown, and unload tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

import custom_components.allpowers_ble as integration
from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble import coordinator as coordinator_module
from custom_components.allpowers_ble.const import INITIAL_DATA_TIMEOUT
from custom_components.allpowers_ble.options import ConnectionOptions

from tests.helpers import ADDRESS, FakeHass
from tests.helpers import snapshot as build_snapshot


class SetupClient:
    """Client fake instantiated by ``async_setup_entry``."""

    instances: list[SetupClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.advertisements: list[Any] = []
        self._snapshot = build_snapshot()
        SetupClient.instances.append(self)

    def update_advertisement(self, service_info: Any) -> None:
        self.advertisements.append(service_info)

    def snapshot(self):
        return self._snapshot


class SetupCoordinator:
    """Coordinator fake controlling readiness and shutdown."""

    instances: list[SetupCoordinator] = []
    ready_error: Exception | None = None

    def __init__(self, hass: Any, entry: Any, client: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.started = False
        self.shutdown_calls = 0
        self.applied: list[ConnectionOptions] = []
        self.repairs = None
        SetupCoordinator.instances.append(self)

    async def async_start(self) -> None:
        self.started = True

    async def async_wait_ready(self, timeout: float) -> None:
        self.timeout = timeout
        if self.ready_error is not None:
            raise self.ready_error

    async def async_shutdown(self) -> None:
        self.shutdown_calls += 1

    async def async_apply_options(self, options: ConnectionOptions) -> None:
        self.applied.append(options)

    def set_repairs_manager(self, manager: Any) -> None:
        self.repairs = manager


@pytest.fixture(autouse=True)
def reset_setup_fakes() -> None:
    SetupClient.instances.clear()
    SetupCoordinator.instances.clear()
    SetupCoordinator.ready_error = None


def make_entry() -> ConfigEntry[Any]:
    return ConfigEntry(
        title="ALLPOWERS R600 AABB",
        data={"address": ADDRESS, "device_name": "ALLPOWERS R600"},
        options=ConnectionOptions().as_dict(),
    )


def make_legacy_entry(*, options: dict[str, Any]) -> ConfigEntry[Any]:
    return ConfigEntry(
        title="ALLPOWERS R600 AABB",
        data={"address": ADDRESS, "device_name": "ALLPOWERS R600"},
        options=options,
        version=1,
        minor_version=0,
        unique_id=ADDRESS,
    )


@pytest.mark.asyncio
async def test_setup_entry_success_registers_callbacks_and_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = FakeHass()
    entry = make_entry()
    registered: dict[str, Any] = {}

    monkeypatch.setattr(
        client_module,
        "AllpowersBLEClient",
        SetupClient,
    )
    monkeypatch.setattr(
        coordinator_module,
        "AllpowersCoordinator",
        SetupCoordinator,
    )
    from homeassistant.components import bluetooth

    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable: object(),
    )

    def register_callback(hass: Any, callback: Any, matcher: Any, mode: Any) -> Any:
        registered.update(
            {"callback": callback, "matcher": matcher, "mode": mode, "hass": hass}
        )
        return lambda: None

    monkeypatch.setattr(bluetooth, "async_register_callback", register_callback)

    assert await integration.async_setup_entry(hass, entry)
    client = SetupClient.instances[0]
    coordinator = SetupCoordinator.instances[0]
    assert client.kwargs["address"] == ADDRESS
    assert client.kwargs["advertised_name"] == "ALLPOWERS R600"
    assert coordinator.started
    assert coordinator.timeout == INITIAL_DATA_TIMEOUT
    assert entry.runtime_data.client is client
    assert entry.runtime_data.coordinator is coordinator
    assert hass.config_entries.forwarded == [(entry, integration.PLATFORMS)]

    advertisement = SimpleNamespace(name="R600 updated", rssi=-50)
    registered["callback"](advertisement, object())
    assert client.advertisements == [advertisement]

    assert hass.bus.listeners
    _, stop_callback = hass.bus.listeners[0]
    await stop_callback(None)
    assert coordinator.shutdown_calls == 1


@pytest.mark.asyncio
async def test_setup_entry_defers_when_no_connectable_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = FakeHass()
    entry = make_entry()
    from homeassistant.components import bluetooth

    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable: None,
    )

    with pytest.raises(ConfigEntryNotReady, match="No connectable Bluetooth path"):
        await integration.async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_setup_entry_defers_and_cleans_up_on_initial_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = FakeHass()
    entry = make_entry()
    SetupCoordinator.ready_error = TimeoutError()

    monkeypatch.setattr(client_module, "AllpowersBLEClient", SetupClient)
    monkeypatch.setattr(coordinator_module, "AllpowersCoordinator", SetupCoordinator)
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

    with pytest.raises(ConfigEntryNotReady, match="no valid status frame"):
        await integration.async_setup_entry(hass, entry)
    assert SetupCoordinator.instances[0].shutdown_calls == 1
    assert not hass.config_entries.forwarded


@pytest.mark.asyncio
async def test_update_listener_applies_normalized_options() -> None:
    entry = make_entry()
    coordinator = SetupCoordinator(None, entry, None)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    entry.options = ConnectionOptions(status_interval=15, stale_timeout=31).as_dict()

    await integration._async_update_listener(FakeHass(), entry)

    assert coordinator.applied[-1].status_interval == 15


@pytest.mark.asyncio
async def test_unload_entry_shuts_down_only_after_platform_unload() -> None:
    hass = FakeHass()
    entry = make_entry()
    coordinator = SetupCoordinator(hass, entry, None)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    assert await integration.async_unload_entry(hass, entry)
    assert coordinator.shutdown_calls == 1

    hass.config_entries.unload_result = False
    assert not await integration.async_unload_entry(hass, entry)
    assert coordinator.shutdown_calls == 1


@pytest.mark.asyncio
async def test_migrate_entry_current_version_is_noop() -> None:
    hass = FakeHass()
    entry = make_entry()

    assert await integration.async_migrate_entry(hass, entry)
    assert not hass.config_entries.updates


@pytest.mark.asyncio
async def test_migrate_entry_from_1_0_normalizes_options_and_sets_latest_version() -> (
    None
):
    hass = FakeHass()
    legacy_options = ConnectionOptions().as_dict()
    legacy_options.pop("enable_car_charger")
    entry = make_legacy_entry(options=legacy_options)
    unique_id = entry.unique_id

    assert await integration.async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert entry.minor_version == 1
    assert entry.unique_id == unique_id
    assert entry.options["enable_car_charger"] is False
    assert len(hass.config_entries.updates) == 1


@pytest.mark.asyncio
async def test_migrate_entry_is_idempotent_after_first_successful_migration() -> None:
    hass = FakeHass()
    legacy_options = ConnectionOptions().as_dict()
    legacy_options.pop("enable_car_charger")
    entry = make_legacy_entry(options=legacy_options)

    assert await integration.async_migrate_entry(hass, entry)
    assert await integration.async_migrate_entry(hass, entry)
    assert len(hass.config_entries.updates) == 1


@pytest.mark.asyncio
async def test_migrate_entry_rejects_invalid_legacy_options() -> None:
    hass = FakeHass()
    entry = make_legacy_entry(options={"settings_keepalive": "invalid"})
    issue_id = f"{entry.entry_id}_invalid_migrated_options"

    assert not await integration.async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert entry.minor_version == 0
    assert not hass.config_entries.updates
    assert ir.async_get(hass).async_get_issue(integration.DOMAIN, issue_id) is not None


@pytest.mark.asyncio
async def test_migrate_entry_success_clears_invalid_options_repair() -> None:
    hass = FakeHass()
    invalid_entry = make_legacy_entry(options={"settings_keepalive": "invalid"})
    valid_entry = make_legacy_entry(options=ConnectionOptions().as_dict())
    issue_id = f"{invalid_entry.entry_id}_invalid_migrated_options"

    assert not await integration.async_migrate_entry(hass, invalid_entry)
    assert ir.async_get(hass).async_get_issue(integration.DOMAIN, issue_id) is not None

    valid_entry.entry_id = invalid_entry.entry_id
    valid_entry.title = invalid_entry.title
    assert await integration.async_migrate_entry(hass, valid_entry)
    assert ir.async_get(hass).async_get_issue(integration.DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_migrate_entry_rejects_future_minor_version() -> None:
    hass = FakeHass()
    entry = ConfigEntry(
        title="ALLPOWERS R600 AABB",
        data={"address": ADDRESS, "device_name": "ALLPOWERS R600"},
        options=ConnectionOptions().as_dict(),
        version=1,
        minor_version=99,
    )

    assert not await integration.async_migrate_entry(hass, entry)
    assert not hass.config_entries.updates


@pytest.mark.asyncio
async def test_migrate_entry_from_1_0_without_optional_data_keys() -> None:
    hass = FakeHass()
    entry = ConfigEntry(
        title="ALLPOWERS R600 AABB",
        data={},
        options=ConnectionOptions().as_dict(),
        version=1,
        minor_version=0,
        unique_id=ADDRESS,
    )

    assert await integration.async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert entry.minor_version == 1
    assert entry.data == {}
    assert len(hass.config_entries.updates) == 1


@pytest.mark.asyncio
async def test_migrate_entry_rejects_unsupported_legacy_version() -> None:
    hass = FakeHass()
    entry = ConfigEntry(
        title="ALLPOWERS R600 AABB",
        data={"address": ADDRESS, "device_name": "ALLPOWERS R600"},
        options=ConnectionOptions().as_dict(),
        version=0,
        minor_version=0,
    )

    assert not await integration.async_migrate_entry(hass, entry)
    assert not hass.config_entries.updates
