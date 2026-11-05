"""Bounded and privacy-preserving HIL capture unit contracts."""

import asyncio
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.allpowers_ble.hil_client import HilClientTracer
from custom_components.allpowers_ble.hil_trace import (
    Capture,
    CaptureStore,
    classify_tx,
    MAX_SEALED_CAPTURES,
    MAX_EVENTS,
    MAX_FRAMES,
    _authorized,
    _lookup,
    clear_entry_capture,
    async_register,
    ws_fetch,
    ws_start,
    ws_stop,
)


class _CaptureClient:
    """Synchronous observer mock for WebSocket lifecycle tests."""

    def __init__(self, generation):
        self._active_session_generation = generation
        self.observer = None

    def attach_observer(self, observer):
        self.observer = observer

    def detach_observer(self, observer):
        if self.observer is observer:
            self.observer = None

    def finalize_observer(self, observer):
        assert self.observer is observer


def _current_observer(client):
    """Inspect a test double or the private production observer slot."""
    if isinstance(client, _CaptureClient):
        return client.observer
    slot = client._observer
    return slot.target if slot is not None else None


def _capture_of(client):
    """Test-only capture accessor; no production getter is required."""
    observer = _current_observer(client)
    return observer.capture if observer is not None else None


def _attach_capture(client, capture):
    observer = HilClientTracer(capture)
    client.attach_observer(observer)
    return observer


def _for_capture(client, capture):
    observer = _current_observer(client)
    return (
        observer
        if observer is not None and observer.capture is capture
        else HilClientTracer(capture)
    )


def _detach_capture(client, capture):
    client.detach_observer(_for_capture(client, capture))


def _finalize_capture(client, capture):
    client.finalize_observer(_for_capture(client, capture))


def new_capture():
    return Capture("entry", "user", "abc12345", "status", "active_proxy")


def test_bounded_capture_sequence_and_raw_private():
    capture = new_capture()
    capture.record("tx", frame=bytes((1, 2)), direction="tx", session_generation=7)
    assert capture.frames[0]["hex"] == "0102"
    assert capture.frames[0]["seq"] == capture.events[0]["seq"]
    assert capture.frames[0]["observed_route"] == "unverified"
    for _ in range(MAX_EVENTS + 4):
        capture.record("event")
    for _ in range(MAX_FRAMES + 3):
        capture.record("rx", frame=b"x", direction="rx")
    assert capture.dropped_events > 0
    assert capture.dropped_frames > 0
    capture.stop()
    assert capture.snapshot()["complete"] is False


def test_frame_oversize_and_no_capture_after_stop():
    capture = new_capture()
    capture.record("rx", frame=b"x" * 257, direction="rx")
    assert capture.dropped_frames == 1
    assert not capture.frames
    capture.stop()
    capture.record("late", frame=b"y")
    assert len(capture.events) == 1


def test_ttl_and_erase_private_buffers():
    capture = new_capture()
    capture.record("rx", frame=b"secret")
    with patch(
        "custom_components.allpowers_ble.hil_trace.monotonic",
        return_value=capture.started + 121,
    ):
        assert not capture.active()
        assert capture.snapshot()["expired"]
    capture.erase()
    assert not capture.frames
    assert not capture.events


def test_admin_only_and_owner_entry_binding():
    connection = MagicMock()
    connection.user.is_admin = False
    assert not _authorized(connection, {"id": 9})
    connection.send_error.assert_called_once()
    connection.user.is_admin = True
    connection.user.id = "wrong"
    hass = MagicMock()
    capture = new_capture()
    store = CaptureStore()
    store.active = capture
    hass.data = {"allpowers_ble_hil_capture": store}
    assert (
        _lookup(
            hass,
            connection,
            {
                "id": 5,
                "capture_id": capture.capture_id,
                "entry_id": "entry",
            },
        )
        is None
    )


def test_unload_erases_buffers_and_manager_reference():
    capture = new_capture()
    capture.record("private", frame=b"secret")
    hass = MagicMock()
    store = CaptureStore()
    store.active = capture
    hass.data = {"allpowers_ble_hil_capture": store}
    clear_entry_capture(hass, "entry")
    assert "allpowers_ble_hil_capture" not in hass.data
    assert capture.frames == []


def _api_case():
    connection = MagicMock()
    connection.user.id = "user"
    connection.user.is_admin = True
    client = _CaptureClient(3)
    entry = SimpleNamespace(
        entry_id="entry",
        domain="allpowers_ble",
        runtime_data=SimpleNamespace(client=client),
    )
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_get_entry.return_value = entry
    return hass, connection, client


@pytest.mark.asyncio
async def test_websocket_explicit_start_stop_fetch_and_scope():
    hass, connection, client = _api_case()
    start = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "scenario-1",
        "requested_route": "active_proxy",
    }
    await ws_start(hass, connection, start)
    capture = _capture_of(client)
    assert capture is not None
    assert capture.requested_route == "active_proxy"
    assert capture.snapshot()["observed_route"] == "unverified"
    capture.record("rx", frame=b"secret", session_generation=3)
    common = {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id}
    await ws_fetch(hass, connection, common)
    assert connection.send_result.call_count == 2
    connection.user.id = "intruder"
    await ws_fetch(hass, connection, common)
    connection.send_error.assert_called()
    connection.user.id = "user"
    await ws_stop(hass, connection, common)
    assert _capture_of(client) is None
    assert capture.closed
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_websocket_rejects_nonadmin_invalid_input_and_busy():
    hass, connection, client = _api_case()
    start = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "scenario",
        "requested_route": "auto",
    }
    connection.user.is_admin = False
    await ws_start(hass, connection, start)
    assert _capture_of(client) is None
    connection.user.is_admin = True
    await ws_start(hass, connection, {**start, "run_id": "a"})
    assert _capture_of(client) is None
    await ws_start(hass, connection, start)
    assert _capture_of(client) is not None
    await ws_start(hass, connection, start)
    assert connection.send_error.call_count >= 3
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_websocket_rejects_foreign_entry_and_not_ready():
    hass, connection, client = _api_case()
    msg = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "scenario",
        "requested_route": "auto",
    }
    hass.config_entries.async_get_entry.return_value.domain = "unrelated"
    await ws_start(hass, connection, msg)
    assert _capture_of(client) is None
    hass.config_entries.async_get_entry.return_value.domain = "allpowers_ble"
    hass.config_entries.async_get_entry.return_value.runtime_data = None
    await ws_start(hass, connection, msg)
    assert _capture_of(client) is None


def test_record_failure_cannot_disrupt_ble():
    capture = new_capture()
    capture.record("malformed", frame=object())
    assert capture.dropped_events == 1
    assert not capture.frames


@pytest.mark.asyncio
async def test_expired_owner_lookup_erases_data():
    hass, connection, client = _api_case()
    start = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "status",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, start)
    capture = _capture_of(client)
    capture.record("rx", frame=b"private")
    capture.started -= 121
    await ws_fetch(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id},
    )
    assert _capture_of(client) is None
    assert capture.frames == []
    assert "allpowers_ble_hil_capture" not in hass.data
    connection.send_error.assert_called()


@pytest.mark.asyncio
async def test_expired_lookup_without_runtime_erases_private_data() -> None:
    """Expired evidence must scrub even after its config entry disappeared."""
    hass, connection, client = _api_case()
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "status",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    assert capture is not None
    capture.record("notification_received", frame=b"private", direction="rx")
    capture.started -= 121
    hass.config_entries.async_get_entry.return_value = None

    await ws_fetch(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id},
    )
    assert capture.frames == []
    assert capture.events == []
    assert "allpowers_ble_hil_capture" not in hass.data
    connection.send_error.assert_called_with(2, "expired", "HIL request rejected")
    _detach_capture(client, capture)
    assert _capture_of(client) is None


@pytest.mark.asyncio
async def test_new_session_retains_prior_sealed_retry():
    hass, connection, client = _api_case()
    start = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "status",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, start)
    old = _capture_of(client)
    old.record("rx", frame=b"private")
    old.stop()
    await ws_start(hass, connection, start)
    assert _capture_of(client) is not old
    assert old.frames and old.retrievable()
    assert hass.data["allpowers_ble_hil_capture"].sealed[old.capture_id] is old
    await ws_fetch(
        hass,
        connection,
        {"id": 3, "entry_id": "entry", "capture_id": old.capture_id},
    )
    assert (
        connection.send_result.call_args.args[1]["frames"][0]["hex"] == b"private".hex()
    )
    clear_entry_capture(hass, "entry")
    assert not old.frames


@pytest.mark.asyncio
async def test_new_session_replaces_unloaded_prior_entry():
    hass, connection, client = _api_case()
    previous = Capture("removed", "other", "abcdefgh", "status", "auto")
    previous.record("preexisting", frame=b"private")
    previous.stop()
    store = CaptureStore()
    store.sealed[previous.capture_id] = previous
    hass.data["allpowers_ble_hil_capture"] = store
    entry = hass.config_entries.async_get_entry.return_value
    hass.config_entries.async_get_entry.side_effect = lambda entry_id: (
        entry if entry_id == "entry" else None
    )
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "status",
            "requested_route": "auto",
        },
    )
    assert _capture_of(client) is not previous
    assert store.sealed[previous.capture_id] is previous
    clear_entry_capture(hass, "entry")
    assert store.sealed[previous.capture_id] is previous
    clear_entry_capture(hass, "removed")
    assert "allpowers_ble_hil_capture" not in hass.data


@pytest.mark.asyncio
async def test_timer_expires_and_scrubs_even_without_fetch():
    hass, connection, client = _api_case()
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "status",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    capture.record("rx", frame=b"secret")
    callback = capture._expiry_handle._callback
    callback()
    assert _capture_of(client) is None
    assert capture.frames == []
    assert not hass.data
    callback()


