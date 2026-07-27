# 0016. Emit persistent Repairs only for actionable failures

- Status: Accepted
- Recorded: 2026-07-28
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None

## Context

The integration already exposes rich diagnostics, but persistent user-facing
guidance was inconsistent for repeated actionable runtime failures. Users could
observe unavailable entities and counters without a deterministic remediation
flow in Home Assistant.

Repairs are useful only when noise is controlled and dismissal conditions are
deterministic. Transient BLE churn, normal reconnects, or one-off transport
events should not produce alerts.

## Decision

Emit entry-scoped persistent Repairs only for actionable failures that satisfy
all of the following:

1. a deterministic persistence threshold is reached;
2. the issue is deduplicated per config entry and issue type;
3. a deterministic automatic dismissal condition exists;
4. the issue includes a concrete user action path.

Current implemented Repair classes are:

- persistent no-route failures (`persistent_no_route`);
- repeated watchdog reconnect failures (`repeated_watchdog_resets`);
- invalid migrated legacy options (`invalid_migrated_options`).

## Alternatives considered

### Alternative A: Keep diagnostics only and avoid Repairs

Rejected because diagnostics require manual interpretation and do not provide
immediate actionable guidance in the normal Home Assistant issue workflow.

### Alternative B: Emit Repairs on first failure event

Rejected because BLE transports are inherently noisy and this would create
alert spam for transient conditions.

## Consequences

### Positive

- Users receive actionable guidance in Home Assistant for persistent failures.
- Repairs remain low-noise through explicit persistence thresholds.
- Issues auto-dismiss on deterministic recovery, reducing manual cleanup.

### Negative and trade-offs

- Additional state tracking is required per config entry.
- Threshold tuning may need revision if Home Assistant runtime behavior changes.
- New Repair classes require translation, tests, and documentation updates.

## Evidence

- Implementation paths:
  - [custom_components/allpowers_ble/repairs.py](../../../custom_components/allpowers_ble/repairs.py)
  - [custom_components/allpowers_ble/coordinator.py](../../../custom_components/allpowers_ble/coordinator.py)
  - [custom_components/allpowers_ble/__init__.py](../../../custom_components/allpowers_ble/__init__.py)
- User-visible translations:
  - [custom_components/allpowers_ble/strings.json](../../../custom_components/allpowers_ble/strings.json)
  - [custom_components/allpowers_ble/translations/en.json](../../../custom_components/allpowers_ble/translations/en.json)
  - [custom_components/allpowers_ble/translations/es.json](../../../custom_components/allpowers_ble/translations/es.json)
- Tests:
  - [tests/test_repairs.py](../../../tests/test_repairs.py)
  - [tests/homeassistant/test_repairs.py](../../../tests/homeassistant/test_repairs.py)

Boundary of evidence: this proves Repairs policy behavior for current thresholds
and issue classes in automated tests; it does not prove hardware-level causality
for all BLE environments.

## Fitness functions

- Unit tests verify persistence thresholds, deduplication, and clear behavior.
- Real Home Assistant lifecycle tests verify multi-entry isolation and
  reload/unload cleanup.
- Translation parity checks validate issue keys in English and Spanish trees.

## Review triggers

- A new Repair class is proposed for another failure category.
- Home Assistant issue-registry API contracts or translation contracts change.
- Persistence thresholds produce operational noise in field reports.
- Migration semantics change for config-entry schema updates.

## Related decisions

- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md):
  Repairs consume snapshot freshness outcomes to avoid transient alerts.
- [0013](0013-persist-only-configuration-and-validated-options.md):
  Repair tracking state remains runtime-only and entry-scoped.
- [0014](0014-apply-runtime-options-without-reloading-the-entry.md):
  timing changes can influence watchdog/no-route persistence behavior.
- [0015](0015-redact-device-identifiers-from-diagnostics.md):
  Repairs complement diagnostics while preserving privacy boundaries.

## Notes

- 2026-07-28: Initial accepted record created from implemented Repairs behavior.
