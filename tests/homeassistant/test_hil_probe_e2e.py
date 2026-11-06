"""Installed-Core end-to-end config-flow probe capture regression."""

from __future__ import annotations

from time import monotonic
from typing import Any

import pytest
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.core import HomeAssistant

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import ProbeConnectionTimeoutError
from custom_components.allpowers_ble.const import DOMAIN, SERVICE_UUID
from custom_components.allpowers_ble.hil_probe import (
    KEY,
    REGISTRY_KEY,
    TTL,
    async_register,
)
from tests.ha_stubs import FakeDevice
from tests.test_client_runtime import ProbeClientWithStopNotify

pytestmark = pytest.mark.homeassistant

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _discovery() -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="ALLPOWERS R600",
        address=ADDRESS,
        rssi=-55,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID],
        source="test",
        device=None,
        advertisement=None,
        connectable=True,
        time=monotonic(),
        tx_power=None,
    )


@pytest.mark.asyncio
async def test_real_ha_flow_websocket_and_simulated_gatt_probe(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
    hass_admin_user,
    status_frame: bytes,
    settings_frame: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arm an actual Bluetooth flow then capture its normal HA form probe."""
    del enable_custom_integrations
    await async_register(hass)

    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "bluetooth"},
        data=_discovery(),
    )
    assert flow["type"] == "form" and flow["step_id"] == "confirm"
    flow_id = flow["flow_id"]
    assert flow_id in hass.data[REGISTRY_KEY]

    admin_ws = await hass_ws_client()
    await admin_ws.send_json_auto_id({"type": "allpowers_ble/hil/probe/list"})
    candidates = await admin_ws.receive_json()
    assert candidates["success"] is True
    offered = next(
        row for row in candidates["result"]["flows"] if row["flow_id"] == flow_id
    )
    assert offered["source"] == "bluetooth"
    assert offered["candidates"] == [
        {"token": offered["candidates"][0]["token"], "label": "Candidate 1"}
    ]
    token = offered["candidates"][0]["token"]
    assert ADDRESS not in token

    # Real WebSocket authorization rejects a non-admin before creating a capture.
    saved_groups = hass_admin_user.groups
    hass_admin_user.groups = []
    await admin_ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/probe/arm",
            "flow_id": flow_id,
            "candidate_token": token,
            "run_id": "e2e-run-0001",
            "scenario_id": "real-ha-probe",
            "requested_route": "auto",
        }
    )
    unauthorized = await admin_ws.receive_json()
    assert unauthorized["success"] is False
    assert KEY not in hass.data
    hass_admin_user.groups = saved_groups

    wrong_target = {
        "type": "allpowers_ble/hil/probe/arm",
        "flow_id": flow_id,
        "candidate_token": "not-the-candidate-token",
        "run_id": "e2e-run-0001",
        "scenario_id": "real-ha-probe",
        "requested_route": "auto",
    }
    await admin_ws.send_json_auto_id(wrong_target)
    rejected = await admin_ws.receive_json()
    assert rejected["success"] is False
    assert KEY not in hass.data

    await admin_ws.send_json_auto_id({**wrong_target, "flow_id": "wrong-flow-id"})
    wrong_flow = await admin_ws.receive_json()
    assert wrong_flow["success"] is False

    await admin_ws.send_json_auto_id({**wrong_target, "candidate_token": token})
    armed = await admin_ws.receive_json()
    assert armed["success"] is True
    capture_id = armed["result"]["capture_id"]

    fake = ProbeClientWithStopNotify([settings_frame, status_frame])

    async def establish(*args: Any, **kwargs: Any) -> ProbeClientWithStopNotify:
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )

    # Submit the ordinary confirm form through Home Assistant's FlowManager.
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] == "create_entry"
    assert result["data"]["address"] == ADDRESS
    assert len(fake.writes) == 1
    assert fake.writes[0] == client_module.encode_status_request()
    assert fake.stop_notify_calls == 1
    assert fake.disconnect_calls == 1

    # Fetch through the same authenticated WS API; response is sealed/private.
    await admin_ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/probe/fetch",
            "flow_id": flow_id,
            "capture_id": capture_id,
        }
    )
    fetched = await admin_ws.receive_json()
    assert fetched["success"] is True
    capture = fetched["result"]
    assert capture["schema_version"] == 2
    assert capture["capture_scope"] == "config_probe"
    assert capture["probe_outcome"] == "success"
    assert capture["capture_complete"] is True
    assert capture["qualification_allowed"] is False
    assert capture["observed_route"] == "unverified"
    assert capture["dropped_events"] == capture["dropped_frames"] == 0
    assert capture["frame_count"] == 3
    frames = capture["frames"]
    assert [frame["direction"] for frame in frames] == ["tx", "rx", "rx"]
    assert frames[0]["command_type"] == "status_request"
    tx_ids = {
        frame.get("transaction_id") for frame in frames if frame["direction"] == "tx"
    }
    assert len(tx_ids) == 1 and None not in tx_ids
    correlated_rx = [frame for frame in frames if frame["direction"] == "rx"]
    assert all(frame.get("transaction_id") in tx_ids for frame in correlated_rx)
    events = [row["event"] for row in capture["events"]]
    required_order = (
        "probe_armed",
        "probe_started",
        "connect_attempt",
        "route_observed",
        "connected",
        "probe_gatt_validated",
        "probe_notify_started",
        "write_attempt",
        "notification_received",
        "notification_decoded",
        "probe_status_received",
        "write_ok",
        "probe_notify_stopped",
        "disconnected",
        "probe_succeeded",
    )
    positions = [events.index(event) for event in required_order]
    assert positions == sorted(positions), list(enumerate(events))
    correlation_events = {
        row["event"]: row.get("transaction_id")
        for row in capture["events"]
        if row["event"]
        in {
            "write_attempt",
            "write_ok",
            "notification_received",
            "notification_decoded",
            "probe_status_received",
        }
    }
    assert set(correlation_events) == {
        "write_attempt",
        "write_ok",
        "notification_received",
        "notification_decoded",
        "probe_status_received",
    }
    assert set(correlation_events.values()) == tx_ids
    assert ADDRESS not in str(capture["events"])
    assert ADDRESS not in str(capture["frames"])
    assert hass.data[KEY].sealed[capture_id].user_id == hass_admin_user.id
    assert capture["flow_id"] == flow_id
    assert capture["probe_outcome"] == "success"
    assert flow_id not in hass.data.get(REGISTRY_KEY, {})

    other_admin = await hass.auth.async_create_user(
        "HIL probe other admin", group_ids=[GROUP_ID_ADMIN]
    )
    refresh = await hass.auth.async_create_refresh_token(
        other_admin, "http://127.0.0.1"
    )
    other_ws = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    await other_ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/probe/fetch",
            "flow_id": flow_id,
            "capture_id": capture_id,
        }
    )
    isolated = await other_ws.receive_json()
    assert isolated["success"] is False
    assert isolated["error"]["code"] == "not_found"

    # The real created entry may initialize its runtime; release it and the
    # deliberately private sealed capture before pytest's leak checks.
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.state.value == "loaded":
            await hass.config_entries.async_unload(entry.entry_id)
    store = hass.data[KEY]
    sealed_capture = store.find(capture_id)
    assert sealed_capture is not None
    store.remove(sealed_capture)
    sealed_capture.erase()
    if store.empty():
        hass.data.pop(KEY, None)


@pytest.mark.asyncio
async def test_real_ha_flow_cancellation_makes_candidate_ineligible(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
) -> None:
    """A cancelled real flow cannot be listed or armed through the WS API."""
    del enable_custom_integrations
    await async_register(hass)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "bluetooth"},
        data=_discovery(),
    )
    flow_id = flow["flow_id"]
    ws = await hass_ws_client()
    await ws.send_json_auto_id({"type": "allpowers_ble/hil/probe/list"})
    listed = await ws.receive_json()
    candidate = listed["result"]["flows"][0]["candidates"][0]["token"]
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/probe/arm",
            "flow_id": flow_id,
            "candidate_token": candidate,
            "run_id": "cancel-run-0001",
            "scenario_id": "cancel-flow",
            "requested_route": "auto",
        }
    )
    armed = await ws.receive_json()
    assert armed["success"] is True
    hass.config_entries.flow.async_abort(flow_id)
    assert flow_id not in hass.data.get(REGISTRY_KEY, {})
    assert KEY not in hass.data

    await ws.send_json_auto_id({"type": "allpowers_ble/hil/probe/list"})
    listed = await ws.receive_json()
    assert listed["success"] is True
    assert all(row["flow_id"] != flow_id for row in listed["result"]["flows"])


@pytest.mark.asyncio
async def test_real_ha_failed_probe_and_expired_capture_cleanup(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed probes seal safe evidence; expired captures erase through WS."""
    del enable_custom_integrations
    await async_register(hass)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "bluetooth"},
        data=_discovery(),
    )
    flow_id = flow["flow_id"]
    ws = await hass_ws_client()
    await ws.send_json_auto_id({"type": "allpowers_ble/hil/probe/list"})
    listed = await ws.receive_json()
    token = listed["result"]["flows"][0]["candidates"][0]["token"]
    arm_request = {
        "type": "allpowers_ble/hil/probe/arm",
        "flow_id": flow_id,
        "candidate_token": token,
        "run_id": "failure-run-0001",
        "scenario_id": "connect-failure",
        "requested_route": "auto",
    }
    await ws.send_json_auto_id(arm_request)
    armed = await ws.receive_json()
    assert armed["success"] is True
    failure_id = armed["result"]["capture_id"]

    async def fail_connect(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise ProbeConnectionTimeoutError("simulated connection timeout")

    monkeypatch.setattr(client_module, "establish_connection", fail_connect)
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/probe/fetch",
            "flow_id": flow_id,
            "capture_id": failure_id,
        }
    )
    fetched = await ws.receive_json()
    assert fetched["success"] is True
    assert fetched["result"]["probe_outcome"] == "failure"
    assert fetched["result"]["capture_complete"] is True
    assert fetched["result"]["frame_count"] == 0

    # Re-arm the still-active flow, then expire through the same real WS API.
    await ws.send_json_auto_id({"type": "allpowers_ble/hil/probe/list"})
    listed_again = await ws.receive_json()
    token = listed_again["result"]["flows"][0]["candidates"][0]["token"]
    await ws.send_json_auto_id(
        {
            **arm_request,
            "candidate_token": token,
            "run_id": "expired-run-0001",
            "scenario_id": "expired-capture",
        }
    )
    expired_arm = await ws.receive_json()
    assert expired_arm["success"] is True
    active = hass.data[KEY].active
    assert active is not None
    active.started -= TTL + 1
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/probe/fetch",
            "flow_id": flow_id,
            "capture_id": expired_arm["result"]["capture_id"],
        }
    )
    expired = await ws.receive_json()
    assert expired["success"] is False
    assert expired["error"]["code"] == "expired"
    store = hass.data[KEY]
    assert store.active is None
    assert failure_id in store.sealed

    hass.config_entries.flow.async_abort(flow_id)
    assert REGISTRY_KEY not in hass.data
    sealed_failure = store.find(failure_id)
    assert sealed_failure is not None
    store.remove(sealed_failure)
    sealed_failure.erase()
    if store.empty():
        hass.data.pop(KEY, None)
