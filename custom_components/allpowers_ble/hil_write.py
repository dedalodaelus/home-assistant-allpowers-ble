"""Fail-closed, in-memory authorization primitives for experimental HIL writes.

This module deliberately contains no production device allowlist. A candidate
must be supplied by an explicitly reviewed feature profile before a grant can
be created.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

KEY = DOMAIN + "_hil_write_grants"
TTL = 30.0


@dataclass(frozen=True, slots=True)
class Grant:
    token: str
    connection_id: int
    user_id: str
    entry_id: str
    identity: str
    hardware: str
    firmware: str | None
    generation: int
    feature: str
    target: Any
    deadline: float


class GrantStore:
    """Ephemeral one-action grants; caller consumes synchronously before I/O."""

    def __init__(self) -> None:
        self.grants: dict[str, Grant] = {}

    def issue(
        self,
        *,
        connection_id: int,
        user_id: str,
        entry_id: str,
        identity: str,
        hardware: str,
        firmware: str | None,
        generation: int,
        feature: str,
        target: Any,
    ) -> Grant:
        token = secrets.token_urlsafe(32)
        grant = Grant(
            token,
            connection_id,
            user_id,
            entry_id,
            identity,
            hardware,
            firmware,
            generation,
            feature,
            target,
            monotonic() + TTL,
        )
        self.grants[token] = grant
        return grant

    def consume(
        self,
        token: str,
        *,
        connection_id: int,
        user_id: str,
        entry_id: str,
        identity: str,
        hardware: str,
        firmware: str | None,
        generation: int,
        feature: str,
        target: Any,
    ) -> Grant | None:
        grant = self.grants.pop(token, None)
        if grant is None or monotonic() >= grant.deadline:
            return None
        expected = (
            connection_id,
            user_id,
            entry_id,
            identity,
            hardware,
            firmware,
            generation,
            feature,
            target,
        )
        actual = (
            grant.connection_id,
            grant.user_id,
            grant.entry_id,
            grant.identity,
            grant.hardware,
            grant.firmware,
            grant.generation,
            grant.feature,
            grant.target,
        )
        return grant if actual == expected else None

    def revoke(self, token: str, *, connection_id: int, user_id: str) -> bool:
        grant = self.grants.get(token)
        if grant is None or (grant.connection_id, grant.user_id) != (
            connection_id,
            user_id,
        ):
            return False
        del self.grants[token]
        return True

    def revoke_entry(self, entry_id: str) -> None:
        self.grants = {
            token: grant
            for token, grant in self.grants.items()
            if grant.entry_id != entry_id
        }


def store(hass: HomeAssistant) -> GrantStore:
    """Return the process-local grant store."""
    value = hass.data.get(KEY)
    if not isinstance(value, GrantStore):
        value = GrantStore()
        hass.data[KEY] = value
    return value
