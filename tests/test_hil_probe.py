"""Opt-in config-flow probe capture: exact targets, evidence and lifecycle."""

from __future__ import annotations

import asyncio
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble import config_flow, hil_probe
from custom_components.allpowers_ble.hil_trace import (
    KEY,
    Capture,
    CaptureStore,
    clear_entry_capture,
)
from custom_components.allpowers_ble.hil_probe import (
    FlowCandidates,
    ProbeCapture,
    consume_probe,
    finish_probe,
    register_flow_candidates,
    skip_probe,
    ws_probe_arm,
    ws_probe_fetch,
    ws_probe_list,
    ws_probe_stop,
)
from tests.ha_stubs import FakeBleakError, FakeDevice
from tests.helpers import ADDRESS, FakeHass
from tests.test_client_runtime import ProbeClient, ProbeClientWithStopNotify, Services


class FlowManager:
    """Public HA FlowManager contract, including user/reconfigure progress."""

    def __init__(self):
        self.flows = {"flow-a": "user", "flow-b": "reconfigure"}

    def async_progress(self):
        return [
            {
                "flow_id": flow_id,
                "handler": "allpowers_ble",
                "context": {"source": source, "private": ADDRESS},
            }
            for flow_id, source in self.flows.items()
        ]

    def async_get(self, flow_id):
        if flow_id in self.flows:
            return {"flow_id": flow_id, "handler": "allpowers_ble", "type": "form"}
        return None


def setup_case():
    hass = FakeHass()
    hass.config_entries.flow = FlowManager()
    connection = MagicMock()
    connection.user.is_admin = True
    connection.user.id = "admin-a"
    return hass, connection


async def arm(hass, connection, *, flow_id="flow-a", address=ADDRESS):
    register_flow_candidates(hass, flow_id, "user", [address])
    await ws_probe_list(hass, connection, {"id": 1})
    listed = connection.send_result.call_args.args[1]["flows"]
    listed_flow = next(item for item in listed if item["flow_id"] == flow_id)
    token = listed_flow["candidates"][0]["token"]
    message = {
        "id": 2,
        "flow_id": flow_id,
        "candidate_token": token,
        "run_id": "run12345",
        "scenario_id": "probe-status",
        "requested_route": "auto",
    }
    await ws_probe_arm(hass, connection, message)
    store = hass.data[KEY]
    assert isinstance(store.active, ProbeCapture)
    return store.active, message


@pytest.mark.asyncio
async def test_registered_candidates_are_ephemeral_opaque_and_rotate_on_change():
    hass, connection = setup_case()
    register_flow_candidates(hass, None, "user", [ADDRESS])
    register_flow_candidates(hass, "", "user", [ADDRESS])
    register_flow_candidates(hass, "flow-a", "invalid", [ADDRESS])
    assert hil_probe.REGISTRY_KEY not in hass.data
    register_flow_candidates(hass, "flow-a", "user", ["not-a-mac"])
    assert hil_probe.REGISTRY_KEY in hass.data
    assert not hass.data[hil_probe.REGISTRY_KEY]

    register_flow_candidates(hass, "flow-a", "user", [ADDRESS.lower()])
    initial = hass.data[hil_probe.REGISTRY_KEY]["flow-a"]
    (token,) = initial.tokens
    assert initial.tokens[token] == ADDRESS
    assert ADDRESS not in token
    register_flow_candidates(hass, "flow-a", "user", [ADDRESS])
    assert hass.data[hil_probe.REGISTRY_KEY]["flow-a"] is initial
    await ws_probe_list(hass, connection, {"id": 1})
    offered = connection.send_result.call_args.args[1]
    assert len(offered["flows"]) == 1
    assert offered["flows"][0]["source"] == "user"
    assert offered["flows"][0]["step"] == "user"
    assert offered["flows"][0]["candidates"] == [
        {"token": token, "label": "Candidate 1"}
    ]
    assert ADDRESS not in str(offered)
    assert "private" not in str(offered)
    other = "AA:BB:CC:DD:EE:01"
    register_flow_candidates(hass, "flow-a", "user", [other, ADDRESS])
    updated = hass.data[hil_probe.REGISTRY_KEY]["flow-a"]
    assert updated is not initial
    assert initial.timer.cancelled()
    initial.timer._callback()
    assert hass.data[hil_probe.REGISTRY_KEY]["flow-a"] is updated
    register_flow_candidates(hass, "flow-a", "confirm", [ADDRESS])
    assert updated.timer.cancelled()
    assert hass.data[hil_probe.REGISTRY_KEY]["flow-a"].step == "confirm"
    register_flow_candidates(hass, "flow-a", "confirm", [])
    assert "flow-a" not in hass.data[hil_probe.REGISTRY_KEY]


