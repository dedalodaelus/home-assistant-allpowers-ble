"""Unit tests for Repairs manager behavior."""

from __future__ import annotations

import builtins
from dataclasses import replace
from types import SimpleNamespace

import pytest

from homeassistant.helpers import issue_registry as ir

from custom_components.allpowers_ble.const import DOMAIN
from custom_components.allpowers_ble import (
    _create_migration_issue,
    _delete_migration_issue,
)
from custom_components.allpowers_ble.repairs import (
    ISSUE_NO_ROUTE,
    ISSUE_WATCHDOG,
    AllpowersRepairsManager,
)

from tests.helpers import FakeHass, snapshot
from tests.ha_stubs import ConfigEntry


def _issue_id(entry_id: str, issue_key: str) -> str:
    return f"{entry_id}_{issue_key}"


def test_no_route_issue_requires_persistence_and_is_deduplicated() -> None:
    hass = FakeHass()
    entry_id = "entry-no-route"
    manager = AllpowersRepairsManager(
        hass,
        entry_id=entry_id,
        entry_title="ALLPOWERS R600",
        initial_snapshot=snapshot(),
    )

    no_route = replace(
        snapshot(),
        connected=False,
        last_error="DeviceNotFoundError: no route",
    )

    manager.evaluate(no_route, status_is_fresh=False)
    manager.evaluate(no_route, status_is_fresh=False)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_NO_ROUTE))
        is None
    )

    manager.evaluate(no_route, status_is_fresh=False)
    created = ir.async_get(hass).async_get_issue(
        DOMAIN, _issue_id(entry_id, ISSUE_NO_ROUTE)
    )
    assert created is not None

    manager.evaluate(no_route, status_is_fresh=False)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_NO_ROUTE))
        is created
    )


def test_no_route_issue_is_removed_after_recovery() -> None:
    hass = FakeHass()
    entry_id = "entry-recovery"
    manager = AllpowersRepairsManager(
        hass,
        entry_id=entry_id,
        entry_title="ALLPOWERS R600",
        initial_snapshot=snapshot(),
    )

    no_route = replace(
        snapshot(),
        connected=False,
        last_error="DeviceNotFoundError: no route",
    )
    for _ in range(3):
        manager.evaluate(no_route, status_is_fresh=False)

    recovered = replace(no_route, connected=True, last_error=None)
    manager.evaluate(recovered, status_is_fresh=True)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_NO_ROUTE))
        is None
    )


def test_watchdog_issue_requires_repeated_resets_and_clears_when_fresh() -> None:
    hass = FakeHass()
    entry_id = "entry-watchdog"
    baseline = snapshot()
    manager = AllpowersRepairsManager(
        hass,
        entry_id=entry_id,
        entry_title="ALLPOWERS R600",
        initial_snapshot=baseline,
    )

    unstable = replace(
        baseline,
        connected=False,
        statistics=replace(baseline.statistics, watchdog_resets=4),
    )
    manager.evaluate(unstable, status_is_fresh=False)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_WATCHDOG))
        is not None
    )

    recovered = replace(unstable, connected=True)
    manager.evaluate(recovered, status_is_fresh=True)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_WATCHDOG))
        is None
    )


def test_clear_removes_owned_issues() -> None:
    hass = FakeHass()
    entry_id = "entry-clear"
    baseline = snapshot()
    manager = AllpowersRepairsManager(
        hass,
        entry_id=entry_id,
        entry_title="ALLPOWERS R600",
        initial_snapshot=baseline,
    )

    no_route = replace(
        baseline,
        connected=False,
        last_error="DeviceNotFoundError: no route",
    )
    unstable = replace(
        no_route,
        statistics=replace(no_route.statistics, watchdog_resets=4),
    )
    for _ in range(3):
        manager.evaluate(unstable, status_is_fresh=False)

    manager.clear()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_NO_ROUTE)) is None
    assert registry.async_get_issue(DOMAIN, _issue_id(entry_id, ISSUE_WATCHDOG)) is None


def test_clear_is_noop_when_no_active_issues() -> None:
    hass = FakeHass()
    manager = AllpowersRepairsManager(
        hass,
        entry_id="entry-noop",
        entry_title="ALLPOWERS R600",
        initial_snapshot=snapshot(),
    )

    manager.clear()


def test_migration_issue_helpers_handle_missing_issue_registry_import() -> None:
    hass = FakeHass()
    entry = ConfigEntry(title="ALLPOWERS R600", data={}, options={})
    original_import = builtins.__import__

    def fake_import(
        name: str,
        globals: object = None,
        locals: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "homeassistant.helpers" and "issue_registry" in fromlist:
            raise ModuleNotFoundError("issue registry is unavailable")
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = fake_import
    try:
        _create_migration_issue(hass, entry)
        _delete_migration_issue(hass, entry)
    finally:
        builtins.__import__ = original_import


@pytest.mark.asyncio
async def test_coordinator_hooks_call_repairs_manager() -> None:
    from tests.helpers import configured_entry

    _, client, coordinator, _ = configured_entry()
    calls: list[tuple[str, bool]] = []

    manager = SimpleNamespace(
        evaluate=lambda state, *, status_is_fresh: calls.append(
            ("evaluate", status_is_fresh)
        ),
        clear=lambda: calls.append(("clear", False)),
    )

    coordinator.set_repairs_manager(manager)
    client.set_snapshot(snapshot())

    assert calls and calls[0][0] == "evaluate"

    await coordinator.async_shutdown()
    assert ("clear", False) in calls