@pytest.mark.asyncio
async def test_timer_does_not_clear_replacement_capture():
    hass, connection, client = _api_case()
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "status",
            "requested_route": "auto",
        },
    )
    prior = _capture_of(client)
    callback = prior._expiry_handle._callback
    replacement = new_capture()
    store = hass.data["allpowers_ble_hil_capture"]
    store.active = replacement
    store.active_observer = _attach_capture(client, replacement)
    callback()
    assert _capture_of(client) is replacement
    assert store.active is replacement
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_invalid_stop_fetch_admin_and_missing_capture():
    hass, connection, client = _api_case()
    msg = {"id": 1, "entry_id": "entry", "capture_id": "unknown"}
    connection.user.is_admin = False
    await ws_stop(hass, connection, msg)
    await ws_fetch(hass, connection, msg)
    connection.user.is_admin = True
    await ws_stop(hass, connection, msg)
    await ws_fetch(hass, connection, msg)
    assert connection.send_error.call_count == 4
    assert _capture_of(client) is None


@pytest.mark.asyncio
async def test_stop_without_runtime_does_not_leak():
    hass, connection, client = _api_case()
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "status",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    hass.config_entries.async_get_entry.return_value.runtime_data = None
    await ws_stop(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id},
    )
    assert capture.closed
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_register_and_integration_setup_are_explicit():
    from custom_components.allpowers_ble import async_setup

    hass, connection, client = _api_case()
    assert await async_setup(hass, {})
    await async_register(hass)
    assert _capture_of(client) is None


@pytest.mark.asyncio
async def test_client_runtime_records_success_and_private_bytes(
    status_frame, settings_frame
):
    from tests.helpers import status
    from tests.ha_stubs import FakeCharacteristic
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    client._status = status()
    client._status_monotonic = monotonic()
    client._settings_monotonic = monotonic()
    client._schedule_status_refresh = lambda: None
    await client.async_set_ac(True)
    assert fake.writes
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    client._notification_handler(FakeCharacteristic(), bytearray(settings_frame))
    assert any(
        row["event"] == "notification_decoded"
        and row["inferred_model_profile"] == "r600-hw-0.3"
        and row["session_hardware_revision"] == "0.3"
        for row in capture.events
    )
    assert any(row["event"] == "notification_received" for row in capture.events)
    assert any(row["event"] == "notification_decoded" for row in capture.events)
    assert any(row["event"] == "transaction_submitted" for row in capture.events)
    assert any(row["event"] == "write_ok" for row in capture.events)
    assert capture.frames
    await client.async_stop()
    assert any(row["event"] == "disconnected" for row in capture.events)
    assert any(row["event"] == "client_stopped" for row in capture.events)


@pytest.mark.asyncio
async def test_client_trace_disconnected_guard_and_write_error(monkeypatch):
    from custom_components.allpowers_ble.client import NotConnectedError
    from tests.ha_stubs import FakeBleakError
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    capture = new_capture()
    _attach_capture(client, capture)
    with pytest.raises(NotConnectedError):
        await client._write_frame_unlocked(b"request")
    fake = connect_fake(client)
    client._status = status()
    client._status_monotonic = monotonic()
    client._settings_monotonic = monotonic()

    async def reject(*args, **kwargs):
        del args, kwargs
        raise FakeBleakError("test write failure")

    monkeypatch.setattr(fake, "write_gatt_char", reject)
    with pytest.raises(FakeBleakError):
        await client.async_set_ac(True)
    assert any(row["event"] == "write_error" for row in capture.events)
    assert any(row["event"] == "transaction_failed" for row in capture.events)
    assert any(row["event"] == "write_rejected" for row in capture.events)


@pytest.mark.asyncio
async def test_connect_and_failed_setup_trace(monkeypatch):
    from custom_components.allpowers_ble import client as client_module
    from custom_components.allpowers_ble.client import UnsupportedDeviceError
    from tests.ha_stubs import FakeClient, FakeDevice
    from tests.test_client_runtime import Services, make_client

    client = make_client()
    _attach_capture(client, new_capture())
    fake = FakeClient()
    fake.services = Services()

    async def establish(*args, **kwargs):
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    await client._connect_once()
    assert any(row["event"] == "connect_attempt" for row in _capture_of(client).events)
    assert any(
        row["event"] == "model_profile_selected"
        and row["inferred_model_profile"] == "r600-unverified-revision"
        for row in _capture_of(client).events
    )
    assert any(row["event"] == "connected" for row in _capture_of(client).events)
    await client.async_stop()

    failing = make_client()
    _attach_capture(failing, new_capture())
    invalid = FakeClient()
    invalid.services = Services(service=False)

    async def establish_invalid(*args, **kwargs):
        del args, kwargs
        return invalid

    monkeypatch.setattr(client_module, "establish_connection", establish_invalid)
    monkeypatch.setattr(failing, "_fresh_ble_device", lambda: FakeDevice())
    with pytest.raises(UnsupportedDeviceError):
        await failing._connect_once()
    assert any(row["event"] == "connect_failed" for row in _capture_of(failing).events)


@pytest.mark.asyncio
async def test_expired_capture_preserves_unrelated_runtime_attachment():
    hass, connection, client = _api_case()
    capture = new_capture()
    capture.started -= 121
    store = CaptureStore()
    store.active = capture
    hass.data["allpowers_ble_hil_capture"] = store
    unrelated = new_capture()
    _attach_capture(client, unrelated)
    await ws_fetch(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id},
    )
    assert _capture_of(client) is unrelated
    assert "allpowers_ble_hil_capture" not in hass.data


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        ("status", "status_request"),
        ("output", "output_control"),
        ("settings", "settings_control"),
    ],
)
def test_protocol_golden_tx_vectors_are_classified(factory, expected):
    from custom_components.allpowers_ble.protocol import (
        encode_output_control,
        encode_settings_control,
        encode_status_request,
    )
    from tests.helpers import settings

    frames = {
        "status": encode_status_request(),
        "output": encode_output_control(dc=True, ac=False, light=True),
        "settings": encode_settings_control(settings()),
    }
    frame = frames[factory]
    assert frame[:2] == bytes((0xA5, 0x65))
    assert classify_tx(frame) == expected
    capture = new_capture()
    capture.record("write_attempt", frame=frame, direction="tx")
    assert bytes.fromhex(capture.frames[0]["hex"]) == frame
    assert capture.frames[0]["command_type"] == expected
    assert capture.frames[0]["frame_validation"] == "validated_tx"


def test_invalid_or_fragmentary_tx_and_raw_rx_never_claim_opcode(status_frame):
    from custom_components.allpowers_ble.protocol import encode_output_control
    from custom_components.allpowers_ble.protocol.codec import append_xor_checksum

    frame = encode_output_control(dc=True, ac=True, light=True)
    corrupted = frame[:-1] + bytes((frame[-1] ^ 0xFF,))
    assert classify_tx(corrupted) == "unknown"
    assert classify_tx(frame[:6]) == "unknown"
    assert classify_tx(b"noise-bytes") == "unknown"
    assert classify_tx(bytes((0xA5, 0x65)) + b"abcdefg") == "unknown"
    assert classify_tx(frame) == "output_control"
    unsupported = append_xor_checksum(frame[:5] + bytes((0x7F, 0x00)) + frame[7:-1])
    assert classify_tx(unsupported) == "unknown"
    altered_reserved = append_xor_checksum(frame[:2] + bytes((0xF1,)) + frame[3:-1])
    assert classify_tx(altered_reserved) == "unknown"

    capture = new_capture()
    for raw in (status_frame, status_frame[:5], corrupted, b"noise"):
        capture.record("notification_received", frame=raw, direction="rx")
    assert all(row["command_type"] == "unknown" for row in capture.frames)
    assert all(row["frame_validation"] == "unvalidated_chunk" for row in capture.frames)
    assert [bytes.fromhex(row["hex"]) for row in capture.frames] == [
        status_frame,
        status_frame[:5],
        corrupted,
        b"noise",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "reason", "exception_type"),
    [
        ("missing", "device_selection", "DeviceNotFoundError"),
        ("unsupported", "model_support", "UnsupportedDeviceError"),
        ("bleak", "gatt_connection", "FakeBleakError"),
        ("timeout", "gatt_connection", "TimeoutError"),
        ("cancel", "gatt_connection", "CancelledError"),
        ("bad_services", "gatt_setup", "UnsupportedDeviceError"),
    ],
)
async def test_failed_connect_has_terminal_typed_event(
    monkeypatch, fault, reason, exception_type
):
    from custom_components.allpowers_ble import client as client_module
    from custom_components.allpowers_ble.client import (
        DeviceNotFoundError,
        UnsupportedDeviceError,
    )
    from tests.ha_stubs import FakeBleakError, FakeClient, FakeDevice
    from tests.test_client_runtime import Services, make_client

    client = make_client(name="AP S700" if fault == "unsupported" else "ALLPOWERS R600")
    capture = new_capture()
    _attach_capture(client, capture)
    monkeypatch.setattr(
        client,
        "_fresh_ble_device",
        lambda: None if fault == "missing" else FakeDevice(),
    )
    fake = FakeClient()
    fake.services = Services(service=fault != "bad_services")

    async def establish(*args, **kwargs):
        del args, kwargs
        if fault == "bleak":
            raise FakeBleakError("PRIVATE:AA:BB:CC")
        if fault == "timeout":
            raise TimeoutError("PRIVATE:10.0.0.1")
        if fault == "cancel":
            raise asyncio.CancelledError()
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    errors = {
        "missing": DeviceNotFoundError,
        "unsupported": UnsupportedDeviceError,
        "bleak": FakeBleakError,
        "timeout": TimeoutError,
        "cancel": asyncio.CancelledError,
        "bad_services": UnsupportedDeviceError,
    }
    with pytest.raises(errors[fault]):
        await client._connect_once()
    attempt, failure = [
        row
        for row in capture.events
        if row["event"] in ("connect_attempt", "connect_failed")
    ]
    assert [attempt["event"], failure["event"]] == [
        "connect_attempt",
        "connect_failed",
    ]
    assert attempt["seq"] < failure["seq"]
    assert attempt["session_generation"] == failure["session_generation"]
    assert failure["reason_category"] == reason
    assert failure["exception_type"] == exception_type
    assert "PRIVATE" not in str(capture.snapshot())
    assert client._active_session_generation is None


