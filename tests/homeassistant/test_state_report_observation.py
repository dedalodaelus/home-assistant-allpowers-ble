"""Distinguish HA state-machine reports from cached REST representations."""

from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.setup import async_setup_component

pytestmark = pytest.mark.homeassistant


@pytest.mark.asyncio
async def test_identical_state_reports_can_leave_rest_timestamp_cached(
    hass, hass_client, freezer
) -> None:
    """Reproduce Core 2026.10.4's observation discrepancy without BLE code."""
    assert await async_setup_component(hass, "api", {})
    http = await hass_client()
    entity_id = "sensor.publication_probe"
    hass.states.async_set(entity_id, "78")
    response = await http.get(f"/api/states/{entity_id}")
    assert response.status == 200
    first_rest = await response.json()
    first_reported = hass.states.get(entity_id).last_reported

    freezer.tick(timedelta(seconds=90))
    hass.states.async_set(entity_id, "78")
    live = hass.states.get(entity_id)
    assert live.last_reported > first_reported
    response = await http.get(f"/api/states/{entity_id}")
    assert response.status == 200
    second_rest = await response.json()
    assert second_rest["last_reported"] == first_rest["last_reported"]
    assert second_rest["last_reported"] != live.last_reported.isoformat()

    response = await http.post(
        "/api/template",
        json={
            "template": "{{ states.sensor.publication_probe.last_reported.isoformat() }}"
        },
    )
    assert response.status == 200
    assert await response.text() == live.last_reported.isoformat()

    # A value change creates a new State and its REST representation catches up.
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(entity_id, "79")
    response = await http.get(f"/api/states/{entity_id}")
    changed_rest = await response.json()
    assert changed_rest["state"] == "79"
    assert (
        changed_rest["last_reported"]
        == hass.states.get(entity_id).last_reported.isoformat()
    )
