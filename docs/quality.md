# Quality and 1.0 readiness contract

This project applies current Home Assistant integration patterns but remains a
community custom integration. Home Assistant does not assign official Bronze,
Silver, Gold, or Platinum scores to this repository.

The readiness contract below is an internal release gate for this repository.
It documents measurable criteria for `devel` to `main` stable promotion and does
not claim Home Assistant Core certification.

## 1.0 readiness contract

Each criterion is measurable and links to evidence. Any unmet criterion must be
marked as `Blocked` with an issue reference, or `N/A` with an explicit rationale.

| Criterion | How it is measured | Evidence | Status | Blocking issues |
|---|---|---|---|---|
| IQS-01 custom integration scope is explicit | `README.md` and roadmap state this is a community custom integration and not Home Assistant Core certification | [README project status](../README.md#project-status), [Roadmap quality flow](roadmap.md#release-and-quality-flow) | Done | None |
| IQS-02 rule-by-rule quality contract is maintained | This section exists with measurable criteria, evidence links, and explicit status per criterion | [Quality contract](#10-readiness-contract), [Repository validator](../scripts/validate_repository.py) | Done | None |
| IQS-03 lifecycle, reconfigure, and repairs behavior is regression-tested | CI executes lifecycle, config-flow, and repairs tests and enforces coverage thresholds | [Lifecycle tests](../tests/homeassistant/test_lifecycle_runtime.py), [Config flow tests](../tests/test_config_flow.py), [Repairs tests](../tests/test_repairs.py), [CI workflow](../.github/workflows/ci.yml) | Done | None |
| IQS-04 diagnostics redaction and repository safety checks are enforced | Diagnostics tests and repository checks verify redaction, required files, metadata, and static-analysis contracts | [Diagnostics tests](../tests/test_diagnostics.py), [Repository validator](../scripts/validate_repository.py) | Done | None |
| IQS-05 hardware qualification evidence exists for verified revisions | Qualification matrix and sanitized fixtures are present and validated in CI | [Qualification matrix](../tests/hil/qualification_matrix.json), [HIL runbook](hil-qualification.md), [HIL validator](../scripts/validate_hil_qualification.py) | Done | None |
| IQS-06 stable promotion is blocked until release blockers are cleared | Stable milestone close requires zero open `release-blocker` issues and all criteria marked `Done` or justified `N/A` | [Release checklist](release-checklist.md), [Merge gate workflow](../.github/workflows/merge-gate.yml), [Issue #55](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/55) | Done | None |
| IQS-07 Home Assistant Core maintainership requirement | Not applicable to this repository because it is intentionally distributed as a custom integration through HACS/manual install | [README project status](../README.md#project-status), [Roadmap quality flow](roadmap.md#release-and-quality-flow) | N/A | Not applicable by scope |

## Stable release review cadence

The readiness contract is reviewed on every stable promotion PR from `devel` to
`main`.

Required review actions for each stable promotion:

- confirm every readiness criterion remains `Done` or justified `N/A`;
- verify no open `release-blocker` issues remain;
- update evidence links if files/tests/workflows moved;
- update roadmap release notes using the same criterion IDs.

## Roadmap alignment

Milestone completion and roadmap readiness statements must use the same criteria
defined in this document. If roadmap text claims readiness, it must reference
the matching `IQS-*` criterion and evidence.

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
| Repairs | Persistence thresholds, deduplication, deterministic dismissal, reload/unload cleanup, multi-entry isolation |
| Diagnostics/setup | Redaction, serialization, initial readiness, entry unload, Home Assistant stop |
| Real HA lifecycle harness | Runs config-entry lifecycle, registry updates, diagnostics, and service/entity actions against the pinned Home Assistant package in CI |

Lifecycle tests also verify status-before-settings and settings-before-status
ordering, one-time dynamic control-entity registration, and capability downgrade
availability transitions.

Maintenance scheduler tests use monotonic-time fixtures to prove:

- status requests never run faster than `status_interval`;
- settings keepalive never runs faster than `settings_keepalive_interval`;
- watchdog and reconnect recovery traffic is isolated from normal polling cadence.

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
- Repairs are emitted only for persistent actionable states and are dismissed automatically on deterministic recovery.
- Translations and diagnostics are included from the initial release.

## Remaining validation boundary

Hardware qualification evidence is tracked in
`tests/hil/qualification_matrix.json` with sanitized captures under
`tests/hil/fixtures/`. CI validates schema and redaction via
`python scripts/validate_hil_qualification.py` so fixture regressions are visible
even when hardware is not attached.

The operational procedure for collecting real-device evidence is documented in
`docs/hil-qualification.md`.

For stable promotions that require HIL evidence, maintainers can enforce the
`HIL qualification (opt-in)` check by enabling
`REQUIRE_HIL_STABLE_GATE=true` in repository variables.

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