@pytest.mark.asyncio
async def test_connection_recovers_with_new_generation_after_missing_route(monkeypatch):
    from custom_components.allpowers_ble import client as client_module
    from custom_components.allpowers_ble.client import DeviceNotFoundError
    from tests.ha_stubs import FakeClient, FakeDevice
    from tests.test_client_runtime import Services, make_client

    client = make_client()
    capture = new_capture()
    _attach_capture(client, capture)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: None)
    with pytest.raises(DeviceNotFoundError):
        await client._connect_once()
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    fake = FakeClient()
    fake.services = Services()

    async def establish(*args, **kwargs):
        del args, kwargs
        return fake

    monkeypatch.setattr(client_module, "establish_connection", establish)
    await client._connect_once()
    terminal = [
        (row["event"], row["session_generation"])
        for row in capture.events
        if row["event"] in ("connect_attempt", "connect_failed", "connected")
    ]
    assert [event for event, _ in terminal] == [
        "connect_attempt",
        "connect_failed",
        "connect_attempt",
        "connected",
    ]
    assert terminal[0][1] == terminal[1][1]
    assert terminal[2][1] == terminal[3][1]
    assert terminal[2][1] != terminal[0][1]
    await client.async_stop()


@pytest.mark.asyncio
async def test_transaction_evidence_distinguishes_submit_and_confirm(
    monkeypatch, notification_builder, status_frame
):
    from custom_components.allpowers_ble.protocol import StateUnavailableError
    from tests.ha_stubs import FakeCharacteristic
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    now = client._loop_time()
    client._status = status()
    client._status_monotonic = now
    client._settings_monotonic = now
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)

    await client.async_set_ac(True)
    assert len(fake.writes) == 1
    phase = [r for r in capture.events if r["event"].startswith("transaction_")]
    assert [r["event"] for r in phase] == [
        "transaction_accepted",
        "transaction_submitted",
    ]
    assert not any(r["event"] == "transaction_confirmed" for r in capture.events)
    tx_id = phase[0]["transaction_id"]
    assert tx_id and phase[1]["transaction_id"] == tx_id

    with pytest.raises(StateUnavailableError, match="pending"):
        await client.async_set_ac(False)
    assert len(fake.writes) == 1
    assert [row["event"] for row in capture.events[-2:]] == [
        "transaction_rejected",
        "control_rejected",
    ]
    assert capture.events[-2]["reason"] == "confirmation_pending"
    assert capture.events[-2]["transaction_id"] == tx_id
    assert capture.events[-1]["reason"] == "confirmation_pending"
    assert capture.events[-1]["control_kind"] == "output"

    mismatch = notification_builder(0x01, bytes((0x11, 73, 1, 44, 0, 150, 0, 120)))
    client._notification_handler(FakeCharacteristic(), bytearray(mismatch))
    assert any(r["event"] == "telemetry_mismatch" for r in capture.events)
    assert not any(r["event"] == "transaction_confirmed" for r in capture.events)
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    confirmed = [r for r in capture.events if r["event"] == "transaction_confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0]["transaction_id"] == tx_id
    assert confirmed[0]["reason"] == "matching_validated_telemetry"
    assert client._pending_output_transaction is None
    assert len(fake.writes) == 1


@pytest.mark.asyncio
async def test_late_matching_status_cannot_resurrect_timed_out_transaction(
    monkeypatch, status_frame
):
    from tests.ha_stubs import FakeCharacteristic
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    now = 1000.0
    monkeypatch.setattr(client, "_loop_time", lambda: now)
    client._status = status()
    client._status_monotonic = now
    client._settings_monotonic = now
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    await client.async_set_ac(True)
    tx_id = client._pending_output_transaction.observation_id
    now = client._pending_output_transaction.confirm_deadline_monotonic + 1
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert any(
        r["event"] == "transaction_timed_out" and r["transaction_id"] == tx_id
        for r in capture.events
    )
    assert not any(r["event"] == "transaction_confirmed" for r in capture.events)
    assert len(fake.writes) == 1


def test_sealed_capture_remains_idempotently_retrievable_then_expires():
    capture = new_capture()
    capture.record("rx", frame=b"private", direction="rx")
    capture.stop()
    first = capture.snapshot()
    assert capture.retrievable()
    capture.stop()
    assert capture.snapshot() == first
    with patch(
        "custom_components.allpowers_ble.hil_trace.monotonic",
        return_value=capture.sealed_at + 31,
    ):
        assert not capture.retrievable()


@pytest.mark.asyncio
async def test_stop_response_failure_recovered_by_fetch():
    hass, connection, client = _api_case()
    req = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "status",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, req)
    capture = _capture_of(client)
    capture.record("tx", frame=b"secret", direction="tx")
    stop = {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id}
    connection.send_result.side_effect = RuntimeError("lost socket")
    with pytest.raises(RuntimeError):
        await ws_stop(hass, connection, stop)
    assert _capture_of(client) is None
    assert capture.closed and capture.retrievable()
    connection.send_result.side_effect = None
    await ws_fetch(hass, connection, stop)
    result = connection.send_result.call_args.args[1]
    assert result["closed"] is True
    assert result["frame_count"] == 1
    assert result["frames"][0]["hex"] == b"secret".hex()
    await ws_stop(hass, connection, stop)
    assert connection.send_result.call_args.args[1] == result
    clear_entry_capture(hass, "entry")
    assert not capture.frames


def test_sustained_traffic_exceeds_budget_and_cannot_claim_complete():
    capture = new_capture()
    for i in range(180):
        capture.record("notification_received", frame=bytes((i % 256,)), direction="rx")
        capture.record("notification_decoded", decoded_packets=1)
    capture.stop()
    snapshot = capture.snapshot()
    assert snapshot["frame_count"] == MAX_FRAMES
    assert snapshot["event_count"] == MAX_EVENTS
    assert snapshot["dropped_frames"] == 180 - MAX_FRAMES
    assert snapshot["dropped_events"] == 360 - MAX_EVENTS
    assert snapshot["complete"] is False
    assert snapshot["qualification_allowed"] is False


@pytest.mark.asyncio
async def test_stop_rearms_bounded_expiry_when_timer_reference_is_missing():
    hass, connection, client = _api_case()
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "status",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    capture._expiry_handle.cancel()
    capture._expiry_handle = None
    await ws_stop(
        hass,
        connection,
        {
            "id": 2,
            "entry_id": "entry",
            "capture_id": capture.capture_id,
        },
    )
    assert capture._expiry_handle is not None
    assert capture.closed and capture.retrievable()
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_opt_in_capture_does_not_add_ble_writes_or_change_status_request():
    from custom_components.allpowers_ble.protocol import encode_status_request
    from tests.test_client_runtime import connect_fake, make_client

    writes = []
    for enabled in (False, True):
        client = make_client()
        fake = connect_fake(client)
        capture = new_capture() if enabled else None
        if capture is not None:
            _attach_capture(client, capture)
        else:
            assert client._observer is None
        await client.async_request_status()
        writes.append(list(fake.writes))
        if capture is not None:
            assert len(capture.frames) == 1
            assert capture.frames[0]["hex"] == encode_status_request().hex()
        else:
            assert _capture_of(client) is None
    assert writes == [[encode_status_request()], [encode_status_request()]]


def test_stop_near_active_ttl_still_allows_bounded_sealed_recovery():
    capture = new_capture()
    capture.record("write_attempt", frame=b"private", direction="tx")
    capture.started -= 119
    capture.stop()
    with patch(
        "custom_components.allpowers_ble.hil_trace.monotonic",
        return_value=capture.sealed_at + 2,
    ):
        assert capture.retrievable()
        result = capture.snapshot()
        assert result["closed"]
        assert result["complete"]
        assert not result["expired"]
        assert result["frames"][0]["hex"] == b"private".hex()
    capture.erase()


@pytest.mark.asyncio
async def test_runtime_stop_seals_incomplete_once_and_preserves_retry_bytes():
    from tests.test_client_runtime import connect_fake, make_client

    hass, connection, _ = _api_case()
    client = make_client()
    connect_fake(client)
    hass.config_entries.async_get_entry.return_value.runtime_data.client = client
    start = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "shutdown",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, start)
    capture = _capture_of(client)
    capture.record("notification_received", frame=b"private", direction="rx")
    original_ttl = capture._expiry_handle
    await client.async_stop()
    assert capture.termination_reason == "runtime_stopped"
    assert capture.closed and capture.sealed_at is not None
    assert original_ttl.cancelled()
    assert capture._expiry_handle is not original_ttl
    assert not capture.snapshot()["complete"]
    stop = {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id}
    await ws_stop(hass, connection, stop)
    stopped = connection.send_result.call_args.args[1]
    assert stopped["termination_reason"] == "runtime_stopped"
    assert stopped["complete"] is False
    assert stopped["frames"][0]["hex"] == b"private".hex()
    assert [r["event"] for r in capture.events].count("capture_stopped") == 0
    await ws_fetch(hass, connection, stop)
    assert connection.send_result.call_args.args[1] == stopped
    await ws_stop(hass, connection, stop)
    assert connection.send_result.call_args.args[1] == stopped
    assert [r["event"] for r in capture.events].count("capture_stopped") == 0
    expiry = capture._expiry_handle._callback
    expiry()
    assert capture.frames == [] and capture.events == []
    assert "allpowers_ble_hil_capture" not in hass.data


