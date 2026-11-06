"""Exercise HIL endpoints through real Home Assistant WebSocket validation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.allpowers_ble.hil_capabilities import (
    async_register as async_register_capabilities,
)
from custom_components.allpowers_ble.hil_trace import (
    KEY,
    async_register,
    clear_entry_capture,
)

pytestmark = pytest.mark.homeassistant


class _CaptureClient:
    """Minimal identity-safe client protocol for WebSocket lifecycle tests."""

    def __init__(self, generation):
        self._active_session_generation = generation
        self.observer = None

    @property
    def hil_capture(self):
        return self.observer.capture if self.observer is not None else None

    def attach_observer(self, observer):
        self.observer = observer

    def detach_observer(self, observer):
        if self.observer is observer:
            self.observer = None

    def finalize_observer(self, observer):
        assert self.observer is observer


@pytest.mark.asyncio
async def test_hil_real_websocket_auth_schema_and_sealed_retrieval(
    hass: HomeAssistant,
    hass_ws_client,
    hass_admin_user,
) -> None:
    """A real authenticated WS connection validates commands and serializes bytes."""
    await async_register(hass)
    client = _CaptureClient(8)
    entry = MockConfigEntry(domain="allpowers_ble", data={})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(client=client)
    ws = await hass_ws_client()
    request = {
        "type": "allpowers_ble/hil/start",
        "entry_id": entry.entry_id,
        "run_id": "test12345",
        "scenario_id": "real-ws",
        "requested_route": "local_adapter",
    }

    await ws.send_json_auto_id({k: v for k, v in request.items() if k != "run_id"})
    invalid = await ws.receive_json()
    assert invalid["success"] is False
    assert invalid["error"]["code"] == "invalid_format"

    saved_groups = hass_admin_user.groups
    hass_admin_user.groups = []
    await ws.send_json_auto_id(request)
    denied = await ws.receive_json()
    assert denied["success"] is False
    assert client.hil_capture is None

    hass_admin_user.groups = saved_groups

    await ws.send_json_auto_id(request)
    success = await ws.receive_json()
    assert success["success"] is True
    capture_id = success["result"]["capture_id"]
    assert client.hil_capture is not None
    client.hil_capture.record("write_attempt", direction="tx", frame=b"private")

    await ws.send_json_auto_id(request)
    busy = await ws.receive_json()
    assert busy["success"] is False
    assert busy["error"]["code"] == "busy"

    lookup = {
        "type": "allpowers_ble/hil/fetch",
        "entry_id": entry.entry_id,
        "capture_id": capture_id,
    }
    await ws.send_json_auto_id({**lookup, "entry_id": "other"})
    wrong_entry = await ws.receive_json()
    assert wrong_entry["success"] is False
    assert wrong_entry["error"]["code"] == "not_found"

    # A second authenticated admin may not retrieve another user's private bytes.
    other = await hass.auth.async_create_user(
        "HIL other admin", group_ids=[GROUP_ID_ADMIN]
    )
    refresh = await hass.auth.async_create_refresh_token(other, "http://127.0.0.1")
    other_ws = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    await other_ws.send_json_auto_id(lookup)
    cross_user = await other_ws.receive_json()
    assert cross_user["success"] is False
    assert cross_user["error"]["code"] == "not_found"

    stop = {**lookup, "type": "allpowers_ble/hil/stop"}
    await ws.send_json_auto_id(stop)
    stopped = await ws.receive_json()
    assert stopped["success"] is True
    sealed = stopped["result"]
    assert sealed["closed"] is True
    assert sealed["frame_count"] == 1
    assert sealed["frames"][0]["hex"] == b"private".hex()
    assert sealed["observed_route"] == "unverified"
    assert sealed["qualification_allowed"] is False
    assert client.hil_capture is None

    await ws.send_json_auto_id(lookup)
    retry = await ws.receive_json()
    assert retry["success"] is True
    assert retry["result"] == sealed
    await ws.send_json_auto_id(stop)
    repeat = await ws.receive_json()
    assert repeat["success"] is True
    assert repeat["result"] == sealed

    # Starting a second scenario must not invalidate the first sealed retry.
    await ws.send_json_auto_id({**request, "scenario_id": "real-ws-B"})
    second = await ws.receive_json()
    assert second["success"] is True
    second_capture = client.hil_capture
    assert second_capture is not None
    assert second_capture.capture_id != capture_id
    second_capture.record("notification_received", direction="rx", frame=b"B-secret")
    await ws.send_json_auto_id(lookup)
    first_again = await ws.receive_json()
    assert first_again["success"] is True
    assert first_again["result"] == sealed
    assert client.hil_capture is second_capture
    await other_ws.send_json_auto_id(lookup)
    still_private = await other_ws.receive_json()
    assert still_private["success"] is False
    assert still_private["error"]["code"] == "not_found"

    store = hass.data[KEY]
    assert store.active is second_capture
    assert capture_id in store.sealed
    store.sealed[capture_id]._expiry_handle._callback()
    assert client.hil_capture is second_capture
    assert store.active is second_capture
    assert capture_id not in store.sealed
    assert KEY in hass.data
    clear_entry_capture(hass, entry.entry_id)
    assert KEY not in hass.data
    assert second_capture.frames == []


@pytest.mark.asyncio
async def test_hil_capability_snapshot_is_admin_scoped_and_allowlisted(
    hass: HomeAssistant,
    hass_ws_client,
    hass_admin_user,
) -> None:
    """The read-only capability endpoint rejects unsafe targets and private data."""
    await async_register_capabilities(hass)
    settings = SimpleNamespace(
        hardware_version="0.3",
        raw_hardware_version=0x03,
        firmware_version="0.2",
        raw_firmware_version=0x02,
    )
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            model_identity="R600",
            identity_conflicted=False,
            session_hardware_version="0.3",
            session_raw_hardware_version=0x03,
            settings=settings,
        ),
        settings_are_fresh=True,
        status_is_fresh=True,
        connected=True,
    )
    client = SimpleNamespace(
        options=SimpleNamespace(enable_car_charger=False),
        active_session_generation=7,
    )
    entry = MockConfigEntry(
        domain="allpowers_ble", data={}, state=ConfigEntryState.LOADED
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, client=client)
    ws = await hass_ws_client()
    request = {
        "type": "allpowers_ble/hil/capabilities",
        "entry_id": entry.entry_id,
    }

    saved_groups = hass_admin_user.groups
    hass_admin_user.groups = []
    await ws.send_json_auto_id(request)
    denied = await ws.receive_json()
    assert denied["success"] is False
    assert denied["error"]["code"] == "unauthorized"
    hass_admin_user.groups = saved_groups

    await ws.send_json_auto_id({**request, "entry_id": "unknown-entry"})
    wrong_entry = await ws.receive_json()
    assert wrong_entry["success"] is False
    assert wrong_entry["error"]["code"] == "not_found"

    unloaded_entry = MockConfigEntry(
        domain="allpowers_ble", data={}, state=ConfigEntryState.NOT_LOADED
    )
    unloaded_entry.add_to_hass(hass)
    unloaded_entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, client=client
    )
    await ws.send_json_auto_id({**request, "entry_id": unloaded_entry.entry_id})
    unloaded = await ws.receive_json()
    assert unloaded["success"] is False
    assert unloaded["error"]["code"] == "not_ready"

    await ws.send_json_auto_id(request)
    response = await ws.receive_json()
    assert response["success"] is True
    result = response["result"]
    assert result == {
        "schema_version": 1,
        "model": "R600",
        "profile": "r600-hw-0.3",
        "classification": "verified",
        "verified": True,
        "identity_conflicted": False,
        "hardware_revision": "0.3",
        "raw_hardware_revision": 0x03,
        "firmware_revision": "0.2",
        "raw_firmware_revision": 0x02,
        "capabilities": {
            "read_telemetry": True,
            "write_output_controls": True,
            "write_settings_controls": True,
            "write_settings_keepalive": True,
            "car_charger_enabled": False,
        },
        "connected": True,
        "status_fresh": True,
        "settings_fresh": True,
        "session_generation": 7,
    }
    assert entry.entry_id not in str(result)


@pytest.mark.asyncio
async def test_hil_real_websocket_expired_other_entry_detaches_its_client(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """An A timer must never remain attached or erase a newer B runtime."""
    await async_register(hass)
    first_client = _CaptureClient(10)
    second_client = _CaptureClient(11)
    first_entry = MockConfigEntry(domain="allpowers_ble", data={})
    second_entry = MockConfigEntry(domain="allpowers_ble", data={})
    first_entry.add_to_hass(hass)
    second_entry.add_to_hass(hass)
    first_entry.runtime_data = SimpleNamespace(client=first_client)
    second_entry.runtime_data = SimpleNamespace(client=second_client)
    ws = await hass_ws_client()
    start = {
        "type": "allpowers_ble/hil/start",
        "entry_id": first_entry.entry_id,
        "run_id": "test12345",
        "scenario_id": "first",
        "requested_route": "auto",
    }

    await ws.send_json_auto_id(start)
    first_response = await ws.receive_json()
    assert first_response["success"]
    first = first_client.hil_capture
    first.record("notification_received", direction="rx", frame=b"A-secret")
    first_timer = first._expiry_handle
    expiry_callback = first_timer._callback

    # Cross-entry start must still be busy until A's monotonic TTL expires.
    await ws.send_json_auto_id(
        {**start, "entry_id": second_entry.entry_id, "scenario_id": "busy"}
    )
    busy = await ws.receive_json()
    assert not busy["success"] and busy["error"]["code"] == "busy"
    assert first_client.hil_capture is first and second_client.hil_capture is None

    first.started -= 121
    await ws.send_json_auto_id(
        {**start, "entry_id": second_entry.entry_id, "scenario_id": "second"}
    )
    second_response = await ws.receive_json()
    assert second_response["success"]
    second = second_client.hil_capture
    assert first_client.hil_capture is None
    assert second is not None and second is not first
    assert first_timer.cancelled() and first._expiry_handle is None
    assert first.events == [] and first.frames == []
    store = hass.data[KEY]
    assert store.active is second

    # Even a forced late callback for A must not touch B.
    expiry_callback()
    assert store.active is second and second_client.hil_capture is second
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/fetch",
            "entry_id": first_entry.entry_id,
            "capture_id": first_response["result"]["capture_id"],
        }
    )
    removed = await ws.receive_json()
    assert removed["success"] is False
    clear_entry_capture(hass, second_entry.entry_id)
    assert second_client.hil_capture is None
    assert KEY not in hass.data
