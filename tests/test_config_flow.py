"""Config-flow discovery, probing, and options tests."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry

from custom_components.allpowers_ble import config_flow
from custom_components.allpowers_ble.client import (
    DeviceNotFoundError,
    ProbeConnectionTimeoutError,
    ProbeGattValidationError,
    ProbeNotificationSetupError,
    ProbeStatusTimeoutError,
    UnsupportedDeviceError,
)
from custom_components.allpowers_ble.const import SERVICE_UUID
from custom_components.allpowers_ble.options import ConnectionOptions

from tests.ha_stubs import FakeBleakError
from tests.helpers import ADDRESS, FakeHass


def service_info(
    *,
    name: str = "ALLPOWERS R600",
    address: str = ADDRESS,
    connectable: bool = True,
    service_uuids: list[str] | None = None,
) -> BluetoothServiceInfoBleak:
    """Build a Bluetooth discovery record."""
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-55,
        service_uuids=service_uuids or [SERVICE_UUID],
        connectable=connectable,
    )


def new_flow() -> config_flow.AllpowersConfigFlow:
    flow = config_flow.AllpowersConfigFlow()
    flow.hass = FakeHass()
    flow.context = {}
    return flow


@pytest.mark.asyncio
async def test_bluetooth_discovery_aborts_nonconnectable_and_unsupported() -> None:
    flow = new_flow()
    result = await flow.async_step_bluetooth(service_info(connectable=False))
    assert result == {"type": "abort", "reason": "not_connectable"}

    flow = new_flow()
    result = await flow.async_step_bluetooth(service_info(name="AP S700"))
    assert result == {"type": "abort", "reason": "not_supported"}


@pytest.mark.asyncio
async def test_bluetooth_discovery_opens_confirm_form() -> None:
    flow = new_flow()

    result = await flow.async_step_bluetooth(service_info())

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["name"].endswith(f"({ADDRESS})")
    assert flow.unique_id == ADDRESS
    assert flow.confirm_only


@pytest.mark.asyncio
async def test_confirm_success_creates_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = new_flow()
    flow._discovery_info = service_info()

    async def probe(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(config_flow, "async_probe_device", probe)

    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert result["title"] == "ALLPOWERS R600 EEFF"
    assert result["data"]["address"] == ADDRESS
    assert result["options"] == ConnectionOptions().as_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DeviceNotFoundError("no route"), "cannot_connect"),
        (ProbeConnectionTimeoutError(), "connect_timeout"),
        (ProbeGattValidationError("missing gatt"), "gatt_unavailable"),
        (ProbeNotificationSetupError("notify failed"), "notify_failed"),
        (ProbeStatusTimeoutError(), "timeout"),
        (FakeBleakError("radio"), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_confirm_probe_errors_return_form(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
) -> None:
    flow = new_flow()
    flow._discovery_info = service_info()

    async def probe(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise error

    monkeypatch.setattr(config_flow, "async_probe_device", probe)

    result = await flow.async_step_confirm({})

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_confirm_unsupported_probe_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = new_flow()
    flow._discovery_info = service_info()

    async def probe(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise UnsupportedDeviceError("wrong protocol")

    monkeypatch.setattr(config_flow, "async_probe_device", probe)
    result = await flow.async_step_confirm({})
    assert result == {"type": "abort", "reason": "not_supported"}


@pytest.mark.asyncio
async def test_manual_step_discovers_candidates_and_creates_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = new_flow()
    candidate = service_info()
    scans = 0

    async def request_scan(hass: Any) -> None:
        nonlocal scans
        del hass
        scans += 1

    monkeypatch.setattr(
        config_flow.bluetooth, "async_request_active_scan", request_scan
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [candidate],
    )

    result = await flow.async_step_user()
    assert scans == 1
    assert result["type"] == "form"
    assert ADDRESS in flow._discovered_devices

    async def probe(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(config_flow, "async_probe_device", probe)
    result = await flow.async_step_user({"address": ADDRESS})
    assert result["type"] == "create_entry"


@pytest.mark.asyncio
async def test_manual_step_aborts_when_no_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = new_flow()

    async def request_scan(hass: Any) -> None:
        del hass

    monkeypatch.setattr(
        config_flow.bluetooth, "async_request_active_scan", request_scan
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: [],
    )

    result = await flow.async_step_user()
    assert result == {"type": "abort", "reason": "no_devices_found"}


def test_candidate_filtering_and_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = new_flow()
    candidates = [
        service_info(),
        service_info(name="AP S300", address="11:22:33:44:55:66"),
        service_info(name="AP S700", address="22:33:44:55:66:77"),
        service_info(
            name="Unrelated",
            address="33:44:55:66:77:88",
            service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"],
        ),
    ]
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_discovered_service_info",
        lambda hass, connectable: candidates,
    )
    flow._discover_candidates()
    assert set(flow._discovered_devices) == {ADDRESS, "11:22:33:44:55:66"}

    assert config_flow._matches_device(service_info(name="R600", service_uuids=[]))
    assert config_flow._matches_device(service_info(name="AP R600", service_uuids=[]))
    assert config_flow._matches_device(service_info(name="AP S300", service_uuids=[]))
    assert config_flow._matches_device(
        service_info(name="ALLPOWERS X", service_uuids=[])
    )
    assert config_flow._matches_device(service_info(name="Other"))
    assert not config_flow._matches_device(
        service_info(name="Other", service_uuids=["180f"])
    )
    unnamed = service_info(name=None)  # type: ignore[arg-type]
    assert config_flow._display_name(unnamed).startswith("ALLPOWERS")
    assert config_flow._entry_title(unnamed).endswith("EEFF")


@pytest.mark.asyncio
async def test_options_flow_schema_valid_and_invalid_values() -> None:
    entry = ConfigEntry(options=ConnectionOptions().as_dict())
    flow = config_flow.AllpowersOptionsFlow()
    flow.config_entry = entry

    result = await flow.async_step_init()
    assert result["type"] == "form"
    keys = {getattr(key, "key", key) for key in result["data_schema"].schema}
    assert keys == {
        "connection_health",
        "advanced_timing",
        "experimental_controls",
    }

    values = ConnectionOptions(status_interval=15, stale_timeout=31).as_dict()
    result = await flow.async_step_init(values)
    assert result["type"] == "create_entry"
    assert result["data"]["status_interval"] == 15

    invalid = dict(values)
    invalid["stale_timeout"] = 10
    result = await flow.async_step_init(invalid)
    assert result["type"] == "form"
    assert result["errors"] == {
        "stale_timeout": "stale_timeout_must_exceed_status_interval"
    }


@pytest.mark.asyncio
async def test_options_flow_sectioned_payload_is_flattened() -> None:
    entry = ConfigEntry(options=ConnectionOptions().as_dict())
    flow = config_flow.AllpowersOptionsFlow()
    flow.config_entry = entry

    values = ConnectionOptions(
        status_interval=15,
        stale_timeout=31,
        watchdog_timeout=45,
        reconnect_max_delay=90,
        settings_stale_timeout=900,
        settings_keepalive=True,
        settings_keepalive_interval=540,
        enable_car_charger=False,
    ).as_dict()
    result = await flow.async_step_init(
        {
            "connection_health": {
                "status_interval": values["status_interval"],
                "stale_timeout": values["stale_timeout"],
                "watchdog_timeout": values["watchdog_timeout"],
            },
            "advanced_timing": {
                "reconnect_max_delay": values["reconnect_max_delay"],
                "settings_stale_timeout": values["settings_stale_timeout"],
                "settings_keepalive": values["settings_keepalive"],
                "settings_keepalive_interval": values["settings_keepalive_interval"],
            },
            "experimental_controls": {
                "enable_car_charger": values["enable_car_charger"],
            },
        }
    )

    assert result["type"] == "create_entry"
    assert result["data"] == values


@pytest.mark.asyncio
async def test_options_flow_field_level_relationship_errors() -> None:
    entry = ConfigEntry(options=ConnectionOptions().as_dict())
    flow = config_flow.AllpowersOptionsFlow()
    flow.config_entry = entry

    base = ConnectionOptions().as_dict()

    stale_invalid = dict(base)
    stale_invalid["status_interval"] = 30
    stale_invalid["stale_timeout"] = 30
    result = await flow.async_step_init(stale_invalid)
    assert result["errors"] == {
        "stale_timeout": "stale_timeout_must_exceed_status_interval"
    }

    watchdog_invalid = dict(base)
    watchdog_invalid["stale_timeout"] = 45
    watchdog_invalid["watchdog_timeout"] = 45
    result = await flow.async_step_init(watchdog_invalid)
    assert result["errors"] == {
        "watchdog_timeout": "watchdog_timeout_must_exceed_stale_timeout"
    }

    keepalive_invalid = dict(base)
    keepalive_invalid["settings_keepalive"] = True
    keepalive_invalid["settings_stale_timeout"] = 300
    keepalive_invalid["settings_keepalive_interval"] = 300
    result = await flow.async_step_init(keepalive_invalid)
    assert result["errors"] == {
        "settings_stale_timeout": (
            "settings_stale_timeout_must_exceed_keepalive_interval"
        )
    }


@pytest.mark.asyncio
async def test_options_flow_field_level_type_and_missing_errors() -> None:
    entry = ConfigEntry(options=ConnectionOptions().as_dict())
    flow = config_flow.AllpowersOptionsFlow()
    flow.config_entry = entry

    values = ConnectionOptions().as_dict()

    missing_numeric = dict(values)
    missing_numeric.pop("reconnect_max_delay")
    result = await flow.async_step_init(missing_numeric)
    assert result["errors"]["reconnect_max_delay"] == "invalid_number"

    invalid_numeric = dict(values)
    invalid_numeric["status_interval"] = "not-a-number"
    result = await flow.async_step_init(invalid_numeric)
    assert result["errors"]["status_interval"] == "invalid_number"

    invalid_boolean = dict(values)
    invalid_boolean["settings_keepalive"] = 1
    result = await flow.async_step_init(invalid_boolean)
    assert result["errors"]["settings_keepalive"] == "invalid_boolean"


def test_flatten_options_input_ignores_non_mapping_section_values() -> None:
    values = ConnectionOptions().as_dict()
    payload = {
        "connection_health": "not-a-mapping",
        "advanced_timing": ["not", "a", "mapping"],
        "experimental_controls": None,
        **values,
    }

    flattened = config_flow._flatten_options_input(payload)
    assert flattened["status_interval"] == values["status_interval"]
    assert flattened["settings_keepalive"] == values["settings_keepalive"]


def test_get_options_flow() -> None:
    handler = config_flow.AllpowersConfigFlow.async_get_options_flow(ConfigEntry())
    assert isinstance(handler, config_flow.AllpowersOptionsFlow)
