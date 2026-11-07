"""Unit coverage for the authenticated HIL capability endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState

from custom_components.allpowers_ble.hil_capabilities import ws_capabilities


class _Connection:
    def __init__(self, *, is_admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=is_admin)
        self.errors: list[tuple[Any, ...]] = []
        self.results: list[tuple[Any, ...]] = []

    def send_error(self, *args: Any) -> None:
        self.errors.append(args)

    def send_result(self, *args: Any) -> None:
        self.results.append(args)


class _Entries:
    def __init__(self, *entries: Any) -> None:
        self.entries = {entry.entry_id: entry for entry in entries}

    def async_get_entry(self, entry_id: str) -> Any:
        return self.entries.get(entry_id)


def _entry(
    *,
    domain: str = "allpowers_ble",
    state: ConfigEntryState = ConfigEntryState.LOADED,
    runtime_data: Any = None,
) -> Any:
    return SimpleNamespace(
        entry_id="private-entry",
        domain=domain,
        state=state,
        runtime_data=runtime_data,
    )


@pytest.mark.asyncio
async def test_capability_endpoint_denies_non_admin_unknown_and_unready_entries() -> None:
    loaded = _entry()
    wrong_domain = _entry(domain="other")
    unloaded = _entry(state=ConfigEntryState.NOT_LOADED, runtime_data=object())
    hass = SimpleNamespace(
        config_entries=_Entries(loaded, wrong_domain, unloaded)
    )
    msg = {"id": 9, "entry_id": "private-entry"}

    cases = (
        (_Connection(is_admin=False), msg, "unauthorized"),
        (_Connection(), {"id": 9, "entry_id": "missing"}, "not_found"),
        (_Connection(), {"id": 9, "entry_id": "private-entry"}, "not_ready"),
        (_Connection(), {"id": 9, "entry_id": "other-entry"}, "not_found"),
    )
    hass.config_entries.entries["other-entry"] = wrong_domain
    for connection, request, expected_code in cases:
        await ws_capabilities(hass, connection, request)
        assert connection.errors[0][1:] == (
            expected_code,
            "HIL capability request rejected",
        )
        assert connection.results == []


@pytest.mark.asyncio
async def test_capability_endpoint_returns_only_bounded_fresh_runtime_facts() -> None:
    settings = SimpleNamespace(
        hardware_version="0.3",
        raw_hardware_version=3,
        firmware_version="2.1",
        raw_firmware_version=21,
    )
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            model_identity="R600",
            identity_conflicted=False,
            session_hardware_version=None,
            session_raw_hardware_version=None,
            settings=settings,
        ),
        settings_are_fresh=True,
        status_is_fresh=True,
        connected=True,
    )
    client = SimpleNamespace(
        options=SimpleNamespace(enable_car_charger=True),
        active_session_generation=7,
        has_pending_transaction=True,
    )
    entry = _entry(runtime_data=SimpleNamespace(coordinator=coordinator, client=client))
    hass = SimpleNamespace(config_entries=_Entries(entry))
    connection = _Connection()

    await ws_capabilities(
        hass, connection, {"id": 12, "entry_id": "private-entry"}
    )

    assert connection.errors == []
    assert connection.results[0][0] == 12
    result = connection.results[0][1]
    assert result["hardware_revision"] == "0.3"
    assert result["raw_hardware_revision"] == 3
    assert result["firmware_revision"] == "2.1"
    assert result["raw_firmware_revision"] == 21
    assert result["session_generation"] == 7
    assert result["pending_transaction"] is True
    assert result["capabilities"]["car_charger_enabled"] is True
    assert entry.entry_id not in str(result)


@pytest.mark.asyncio
async def test_capability_endpoint_filters_invalid_or_stale_revisions() -> None:
    settings = SimpleNamespace(
        hardware_version="private-hardware-string",
        raw_hardware_version=999,
        firmware_version="3.4.5.6.7",
        raw_firmware_version=-1,
    )
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            model_identity="Unknown model",
            identity_conflicted=True,
            session_hardware_version="1." + "2" * 500,
            session_raw_hardware_version=True,
            settings=settings,
        ),
        settings_are_fresh=False,
        status_is_fresh=False,
        connected=False,
    )
    client = SimpleNamespace(
        options=SimpleNamespace(enable_car_charger=False),
        active_session_generation=True,
        has_pending_transaction=False,
    )
    entry = _entry(runtime_data=SimpleNamespace(coordinator=coordinator, client=client))
    hass = SimpleNamespace(config_entries=_Entries(entry))
    connection = _Connection()

    await ws_capabilities(
        hass, connection, {"id": 13, "entry_id": "private-entry"}
    )

    result = connection.results[0][1]
    assert result["hardware_revision"] is None
    assert result["raw_hardware_revision"] is None
    assert result["firmware_revision"] is None
    assert result["raw_firmware_revision"] is None
    assert result["session_generation"] is None
    assert result["settings_fresh"] is False

