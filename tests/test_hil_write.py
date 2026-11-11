"""Tests for the in-memory experimental HIL grant lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.allpowers_ble import hil_write
from custom_components.allpowers_ble.const import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID
from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.model_support import ModelFeature, PhysicalPresence
from custom_components.allpowers_ble.hil_write import (
    MAX_OUTSTANDING_GRANTS,
    TTL,
    GrantCapacityError,
    GrantStore,
)
from custom_components.allpowers_ble.hil_write import store as grant_store


class _Connection:
    def __init__(self, *, admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=admin, id="admin-user")
        self.errors: list[tuple[Any, ...]] = []
        self.results: list[tuple[Any, ...]] = []
        self.subscriptions: dict[Any, Any] = {}

    def send_error(self, *args: Any) -> None:
        self.errors.append(args)

    def send_result(self, *args: Any) -> None:
        self.results.append(args)


class _FakeHass:
    def __init__(self, runtime: Any = None, *, state=ConfigEntryState.LOADED):
        self.data: dict[str, Any] = {}
        entry = SimpleNamespace(
            entry_id="entry-a",
            domain="allpowers_ble",
            state=state,
            runtime_data=runtime,
        )
        self.config_entries = SimpleNamespace(
            async_get_entry=lambda entry_id: entry if entry_id == "entry-a" else None
        )


def _issue(store: GrantStore) -> str:
    return store.issue(
        connection_id="session-a",
        user_id="admin-user",
        entry_id="entry-a",
        identity="TEST SYNTHETIC",
        hardware="1.0",
        firmware="2.0",
        generation=4,
        feature="dc_output",
        target=True,
    ).token


def _scope() -> dict[str, object]:
    return {
        "connection_id": "session-a",
        "user_id": "admin-user",
        "entry_id": "entry-a",
        "identity": "TEST SYNTHETIC",
        "hardware": "1.0",
        "firmware": "2.0",
        "generation": 4,
        "feature": "dc_output",
        "target": True,
    }


def test_grant_is_opaque_and_consumed_once() -> None:
    store = GrantStore()
    token = _issue(store)
    assert len(token) >= 32
    assert store.consume(token, **_scope()) is not None
    assert store.consume(token, **_scope()) is None


def test_grant_scope_mismatch_consumes_and_denies() -> None:
    store = GrantStore()
    token = _issue(store)
    assert store.consume(token, **(_scope() | {"target": False})) is None
    assert store.consume(token, **_scope()) is None


def test_grant_target_requires_exact_type_and_value() -> None:
    store = GrantStore()
    token = _issue(store)
    assert store.consume(token, **(_scope() | {"target": 1})) is None
    assert store.consume(token, **_scope()) is None


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("connection_id", 8),
        ("user_id", "other-user"),
        ("entry_id", "entry-b"),
        ("identity", "OTHER MODEL"),
        ("hardware", "1.1"),
        ("firmware", "2.1"),
        ("generation", 5),
        ("feature", "ac_output"),
        ("target", False),
    ],
)
def test_scope_mismatch_denies_and_burns_token(field: str, wrong_value: object) -> None:
    store = GrantStore()
    token = _issue(store)
    wrong_scope = _scope() | {field: wrong_value}
    assert store.consume(token, **wrong_scope) is None
    assert store.consume(token, **_scope()) is None


@pytest.mark.parametrize("target", [{"enabled": True}, [True], (True,)])
def test_mutable_or_compound_target_is_rejected(target: object) -> None:
    store = GrantStore()
    with pytest.raises(ValueError, match="bool, int, or str scalars"):
        store.issue(**(_scope() | {"target": target}))


def test_compound_consume_target_denies_and_burns_grant() -> None:
    store = GrantStore()
    token = _issue(store)
    assert store.consume(token, **(_scope() | {"target": {"enabled": True}})) is None
    assert store.consume(token, **_scope()) is None


def test_long_string_target_is_rejected() -> None:
    store = GrantStore()
    with pytest.raises(ValueError, match="limited to 128 characters"):
        store.issue(**(_scope() | {"target": "x" * 129}))


def test_expired_grants_are_pruned_on_issue_and_access() -> None:
    now = [10.0]
    store = GrantStore(clock=lambda: now[0])
    token = _issue(store)
    assert len(store.grants) == 1

    now[0] += TTL
    assert store.consume(token, **_scope()) is None
    assert not store.grants

    _issue(store)
    now[0] += TTL
    _issue(store)
    assert len(store.grants) == 1


def test_grant_store_capacity_fails_closed_and_prunes_expired() -> None:
    now = [0.0]
    store = GrantStore(clock=lambda: now[0])
    for _ in range(MAX_OUTSTANDING_GRANTS):
        _issue(store)
    with pytest.raises(GrantCapacityError):
        _issue(store)

    now[0] += TTL
    _issue(store)
    assert len(store.grants) == 1


def test_grant_revoke_and_entry_cleanup() -> None:
    store = GrantStore()
    token = _issue(store)
    assert not store.revoke(token, connection_id="session-b", user_id="admin-user")
    assert not store.revoke(token, connection_id="session-a", user_id="other-user")
    assert token in store.grants
    assert store.revoke(token, connection_id="session-a", user_id="admin-user")
    assert store.consume(token, **_scope()) is None

    _issue(store)
    other_entry = store.issue(**(_scope() | {"entry_id": "entry-b"}))
    store.revoke_entry("entry-a")
    assert list(store.grants) == [other_entry.token]


def test_store_reuses_process_local_store() -> None:
    hass = SimpleNamespace(data={})
    first = grant_store(hass)  # type: ignore[arg-type]
    assert grant_store(hass) is first  # type: ignore[arg-type]


def test_connection_session_requires_manageable_close_subscription() -> None:
    hass = SimpleNamespace(data={})
    assert (
        hil_write._connection_session(
            hass,
            SimpleNamespace(),
            create=True,  # type: ignore[arg-type]
        )
        is None
    )
    connection = SimpleNamespace(subscriptions={})
    assert (
        hil_write._connection_session(
            hass,
            connection,
            create=False,  # type: ignore[arg-type]
        )
        is None
    )
    connection.subscriptions[hil_write._SESSION_SUBSCRIPTION_KEY] = lambda: None
    assert (
        hil_write._connection_session(
            hass,
            connection,
            create=True,  # type: ignore[arg-type]
        )
        is None
    )


@pytest.mark.asyncio
async def test_websocket_endpoints_fail_closed_without_session_cleanup_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, _support, _policy = _api_harness(monkeypatch)
    connection = _Connection()
    del connection.subscriptions
    await hil_write.ws_write_authorize(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry-a",
            "feature": "dc_output",
            "target": True,
        },
    )
    assert connection.errors[-1][1] == "invalid_session"

    await hil_write.ws_write_execute(
        hass,
        connection,
        {"id": 2, "entry_id": "entry-a", "token": "A" * 43},
    )
    assert connection.errors[-1][1] == "invalid_grant"
    assert (
        hass.config_entries.async_get_entry(
            "entry-a"
        ).runtime_data.client.execution_calls
        == 0
    )


@pytest.mark.asyncio
async def test_execute_burns_grant_if_session_cleanup_record_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, _support, _policy = _api_harness(monkeypatch)
    connection = _Connection()
    await hil_write.ws_write_authorize(
        hass,
        connection,
        {
            "id": 1,
            "entry_id": "entry-a",
            "feature": "dc_output",
            "target": True,
        },
    )
    token = connection.results[-1][1]["token"]
    monkeypatch.setattr(hil_write, "_connection_session_cleanup", lambda _conn: None)
    await hil_write.ws_write_execute(
        hass, connection, {"id": 2, "entry_id": "entry-a", "token": token}
    )
    assert connection.errors[-1][1] == "invalid_grant"
    assert token not in hil_write.store(hass).grants
    assert (
        hass.config_entries.async_get_entry(
            "entry-a"
        ).runtime_data.client.execution_calls
        == 0
    )


def _api_harness(monkeypatch: pytest.MonkeyPatch):
    support = SimpleNamespace(model="SYNTHETIC", profile="synthetic-v1")

    class Client:
        active_session_generation = 5
        is_connected = True
        has_pending_transaction = False
        _connected_route = (5, SimpleNamespace(observed_route="local_adapter"))
        execution_calls = 0

        async def async_hil_set_output(self, feature, target, **kwargs):
            self.execution_calls += 1
            return {
                "outcome": "confirmed",
                "feature": feature.value,
                "source": False,
                "target": target,
                "confirmed": True,
            }

    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            session_hardware_version="9.1",
            settings=SimpleNamespace(
                hardware_version="9.1", firmware_version="test-fw"
            ),
            identity_conflicted=False,
        ),
        settings_are_fresh=True,
        status_is_fresh=True,
    )
    hass = _FakeHass(SimpleNamespace(client=Client(), coordinator=coordinator))
    monkeypatch.setattr(
        hil_write, "runtime_model_support", lambda _coordinator: support
    )
    monkeypatch.setattr(
        hil_write,
        "model_feature_capability",
        lambda _support, feature: SimpleNamespace(
            hil_candidate_eligible=True, physical_presence=PhysicalPresence.PRESENT
        ),
    )

    def validator(_profile, _status):
        return True

    policy = hil_write.CandidatePolicy(
        model="SYNTHETIC",
        hardware="9.1",
        firmware="test-fw",
        profile="synthetic-v1",
        feature=ModelFeature.DC_OUTPUT,
        gatt_contract=(SERVICE_UUID, WRITE_UUID, NOTIFY_UUID),
        physical_presence=PhysicalPresence.PRESENT,
        semantic_validator=validator,
    )
    monkeypatch.setattr(
        hil_write,
        "CANDIDATE_POLICIES",
        {("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): policy},
    )
    return hass, support, policy


@pytest.mark.asyncio
async def test_websocket_authorize_execute_replay_revoke_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = SimpleNamespace(model="SYNTHETIC", profile="synthetic-v1")
    calls = []

    class Client:
        active_session_generation = 5
        is_connected = True
        has_pending_transaction = False
        _connected_route = (5, SimpleNamespace(observed_route="local_adapter"))

        async def async_hil_set_output(self, feature, target, **kwargs):
            calls.append((feature, target, kwargs))
            return {
                "outcome": "confirmed",
                "feature": feature.value,
                "source": False,
                "target": target,
                "confirmed": True,
            }

    settings = SimpleNamespace(hardware_version="9.1", firmware_version="test-fw")
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            session_hardware_version="9.1",
            settings=settings,
            identity_conflicted=False,
        ),
        settings_are_fresh=True,
        status_is_fresh=True,
    )
    hass = _FakeHass(SimpleNamespace(client=Client(), coordinator=coordinator))
    monkeypatch.setattr(
        hil_write, "runtime_model_support", lambda _coordinator: support
    )
    monkeypatch.setattr(
        hil_write,
        "model_feature_capability",
        lambda _support, feature: SimpleNamespace(
            hil_candidate_eligible=True, physical_presence=PhysicalPresence.PRESENT
        ),
    )

    def validator(_profile, _status):
        return True

    policy = hil_write.CandidatePolicy(
        model="SYNTHETIC",
        hardware="9.1",
        firmware="test-fw",
        profile="synthetic-v1",
        feature=ModelFeature.DC_OUTPUT,
        gatt_contract=(SERVICE_UUID, WRITE_UUID, NOTIFY_UUID),
        physical_presence=PhysicalPresence.PRESENT,
        semantic_validator=validator,
    )
    monkeypatch.setattr(
        hil_write,
        "CANDIDATE_POLICIES",
        {("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): policy},
    )
    connection = _Connection()
    authorize = {
        "id": 1,
        "entry_id": "entry-a",
        "feature": "dc_output",
        "target": True,
    }
    await hil_write.ws_write_authorize(hass, connection, authorize)
    token = connection.results[-1][1]["token"]
    await hil_write.ws_write_execute(
        hass,
        connection,
        {"id": 2, "entry_id": "entry-a", "token": token},
    )
    assert connection.results[-1][1]["outcome"] == "confirmed"
    assert calls and calls[0][0] is ModelFeature.DC_OUTPUT
    assert not hil_write.store(hass).grants

    await hil_write.ws_write_execute(
        hass,
        connection,
        {"id": 3, "entry_id": "entry-a", "token": token},
    )
    assert connection.errors[-1][1] == "invalid_grant"

    await hil_write.ws_write_authorize(hass, connection, authorize)
    unused = connection.results[-1][1]["token"]
    await hil_write.ws_write_revoke(hass, connection, {"id": 4, "token": unused})
    assert connection.results[-1][1]["revoked"] is True
    await hil_write.ws_write_revoke(hass, connection, {"id": 5, "token": unused})
    assert connection.results[-1][1]["revoked"] is False


@pytest.mark.asyncio
async def test_closed_connection_revokes_unused_grant_and_new_session_cannot_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, _support, _policy = _api_harness(monkeypatch)
    authorize = {
        "id": 1,
        "entry_id": "entry-a",
        "feature": "dc_output",
        "target": True,
    }
    connection_a = _Connection()
    await hil_write.ws_write_authorize(hass, connection_a, authorize)
    token = connection_a.results[-1][1]["token"]
    session_a = hil_write._connection_session(hass, connection_a, create=False)
    assert session_a is not None
    session_cleanup = hil_write._connection_session_cleanup(connection_a)
    assert session_cleanup is not None and session_cleanup.is_active()
    assert token in hil_write.store(hass).grants

    connection_a.subscriptions[hil_write._SESSION_SUBSCRIPTION_KEY]()
    assert not session_cleanup.is_active()
    assert token not in hil_write.store(hass).grants

    connection_b = _Connection()
    session_b = hil_write._connection_session(hass, connection_b, create=True)
    assert session_b is not None
    assert session_b != session_a
    await hil_write.ws_write_execute(
        hass,
        connection_b,
        {"id": 2, "entry_id": "entry-a", "token": token},
    )
    assert connection_b.errors[-1][1] == "invalid_grant"
    assert (
        hass.config_entries.async_get_entry(
            "entry-a"
        ).runtime_data.client.execution_calls
        == 0
    )


@pytest.mark.asyncio
async def test_concurrent_execute_consumes_grant_before_ble_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, _support, _policy = _api_harness(monkeypatch)
    entry = hass.config_entries.async_get_entry("entry-a")
    client = entry.runtime_data.client
    connection = _Connection()
    session_id = hil_write._connection_session(hass, connection, create=True)
    assert session_id is not None
    grant = hil_write.store(hass).issue(
        connection_id=session_id,
        user_id=connection.user.id,
        entry_id="entry-a",
        identity="SYNTHETIC",
        hardware="9.1",
        firmware="test-fw",
        generation=5,
        feature="dc_output",
        target=True,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"outcome": "confirmed", "feature": "dc_output", "confirmed": True}

    client.async_hil_set_output = delayed_execute
    message = {"id": 22, "entry_id": "entry-a", "token": grant.token}
    first = asyncio.create_task(hil_write.ws_write_execute(hass, connection, message))
    await started.wait()
    await hil_write.ws_write_execute(hass, connection, {**message, "id": 23})
    assert calls == 1
    assert connection.errors[-1][1] == "invalid_grant"
    release.set()
    await first
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_failure", [False, True])
async def test_websocket_grant_executes_exact_packet_on_fake_ble(
    notification_builder, monkeypatch: pytest.MonkeyPatch, transport_failure: bool
) -> None:
    """Exercise authorize, one-shot consume, fake GATT TX and readback together."""
    from time import monotonic
    from bleak.exc import BleakError

    from custom_components.allpowers_ble.const import (
        NOTIFY_UUID,
        SERVICE_UUID,
        WRITE_UUID,
    )
    from custom_components.allpowers_ble.protocol import encode_output_control
    from custom_components.allpowers_ble.route_attribution import ConnectedRoute
    from tests.test_client_logic import _connected_client, _settings, _status
    from tests.ha_stubs import FakeCharacteristic

    client, fake_ble = _connected_client()
    client._status = _status()
    client._status_monotonic = monotonic()
    client._settings = _settings(firmware_version="test-fw")
    client._settings_monotonic = monotonic()
    client._session_hardware_revision = ("test-hw", 1)
    client._connected_route = (
        1,
        ConnectedRoute("local_adapter", "bluez", "synthetic-device-path"),
    )
    client._schedule_status_refresh = lambda: None
    support = SimpleNamespace(model="SYNTHETIC TEST MODEL", profile="synthetic-v1")
    client._runtime_model_support = lambda: support
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            session_hardware_version="test-hw",
            settings=_settings(firmware_version="test-fw"),
            identity_conflicted=False,
        ),
        settings_are_fresh=True,
        status_is_fresh=True,
    )
    hass = _FakeHass(SimpleNamespace(client=client, coordinator=coordinator))
    monkeypatch.setattr(
        hil_write, "runtime_model_support", lambda _coordinator: support
    )
    monkeypatch.setattr(
        hil_write,
        "model_feature_capability",
        lambda _support, feature: SimpleNamespace(
            hil_candidate_eligible=True, physical_presence=PhysicalPresence.PRESENT
        ),
    )
    monkeypatch.setattr(
        client_module,
        "model_feature_capability",
        lambda _support, feature: SimpleNamespace(
            hil_candidate_eligible=True, physical_presence=PhysicalPresence.PRESENT
        ),
    )

    def validator(profile, status):
        return profile == "synthetic-v1" and isinstance(status, type(_status()))

    policy = hil_write.CandidatePolicy(
        model=support.model,
        hardware="test-hw",
        firmware="test-fw",
        profile=support.profile,
        feature=ModelFeature.DC_OUTPUT,
        gatt_contract=(SERVICE_UUID, WRITE_UUID, NOTIFY_UUID),
        physical_presence=PhysicalPresence.PRESENT,
        semantic_validator=validator,
    )
    monkeypatch.setattr(
        hil_write,
        "CANDIDATE_POLICIES",
        {(support.model, "test-hw", "test-fw", ModelFeature.DC_OUTPUT): policy},
    )
    packet = notification_builder(0x01, bytes((0x11, 50, 0, 100, 0, 25, 0, 90)))
    original_write = fake_ble.write_gatt_char

    async def write_and_confirm(characteristic, data, *, response):
        await original_write(characteristic, data, response=response)
        if transport_failure:
            raise BleakError("synthetic transport failure after write")
        client._notification_handler(FakeCharacteristic(), bytearray(packet))

    monkeypatch.setattr(fake_ble, "write_gatt_char", write_and_confirm)
    connection = _Connection()
    await hil_write.ws_write_authorize(
        hass,
        connection,
        {
            "id": 30,
            "entry_id": "entry-a",
            "feature": "dc_output",
            "target": True,
        },
    )
    token = connection.results[-1][1]["token"]
    await hil_write.ws_write_execute(
        hass, connection, {"id": 31, "entry_id": "entry-a", "token": token}
    )
    assert fake_ble.writes == [encode_output_control(dc=True, ac=False, light=True)]
    assert connection.results[-1][1]["outcome"] == (
        "inconclusive" if transport_failure else "confirmed"
    )
    assert connection.results[-1][1]["confirmed"] is (not transport_failure)
    assert token not in hil_write.store(hass).grants
    await hil_write.ws_write_revoke(hass, connection, {"id": 32, "token": token})
    assert connection.results[-1][1]["revoked"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "non_admin",
        "bad_entry_id",
        "not_ready",
        "empty_policy",
        "disconnected",
        "identity_conflict",
        "stale_status",
        "pending_transaction",
        "missing_generation",
        "missing_route",
        "wrong_route_generation",
        "unverified_route",
        "invalid_target",
        "wrong_presence",
        "wrong_gatt",
        "wrong_policy_identity",
        "catalog_ineligible",
        "non_boolean_target",
        "capacity",
    ],
)
async def test_authorize_denials_are_fail_closed(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass, _support, policy = _api_harness(monkeypatch)
    entry = hass.config_entries.async_get_entry("entry-a")
    client = entry.runtime_data.client
    client = entry.runtime_data.client
    message = {
        "id": 17,
        "entry_id": "entry-a",
        "feature": "dc_output",
        "target": True,
    }
    connection = _Connection(admin=failure != "non_admin")

    if failure == "bad_entry_id":
        message["entry_id"] = "bad/id"
    elif failure == "not_ready":
        hass.config_entries.async_get_entry = lambda _entry_id: None
    elif failure == "empty_policy":
        monkeypatch.setattr(hil_write, "CANDIDATE_POLICIES", {})
    elif failure == "disconnected":
        client.is_connected = False
    elif failure == "identity_conflict":
        entry.runtime_data.coordinator.data.identity_conflicted = True
    elif failure == "stale_status":
        entry.runtime_data.coordinator.status_is_fresh = False
    elif failure == "pending_transaction":
        client.has_pending_transaction = True
    elif failure == "missing_generation":
        client.active_session_generation = None
    elif failure == "missing_route":
        client._connected_route = None
    elif failure == "wrong_route_generation":
        client._connected_route = (3, SimpleNamespace(observed_route="local_adapter"))
    elif failure == "unverified_route":
        client._connected_route = (5, SimpleNamespace(observed_route="unverified"))
    elif failure == "invalid_target":
        message["target"] = {"enabled": True}
    elif failure == "wrong_presence":
        monkeypatch.setattr(
            hil_write,
            "CANDIDATE_POLICIES",
            {
                ("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): replace(
                    policy, physical_presence=PhysicalPresence.UNKNOWN
                )
            },
        )
    elif failure == "wrong_gatt":
        monkeypatch.setattr(
            hil_write,
            "CANDIDATE_POLICIES",
            {
                ("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): replace(
                    policy, gatt_contract=("wrong", "wrong", "wrong")
                )
            },
        )
    elif failure == "wrong_policy_identity":
        monkeypatch.setattr(
            hil_write,
            "CANDIDATE_POLICIES",
            {
                ("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): replace(
                    policy, profile="another-profile"
                )
            },
        )
    elif failure == "catalog_ineligible":
        monkeypatch.setattr(
            hil_write,
            "model_feature_capability",
            lambda _support, _feature: SimpleNamespace(
                hil_candidate_eligible=False,
                physical_presence=PhysicalPresence.PRESENT,
            ),
        )
    elif failure == "non_boolean_target":
        message["target"] = 1
    elif failure == "capacity":
        store = hil_write.store(hass)
        for _ in range(hil_write.MAX_OUTSTANDING_GRANTS):
            _issue(store)

    await hil_write.ws_write_authorize(hass, connection, message)
    assert connection.results == []
    assert connection.errors
    assert (
        not any(
            grant.entry_id == "entry-a"
            for grant in hil_write.store(hass).grants.values()
        )
        or failure == "capacity"
    )
    assert client.execution_calls == 0


@pytest.mark.asyncio
async def test_scope_helpers_and_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    hass, _support, _policy = _api_harness(monkeypatch)
    entry = hass.config_entries.async_get_entry("entry-a")
    assert hil_write._entry_scope(hass, "entry-a") is not None
    assert hil_write._entry_scope(hass, "missing") is None
    entry.domain = "other"
    assert hil_write._entry_scope(hass, "entry-a") is None
    entry.domain = "allpowers_ble"
    entry.state = ConfigEntryState.NOT_LOADED
    assert hil_write._entry_scope(hass, "entry-a") is None
    entry.state = ConfigEntryState.LOADED
    entry.runtime_data = None
    assert hil_write._entry_scope(hass, "entry-a") is None
    assert hil_write.data_conflicted(SimpleNamespace(identity_conflicted=True))
    assert hil_write.data_conflicted(SimpleNamespace())
    assert not hil_write.data_conflicted(SimpleNamespace(identity_conflicted=False))
    connection = _Connection(admin=False)
    assert not hil_write._admin(connection, {"id": 8})
    assert connection.errors[-1][1] == "unauthorized"
    registered = []
    monkeypatch.setattr(
        hil_write.websocket_api,
        "async_register_command",
        lambda _hass, handler: registered.append(handler),
    )
    await hil_write.async_register(hass)
    assert registered == [
        hil_write.ws_write_authorize,
        hil_write.ws_write_execute,
        hil_write.ws_write_revoke,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "non_admin",
        "bad_scope",
        "not_ready",
        "stale_generation",
        "missing_grant",
        "wrong_connection",
        "candidate_missing",
        "wrong_presence",
        "wrong_gatt",
        "catalog_ineligible",
        "executor_error",
        "executor_timeout",
        "executor_value_error",
        "no_route",
    ],
)
async def test_execute_denials_and_inconclusive_results(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass, _support, policy = _api_harness(monkeypatch)
    entry = hass.config_entries.async_get_entry("entry-a")
    client = entry.runtime_data.client
    connection = _Connection(admin=failure != "non_admin")
    token = "A" * 43
    if failure not in {
        "non_admin",
        "bad_scope",
        "not_ready",
        "stale_generation",
        "missing_grant",
    }:
        scope_connection = (
            hil_write._connection_session(hass, connection, create=True)
            if failure != "wrong_connection"
            else "different-session"
        )
        assert scope_connection is not None
        hil_write.store(hass).issue(
            connection_id=scope_connection,
            user_id=connection.user.id,
            entry_id="entry-a",
            identity="SYNTHETIC",
            hardware="9.1",
            firmware="test-fw",
            generation=5,
            feature="dc_output",
            target=True,
        )
        token = next(iter(hil_write.store(hass).grants))
    if failure == "bad_scope":
        msg_entry = "bad/id"
    else:
        msg_entry = "entry-a"
    if failure == "not_ready":
        hass.config_entries.async_get_entry = lambda _entry_id: None
    elif failure == "stale_generation":
        client.active_session_generation = None
    elif failure == "candidate_missing":
        monkeypatch.setattr(hil_write, "CANDIDATE_POLICIES", {})
    elif failure == "wrong_presence":
        monkeypatch.setattr(
            hil_write,
            "CANDIDATE_POLICIES",
            {
                ("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): replace(
                    policy, physical_presence=PhysicalPresence.UNKNOWN
                )
            },
        )
    elif failure == "wrong_gatt":
        monkeypatch.setattr(
            hil_write,
            "CANDIDATE_POLICIES",
            {
                ("SYNTHETIC", "9.1", "test-fw", ModelFeature.DC_OUTPUT): replace(
                    policy, gatt_contract=("wrong", "wrong", "wrong")
                )
            },
        )
    elif failure == "catalog_ineligible":
        monkeypatch.setattr(
            hil_write,
            "model_feature_capability",
            lambda _support, _feature: SimpleNamespace(
                hil_candidate_eligible=False,
                physical_presence=PhysicalPresence.PRESENT,
            ),
        )
    elif failure in {"executor_error", "executor_timeout", "executor_value_error"}:
        error = {
            "executor_error": hil_write.AllpowersClientError("denied"),
            "executor_timeout": TimeoutError("late"),
            "executor_value_error": ValueError("invalid"),
        }[failure]

        async def fail_execute(*_args, **_kwargs):
            raise error

        client.async_hil_set_output = fail_execute
    elif failure == "no_route":
        client._connected_route = None

        async def reject_unrouted_execute(*_args, **_kwargs):
            raise hil_write.StateUnavailableError("route unavailable")

        client.async_hil_set_output = reject_unrouted_execute

    await hil_write.ws_write_execute(
        hass,
        connection,
        {"id": 21, "entry_id": msg_entry, "token": token},
    )
    if failure in {
        "executor_error",
        "executor_timeout",
        "executor_value_error",
        "no_route",
    }:
        assert connection.results[-1][1]["outcome"] == "inconclusive"
    else:
        assert connection.errors
    assert client.execution_calls == 0


@pytest.mark.asyncio
async def test_revoke_rejects_non_admin_and_malformed_token() -> None:
    hass = _FakeHass()
    connection = _Connection(admin=False)
    await hil_write.ws_write_revoke(hass, connection, {"id": 1, "token": "x" * 43})
    assert connection.errors[-1][1] == "unauthorized"
    connection = _Connection()
    await hil_write.ws_write_revoke(hass, connection, {"id": 2, "token": "short"})
    assert connection.errors[-1][1] == "invalid_scope"


def test_entry_scope_rejects_missing_model_and_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, _support, _policy = _api_harness(monkeypatch)
    coordinator = hass.config_entries.async_get_entry(
        "entry-a"
    ).runtime_data.coordinator
    monkeypatch.setattr(
        hil_write,
        "runtime_model_support",
        lambda _coordinator: SimpleNamespace(model=None, profile="synthetic-v1"),
    )
    assert hil_write._entry_scope(hass, "entry-a") is None
    monkeypatch.setattr(
        hil_write,
        "runtime_model_support",
        lambda _coordinator: SimpleNamespace(model="SYNTHETIC", profile="synthetic-v1"),
    )
    coordinator.data.session_hardware_version = None
    coordinator.data.settings = None
    assert hil_write._entry_scope(hass, "entry-a") is None