@pytest.mark.asyncio
async def test_requested_stop_keeps_complete_and_sealed_timer_on_retry():
    hass, connection, client = _api_case()
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "normal",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    capture.record("notification_received", frame=b"ok", direction="rx")
    old_timer = capture._expiry_handle
    stop = {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id}
    await ws_stop(hass, connection, stop)
    expected = connection.send_result.call_args.args[1]
    assert expected["complete"] is True
    assert expected["termination_reason"] == "requested_stop"
    assert old_timer.cancelled()
    assert capture._expiry_handle is not old_timer
    await ws_stop(hass, connection, stop)
    assert connection.send_result.call_args.args[1] == expected
    assert [e["event"] for e in capture.events].count("capture_stopped") == 1
    clear_entry_capture(hass, "entry")


def test_expired_stop_cannot_promote_capture_or_erasure_to_complete():
    capture = new_capture()
    capture.record("notification_received", frame=b"ok", direction="rx")
    capture.started -= 121
    capture.stop()
    assert capture.termination_reason == "expired"
    assert capture.closed and not capture.retrievable()
    assert not capture.snapshot()["complete"]
    capture.erase()
    assert capture.termination_reason == "erased"
    assert not capture.snapshot()["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["output", "settings"])
@pytest.mark.parametrize("deadline_passed", [False, True])
@pytest.mark.parametrize("action", ["reconnect", "stop"])
async def test_real_disconnect_records_one_terminal_pending_transaction(
    monkeypatch, kind, deadline_passed, action, status_frame
):
    from tests.ha_stubs import FakeCharacteristic
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    clock = [1000.0]
    monkeypatch.setattr(client, "_loop_time", lambda: clock[0])
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    client._status = status()
    client._status_monotonic = clock[0]
    client._settings_monotonic = clock[0]
    capture = new_capture()
    _attach_capture(client, capture)
    if kind == "output":
        await client.async_set_ac(True)
        transaction = client._pending_output_transaction
    else:
        await client.async_set_eco(False)
        transaction = client._pending_settings_transaction
    assert transaction is not None
    identifier = transaction.observation_id
    generation = transaction.session_generation
    assert identifier is not None
    assert len(fake.writes) == 1
    old_callback = client._make_notification_handler(generation, fake)
    clock[0] = (
        transaction.confirm_deadline_monotonic + 1
        if deadline_passed
        else transaction.confirm_deadline_monotonic - 1
    )
    if action == "reconnect":
        await client.async_reconnect("Manual reconnect")
    else:
        await client.async_stop()
    rows = [
        row
        for row in capture.events
        if row.get("transaction_id") == identifier
        and row["event"].startswith("transaction_")
    ]
    terminal = "transaction_timed_out" if deadline_passed else "transaction_abandoned"
    assert [row["event"] for row in rows] == [
        "transaction_accepted",
        "transaction_submitted",
        terminal,
    ]
    assert all(row["session_generation"] == generation for row in rows)
    assert rows[-1]["reason"] == (
        "deadline_passed" if deadline_passed else "session_disconnected"
    )
    disconnected = [row for row in capture.events if row["event"] == "disconnected"]
    assert len(disconnected) == 1
    assert disconnected[0]["seq"] > rows[-1]["seq"]
    assert disconnected[0]["session_generation"] == generation
    assert disconnected[0]["reason_category"] == "manual"
    old_callback(FakeCharacteristic(), bytearray(status_frame))
    assert len(fake.writes) == 1
    assert not any(row["event"] == "transaction_confirmed" for row in capture.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["output", "settings"])
@pytest.mark.parametrize("deadline_passed", [False, True])
async def test_user_stop_records_silent_pending_state_without_mutating_it(
    monkeypatch, kind, deadline_passed
):
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    hass, connection, _ = _api_case()
    client = make_client()
    fake = connect_fake(client)
    hass.config_entries.async_get_entry.return_value.runtime_data.client = client
    clock = [1000.0]
    monkeypatch.setattr(client, "_loop_time", lambda: clock[0])
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    client._status = status()
    client._status_monotonic = clock[0]
    client._settings_monotonic = clock[0]
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "silence",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    if kind == "output":
        await client.async_set_ac(True)
        original = client._pending_output_transaction
    else:
        await client.async_set_eco(False)
        original = client._pending_settings_transaction
    assert original is not None
    clock[0] = (
        original.confirm_deadline_monotonic + 1
        if deadline_passed
        else original.confirm_deadline_monotonic - 1
    )
    stop = {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id}
    await ws_stop(hass, connection, stop)
    first = connection.send_result.call_args.args[1]
    assert first["complete"] is True
    assert first["termination_reason"] == "requested_stop"
    assert first["qualification_allowed"] is False
    terminal = "transaction_timed_out" if deadline_passed else "transaction_unresolved"
    tx_events = [
        row
        for row in first["events"]
        if row.get("transaction_id") == original.observation_id
        and row["event"].startswith("transaction_")
    ]
    assert [row["event"] for row in tx_events] == [
        "transaction_accepted",
        "transaction_submitted",
        terminal,
    ]
    assert tx_events[-1]["seq"] < first["events"][-1]["seq"]
    assert first["events"][-1]["event"] == "capture_stopped"
    assert tx_events[-1]["session_generation"] == original.session_generation
    assert tx_events[-1]["reason"] == (
        "deadline_passed" if deadline_passed else "deadline_open"
    )
    assert (
        client._pending_output_transaction
        if kind == "output"
        else client._pending_settings_transaction
    ) is original
    assert len(fake.writes) == 1
    await ws_stop(hass, connection, stop)
    assert connection.send_result.call_args.args[1] == first
    clear_entry_capture(hass, "entry")


def test_pending_trace_finalizer_is_read_only_and_skips_confirmed():
    from custom_components.allpowers_ble.client import TransactionPhase
    from tests.test_client_runtime import make_client

    client = make_client()
    _finalize_capture(client, _capture_of(client))
    capture = new_capture()
    _attach_capture(client, capture)
    client._pending_output_transaction = None
    client._pending_settings_transaction = None
    _finalize_capture(client, _capture_of(client))
    from custom_components.allpowers_ble.client import PendingOutputTransaction

    client._pending_output_transaction = PendingOutputTransaction(
        session_generation=1,
        source_version=0,
        target_dc=True,
        target_ac=True,
        target_light=False,
        sent_monotonic=1,
        confirm_deadline_monotonic=client._loop_time() + 60,
        phase=TransactionPhase.CONFIRMED,
        observation_id="tx-confirmed",
    )
    _finalize_capture(client, _capture_of(client))
    assert not capture.events
    assert client._pending_output_transaction.phase is TransactionPhase.CONFIRMED


@pytest.mark.asyncio
async def test_interleaved_status_and_controls_join_exact_frame_id(monkeypatch):
    from custom_components.allpowers_ble.protocol import encode_status_request
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    client._status = status()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    capture = new_capture()
    _attach_capture(client, capture)
    await client.async_request_status()
    await client.async_set_ac(True)
    await client.async_request_status()
    await client.async_set_eco(False)
    assert len(fake.writes) == 4
    assert fake.writes[0] == fake.writes[2] == encode_status_request()
    tx_frames = [row for row in capture.frames if row["event"] == "write_attempt"]
    assert len(tx_frames) == 4
    ids = [row["transaction_id"] for row in tx_frames]
    assert ids[0] is None and ids[2] is None
    assert isinstance(ids[1], str) and isinstance(ids[3], str)
    assert ids[1].endswith("-1") and ids[3].endswith("-2")
    assert ids[1].rsplit("-", 1)[0] == ids[3].rsplit("-", 1)[0]
    assert capture.capture_id not in ids[1]
    assert [row["command_type"] for row in tx_frames] == [
        "status_request",
        "output_control",
        "status_request",
        "settings_control",
    ]
    for frame in tx_frames:
        assert any(
            event["seq"] == frame["seq"]
            and event["transaction_id"] == frame["transaction_id"]
            and event["event"] == "write_attempt"
            for event in capture.events
        )
        assert bytes.fromhex(frame["hex"]) in fake.writes
    write_ok = [
        row["transaction_id"] for row in capture.events if row["event"] == "write_ok"
    ]
    assert write_ok == ids
    assert len({value for value in ids if value is not None}) == 2
    await client.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_old", "packet_old", "expected"),
    [
        (True, False, "telemetry_watchdog"),
        (False, True, "transport_watchdog"),
    ],
)
async def test_watchdog_and_unsolicited_disconnects_are_distinct_causes(
    monkeypatch, status_old, packet_old, expected
):
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    clock = 1000.0
    monkeypatch.setattr(client, "_loop_time", lambda: clock)
    watchdog = client._options.watchdog_timeout
    client._connected_monotonic = clock - watchdog - 1
    client._status_monotonic = clock - watchdog - 1 if status_old else clock
    client._last_packet_monotonic = clock - watchdog - 1 if packet_old else clock

    async def one_iteration(timeout):
        del timeout
        client._stop_event.set()

    monkeypatch.setattr(client, "_wait_for_maintenance_wakeup", one_iteration)
    await client._maintenance_loop()
    disconnected = [row for row in capture.events if row["event"] == "disconnected"]
    assert len(disconnected) == 1
    assert disconnected[0]["reason_category"] == expected
    assert disconnected[0]["session_generation"] == 1
    assert not fake.is_connected


@pytest.mark.asyncio
async def test_external_disconnect_has_safe_cause_even_with_private_data():
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    client._loop = asyncio.get_running_loop()
    capture = new_capture()
    _attach_capture(client, capture)
    callback = client._make_disconnected_callback(1)
    fake.is_connected = False
    callback(fake)
    await asyncio.sleep(0)
    await client._disconnect_client()
    rows = [row for row in capture.events if row["event"] == "disconnected"]
    assert len(rows) == 1
    assert rows[0]["reason_category"] == "unsolicited_disconnect"
    assert "aa:bb" not in str(capture.snapshot()).lower()
    assert client._active_session_generation is None


