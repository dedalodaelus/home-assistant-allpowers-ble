"""Route proof follows the established transport, never advertisement candidates."""

from __future__ import annotations

import pytest

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.hil_client import HilClientTracer
from custom_components.allpowers_ble.hil_trace import Capture
from custom_components.allpowers_ble.route_attribution import ConnectedRoute
from tests.ha_stubs import FakeBleakError, FakeClient, FakeDevice
from tests.helpers import FakeHass
from tests.test_client_runtime import ProbeClient, Services, make_client


LOCAL = ConnectedRoute("local_adapter", "bluez", "bluez_device_path", adapter_id="hci1")
PROXY = ConnectedRoute(
    "active_proxy",
    "esphome",
    "esphome_scanner_binding",
    proxy_id="PRIVATE-PROXY",
)
UNKNOWN = ConnectedRoute(evidence="no_ha_wrapper")


def _trace() -> tuple[Capture, HilClientTracer]:
    capture = Capture("entry", "owner", "run12345", "route", "local_adapter")
    return capture, HilClientTracer(capture)


@pytest.mark.asyncio
async def test_runtime_and_probe_use_late_patched_client_class(
    monkeypatch, status_frame
):
    """Both actual connection calls use the class patched after module import."""
    patched_cls = type("LatePatchedClass", (), {})
    monkeypatch.setattr(client_module.brc, "BleakClientWithServiceCache", patched_cls)

    fake = FakeClient()
    fake.services = Services()
    seen = []

    async def runtime_connect(cls, *args, **kwargs):
        del args, kwargs
        seen.append(cls)
        return fake

    monkeypatch.setattr(client_module, "establish_connection", runtime_connect)
    client = make_client()
    monkeypatch.setattr(client, "_fresh_ble_device", FakeDevice)
    await client._connect_once()
    assert seen == [patched_cls]
    await client._disconnect_client()

    probe = ProbeClient([status_frame])

    async def probe_connect(cls, *args, **kwargs):
        del args, kwargs
        seen.append(cls)
        return probe

    monkeypatch.setattr(client_module, "establish_connection", probe_connect)
    monkeypatch.setattr(
        client_module.bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: FakeDevice(),
    )
    result = await client_module.async_probe_device(
        FakeHass(),  # type: ignore[arg-type]
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="ALLPOWERS R600",
        timeout=0.1,
    )
    assert result.status.battery_percent == 73
    assert seen == [patched_cls, patched_cls]
    assert len(fake.writes) == 1
    assert len(probe.writes) == 1


@pytest.mark.asyncio
async def test_route_on_success_reconnect_updates_generation_not_prior_rows(
    monkeypatch,
):
    """Local→proxy is mixed; the first generation's frames remain local."""
    client = make_client()
    capture, tracer = _trace()
    client.attach_observer(tracer)
    monkeypatch.setattr(client, "_fresh_ble_device", FakeDevice)
    choices = iter((LOCAL, PROXY))
    monkeypatch.setattr(client_module, "connected_route", lambda _: next(choices))

    async def connect(*args, **kwargs):
        del args, kwargs
        c = FakeClient()
        c.services = Services()
        return c

    monkeypatch.setattr(client_module, "establish_connection", connect)
    await client._connect_once()
    first_generation = client._active_session_generation
    assert first_generation == 1
    assert capture.snapshot()["observed_route"] == "local_adapter"
    first_frames = [row.copy() for row in capture.frames]
    assert len(first_frames) == 1
    assert first_frames[0]["observed_route"] == "local_adapter"
    await client._disconnect_client()

    await client._connect_once()
    second_generation = client._active_session_generation
    assert second_generation == 2
    assert capture.snapshot()["observed_route"] == "mixed"
    assert capture.frames[0] == first_frames[0]
    assert capture.frames[-1]["observed_route"] == "active_proxy"
    assert [r["session_generation"] for r in capture.route_observations.values()] == [
        first_generation,
        second_generation,
    ]
    assert capture.snapshot()["qualification_allowed"] is False
    assert "PRIVATE-PROXY" not in str(capture.events)
    assert "PRIVATE-PROXY" not in str(capture.frames)
    assert capture.route_observations[2]["proxy_id"] == "PRIVATE-PROXY"
    await client._disconnect_client()
    capture.erase()
    assert not capture.route_observations