@pytest.mark.asyncio
async def test_flow_registry_expires_without_clearing_a_later_flow():
    hass, _ = setup_case()
    register_flow_candidates(hass, "flow-a", "user", [ADDRESS])
    record = hass.data[hil_probe.REGISTRY_KEY]["flow-a"]
    assert isinstance(record, FlowCandidates)
    record.timer._callback()
    assert hil_probe.REGISTRY_KEY not in hass.data
    register_flow_candidates(hass, "flow-a", "user", [ADDRESS])
    new = hass.data[hil_probe.REGISTRY_KEY]["flow-a"]
    record.timer._callback()
    assert hass.data[hil_probe.REGISTRY_KEY]["flow-a"] is new
    new.timer.cancel()


@pytest.mark.asyncio
async def test_probe_list_filters_nonactive_and_nonadmin_without_private_data():
    hass, connection = setup_case()
    register_flow_candidates(hass, "flow-a", "user", [ADDRESS])
    register_flow_candidates(hass, "flow-b", "reconfigure", [ADDRESS])
    register_flow_candidates(hass, "gone", "confirm", [ADDRESS])
    connection.user.is_admin = False
    await ws_probe_list(hass, connection, {"id": 1})
    connection.send_error.assert_called_once()
    connection.user.is_admin = True
    await ws_probe_list(hass, connection, {"id": 2})
    result = connection.send_result.call_args.args[1]
    assert {item["flow_id"] for item in result["flows"]} == {"flow-a", "flow-b"}
    assert {item["source"] for item in result["flows"]} == {
        "user",
        "reconfigure",
    }
    assert ADDRESS not in str(result)
    assert "private" not in str(result)
    hass.config_entries.flow.flows["flow-a"] = "unknown-private-source"
    await ws_probe_list(hass, connection, {"id": 3})
    result = connection.send_result.call_args.args[1]
    assert result["flows"][0]["source"] == "other"
    record = hass.data[hil_probe.REGISTRY_KEY]["flow-b"]
    record.deadline = monotonic() - 1
    await ws_probe_list(hass, connection, {"id": 4})
    assert len(connection.send_result.call_args.args[1]["flows"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "wrong_token", "run", "scenario", "route"])
async def test_arm_rejects_invalid_target_and_format(invalid):
    hass, connection = setup_case()
    register_flow_candidates(hass, "flow-a", "user", [ADDRESS])
    record = hass.data[hil_probe.REGISTRY_KEY]["flow-a"]
    (token,) = record.tokens
    request = {
        "id": 5,
        "flow_id": "flow-a",
        "candidate_token": token,
        "run_id": "run12345",
        "scenario_id": "status",
        "requested_route": "auto",
    }
    if invalid == "missing":
        request["flow_id"] = "not-a-flow"
    elif invalid == "wrong_token":
        request["candidate_token"] = ADDRESS
    elif invalid == "run":
        request["run_id"] = "bad!"
    elif invalid == "scenario":
        request["scenario_id"] = "bad!"
    else:
        request["requested_route"] = "proxy-or-local"
    await ws_probe_arm(hass, connection, request)
    connection.send_error.assert_called_once()
    assert KEY not in hass.data or hass.data[KEY].active is None
    record.timer.cancel()