@pytest.mark.asyncio
async def test_reconnect_unknown_reason_and_settings_recovery_use_safe_categories():
    from tests.test_client_runtime import connect_fake, make_client

    for reason, expected in (
        ("Contains private 192.168.0.18", "manual"),
        (
            "Settings confirmation unavailable; renewing BLE session",
            "settings_recovery",
        ),
    ):
        client = make_client()
        connect_fake(client)
        capture = new_capture()
        _attach_capture(client, capture)
        await client.async_reconnect(reason)
        rows = [row for row in capture.events if row["event"] == "disconnected"]
        assert len(rows) == 1
        assert rows[0]["reason_category"] == expected
        assert "192.168" not in str(capture.snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "reconnect"])
async def test_existing_disconnect_cause_is_not_overwritten(operation):
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    client._disconnect_reason_category = "unsolicited_disconnect"
    if operation == "stop":
        await client.async_stop()
    else:
        await client.async_reconnect("Telemetry watchdog expired")
    rows = [row for row in capture.events if row["event"] == "disconnected"]
    assert len(rows) == 1
    assert rows[0]["reason_category"] == "unsolicited_disconnect"
    assert client._disconnect_reason_category is None


@pytest.mark.asyncio
async def test_write_error_cannot_replace_an_existing_disconnect_cause(monkeypatch):
    from tests.ha_stubs import FakeBleakError
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    client._disconnect_reason_category = "manual"

    async def reject(*args, **kwargs):
        del args, kwargs
        raise FakeBleakError("private BLE message")

    monkeypatch.setattr(fake, "write_gatt_char", reject)
    with pytest.raises(FakeBleakError):
        await client.async_request_status()
    assert client._disconnect_reason_category == "manual"
    assert any(
        row["event"] == "write_error" and row["transaction_id"] is None
        for row in capture.events
    )
    await client._disconnect_client()
    rows = [row for row in capture.events if row["event"] == "disconnected"]
    assert len(rows) == 1
    assert rows[0]["reason_category"] == "manual"
    assert "private BLE message" not in str(capture.snapshot())


@pytest.mark.asyncio
async def test_sealed_capture_survives_new_active_run_and_remains_private():
    hass, connection, client = _api_case()
    request = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abcdefgh",
        "scenario_id": "first",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, request)
    first = _capture_of(client)
    first.record("notification_received", frame=b"private-A", direction="rx")
    stop_a = {"id": 2, "entry_id": "entry", "capture_id": first.capture_id}
    await ws_stop(hass, connection, stop_a)
    sealed = connection.send_result.call_args.args[1]
    assert sealed["complete"] and sealed["frame_count"] == 1
    await ws_start(hass, connection, {**request, "id": 3, "scenario_id": "second"})
    second = _capture_of(client)
    store = hass.data["allpowers_ble_hil_capture"]
    assert store.active is second and store.sealed[first.capture_id] is first
    second.record("notification_received", frame=b"private-B", direction="rx")
    await ws_fetch(hass, connection, stop_a)
    assert connection.send_result.call_args.args[1] == sealed
    await ws_stop(hass, connection, stop_a)
    assert connection.send_result.call_args.args[1] == sealed
    assert store.active is second
    connection.user.id = "other-admin"
    await ws_fetch(hass, connection, stop_a)
    assert connection.send_error.call_args.args[1] == "not_found"
    connection.user.id = "user"
    await ws_fetch(hass, connection, {**stop_a, "entry_id": "wrong"})
    assert connection.send_error.call_args.args[1] == "not_found"
    first._expiry_handle._callback()
    assert first.frames == [] and first.capture_id not in store.sealed
    assert _capture_of(client) is second and store.active is second
    clear_entry_capture(hass, "entry")
    assert not second.frames and not hass.data


@pytest.mark.asyncio
async def test_sealed_fifo_evicts_only_oldest_and_entry_unload_erases_every_run():
    hass, connection, client = _api_case()
    sealed = []
    for number in range(MAX_SEALED_CAPTURES + 1):
        await ws_start(
            hass,
            connection,
            {
                "id": number * 2 + 1,
                "entry_id": "entry",
                "run_id": "abc12345",
                "scenario_id": f"run-{number}",
                "requested_route": "auto",
            },
        )
        capture = _capture_of(client)
        capture.record(
            "notification_received", frame=f"private-{number}".encode(), direction="rx"
        )
        await ws_stop(
            hass,
            connection,
            {
                "id": number * 2 + 2,
                "entry_id": "entry",
                "capture_id": capture.capture_id,
            },
        )
        sealed.append(capture)
    store = hass.data["allpowers_ble_hil_capture"]
    assert store.active is None
    assert list(store.sealed) == [item.capture_id for item in sealed[1:]]
    assert not sealed[0].frames and sealed[0].termination_reason == "erased"
    assert all(item.frames for item in sealed[1:])
    await ws_start(
        hass,
        connection,
        {
            "id": 99,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "new-run",
            "requested_route": "auto",
        },
    )
    latest = _capture_of(client)
    latest.record("notification_received", frame=b"private-latest", direction="rx")
    clear_entry_capture(hass, "entry")
    assert not hass.data
    assert not latest.frames and latest._expiry_handle is None
    assert all(not item.frames and item._expiry_handle is None for item in sealed)
    assert _capture_of(client) is None


@pytest.mark.asyncio
async def test_unload_first_entry_preserves_other_entry_active_capture():
    hass, connection, client = _api_case()
    other_client = _CaptureClient(7)
    entry = hass.config_entries.async_get_entry.return_value
    other_entry = SimpleNamespace(
        entry_id="other-entry",
        domain="allpowers_ble",
        runtime_data=SimpleNamespace(client=other_client),
    )
    hass.config_entries.async_get_entry.side_effect = {
        "entry": entry,
        "other-entry": other_entry,
    }.get
    request = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abcdefgh",
        "scenario_id": "first",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, request)
    first = _capture_of(client)
    first.record("notification_received", frame=b"first", direction="rx")
    await ws_stop(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": first.capture_id},
    )
    await ws_start(
        hass,
        connection,
        {**request, "entry_id": "other-entry", "scenario_id": "second"},
    )
    second = _capture_of(other_client)
    second.record("notification_received", frame=b"second", direction="rx")
    clear_entry_capture(hass, "entry")
    store = hass.data["allpowers_ble_hil_capture"]
    assert not first.frames and first.capture_id not in store.sealed
    assert store.active is second and second.frames
    assert _capture_of(other_client) is second
    clear_entry_capture(hass, "other-entry")
    assert not hass.data


def test_capture_store_unknown_and_foreign_remove_are_noops():
    store = CaptureStore()
    first = new_capture()
    second = new_capture()
    store.active = first
    store.remove(second)
    assert store.active is first
    assert store.find("missing") is None
    store.seal(first)
    assert store.active is None
    assert store.find(first.capture_id) is first
    store.remove(second)
    assert store.find(first.capture_id) is first
    store.remove(first)
    assert store.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["output", "settings"])
async def test_new_run_inherits_pending_transaction_without_replaying_tx(
    monkeypatch, kind, status_frame, notification_builder
):
    from tests.ha_stubs import FakeCharacteristic
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    hass, connection, _ = _api_case()
    client = make_client()
    fake = connect_fake(client)
    hass.config_entries.async_get_entry.return_value.runtime_data.client = client
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    client._status = status()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    request = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "capture-A",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, request)
    first = _capture_of(client)
    if kind == "output":
        await client.async_set_ac(True)
        pending = client._pending_output_transaction
    else:
        await client.async_set_eco(False)
        pending = client._pending_settings_transaction
    assert pending is not None and pending.observation_id is not None
    previous_id = pending.observation_id
    assert previous_id.startswith("tx-")
    assert first.capture_id not in previous_id
    assert len(fake.writes) == 1
    await ws_stop(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": first.capture_id},
    )
    assert any(
        e["event"] == "transaction_unresolved" and e["transaction_id"] == previous_id
        for e in first.events
    )
    await ws_start(hass, connection, {**request, "id": 3, "scenario_id": "capture-B"})
    second = _capture_of(client)
    inherited = [r for r in second.events if r["event"] == "transaction_inherited"]
    assert len(inherited) == 1
    assert inherited[0]["transaction_id"] == previous_id
    assert inherited[0]["origin"] == "preexisting"
    assert inherited[0]["attribution"] == "prior_capture"
    assert inherited[0]["session_generation"] == pending.session_generation
    assert inherited[0]["transaction_kind"] == kind
    assert not second.frames and len(fake.writes) == 1
    if kind == "output":
        notification = status_frame
    else:
        target = pending.target
        notification = notification_builder(
            0x03,
            bytes(
                (
                    target.raw_flags,
                    target.eco_timeout_hours,
                    0,
                    0,
                    target.raw_hardware_version,
                    target.raw_firmware_version,
                )
            ),
        )
    client._notification_handler(FakeCharacteristic(), bytearray(notification))
    confirmed = [r for r in second.events if r["event"] == "transaction_confirmed"]
    assert len(confirmed) == 1 and confirmed[0]["transaction_id"] == previous_id
    assert len(fake.writes) == 1
    if kind == "output":
        await client.async_set_ac(False)
    else:
        await client.async_set_eco(True)
    assert len(fake.writes) == 2
    new = [r for r in second.events if r["event"] == "transaction_accepted"]
    assert len(new) == 1
    assert new[0]["transaction_id"].startswith("tx-")
    assert new[0]["transaction_id"].endswith("-1")
    assert second.capture_id not in new[0]["transaction_id"]
    assert new[0]["transaction_id"] != previous_id
    actual_tx = [row for row in second.frames if row["event"] == "write_attempt"]
    assert len(actual_tx) == 1
    assert actual_tx[0]["transaction_id"] == new[0]["transaction_id"]
    assert not any(row.get("transaction_id") == previous_id for row in second.frames)
    assert all(row["run_id"] == "abc12345" for row in second.events)
    assert all(row["scenario_id"] == "capture-B" for row in second.events)
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_new_capture_marks_untraced_pending_write_as_unattributed(
    monkeypatch, status_frame
):
    from tests.ha_stubs import FakeCharacteristic
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    hass, connection, _ = _api_case()
    client = make_client()
    fake = connect_fake(client)
    hass.config_entries.async_get_entry.return_value.runtime_data.client = client
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    client._status = status()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    await client.async_set_ac(True)
    original = client._pending_output_transaction
    assert original is not None and original.observation_id is None
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "unattributed",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    inherited = [r for r in capture.events if r["event"] == "transaction_inherited"]
    assert len(inherited) == 1
    assert inherited[0]["transaction_id"] is None
    assert inherited[0]["attribution"] == "unattributed"
    assert len(fake.writes) == 1 and not capture.frames
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert any(
        r["event"] == "transaction_confirmed" and r["transaction_id"] is None
        for r in capture.events
    )
    await client.async_set_ac(False)
    assert len(fake.writes) == 2
    assert any(
        r["event"] == "transaction_accepted"
        and r["transaction_id"].startswith("tx-")
        and r["transaction_id"].endswith("-1")
        and capture.capture_id not in r["transaction_id"]
        for r in capture.events
    )
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
async def test_attaching_trace_skips_already_confirmed_transaction():
    from custom_components.allpowers_ble.client import (
        PendingOutputTransaction,
        TransactionPhase,
    )
    from tests.test_client_runtime import make_client

    client = make_client()
    client._pending_output_transaction = PendingOutputTransaction(
        session_generation=1,
        source_version=0,
        target_dc=True,
        target_ac=True,
        target_light=True,
        sent_monotonic=1.0,
        confirm_deadline_monotonic=99999.0,
        phase=TransactionPhase.CONFIRMED,
        observation_id="tx-prior-1",
    )
    capture = new_capture()
    _attach_capture(client, capture)
    assert _capture_of(client) is capture
    assert not capture.events


