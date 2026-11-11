"""End-to-end HIL write endpoint tests with a synthetic profile and fake BLE."""

from __future__ import annotations

from time import monotonic
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

import custom_components.allpowers_ble as integration
from custom_components.allpowers_ble import hil_write
from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import AllpowersBLEClient
from custom_components.allpowers_ble.const import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID
from custom_components.allpowers_ble.model_support import ModelFeature, PhysicalPresence
from custom_components.allpowers_ble.options import ConnectionOptions
from custom_components.allpowers_ble.protocol import StatusData, encode_output_control

pytestmark = pytest.mark.homeassistant


async def _load_entry(hass, monkeypatch, runtime):
    """Load a controlled runtime through HA's config-entry lifecycle."""

    async def setup_entry(_hass, entry):
        entry.runtime_data = runtime
        return True

    monkeypatch.setattr(integration, "async_setup_entry", setup_entry)
    entry = MockConfigEntry(domain="allpowers_ble", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry


def _candidate_status(enabled: bool) -> StatusData:
    flags = 0x10 | int(enabled)
    return StatusData(
        dc_enabled=enabled,
        ac_enabled=False,
        light_enabled=True,
        battery_percent=50,
        input_power_w=100,
        output_power_w=25,
        remaining_minutes=90,
        raw_flags=flags,
    )


class _FakeGatt:
    is_connected = True

    def __init__(self, client: AllpowersBLEClient, frame: bytes) -> None:
        self.client = client
        self.frame = frame
        self.writes: list[bytes] = []

    async def write_gatt_char(
        self, _characteristic, data: bytes, *, response: bool
    ) -> None:
        del response
        self.writes.append(bytes(data))
        self.client._notification_handler(SimpleNamespace(), bytearray(self.frame))


@pytest.mark.asyncio
async def test_admin_websocket_grant_execute_confirm_revoke_fake_ble(
    hass: HomeAssistant,
    enable_custom_integrations,
    hass_ws_client,
    hass_admin_user,
    notification_builder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthetic exact-profile candidate completes one confirmed action."""
    del enable_custom_integrations
    support = SimpleNamespace(
        model="SYNTHETIC TEST MODEL", profile="synthetic-output-v1"
    )

    def validator(profile: str, status: object) -> bool:
        return (
            profile == "synthetic-output-v1"
            and isinstance(status, StatusData)
            and status.raw_flags & ~0x11 == 0
        )

    policy = hil_write.CandidatePolicy(
        model=support.model,
        hardware="9.1",
        firmware="test-fw",
        profile="synthetic-output-v1",
        feature=ModelFeature.DC_OUTPUT,
        gatt_contract=(SERVICE_UUID, WRITE_UUID, NOTIFY_UUID),
        physical_presence=PhysicalPresence.PRESENT,
        semantic_validator=validator,
    )
    monkeypatch.setattr(
        hil_write,
        "CANDIDATE_POLICIES",
        {(support.model, "9.1", "test-fw", ModelFeature.DC_OUTPUT): policy},
    )
    monkeypatch.setattr(
        hil_write, "runtime_model_support", lambda _coordinator: support
    )
    eligible = SimpleNamespace(
        hil_candidate_eligible=True, physical_presence=PhysicalPresence.PRESENT
    )
    monkeypatch.setattr(
        hil_write, "model_feature_capability", lambda _support, _feature: eligible
    )
    monkeypatch.setattr(
        client_module, "model_feature_capability", lambda _support, _feature: eligible
    )

    client = AllpowersBLEClient(
        hass=hass,
        address="AA:BB:CC:DD:EE:FF",
        advertised_name="synthetic test fixture",
        options=ConnectionOptions(),
    )
    client._connected = True
    client._runtime_model_support = lambda: support
    client._active_session_generation = 17
    client._session_hardware_revision = ("9.1", 0x91)
    client._settings = SimpleNamespace(firmware_version="test-fw")
    client._settings_monotonic = monotonic()
    client._status = _candidate_status(False)
    client._status_monotonic = monotonic()
    client._connected_route = (
        17,
        SimpleNamespace(observed_route="local_adapter"),
    )
    client._write_characteristic = SimpleNamespace(
        properties=["write-without-response"]
    )
    client._schedule_status_refresh = lambda: None
    notification = notification_builder(0x01, bytes((0x11, 50, 0, 100, 0, 25, 0, 90)))
    fake_gatt = _FakeGatt(client, notification)
    client._client = fake_gatt

    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            session_hardware_version="9.1",
            settings=client._settings,
            identity_conflicted=False,
        ),
        settings_are_fresh=True,
        status_is_fresh=True,
        connected=True,
    )
    entry = await _load_entry(
        hass, monkeypatch, SimpleNamespace(client=client, coordinator=coordinator)
    )

    await hil_write.async_register(hass)
    ws = await hass_ws_client()
    authorize = {
        "type": "allpowers_ble/hil/write/authorize",
        "version": 1,
        "entry_id": entry.entry_id,
        "feature": "dc_output",
        "target": True,
        "confirmed": True,
    }
    await ws.send_json_auto_id({**authorize, "target": False})
    revocable = await ws.receive_json()
    assert revocable["success"] is True
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/revoke",
            "version": 1,
            "token": revocable["result"]["token"],
        }
    )
    revoked_unused = await ws.receive_json()
    assert revoked_unused["success"] is True
    assert revoked_unused["result"]["revoked"] is True

    await ws.send_json_auto_id(authorize)
    granted = await ws.receive_json()
    assert granted["success"] is True
    token = granted["result"]["token"]

    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/execute",
            "version": 1,
            "entry_id": entry.entry_id,
            "token": token,
            "confirmed": True,
        }
    )
    executed = await ws.receive_json()
    assert executed["success"] is True
    assert executed["result"]["outcome"] == "confirmed"
    assert fake_gatt.writes == [encode_output_control(dc=True, ac=False, light=True)]
    assert client._pending_output_transaction is None
    assert not hil_write.store(hass).grants
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/execute",
            "version": 1,
            "entry_id": entry.entry_id,
            "token": token,
            "confirmed": True,
        }
    )
    replay = await ws.receive_json()
    assert replay["success"] is False
    assert replay["error"]["code"] == "invalid_grant"
    assert len(fake_gatt.writes) == 1
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/revoke",
            "version": 1,
            "token": token,
        }
    )
    revoked = await ws.receive_json()
    assert revoked["success"] is True
    assert revoked["result"]["revoked"] is False

    await ws.send_json_auto_id(authorize)
    unconsumed = await ws.receive_json()
    stale_token = unconsumed["result"]["token"]
    assert stale_token in hil_write.store(hass).grants
    await ws.close()
    assert stale_token not in hil_write.store(hass).grants

    next_ws = await hass_ws_client()
    await next_ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/execute",
            "version": 1,
            "entry_id": entry.entry_id,
            "token": stale_token,
            "confirmed": True,
        }
    )
    stale_execution = await next_ws.receive_json()
    assert stale_execution["success"] is False
    assert stale_execution["error"]["code"] == "invalid_grant"
    assert fake_gatt.writes == [encode_output_control(dc=True, ac=False, light=True)]
    await next_ws.close()


@pytest.mark.asyncio
async def test_unauthorized_candidate_denial_has_zero_ble_tx(
    hass: HomeAssistant,
    enable_custom_integrations,
    hass_ws_client,
    hass_admin_user,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production candidate policy stays empty and denial never touches BLE."""
    del enable_custom_integrations
    monkeypatch.setattr(hil_write, "CANDIDATE_POLICIES", {})
    fake_gatt = SimpleNamespace(writes=[])
    client = SimpleNamespace(
        active_session_generation=1,
        is_connected=True,
        has_pending_transaction=False,
        _connected_route=(1, SimpleNamespace(observed_route="local_adapter")),
        fake_gatt=fake_gatt,
    )
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            session_hardware_version="9.1", settings=None, identity_conflicted=False
        ),
        settings_are_fresh=False,
        status_is_fresh=True,
        connected=True,
    )
    entry = await _load_entry(
        hass, monkeypatch, SimpleNamespace(client=client, coordinator=coordinator)
    )
    monkeypatch.setattr(
        hil_write,
        "runtime_model_support",
        lambda _coordinator: SimpleNamespace(model="SYNTHETIC TEST MODEL"),
    )
    await hil_write.async_register(hass)
    ws = await hass_ws_client()
    saved_groups = hass_admin_user.groups
    hass_admin_user.groups = []
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/authorize",
            "version": 1,
            "entry_id": entry.entry_id,
            "feature": "dc_output",
            "target": True,
            "confirmed": True,
        }
    )
    unauthorized = await ws.receive_json()
    assert unauthorized["success"] is False
    assert unauthorized["error"]["code"] == "unauthorized"
    hass_admin_user.groups = saved_groups
    await ws.send_json_auto_id(
        {
            "type": "allpowers_ble/hil/write/authorize",
            "version": 1,
            "entry_id": entry.entry_id,
            "feature": "dc_output",
            "target": True,
            "confirmed": True,
        }
    )
    denied = await ws.receive_json()
    assert denied["success"] is False
    assert denied["error"]["code"] == "candidate_unavailable"
    assert not hil_write.store(hass).grants
    assert fake_gatt.writes == []
    await ws.close()
