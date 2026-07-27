# Quality and test strategy

This project applies current Home Assistant integration patterns but remains a
community custom integration. Home Assistant does not assign official Bronze,
Silver, Gold, or Platinum scores to this repository.

## Automated test layers

| Layer | Coverage |
|---|---|
| Protocol vectors | Header, length, XOR, payload validation, known packets, unknown packets, versions, control frames, bit preservation |
| Stream decoder | Fragmentation, concatenation, noise, oversized payloads, invalid candidates, reset behavior |
| Options | Ranges, cross-field relationships, booleans, defaults, serialization |
| Client state | Freshness, versioned command transactions, disconnect invalidation, serialized writes, delayed refresh, status requests |
| Transport runtime | Route loss, connection retry, GATT validation, notification handling, watchdog, keepalive, shutdown, cancellation |
| Config flow | Discovery, duplicates, unsupported models, active probe outcomes, manual selection, options errors |
| Coordinator/entities | Fresh/stale/disconnected availability, values, controls, service errors, live options |
| Diagnostics/setup | Redaction, serialization, initial readiness, entry unload, Home Assistant stop |
| Real HA lifecycle harness | Runs config-entry lifecycle, registry updates, diagnostics, and service/entity actions against the pinned Home Assistant package in CI |

Lifecycle tests also verify status-before-settings and settings-before-status
ordering, one-time dynamic control-entity registration, and capability downgrade
availability transitions.

The repository sets a coverage failure threshold of 98 percent with branch coverage
enabled. New code should test behavior and failure modes rather than adding lines
only to satisfy the metric.

Config-flow readiness has an additional strict gate: CI runs
`tests/test_config_flow.py` with branch coverage scoped to
`custom_components.allpowers_ble.config_flow` and fails unless coverage is 100%.

## Static and repository checks

- Ruff formatting and linting.
- Mypy type checking for integration source.
- Pylint for integration source.
- Python compilation and custom repository invariant checks.
- HACS validation action.
- Home Assistant Hassfest action.
- CodeQL security analysis.
- Dependency review for pull requests.
- Dependabot for Python packages and GitHub Actions.
- Conventional pull-request titles.

## Reliability practices

- Config flow and unique IDs prevent duplicate device entries.
- Initial setup waits for valid data and uses `ConfigEntryNotReady` semantics.
- Entity availability is based on data freshness, not socket state alone.
- Every active route is resolved through Home Assistant Bluetooth.
- All writes are serialized, require a safe snapshot, and complete through explicit transaction confirmation.
- Unknown settings bits are preserved.
- Background tasks are named, owned, canceled, and awaited.
- Diagnostics redact the Bluetooth address.
- Translations and diagnostics are included from the initial release.

## Remaining validation boundary

## Deterministic sequence regression fixtures

Transport and command-transaction regressions use deterministic fixtures instead of
wall-clock sleeps. Stateful fakes can hold writes, release awaits, inject ordered
notifications, and model stale versus active session callbacks so tests can verify
event-order contracts directly.

Critical sequence scenarios include:

- late old-session disconnect and notification callbacks;
- reconnect, write, and shutdown interleavings;
- consecutive output/settings writes with delayed, duplicate, contradictory, and
  missing confirmations;
- cancellation while awaiting transport operations;
- independent clients for separate config entries.

Automated tests cannot prove radio behavior or command safety on an untested
hardware revision. A model remains experimental until hardware-in-the-loop results
cover telemetry, each writable control, reconnects, proxy failover, and prolonged
operation.