@pytest.mark.asyncio
async def test_probe_arming_and_single_use_cannot_be_stolen_or_replayed():
    hass, connection = setup_case()
    capture, request = await arm(hass, connection)
    assert capture.entry_id is None
    assert capture.probe_state == "armed"
    assert capture.snapshot()["capture_scope"] == "config_probe"
    assert capture.snapshot()["complete"] is False
    assert capture.snapshot()["qualification_allowed"] is False
    await ws_probe_arm(hass, connection, request)
    connection.send_error.assert_called()
    assert consume_probe(hass, "flow-b", ADDRESS) is None
    assert consume_probe(hass, "flow-a", "11:22:33:44:55:66") is None
    assert consume_probe(hass, "flow-a", ADDRESS.lower()) is not None
    assert capture.probe_state == "probing"
    assert consume_probe(hass, "flow-a", ADDRESS) is None
    foreign = MagicMock()
    foreign.user.id = "admin-b"
    foreign.user.is_admin = True
    scope = {"id": 3, "flow_id": "flow-a", "capture_id": capture.capture_id}
    await ws_probe_fetch(hass, foreign, scope)
    foreign.send_error.assert_called_once()
    await ws_probe_fetch(hass, connection, {**scope, "flow_id": "flow-b"})
    connection.send_error.assert_called()
    await ws_probe_fetch(hass, connection, scope)
    seen = connection.send_result.call_args.args[1]
    assert seen["probe_state"] == "probing"
    assert seen["capture_complete"] is False
    capture.finish("failure", reason="cannot_connect")
    await ws_probe_stop(hass, connection, scope)
    assert connection.send_result.call_args.args[1]["probe_outcome"] == "failure"
    assert connection.send_result.call_args.args[1]["capture_complete"] is True
    assert not capture.target_address and not capture.candidate_token
    assert consume_probe(hass, "flow-a", ADDRESS) is None
    clear_entry_capture(hass, "entry-id")
    await ws_probe_fetch(hass, connection, scope)
    assert connection.send_result.call_args.args[1]["capture_complete"] is True
    capture.erase()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason", "complete"),
    [
        ("success", "probe_finished", True),
        ("failure", "probe_finished", True),
        ("cancelled", "probe_finished", True),
        ("skipped", "probe_skipped", False),
        ("interrupted", "probe_interrupted", False),
    ],
)
async def test_probe_outcome_separation_and_idempotent_fetch(outcome, reason, complete):
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    capture.record("diagnostic", frame=b"private")
    capture.finish(outcome, reason="safe_cause")
    snap = capture.snapshot()
    assert snap["probe_outcome"] == outcome
    assert snap["capture_complete"] is complete
    assert snap["complete"] is complete
    assert snap["termination_reason"] == reason
    assert snap["qualification_allowed"] is False
    assert snap["events"][-1]["reason_category"] == "safe_cause"
    count = len(capture.events)
    capture.finish("success")
    assert len(capture.events) == count
    req = {"id": 4, "flow_id": "flow-a", "capture_id": capture.capture_id}
    await ws_probe_fetch(hass, connection, req)
    await ws_probe_stop(hass, connection, req)
    assert connection.send_result.call_args.args[1] == snap
    assert len(capture.events) == count
    capture.erase()
    assert not capture.events
    assert not capture.frames


@pytest.mark.asyncio
async def test_stop_while_armed_or_probing_never_disconnects_ha_probe():
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    msg = {"id": 4, "flow_id": "flow-a", "capture_id": capture.capture_id}
    await ws_probe_stop(hass, connection, msg)
    assert capture.probe_outcome == "interrupted"
    assert capture.snapshot()["capture_complete"] is False
    assert consume_probe(hass, "flow-a", ADDRESS) is None
    clear_entry_capture(hass, "entry")
    capture.erase()

    capture, _ = await arm(hass, connection)
    tracer = consume_probe(hass, "flow-a", ADDRESS)
    assert tracer is not None
    await ws_probe_stop(hass, connection, {"id": 5, **msg, "capture_id": capture.capture_id})
    finish_probe(tracer, "success")
    assert capture.probe_outcome == "interrupted"
    assert capture.snapshot()["frame_count"] == 0
    capture.erase()


@pytest.mark.asyncio
async def test_skip_active_reconfigure_only_consumes_exact_target():
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection, flow_id="flow-b")
    skip_probe(hass, "flow-b", "01:02:03:04:05:06")
    assert capture.probe_state == "armed"
    skip_probe(hass, "flow-b", ADDRESS)
    assert capture.probe_outcome == "skipped"
    assert capture.snapshot()["frame_count"] == 0
    assert capture.snapshot()["capture_complete"] is False
    assert capture.snapshot()["events"][-1]["reason_category"] == "active_session_reused"
    capture.erase()