@pytest.mark.asyncio
async def test_gatt_setup_first_write_failure_preserves_original_generation(
    monkeypatch,
):
    from custom_components.allpowers_ble import client as client_module
    from tests.ha_stubs import FakeBleakError, FakeClient, FakeDevice
    from tests.test_client_runtime import Services, make_client

    client = make_client()
    capture = new_capture()
    _attach_capture(client, capture)
    fake = FakeClient()
    fake.services = Services()
    write_count = []

    async def establish(*args, **kwargs):
        del args, kwargs
        return fake

    async def fail_write(*args, **kwargs):
        del args, kwargs
        write_count.append(True)
        raise FakeBleakError("PRIVATE address or adapter")

    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    monkeypatch.setattr(fake, "write_gatt_char", fail_write)
    with pytest.raises(FakeBleakError):
        await client._connect_once()
    kinds = (
        "connect_attempt",
        "connected",
        "write_error",
        "connect_failed",
        "disconnected",
    )
    records = [r for r in capture.events if r["event"] in kinds]
    assert [r["event"] for r in records] == list(kinds)
    generation = records[0]["session_generation"]
    assert generation is not None
    assert all(r["session_generation"] == generation for r in records)
    assert records[-1]["reason_category"] == "write_failure"
    assert len(write_count) == 1 and not fake.is_connected
    assert client._client is None and client._active_session_generation is None
    assert "PRIVATE" not in str(capture.snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "case", "reason"),
    [
        ("output", "unsupported", "unsupported_capability"),
        ("settings", "unsupported", "unsupported_capability"),
        ("keepalive", "unsupported", "unsupported_capability"),
        ("output", "stale", "stale_telemetry"),
        ("settings", "stale", "stale_telemetry"),
        ("keepalive", "stale", "stale_telemetry"),
        ("output", "disconnected", "disconnected"),
        ("settings", "disconnected", "disconnected"),
        ("keepalive", "disconnected", "disconnected"),
        ("output", "ambiguous", "ambiguous_state"),
        ("settings", "ambiguous", "ambiguous_state"),
        ("output", "pending", "confirmation_pending"),
        ("settings", "pending", "confirmation_pending"),
        ("keepalive", "pending", "confirmation_pending"),
        ("output", "timed_out", "confirmation_timed_out"),
        ("settings", "timed_out", "confirmation_timed_out"),
        ("output", "invalid", "semantic_validation"),
        ("settings", "invalid", "semantic_validation"),
    ],
)
async def test_pre_gatt_guards_record_safe_rejection_without_writes(
    monkeypatch, kind, case, reason
):
    from custom_components.allpowers_ble.client import (
        PendingOutputTransaction,
        PendingSettingsTransaction,
        TransactionPhase,
    )
    from custom_components.allpowers_ble.protocol import StateUnavailableError
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    now = client._loop_time()
    client._status = status()
    client._status_monotonic = now
    client._settings_monotonic = now
    client._session_hardware_revision = ("0.3", 3)
    capture = new_capture()
    _attach_capture(client, capture)
    if case == "unsupported":
        client._identity_conflicted = True
    elif case == "stale":
        if kind == "output":
            client._status_monotonic = now - client._options.stale_timeout - 1
        else:
            client._settings_monotonic = (
                now - client._options.settings_stale_timeout - 1
            )
    elif case == "disconnected":
        client._disconnect_event.set()
    elif case == "ambiguous":
        if kind == "output":
            client._output_blocked_until_version = client._status_version + 1
        else:
            client._settings_blocked_until_version = client._settings_version + 1
    elif case in ("pending", "timed_out"):
        deadline = now + 10 if case == "pending" else now - 1
        if kind == "output":
            client._pending_output_transaction = PendingOutputTransaction(
                session_generation=1,
                source_version=0,
                target_dc=True,
                target_ac=True,
                target_light=True,
                sent_monotonic=now - 20,
                confirm_deadline_monotonic=deadline,
                phase=TransactionPhase.AWAITING_CONFIRMATION,
            )
        else:
            client._pending_settings_transaction = PendingSettingsTransaction(
                session_generation=1,
                source_version=0,
                target=client._settings,
                sent_monotonic=now - 20,
                confirm_deadline_monotonic=deadline,
                phase=TransactionPhase.AWAITING_CONFIRMATION,
            )
    elif case == "invalid":
        if kind == "output":
            monkeypatch.setattr(
                "custom_components.allpowers_ble.client.status_write_validation_errors",
                lambda *_: ["private invalid state"],
            )
        else:
            monkeypatch.setattr(
                "custom_components.allpowers_ble.client.settings_write_validation_errors",
                lambda *_: ["private invalid state"],
            )
    operation = {
        "output": lambda: client.async_set_ac(True),
        "settings": lambda: client.async_set_eco(False),
        "keepalive": client.async_send_settings_keepalive,
    }[kind]
    with pytest.raises(StateUnavailableError):
        await operation()
    rejected = [row for row in capture.events if row["event"] == "control_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["control_kind"] == kind
    assert rejected[0]["reason"] == reason
    assert rejected[0]["session_generation"] == 1
    assert "private invalid state" not in str(capture.events)
    assert fake.writes == []
    transaction_rejected = [
        row for row in capture.events if row["event"] == "transaction_rejected"
    ]
    assert len(transaction_rejected) == (1 if case == "pending" else 0)


@pytest.mark.asyncio
async def test_optional_control_and_invalid_timeout_reject_before_gatt():
    from custom_components.allpowers_ble.protocol import StateUnavailableError
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    _attach_capture(client, capture)
    with pytest.raises(StateUnavailableError):
        await client.async_set_car_charger(True)
    assert not fake.writes
    assert capture.events[-1]["event"] == "control_rejected"
    assert capture.events[-1]["control_kind"] == "settings"
    assert capture.events[-1]["reason"] == "unsupported_capability"
    client._settings_monotonic = client._loop_time()
    with pytest.raises(ValueError):
        await client.async_set_eco_timeout(42)
    assert not fake.writes
    assert capture.events[-1]["event"] == "control_rejected"
    assert capture.events[-1]["reason"] == "semantic_validation"


@pytest.mark.asyncio
async def test_disabled_trace_does_not_add_pre_write_guard_observations():
    from custom_components.allpowers_ble.protocol import StateUnavailableError
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    assert _capture_of(client) is None
    with pytest.raises(StateUnavailableError):
        await client.async_set_ac(True)
    assert fake.writes == []
    assert _capture_of(client) is None


def test_transaction_namespace_is_distinct_from_private_capture_capability():
    first = new_capture()
    second = new_capture()
    first_tx = first.new_transaction_id()
    second_tx = second.new_transaction_id()
    assert first_tx != second_tx
    assert first.capture_id not in first_tx
    assert second.capture_id not in second_tx
    assert first.new_transaction_id() != first_tx


@pytest.mark.asyncio
@pytest.mark.parametrize("still_attached", [True, False])
async def test_new_start_forgets_expired_active_capture_without_erasing_replacement(
    still_attached,
):
    hass, connection, client = _api_case()
    start = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abc12345",
        "scenario_id": "expired-a",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, start)
    old = _capture_of(client)
    old.record("notification_received", frame=b"secret", direction="rx")
    old.started -= 121
    if not still_attached:
        _detach_capture(client, _capture_of(client))
    await ws_start(hass, connection, {**start, "id": 2, "scenario_id": "new-b"})
    new = _capture_of(client)
    assert new is not None and new is not old
    assert old.frames == [] and old._expiry_handle is None
    assert hass.data["allpowers_ble_hil_capture"].active is new
    clear_entry_capture(hass, "entry")
    assert not hass.data


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["output", "settings"])
@pytest.mark.parametrize("outcome", ["success", "error", "repeated", "contradiction"])
async def test_trace_inflight_matching_rx_cannot_confirm_failed_gatt_write(
    monkeypatch, notification_builder, kind, outcome
):
    """Only a successfully returned GATT call may make an early match terminal."""
    from tests.ha_stubs import FakeCharacteristic
    from tests.test_client_logic import _settings, _status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    client._status = _status()
    client._settings = _settings()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    capture = new_capture()
    _attach_capture(client, capture)
    is_output = kind == "output"
    matching = notification_builder(
        0x01 if is_output else 0x03,
        bytes((0x12, 50, 0, 100, 0, 25, 0, 90))
        if is_output
        else bytes((0xA1, 2, 0, 0, 0x03, 0x11)),
    )
    contradictory = notification_builder(
        0x01 if is_output else 0x03,
        bytes((0x10, 50, 0, 100, 0, 25, 0, 90))
        if is_output
        else bytes((0xA0, 2, 0, 0, 0x03, 0x11)),
    )
    original_write = fake.write_gatt_char

    async def inject_during_write(*args, **kwargs):
        await original_write(*args, **kwargs)
        client._notification_handler(FakeCharacteristic(), bytearray(matching))
        if outcome in ("repeated", "contradiction"):
            client._notification_handler(FakeCharacteristic(), bytearray(matching))
        if outcome == "contradiction":
            client._notification_handler(FakeCharacteristic(), bytearray(contradictory))
        # Any provisional match is observed before write submission completes.
        assert not any(r["event"] == "transaction_confirmed" for r in capture.events)
        if outcome == "error":
            raise RuntimeError("private failed write")

    monkeypatch.setattr(fake, "write_gatt_char", inject_during_write)
    command = client.async_set_ac if is_output else client.async_set_eco
    if outcome == "error":
        with pytest.raises(RuntimeError):
            await command(True)
    else:
        await command(True)

    tx = [r for r in capture.events if r["event"].startswith("transaction_")]
    identifier = tx[0]["transaction_id"]
    assert identifier is not None
    assert all(r["transaction_id"] == identifier for r in tx)
    assert tx[0]["event"] == "transaction_accepted"
    assert len(fake.writes) == 1
    provisional = [
        r for r in capture.events if r["event"] == "telemetry_match_pending_submission"
    ]
    assert len(provisional) == 1
    assert provisional[0]["transaction_id"] == identifier
    assert provisional[0]["seq"] < next(
        r["seq"]
        for r in capture.events
        if r["event"] in ("transaction_submitted", "transaction_failed")
    )
    confirmations = [r for r in tx if r["event"] == "transaction_confirmed"]
    if outcome in ("success", "repeated"):
        assert [r["event"] for r in tx] == [
            "transaction_accepted",
            "transaction_submitted",
            "transaction_confirmed",
        ]
        assert len(confirmations) == 1
        assert (
            client._pending_output_transaction
            if is_output
            else client._pending_settings_transaction
        ) is None
    elif outcome == "contradiction":
        assert [r["event"] for r in tx] == [
            "transaction_accepted",
            "transaction_submitted",
        ]
        assert not confirmations
        assert any(r["event"] == "telemetry_mismatch" for r in capture.events)
        pending = (
            client._pending_output_transaction
            if is_output
            else client._pending_settings_transaction
        )
        assert pending is not None and pending.phase.name == "AWAITING_CONFIRMATION"
    else:
        assert [r["event"] for r in tx] == [
            "transaction_accepted",
            "transaction_failed",
        ]
        assert not confirmations
        assert (
            client._pending_output_transaction
            if is_output
            else client._pending_settings_transaction
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["status", "output"])
@pytest.mark.parametrize(
    ("failure", "exception_type", "reason"),
    [
        ("runtime", RuntimeError, "unexpected_failure"),
        ("cancel", asyncio.CancelledError, "cancelled"),
        ("generation", Exception, "session_changed"),
        ("timeout", TimeoutError, "transport_failure"),
    ],
)
async def test_all_attempted_gatt_failures_have_one_safe_terminal_error(
    monkeypatch, kind, failure, exception_type, reason
):
    """A failed/cancelled session must never emit write_ok or change retry policy."""
    from custom_components.allpowers_ble.client import NotConnectedError
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    client._status = status()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    capture = new_capture()
    _attach_capture(client, capture)
    old_generation = client._active_session_generation
    original_write = fake.write_gatt_char

    async def faulty_write(*args, **kwargs):
        await original_write(*args, **kwargs)
        if failure == "generation":
            client._active_session_generation = old_generation + 1
        elif failure == "runtime":
            raise RuntimeError("private device addr")
        elif failure == "cancel":
            raise asyncio.CancelledError()
        elif failure == "timeout":
            raise TimeoutError("private IP address")

    monkeypatch.setattr(fake, "write_gatt_char", faulty_write)
    command = (
        client.async_request_status
        if kind == "status"
        else (lambda: client.async_set_ac(True))
    )
    expected = NotConnectedError if failure == "generation" else exception_type
    with pytest.raises(expected):
        await command()
    assert len(fake.writes) == 1
    attempts = [r for r in capture.events if r["event"] == "write_attempt"]
    errors = [r for r in capture.events if r["event"] == "write_error"]
    assert len(attempts) == len(errors) == 1
    assert errors[0]["seq"] > attempts[0]["seq"]
    assert errors[0]["session_generation"] == attempts[0]["session_generation"]
    assert errors[0]["session_generation"] == old_generation
    assert errors[0]["transaction_id"] == attempts[0]["transaction_id"]
    assert errors[0]["reason_category"] == reason
    assert errors[0]["error_type"] == expected.__name__
    for secret in ("private device addr", "private IP address"):
        assert secret not in str(capture.snapshot())
    assert not any(r["event"] == "write_ok" for r in capture.events)
    if kind == "output":
        assert attempts[0]["transaction_id"] is not None
        assert any(
            r["event"] == "transaction_failed"
            and r["transaction_id"] == attempts[0]["transaction_id"]
            for r in capture.events
        )
    else:
        assert attempts[0]["transaction_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["loaded", "runtime_unloaded", "entry_removed"])
async def test_expired_active_capture_detaches_own_entry_not_new_entry(origin):
    hass, connection, first_client = _api_case()
    first_entry = hass.config_entries.async_get_entry.return_value
    second_client = _CaptureClient(8)
    second_entry = SimpleNamespace(
        entry_id="entry-B",
        domain="allpowers_ble",
        runtime_data=SimpleNamespace(client=second_client),
    )
    registry = {"entry": first_entry, "entry-B": second_entry}
    hass.config_entries.async_get_entry.side_effect = registry.get
    request = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abcdefgh",
        "scenario_id": "first",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, request)
    first_capture = _capture_of(first_client)
    first_capture.record("notification_received", frame=b"A-private", direction="rx")
    first_timer = first_capture._expiry_handle
    expiry_callback = first_timer._callback
    first_capture.started -= 121
    if origin == "runtime_unloaded":
        first_entry.runtime_data = None
    elif origin == "entry_removed":
        registry.pop("entry")
    await ws_start(
        hass,
        connection,
        {**request, "id": 2, "entry_id": "entry-B", "scenario_id": "second"},
    )
    second_capture = _capture_of(second_client)
    assert second_capture is not None and second_capture is not first_capture
    assert second_capture.entry_id == "entry-B"
    if origin == "loaded":
        assert _capture_of(first_client) is None
    assert first_capture.events == [] and first_capture.frames == []
    assert first_timer.cancelled()
    store = hass.data["allpowers_ble_hil_capture"]
    assert store.active is second_capture
    expiry_callback()
    assert _capture_of(second_client) is second_capture
    assert store.active is second_capture
    await ws_fetch(
        hass,
        connection,
        {"id": 3, "entry_id": "entry", "capture_id": first_capture.capture_id},
    )
    connection.send_error.assert_called()
    await ws_start(
        hass,
        connection,
        {**request, "id": 4, "entry_id": "entry", "scenario_id": "busy-test"},
    )
    # A live B must continue owning the one active slot.
    assert store.active is second_capture
    assert _capture_of(second_client) is second_capture
    clear_entry_capture(hass, "entry-B")
    assert not hass.data


