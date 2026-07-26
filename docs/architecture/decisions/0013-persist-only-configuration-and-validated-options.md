# 0013. Persist only configuration and validated options

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from config-entry schema, migration, and runtime models.

## Context

Telemetry, parser fragments, freshness timestamps, pending transactions, and
connection counters describe one volatile BLE session. Restoring any of them after
a process restart could make old device state appear authoritative. Persisting
high-frequency values would also create unnecessary storage churn.

The integration still needs stable identity and user-selected runtime behavior
across restarts.

## Decision

Persist only config-entry identity data and validated options. Keep telemetry,
settings snapshots, decoder buffers, freshness, pending command transactions,
connection state, and diagnostic counters in memory. On restart, require a new
connectable route and fresh protocol data before entities or controls become
available.

Version the config-entry schema and perform explicit, idempotent migrations without
network I/O. Reject unsupported future schemas.

## Alternatives considered

### Persist the latest device snapshot

This can make entities look populated immediately after restart, but the values are
not current and must not authorize commands.

### Persist counters and parser state

Long-term counters may help analysis, but parser fragments and session counters do
not have valid meaning across a new GATT connection.

### Store integration state in a separate database

This enables history but duplicates Home Assistant recorder concerns and adds a
migration and privacy surface.

### Avoid schema versioning

This reduces code now but makes future option or identity changes ambiguous and
unsafe.

## Consequences

### Positive

- Restart cannot reuse an old command shadow or freshness claim.
- Storage writes remain limited to deliberate configuration changes.
- Migration behavior is explicit and testable.

### Negative and trade-offs

- Diagnostic counters reset with the runtime session.
- Entities remain unavailable until fresh data arrives after restart.
- Long-term transport reliability analysis requires external logs or Home
  Assistant history rather than persisted client counters.

## Evidence

- [`__init__.py`](../../../custom_components/allpowers_ble/__init__.py)
- [`const.py`](../../../custom_components/allpowers_ble/const.py)
- [`models.py`](../../../custom_components/allpowers_ble/models.py)
- [`options.py`](../../../custom_components/allpowers_ble/options.py)
- [Existing architecture summary](../implementation-architecture.md#persistence)

## Fitness functions

- Migration tests cover `1.0` to `1.1`, idempotence, normalized values, safe
  defaults, invalid options, and unsupported future versions.
- Setup tests require a valid current status frame before readiness.
- No runtime snapshot or transaction type is serialized into config-entry data.

## Review triggers

- Durable reliability counters or state restoration become a product requirement.
- Home Assistant adds an approved restore-state contract for this integration type.
- A schema change adds or removes identity or option fields.

## Related decisions

- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
- [0014](0014-apply-runtime-options-without-reloading-the-entry.md)
- [0015](0015-redact-device-identifiers-from-diagnostics.md)