@pytest.mark.asyncio
async def test_failed_attempt_not_connected_route_and_unknown_generation_veto(
    monkeypatch,
):
    """A failed local attempt has no proof; unknown connected generation vetoes."""
    client = make_client()
    capture, tracer = _trace()
    client.attach_observer(tracer)
    monkeypatch.setattr(client, "_fresh_ble_device", FakeDevice)
    attempts = 0

    async def connect(*args, **kwargs):
        nonlocal attempts
        del args, kwargs
        attempts += 1
        if attempts == 1:
            raise FakeBleakError("PRIVATE DEVICE")
        fake = FakeClient()
        fake.services = Services()
        return fake

    monkeypatch.setattr(client_module, "establish_connection", connect)
    routes = iter((PROXY, UNKNOWN))
    monkeypatch.setattr(client_module, "connected_route", lambda _: next(routes))
    with pytest.raises(FakeBleakError):
        await client._connect_once()
    assert capture.route_observations == {}
    await client._connect_once()
    assert capture.snapshot()["observed_route"] == "active_proxy"
    assert list(capture.route_observations) == [2]
    await client._disconnect_client()
    await client._connect_once()
    assert capture.snapshot()["observed_route"] == "unverified"
    assert list(capture.route_observations) == [2, 3]
    assert all("PRIVATE DEVICE" not in str(row) for row in capture.events)
    await client._disconnect_client()


@pytest.mark.asyncio
async def test_capture_started_mid_connection_reuses_snapshot_without_ble_io(
    monkeypatch,
):
    """Attaching a capture only observes the existing session, no reconnect/TX."""
    client = make_client()
    monkeypatch.setattr(client, "_fresh_ble_device", FakeDevice)
    monkeypatch.setattr(client_module, "connected_route", lambda _: PROXY)
    fake = FakeClient()
    fake.services = Services()
    calls = []

    async def connect(*args, **kwargs):
        del args, kwargs
        calls.append("establish")
        return fake

    monkeypatch.setattr(client_module, "establish_connection", connect)
    await client._connect_once()
    assert calls == ["establish"]
    writes = list(fake.writes)
    assert len(writes) == 1
    capture, tracer = _trace()
    capture.record("capture_started", session_generation=1)
    assert capture.events[0]["observed_route"] == "unverified"
    client.attach_observer(tracer)
    assert calls == ["establish"]
    assert fake.writes == writes
    assert capture.events[1]["event"] == "route_observed"
    assert capture.events[1]["observed_route"] == "active_proxy"
    assert capture.snapshot()["observed_route"] == "active_proxy"
    assert capture.snapshot()["requested_route"] == "local_adapter"
    assert capture.snapshot()["qualification_allowed"] is False
    await client._disconnect_client()


def test_route_capture_sealed_and_summary_cannot_be_promoted_by_failed_attempt():
    """Explicitly avoid using the requested route as connected-route proof."""
    capture, tracer = _trace()
    assert capture.summary_route([]) == "unverified"
    assert capture.summary_route(["local_adapter", "unverified"]) == "unverified"
    assert capture.summary_route(["local_adapter", "active_proxy"]) == "mixed"
    assert capture.summary_route(["local_adapter", "local_adapter"]) == "local_adapter"
    assert capture.summary_route(["active_proxy"]) == "active_proxy"
    tracer.route_connected(1, LOCAL)
    rows = len(capture.events)
    tracer.route_connected(1, LOCAL)
    assert len(capture.events) == rows
    tracer.route_connected(1, PROXY)
    assert capture.dropped_events == 1
    assert capture.snapshot()["complete"] is False
    capture.stop("runtime_stopped")
    tracer.route_connected(2, PROXY)
    assert len(capture.route_observations) == 1
    capture.erase()


def test_route_observation_full_buffer_cannot_fabricate_complete_capture():
    """Truncated route evidence fails closed; private proxy identifier is erased."""
    from custom_components.allpowers_ble.hil_trace import MAX_EVENTS

    capture, tracer = _trace()
    for _ in range(MAX_EVENTS):
        capture.record("filler")
    tracer.route_connected(12, PROXY)
    assert capture.dropped_events == 1
    assert capture.route_observations[12]["proxy_id"] == "PRIVATE-PROXY"
    capture.stop()
    assert capture.snapshot()["complete"] is False
    capture.erase()
    assert not capture.route_observations