@pytest.mark.asyncio
async def test_start_for_other_entry_rejects_busy_active_capture():
    hass, connection, first_client = _api_case()
    first_entry = hass.config_entries.async_get_entry.return_value
    second_client = _CaptureClient(8)
    second_entry = SimpleNamespace(
        entry_id="entry-B",
        domain="allpowers_ble",
        runtime_data=SimpleNamespace(client=second_client),
    )
    hass.config_entries.async_get_entry.side_effect = {
        "entry": first_entry,
        "entry-B": second_entry,
    }.get
    request = {
        "id": 1,
        "entry_id": "entry",
        "run_id": "abcdefgh",
        "scenario_id": "first",
        "requested_route": "auto",
    }
    await ws_start(hass, connection, request)
    active = _capture_of(first_client)
    await ws_start(
        hass,
        connection,
        {**request, "id": 2, "entry_id": "entry-B", "scenario_id": "second"},
    )
    assert _capture_of(first_client) is active
    assert _capture_of(second_client) is None
    assert connection.send_error.call_args.args[1] == "busy"
    clear_entry_capture(hass, "entry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_method",
    [
        "notification_received",
        "transaction_runtime",
        "finalize_runtime",
        "new_transaction_id",
    ],
)
async def test_observer_loss_is_nonfatal_but_capture_is_incomplete(
    monkeypatch, status_frame, failed_method
):
    """Every lost callback or correlation ID invalidates evidence integrity."""
    from scripts.hil.cli import _validate_trace_result
    from tests.ha_stubs import FakeCharacteristic
    from tests.helpers import status
    from tests.test_client_runtime import connect_fake, make_client

    client = make_client()
    fake = connect_fake(client)
    capture = new_capture()
    observer = _attach_capture(client, capture)
    client._status = status()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)

    # Valid, live GATT bytes must never mask an unrecorded observer failure.
    await client.async_request_status()
    assert len(fake.writes) == 1 and capture.frames

    def broken(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("PRIVATE:AA:BB:CC")

    original = getattr(observer, failed_method)

    def once_broken(*args, **kwargs):
        if failed_method == "transaction_runtime" and args[0] != "transaction_accepted":
            return original(*args, **kwargs)
        return broken(*args, **kwargs)

    monkeypatch.setattr(observer, failed_method, once_broken)
    if failed_method == "notification_received":
        client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
        assert client.snapshot().status == status(ac_enabled=True, raw_flags=0x13)
        assert client.snapshot().statistics.valid_packets == 1
    elif failed_method == "finalize_runtime":
        client.finalize_observer(observer)
    else:
        await client.async_set_ac(True)
        assert len(fake.writes) == 2
        assert client._pending_output_transaction is not None
        if failed_method == "new_transaction_id":
            assert client._pending_output_transaction.observation_id is None
        else:
            assert client._pending_output_transaction.observation_id is not None

    assert capture.dropped_events == 1
    assert capture.dropped_frames == 0
    capture.record("capture_stopped")
    capture.stop("requested_stop")
    result = capture.snapshot()
    assert result["termination_reason"] == "requested_stop"
    assert result["frame_count"] >= 1
    assert result["dropped_events"] == 1
    assert result["complete"] is False
    assert result["qualification_allowed"] is False
    _validate_trace_result(
        result,
        run_id=capture.run_id,
        scenario_id=capture.scenario_id,
        capture_id=capture.capture_id,
        requested_route=capture.requested_route,
    )
    assert "PRIVATE" not in str(result)


@pytest.mark.asyncio
async def test_mutating_generic_observer_cannot_change_notification_decoder(
    monkeypatch, status_frame, settings_frame
):
    """Only an immutable copy reaches observers; the decoder keeps live input."""
    from unittest.mock import MagicMock

    from custom_components.allpowers_ble import client as client_module
    from custom_components.allpowers_ble.client_observer import ClientObserver
    from tests.ha_stubs import FakeCharacteristic
    from tests.test_client_runtime import connect_fake, make_client

    with_observer = make_client()
    without_observer = make_client()
    observed_gatt = connect_fake(with_observer)
    plain_gatt = connect_fake(without_observer)
    mutator = MagicMock(spec=ClientObserver)
    received = []

    def try_mutate(_generation, data):
        received.append(data)
        assert type(data) is bytes
        with pytest.raises(TypeError):
            data[0] = 0  # type: ignore[index]

    mutator.notification_received.side_effect = try_mutate
    with_observer.attach_observer(mutator)

    for frame in (status_frame, settings_frame):
        with_observer._notification_handler(FakeCharacteristic(), bytearray(frame))
    assert received == [status_frame, settings_frame]

    copied = []
    builtin_bytes = bytes

    def copy_spy(data):
        copied.append(len(data))
        return builtin_bytes(data)

    monkeypatch.setattr(client_module, "bytes", copy_spy, raising=False)
    for frame in (status_frame, settings_frame):
        without_observer._notification_handler(FakeCharacteristic(), bytearray(frame))
    assert copied == [], "Disabled observers must not copy notification chunks"
    assert with_observer._status == without_observer._status
    assert with_observer._settings == without_observer._settings
    assert with_observer._statistics == without_observer._statistics
    assert observed_gatt.writes == plain_gatt.writes == []
    mutator.observation_failed.assert_not_called()


@pytest.mark.asyncio
async def test_v1_golden_real_ble_client_path_from_pre_refactor_contract(
    monkeypatch, notification_builder, status_frame
):
    """Fixed v1 witness: connect, GATT TX, mismatch, match, disconnect and stop."""
    from custom_components.allpowers_ble import client as client_module
    from tests.ha_stubs import FakeCharacteristic, FakeClient, FakeDevice
    from tests.helpers import status
    from tests.test_client_runtime import Services, make_client

    hass, connection, _ = _api_case()
    client = make_client()
    hass.config_entries.async_get_entry.return_value.runtime_data.client = client
    fake = FakeClient()
    fake.services = Services()

    async def establish(*args, **kwargs):
        del args, kwargs
        return current_gatt[0]

    current_gatt = [fake]
    monkeypatch.setattr(client_module, "establish_connection", establish)
    monkeypatch.setattr(client, "_fresh_ble_device", lambda: FakeDevice())
    await ws_start(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry",
            "run_id": "abc12345",
            "scenario_id": "golden-client-v1",
            "requested_route": "auto",
        },
    )
    capture = _capture_of(client)
    assert capture is not None
    await client._connect_once()
    client._status = status()
    client._status_monotonic = client._loop_time()
    client._settings_monotonic = client._loop_time()
    monkeypatch.setattr(client, "_schedule_status_refresh", lambda: None)
    await client.async_set_ac(True)

    # Fixed v1 inputs rather than expectations computed from the new tracer.
    mismatch = notification_builder(0x01, bytes((0x11, 73, 1, 44, 0, 150, 0, 120)))
    client._notification_handler(FakeCharacteristic(), bytearray(mismatch))
    client._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert client._pending_output_transaction is None

    client._disconnect_reason_category = "manual"
    await client._disconnect_client()
    await ws_stop(
        hass,
        connection,
        {"id": 2, "entry_id": "entry", "capture_id": capture.capture_id},
    )
    rows = capture.events
    expected_events = [
        ("capture_started", None),
        ("connect_attempt", 1),
        ("model_profile_selected", 1),
        ("route_observed", 1),
        ("connected", 1),
        ("write_attempt", 1),
        ("write_ok", 1),
        ("transaction_accepted", 1),
        ("write_attempt", 1),
        ("write_ok", 1),
        ("transaction_submitted", 1),
        ("notification_received", 1),
        ("telemetry_mismatch", 1),
        ("notification_decoded", 1),
        ("notification_received", 1),
        ("transaction_confirmed", 1),
        ("notification_decoded", 1),
        ("disconnected", 1),
        ("capture_stopped", None),
    ]
    assert [(r["event"], r["session_generation"]) for r in rows] == expected_events
    assert [r["seq"] for r in rows] == list(range(1, 20))

    tx_id = rows[7]["transaction_id"]
    assert isinstance(tx_id, str) and tx_id.startswith("tx-")
    assert tx_id.endswith("-1")
    assert [rows[i]["transaction_id"] for i in (7, 8, 9, 10, 12, 15)] == [tx_id] * 6
    assert [
        (rows[i]["event"], rows[i]["transaction_phase"], rows[i]["reason"])
        for i in (7, 10, 12, 15)
    ] == [
        ("transaction_accepted", "sending", None),
        ("transaction_submitted", "sending", None),
        (
            "telemetry_mismatch",
            "awaiting_confirmation",
            "unconfirmed_or_contradictory",
        ),
        (
            "transaction_confirmed",
            "awaiting_confirmation",
            "matching_validated_telemetry",
        ),
    ]
    assert rows[2]["inferred_model_profile"] == "r600-unverified-revision"
    assert rows[17]["reason_category"] == "manual"

    # Externally fixed v1 byte vectors, including XOR checksum and sequence joins.
    assert [
        (frame["seq"], frame["event"], frame["hex"], frame.get("transaction_id"))
        for frame in capture.frames
    ] == [
        (6, "write_attempt", "a565b1000106010000000000", None),
        (9, "write_attempt", "a56500b10101002352", tx_id),
        (12, "notification_received", "a56500000008011149012c0096007852", None),
        (15, "notification_received", "a56500000008011349012c0096007850", None),
    ]
    for frame in capture.frames:
        matching_event = rows[frame["seq"] - 1]
        assert frame["event"] == matching_event["event"]
        assert frame.get("transaction_id") == matching_event.get("transaction_id")
    assert [f["command_type"] for f in capture.frames] == [
        "status_request",
        "output_control",
        "unknown",
        "unknown",
    ]
    result = connection.send_result.call_args.args[1]
    assert result["schema_version"] == 2
    assert result["event_count"] == 19
    assert result["frame_count"] == 4
    assert result["complete"] is True
    assert result["termination_reason"] == "requested_stop"
    assert result["dropped_events"] == result["dropped_frames"] == 0

    # Repeat actual connect/write/RX on an unobserved client: same traffic, no copies.
    plain = make_client()
    fresh = FakeClient()
    fresh.services = Services()
    current_gatt[0] = fresh
    monkeypatch.setattr(plain, "_fresh_ble_device", lambda: FakeDevice())
    await plain._connect_once()
    plain._status = status()
    plain._status_monotonic = plain._loop_time()
    plain._settings_monotonic = plain._loop_time()
    monkeypatch.setattr(plain, "_schedule_status_refresh", lambda: None)
    await plain.async_set_ac(True)

    copied = []
    real_bytes = bytes

    def count_copies(data):
        copied.append(len(data))
        return real_bytes(data)

    monkeypatch.setattr(client_module, "bytes", count_copies, raising=False)
    plain._notification_handler(FakeCharacteristic(), bytearray(mismatch))
    plain._notification_handler(FakeCharacteristic(), bytearray(status_frame))
    assert copied == []
    assert fresh.writes == fake.writes
    assert plain.snapshot().status == client.snapshot().status
    assert plain.snapshot().statistics.notifications == 2
    assert client.snapshot().statistics.notifications == 2
    assert plain._pending_output_transaction is None
    plain._disconnect_reason_category = "manual"
    await plain._disconnect_client()
    assert plain.snapshot().statistics.disconnects == 1
    clear_entry_capture(hass, "entry")
    assert "allpowers_ble_hil_capture" not in hass.data
