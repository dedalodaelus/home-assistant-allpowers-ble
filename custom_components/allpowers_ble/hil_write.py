"""Fail-closed, in-memory authorization primitives for experimental HIL writes.

This module deliberately contains no production device allowlist. A candidate
must be supplied by an explicitly reviewed feature profile before a grant can
be created.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import secrets
import re
from time import monotonic
from types import MappingProxyType
from typing import Any
from collections.abc import Mapping

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import voluptuous as vol

from .const import DOMAIN, NOTIFY_UUID, SERVICE_UUID, WRITE_UUID
from .client import AllpowersClientError
from .entity import runtime_model_support
from .model_support import ModelFeature, PhysicalPresence, model_feature_capability
from .protocol import StateUnavailableError

KEY = DOMAIN + "_hil_write_grants"
TTL = 30.0
MAX_OUTSTANDING_GRANTS = 64
API_VERSION = 1


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    """Exact profile contract; the deployed policy registry intentionally stays empty."""

    model: str
    hardware: str
    firmware: str | None
    profile: str
    feature: ModelFeature
    gatt_contract: tuple[str, str, str]
    physical_presence: PhysicalPresence
    semantic_validator: Callable[[str, Any], bool]


# Reviewed real-device candidates are intentionally absent. Tests may patch this
# registry with an exact synthetic profile to exercise the guarded execution path.
CANDIDATE_POLICIES: Mapping[
    tuple[str, str, str | None, ModelFeature], CandidatePolicy
] = MappingProxyType({})


class GrantCapacityError(Exception):
    """Raised when the bounded in-memory grant store is full."""


_AUTHORIZE_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): DOMAIN + "/hil/write/authorize",
    vol.Required("version"): API_VERSION,
    vol.Required("entry_id"): str,
    vol.Required("feature"): vol.In({feature.value for feature in ModelFeature}),
    vol.Required("target"): object,
    vol.Required("confirmed"): True,
}
_EXECUTE_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): DOMAIN + "/hil/write/execute",
    vol.Required("version"): API_VERSION,
    vol.Required("entry_id"): str,
    vol.Required("token"): str,
    vol.Required("confirmed"): True,
}
_REVOKE_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): DOMAIN + "/hil/write/revoke",
    vol.Required("version"): API_VERSION,
    vol.Required("token"): str,
}


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

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self.grants: dict[str, Grant] = {}
        self._clock = clock

    def _prune_expired(self, now: float) -> None:
        self.grants = {
            token: grant for token, grant in self.grants.items() if now < grant.deadline
        }

    @staticmethod
    def _validate_target(target: Any) -> None:
        """Allow only immutable scalar targets with exact built-in types."""
        if type(target) not in {bool, int, str}:
            raise ValueError("HIL grant targets must be bool, int, or str scalars")
        if type(target) is str and len(target) > 128:
            raise ValueError("HIL grant string targets are limited to 128 characters")

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
        self._validate_target(target)
        now = self._clock()
        self._prune_expired(now)
        if len(self.grants) >= MAX_OUTSTANDING_GRANTS:
            raise GrantCapacityError("The HIL grant store is at capacity")
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
            now + TTL,
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
        now = self._clock()
        self._prune_expired(now)
        grant = self.grants.pop(token, None)
        if grant is None or now >= grant.deadline:
            return None
        try:
            self._validate_target(target)
        except ValueError:
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
        if any(
            type(wanted) is not type(saved)
            for wanted, saved in zip(expected, actual, strict=True)
        ):
            return None
        return grant if actual == expected else None

    def revoke(self, token: str, *, connection_id: int, user_id: str) -> bool:
        self._prune_expired(self._clock())
        grant = self.grants.get(token)
        if grant is None or (grant.connection_id, grant.user_id) != (
            connection_id,
            user_id,
        ):
            return False
        del self.grants[token]
        return True

    def revoke_entry(self, entry_id: str) -> None:
        self._prune_expired(self._clock())
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


def _entry_scope(
    hass: HomeAssistant, entry_id: str
) -> tuple[Any, Any, tuple[Any, str, str | None]] | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
        or getattr(entry, "runtime_data", None) is None
    ):
        return None
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data
    support = runtime_model_support(coordinator)
    settings = data.settings
    hardware = data.session_hardware_version or (
        settings.hardware_version
        if settings and coordinator.settings_are_fresh
        else None
    )
    firmware = (
        settings.firmware_version
        if settings and coordinator.settings_are_fresh
        else None
    )
    if not isinstance(support.model, str) or not isinstance(hardware, str):
        return None
    return entry, runtime, (support, hardware, firmware)


def _deny(connection: Any, msg: dict[str, Any], code: str) -> None:
    connection.send_error(msg["id"], code, "Experimental HIL write rejected")


def _admin(connection: Any, msg: dict[str, Any]) -> bool:
    if getattr(getattr(connection, "user", None), "is_admin", False):
        return True
    _deny(connection, msg, "unauthorized")
    return False


@websocket_api.websocket_command(_AUTHORIZE_SCHEMA)
@websocket_api.async_response
async def ws_write_authorize(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    """Issue an ephemeral grant only for an exact reviewed candidate policy."""
    if not _admin(connection, msg):
        return
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", msg["entry_id"]) is None:
        _deny(connection, msg, "invalid_scope")
        return
    scope = _entry_scope(hass, msg["entry_id"])
    if scope is None:
        _deny(connection, msg, "not_ready")
        return
    entry, runtime, (support, hardware, firmware) = scope
    feature = ModelFeature(msg["feature"])
    policy = CANDIDATE_POLICIES.get((support.model, hardware, firmware, feature))
    capability = model_feature_capability(support, feature)
    client = runtime.client
    generation = client.active_session_generation
    route = getattr(client, "_connected_route", None)
    if (
        policy is None
        or not client.is_connected
        or data_conflicted(runtime.coordinator.data)
        or not runtime.coordinator.status_is_fresh
        or client.has_pending_transaction
        or type(generation) is not int
        or route is None
        or route[0] != generation
        or route[1].observed_route == "unverified"
    ):
        _deny(connection, msg, "candidate_unavailable")
        return
    try:
        GrantStore._validate_target(msg["target"])
    except ValueError:
        _deny(connection, msg, "invalid_target")
        return
    if (
        not isinstance(policy, CandidatePolicy)
        or capability.hil_candidate_eligible is not True
        or capability.physical_presence is not PhysicalPresence.PRESENT
        or policy.physical_presence is not PhysicalPresence.PRESENT
        or policy.gatt_contract != (SERVICE_UUID, WRITE_UUID, NOTIFY_UUID)
        or (
            policy.model,
            policy.hardware,
            policy.firmware,
            policy.profile,
            policy.feature,
        )
        != (support.model, hardware, firmware, support.profile, feature)
        or type(msg["target"]) is not bool
        or feature
        not in {
            ModelFeature.AC_OUTPUT,
            ModelFeature.DC_OUTPUT,
            ModelFeature.LIGHT_OUTPUT,
        }
    ):
        _deny(connection, msg, "candidate_unavailable")
        return
    try:
        grant = store(hass).issue(
            connection_id=id(connection),
            user_id=connection.user.id,
            entry_id=entry.entry_id,
            identity=support.model,
            hardware=hardware,
            firmware=firmware,
            generation=generation,
            feature=feature.value,
            target=msg["target"],
        )
    except GrantCapacityError:
        _deny(connection, msg, "grant_capacity")
        return
    connection.send_result(
        msg["id"],
        {"schema_version": API_VERSION, "token": grant.token, "expires_in": TTL},
    )


def data_conflicted(data: Any) -> bool:
    return bool(getattr(data, "identity_conflicted", True))


@websocket_api.websocket_command(_EXECUTE_SCHEMA)
@websocket_api.async_response
async def ws_write_execute(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    """Consume the grant synchronously before the first await or BLE operation."""
    if not _admin(connection, msg):
        return
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", msg["entry_id"]) is None
        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", msg["token"]) is None
    ):
        _deny(connection, msg, "invalid_scope")
        return
    scope = _entry_scope(hass, msg["entry_id"])
    if scope is None:
        _deny(connection, msg, "not_ready")
        return
    entry, runtime, (support, hardware, firmware) = scope
    client = runtime.client
    generation = client.active_session_generation
    if type(generation) is not int:
        _deny(connection, msg, "stale_session")
        return
    token = msg["token"]
    grant = store(hass).grants.get(token)
    if grant is None:
        _deny(connection, msg, "invalid_grant")
        return
    consumed = store(hass).consume(
        token,
        connection_id=id(connection),
        user_id=connection.user.id,
        entry_id=entry.entry_id,
        identity=support.model,
        hardware=hardware,
        firmware=firmware,
        generation=generation,
        feature=grant.feature,
        target=grant.target,
    )
    if consumed is None:
        _deny(connection, msg, "invalid_grant")
        return
    feature = ModelFeature(consumed.feature)
    policy = CANDIDATE_POLICIES.get((support.model, hardware, firmware, feature))
    capability = model_feature_capability(support, feature)
    if (
        not isinstance(policy, CandidatePolicy)
        or capability.hil_candidate_eligible is not True
        or capability.physical_presence is not PhysicalPresence.PRESENT
        or policy.physical_presence is not PhysicalPresence.PRESENT
        or policy.gatt_contract != (SERVICE_UUID, WRITE_UUID, NOTIFY_UUID)
    ):
        _deny(connection, msg, "candidate_unavailable")
        return
    try:
        route = getattr(client, "_connected_route", None)
        route_name = route[1].observed_route if route is not None else "unverified"
        result = await client.async_hil_set_output(
            feature,
            consumed.target,
            expected_generation=consumed.generation,
            semantic_validator=policy.semantic_validator,
            expected_model=policy.model,
            expected_hardware=policy.hardware,
            expected_firmware=policy.firmware,
        )
    except AllpowersClientError, StateUnavailableError, TimeoutError, ValueError:
        connection.send_result(
            msg["id"], {"schema_version": API_VERSION, "outcome": "inconclusive"}
        )
        return
    connection.send_result(
        msg["id"],
        {
            "schema_version": API_VERSION,
            "model": support.model,
            "profile": support.profile,
            "hardware_revision": hardware,
            "firmware_revision": firmware,
            "session_generation": consumed.generation,
            "route": route_name,
            **result,
        },
    )


@websocket_api.websocket_command(_REVOKE_SCHEMA)
@websocket_api.async_response
async def ws_write_revoke(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    """Revoke a grant held by this authenticated session and user."""
    if not _admin(connection, msg):
        return
    if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", msg["token"]) is None:
        _deny(connection, msg, "invalid_scope")
        return
    revoked = store(hass).revoke(
        msg["token"], connection_id=id(connection), user_id=connection.user.id
    )
    connection.send_result(
        msg["id"], {"schema_version": API_VERSION, "revoked": revoked}
    )


async def async_register(hass: HomeAssistant) -> None:
    """Register versioned admin-only experimental write commands."""
    websocket_api.async_register_command(hass, ws_write_authorize)
    websocket_api.async_register_command(hass, ws_write_execute)
    websocket_api.async_register_command(hass, ws_write_revoke)