@pytest.mark.asyncio
async def test_expired_runtime_capture_can_be_replaced_but_active_is_busy():
    hass, connection = setup_case()
    regular = Capture("entry", "owner", "run12345", "status", "auto")
    store = CaptureStore()
    store.active = regular
    hass.data[KEY] = store
    register_flow_candidates(hass, "flow-a", "user", [ADDRESS])
    record = hass.data[hil_probe.REGISTRY_KEY]["flow-a"]
    (token,) = record.tokens
    request = {
        "id": 1,
        "flow_id": "flow-a",
        "candidate_token": token,
        "run_id": "run12345",
        "scenario_id": "test",
        "requested_route": "auto",
    }
    await ws_probe_arm(hass, connection, request)
    assert store.active is regular
    assert connection.send_error.call_count == 1
    regular.stop()
    hass.config_entries.async_get_entry = lambda _: None
    await ws_probe_arm(hass, connection, request)
    assert isinstance(store.active, ProbeCapture)
    assert regular.termination_reason == "erased"
    store.active.erase()


@pytest.mark.asyncio
async def test_private_capture_expiry_and_access_checks_are_flow_scoped():
    hass, connection = setup_case()
    missing = {"id": 3, "flow_id": "flow-a", "capture_id": "absent"}
    await ws_probe_fetch(hass, connection, missing)
    connection.send_error.assert_called_once()
    capture, _ = await arm(hass, connection)
    msg = {"id": 4, "flow_id": "flow-a", "capture_id": capture.capture_id}
    connection.user.is_admin = False
    await ws_probe_stop(hass, connection, msg)
    connection.user.is_admin = True
    assert capture.probe_state == "armed"
    with patch(
        "custom_components.allpowers_ble.hil_trace.monotonic",
        return_value=capture.started + 121,
    ):
        await ws_probe_fetch(hass, connection, msg)
    assert capture.termination_reason == "erased"
    assert hass.data.get(KEY) is None
    assert not capture.events


@pytest.mark.asyncio
async def test_expiry_timer_erases_only_old_capture_not_replacement():
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    old_expire = capture._on_expire
    capture.finish("success")
    another, _ = await arm(hass, connection, flow_id="flow-b")
    old_expire()
    assert hass.data[KEY].active is another
    assert capture.termination_reason == "erased"
    assert another.probe_state == "armed"
    another.erase()


