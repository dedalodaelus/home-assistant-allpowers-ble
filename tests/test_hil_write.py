"""Tests for the in-memory experimental HIL grant lifecycle."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.allpowers_ble.hil_write import (
    MAX_OUTSTANDING_GRANTS,
    TTL,
    GrantCapacityError,
    GrantStore,
)
from custom_components.allpowers_ble.hil_write import store as grant_store


def _issue(store: GrantStore) -> str:
    return store.issue(
        connection_id=7,
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
        "connection_id": 7,
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
    assert not store.revoke(token, connection_id=8, user_id="admin-user")
    assert store.revoke(token, connection_id=7, user_id="admin-user")
    assert store.consume(token, **_scope()) is None

    _issue(store)
    store.revoke_entry("entry-a")
    assert not store.grants


def test_store_reuses_process_local_store() -> None:
    hass = SimpleNamespace(data={})
    first = grant_store(hass)  # type: ignore[arg-type]
    assert grant_store(hass) is first  # type: ignore[arg-type]
