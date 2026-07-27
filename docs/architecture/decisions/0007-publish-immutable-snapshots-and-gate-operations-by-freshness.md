# 0007. Publish immutable snapshots and gate operations by freshness

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from client, models, coordinator, and entity behavior.

## Context

A live BLE socket does not prove that status or settings are current. Entities and
commands can otherwise consume stale mutable state while notification callbacks,
watchdogs, disconnects, and service calls interleave on the event loop.

Status and settings also have different update rates and safety uses, so one global
connected flag is insufficient.

## Decision

Represent decoded protocol and integration state as immutable snapshots. Publish a
new snapshot to the push coordinator when values or freshness transitions change.
Track status, settings, and last-valid-packet timestamps independently, and make
entity availability and command authorization depend on the relevant freshness
domain plus the active connected session.

Cached stale data may remain visible in diagnostics but must not authorize a write.

## Alternatives considered

### Expose mutable client objects directly to entities

This reduces allocations but allows readers to observe partially updated state and
couples entity code to transport internals.

### Use only the GATT connected flag

This misses silent links, delayed telemetry, and stale settings.

### Clear all values immediately when any domain becomes stale

This avoids accidental use but removes useful diagnostic evidence and conflates
status, settings, and transport freshness.

### Persist the last snapshot and restore it after restart

This improves apparent continuity but risks treating old device state as current.

## Consequences

### Positive

- Entity reads observe coherent values.
- Stale state becomes an explicit safety and availability boundary.
- Status, settings, and transport failures can be diagnosed independently.
- Snapshots are straightforward to serialize in diagnostics and assert in tests.

### Negative and trade-offs

- Snapshot replacement creates more short-lived objects.
- Freshness transitions require a maintenance loop even without new notifications.
- Cached diagnostic values must be clearly distinguished from available entity
  state.

## Evidence

- [`models.py`](../../../custom_components/allpowers_ble/models.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [`coordinator.py`](../../../custom_components/allpowers_ble/coordinator.py)
- [`entity.py`](../../../custom_components/allpowers_ble/entity.py)
- [Existing architecture summary](../implementation-architecture.md#data-freshness)

## Fitness functions

- Models use frozen dataclasses or equivalent immutable value types.
- Entity tests cover connected/fresh, connected/stale, and disconnected states.
- Client tests advance deterministic clocks and assert freshness transitions
  without requiring new notifications.
- Write tests reject missing, stale, or disconnected source state.

## Review triggers

- Home Assistant introduces a different state-publication contract.
- The integration adds a durable historical cache or state restoration.
- New protocol domains need independent freshness rules.
- Performance evidence shows immutable replacement is a material bottleneck.

## Related decisions

- [0002](0002-maintain-one-persistent-session-per-config-entry.md)
- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [0011](0011-serialize-writes-and-require-device-confirmation.md)
- [0013](0013-persist-only-configuration-and-validated-options.md)
