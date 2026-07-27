# 0014. Apply runtime options without reloading the entry

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from options flow, update listener, and client behavior.

## Context

Status intervals, stale thresholds, watchdog timing, reconnect caps, keepalive, and
experimental feature flags tune an already running connection. Reloading the
config entry for every change would create an avoidable disconnect, discard useful
fresh state, consume proxy connection slots, and make tuning slower.

At the same time, invalid timing relationships can undermine freshness and watchdog
semantics.

## Decision

Represent runtime settings as one immutable, validated `ConnectionOptions` value.
Validate types, ranges, and cross-field safety relationships before acceptance, then
apply a valid replacement to the active client through the config-entry update
listener without reloading the BLE connection.

The running client and entities must react to the new thresholds and feature flags
immediately. A change must not bypass profile capability gates.

## Alternatives considered

### Reload the config entry on every option change

This is a common simple pattern, but it needlessly tears down a healthy GATT
session and introduces route/reconnect failure into local tuning.

### Mutate individual client fields directly from the options flow

This minimizes object replacement but can expose partially applied option sets and
skip relationship validation.

### Allow arbitrary numeric values and rely on user judgment

This increases flexibility but can make stale timeouts shorter than request
intervals or watchdogs shorter than freshness windows.

## Consequences

### Positive

- Timing changes do not cause avoidable BLE disconnects.
- The client observes a coherent validated option set.
- Entity availability reacts immediately to new freshness thresholds.

### Negative and trade-offs

- The client maintenance loop must safely observe option replacement while running.
- Feature enablement may change entity availability without a reload and therefore
  needs explicit tests.
- Options remain constrained to relationships understood by the current runtime.

## Evidence

- [`options.py`](../../../custom_components/allpowers_ble/options.py)
- [`config_flow.py`](../../../custom_components/allpowers_ble/config_flow.py)
- [`__init__.py`](../../../custom_components/allpowers_ble/__init__.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [README runtime options](../../../README.md#runtime-options)

## Fitness functions

- Options tests cover defaults, booleans, ranges, and all cross-field
  relationships.
- Update-listener tests assert the active client receives the replacement without
  entry reload.
- Client/entity tests assert freshness and optional controls react to the new
  values while capability checks remain enforced.

## Review triggers

- An option changes GATT structure or another property that requires reconnection.
- Home Assistant lifecycle guidance requires reload for config-entry options.
- Options become mutable across tasks in a way that cannot be applied atomically.

## Related decisions

- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
- [0012](0012-keep-unverified-controls-read-only-or-opt-in.md)
- [0013](0013-persist-only-configuration-and-validated-options.md)
