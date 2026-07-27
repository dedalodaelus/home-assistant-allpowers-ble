# 0002. Maintain one persistent session per config entry

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from existing implementation and documentation.

## Context

The station publishes telemetry through notifications and exposes writable GATT
characteristics. Safe commands require recent state, while reconnecting for every
read or write would increase latency, connection-slot churn, radio traffic, and
contention. Multiple configured stations must remain isolated from one another.

## Decision

Create one long-lived `AllpowersBLEClient` per Home Assistant config entry. That
client owns at most one active GATT connection and all tasks, parser state,
freshness state, transactions, and serialization needed for that station.

## Alternatives considered

### Connect only when an entity is read or a service is called

This reduces idle connection occupancy but loses notifications, makes entity state
less current, and adds connection latency to every operation.

### Share one global client across all stations

This could centralize scheduling, but it would couple independent failures,
transactions, parser buffers, and shutdown behavior.

### Permit multiple simultaneous GATT sessions to one station

This could parallelize operations but creates ordering ambiguity and may exceed
station or proxy connection limits without a demonstrated benefit.

## Consequences

### Positive

- Telemetry can be consumed continuously with low command latency.
- Runtime ownership and cleanup are bounded by one config entry.
- Independent stations do not share transaction or parser state.

### Negative and trade-offs

- Each configured station consumes an active BLE connection slot while connected.
- The client needs explicit retry, watchdog, cancellation, and shutdown logic.
- Long-lived sessions must detect silent or stale links rather than trusting socket
  state.

## Evidence

- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [`coordinator.py`](../../../custom_components/allpowers_ble/coordinator.py)
- [`__init__.py`](../../../custom_components/allpowers_ble/__init__.py)
- [Existing design decisions](../../design-decisions.md#keep-a-persistent-active-connection)

## Fitness functions

- Runtime tests create independent clients and verify that tasks, callbacks, and
  transactions do not cross config-entry boundaries.
- Shutdown tests verify that owned tasks are canceled and awaited and that the GATT
  client is released once.
- Tests cover reconnect, watchdog, and write interleavings on a single client.

## Review triggers

- Supported devices can provide all required state without an active connection.
- Connection-slot limits make persistent sessions unacceptable at expected scale.
- Home Assistant introduces a shared connection manager that changes ownership.
- Parallel GATT sessions become necessary and are supported by hardware evidence.

## Related decisions

- [0001](0001-route-all-ble-access-through-home-assistant.md)
- [0003](0003-classify-the-integration-as-local-polling.md)
- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
- [0011](0011-serialize-writes-and-require-device-confirmation.md)
