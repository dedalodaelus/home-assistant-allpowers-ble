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
    assert not store.revoke(token, connection_id=7, user_id="other-user")
    assert token in store.grants
    assert store.revoke(token, connection_id=7, user_id="admin-user")
    assert store.consume(token, **_scope()) is None

    _issue(store)
    other_entry = store.issue(**(_scope() | {"entry_id": "entry-b"}))
    store.revoke_entry("entry-a")
    assert list(store.grants) == [other_entry.token]


def test_store_reuses_process_local_store() -> None:
    hass = SimpleNamespace(data={})
    first = grant_store(hass)  # type: ignore[arg-type]
    assert grant_store(hass) is first  # type: ignore[arg-type]