@pytest.mark.asyncio
async def test_real_probe_records_status_tx_rx_decoder_route_and_cleanup(
    monkeypatch, status_frame, settings_frame
):
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    tracer = consume_probe(hass, "flow-a", ADDRESS)
    assert tracer is not None
    fake = ProbeClientWithStopNotify([settings_frame, status_frame])

    async def establish(*args, **kwargs):
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(
        client_module.bluetooth, "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )
    result = await client_module.async_probe_device(
        hass,
        address=ADDRESS,
        advertised_name="ALLPOWERS R600",
        timeout=0.2,
        observer=tracer,
    )
    finish_probe(tracer, "success")
    assert result.status.battery_percent == 73
    assert len(fake.writes) == 1
    assert fake.stop_notify_calls == 1
    assert fake.disconnect_calls == 1
    snapshot = capture.snapshot()
    assert snapshot["capture_complete"] is True
    assert snapshot["probe_outcome"] == "success"
    assert snapshot["observed_route"] == "unverified"
    assert snapshot["route_observations"][0]["session_generation"] == 1
    events = [row["event"] for row in snapshot["events"]]
    for event in (
        "probe_started",
        "connect_attempt",
        "connected",
        "route_observed",
        "probe_gatt_validated",
        "probe_notify_started",
        "write_attempt",
        "notification_received",
        "notification_decoded",
        "probe_settings_received",
        "probe_status_received",
        "write_ok",
        "probe_notify_stopped",
        "disconnected",
        "probe_succeeded",
    ):
        assert event in events
    frames = snapshot["frames"]
    assert [row["direction"] for row in frames] == ["tx", "rx", "rx"]
    assert all("AA:BB:CC" not in str(row) for row in snapshot["events"])
    assert snapshot["qualification_allowed"] is False
    capture.erase()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["not_found", "connect_timeout", "bad_services", "notify", "write", "status"],
)
async def test_probe_failures_keep_typed_private_evidence(
    monkeypatch, status_frame, failure
):
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    tracer = consume_probe(hass, "flow-a", ADDRESS)
    assert tracer is not None
    fake = ProbeClient([status_frame])
    if failure == "bad_services":
        fake.services = Services(service=False)
    if failure == "write":
        fake.raise_on_write = FakeBleakError("PRIVATE-DEVICE")
    if failure == "status":
        fake.frames = []

    async def establish(*args, **kwargs):
        del args, kwargs
        if failure == "connect_timeout":
            raise TimeoutError()
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: None if failure == "not_found" else FakeDevice(),
    )
    if failure == "notify":
        async def broken_notify(*args, **kwargs):
            del args, kwargs
            raise FakeBleakError("PRIVATE-NOTIFY")
        fake.start_notify = broken_notify
    with pytest.raises(Exception):
        await client_module.async_probe_device(
            hass, address=ADDRESS, advertised_name="ALLPOWERS R600",
            timeout=0.05, observer=tracer,
        )
    finish_probe(tracer, "failure", reason="safe_failure")
    events = [row["event"] for row in capture.events]
    assert "probe_failed" in events
    assert capture.snapshot()["capture_complete"]
    assert "PRIVATE" not in str(capture.events)
    if failure in ("not_found", "connect_timeout"):
        assert "connect_failed" in events
    if failure == "bad_services":
        assert "probe_gatt_rejected" in events
    if failure == "notify":
        assert "probe_notify_failed" in events
    if failure == "write":
        assert "write_error" in events and "write_ok" not in events
    if failure == "status":
        assert "probe_status_timeout" in events
    capture.erase()


@pytest.mark.asyncio
async def test_flow_probe_consumes_matching_arm_and_seals_on_failure(monkeypatch):
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    flow = config_flow.AllpowersConfigFlow()
    flow.hass = hass
    flow.context = {}
    flow.flow_id = "flow-a"

    async def failed(*args, **kwargs):
        assert kwargs["observer"] is not None
        raise client_module.DeviceNotFoundError("PRIVATE-MAC")

    monkeypatch.setattr(config_flow, "async_probe_device", failed)
    reason = await flow._async_probe_candidate(
        address=ADDRESS, advertised_name="ALLPOWERS R600"
    )
    assert reason is None
    assert flow._probe_error == "cannot_connect"
    assert capture.probe_outcome == "failure"
    assert capture.snapshot()["capture_complete"] is True
    assert "PRIVATE-MAC" not in str(capture.events)
    capture.erase()


@pytest.mark.asyncio
async def test_probe_flow_rejects_mismatch_and_unarmed_stays_unobserved(monkeypatch):
    hass, connection = setup_case()
    capture, _ = await arm(hass, connection)
    flow = config_flow.AllpowersConfigFlow()
    flow.hass = hass
    flow.context = {}
    flow.flow_id = "flow-b"

    async def succeeded(*args, **kwargs):
        assert kwargs["observer"] is None

    monkeypatch.setattr(config_flow, "async_probe_device", succeeded)
    reason = await flow._async_probe_candidate(
        address=ADDRESS, advertised_name="ALLPOWERS R600"
    )
    assert reason is None
    assert capture.probe_state == "armed"
    capture.erase()


@pytest.mark.asyncio
async def test_hil_probe_commands_register_without_autoarming(monkeypatch):
    hass, _ = setup_case()
    registered = []
    monkeypatch.setattr(
        hil_probe.websocket_api,
        "async_register_command",
        lambda _hass, handler: registered.append(handler),
    )
    await hil_probe.async_register(hass)
    assert registered == [
        ws_probe_list,
        ws_probe_arm,
        ws_probe_stop,
        ws_probe_fetch,
    ]
    assert KEY not in hass.data
