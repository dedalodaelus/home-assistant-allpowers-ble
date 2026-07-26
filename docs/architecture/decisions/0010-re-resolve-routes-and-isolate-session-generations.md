# 0010. Re-resolve routes and isolate session generations

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from connection lifecycle and deterministic race tests.

## Context

Home Assistant may change the best connectable route when a local adapter or proxy
appears, disappears, or changes quality. At the same time, asynchronous BLE
libraries can deliver delayed disconnect or notification callbacks after a new
connection has already become active. Reusing the original route or accepting late
callbacks can strand the client or corrupt current state.

## Decision

Before every connection attempt, ask Home Assistant for a fresh connectable device
route. Increment a session generation for each new GATT setup, bind notification
and disconnect callbacks to that generation, and ignore callbacks that do not
match the active generation. Reset freshness, decoder state, and pending
transactions at the session boundary.

## Alternatives considered

### Reuse the discovered `BLEDevice` forever

This avoids lookups but pins the entry to stale adapter or proxy metadata and
prevents transparent failover.

### Trust callback ordering from the BLE library

This simplifies code but assumes a guarantee that is unsafe across cancellation,
disconnect, and reconnect races.

### Compare only the client object identity

Identity helps but does not provide one explicit boundary for every callback,
transaction, and snapshot version associated with the session.

### Restart Home Assistant on route failure

This can recover some conditions but is operationally disruptive and does not fix
late-callback races.

## Consequences

### Positive

- Reconnection can follow Home Assistant to a different adapter or proxy.
- Old-session events cannot disconnect or mutate a newer session.
- Parser fragments and command transactions cannot cross GATT boundaries.

### Negative and trade-offs

- Every retry performs route resolution and more state invalidation.
- Generation checks must be applied consistently to all asynchronous callbacks.
- Session-scoped diagnostic counters need clear interpretation.

## Evidence

- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Existing architecture summary](../implementation-architecture.md#session-generation-boundaries)
- [Quality strategy](../../quality.md#deterministic-sequence-regression-fixtures)

## Fitness functions

- Deterministic tests inject late disconnect and notification callbacks from an old
  session and assert that current state is unchanged.
- Route-loss tests provide a different device/proxy on a later attempt.
- New-session tests assert decoder, freshness, and pending-transaction reset.
- Reconnect delay remains capped and jitter remains bounded.

## Review triggers

- Home Assistant supplies a stable route abstraction that removes the need to
  refresh `BLEDevice` instances.
- The BLE library introduces a stronger callback-lifetime guarantee.
- Multiple concurrent sessions per entry are proposed.

## Related decisions

- [0001](0001-route-all-ble-access-through-home-assistant.md)
- [0002](0002-maintain-one-persistent-session-per-config-entry.md)
- [0006](0006-decode-notifications-with-a-bounded-incremental-stream-parser.md)
- [0011](0011-serialize-writes-and-require-device-confirmation.md)
