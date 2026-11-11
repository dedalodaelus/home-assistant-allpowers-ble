"""Tests for the in-memory experimental HIL grant lifecycle."""

from __future__ import annotations

from custom_components.allpowers_ble.hil_write import GrantStore


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


def test_grant_revoke_and_entry_cleanup() -> None:
    store = GrantStore()
    token = _issue(store)
    assert not store.revoke(token, connection_id=8, user_id="admin-user")
    assert store.revoke(token, connection_id=7, user_id="admin-user")
    assert store.consume(token, **_scope()) is None

    _issue(store)
    store.revoke_entry("entry-a")
    assert not store.grants
