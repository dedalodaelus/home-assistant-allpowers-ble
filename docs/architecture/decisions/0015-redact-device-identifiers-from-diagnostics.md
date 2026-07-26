# 0015. Redact device identifiers from diagnostics

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from diagnostics implementation and public documentation.

## Context

Diagnostics are intended to expose enough runtime, protocol, and compatibility
state to investigate failures. A downloadable payload can also contain BLE
addresses, device-provided names, config-entry titles, user-selected labels, or
identifiers embedded inside nested error strings. Those values can identify a
household device or reveal local infrastructure.

Simple top-level key redaction is insufficient because identifiers can appear in
nested dataclasses, free-form text, and device-registry fields.

## Decision

Serialize diagnostics into structured data, then recursively sanitize the complete
payload before export. Redact Bluetooth addresses in structured fields and nested
strings, replace device and user names with stable markers, and retain only a
sanitized error category and non-sensitive detail. Do not include cloud credentials
because the integration has no cloud credential boundary.

## Alternatives considered

### Export raw diagnostics and rely on users to edit them

This preserves maximum evidence but places privacy responsibility on every reporter
and makes accidental disclosure likely.

### Redact only the config-entry address field

This misses addresses and names embedded in snapshots, registry metadata, and error
strings.

### Omit snapshots and errors entirely

This minimizes privacy risk but removes much of the evidence needed to diagnose
protocol, freshness, and compatibility failures.

### Hash identifiers

Stable hashes still permit correlation across reports and add complexity without a
current diagnostic requirement.

## Consequences

### Positive

- Users can share diagnostics with a lower risk of exposing local identifiers.
- Structured model, freshness, capability, and counter evidence remains available.
- Sanitization applies consistently to entry-level and device-level diagnostics.

### Negative and trade-offs

- Redaction can remove a device name that would otherwise help distinguish models.
- New free-form fields may create leakage if sanitization rules are not extended.
- Regex-based address removal cannot prove that every future identifier format is
  covered.

## Evidence

- [`diagnostics.py`](../../../custom_components/allpowers_ble/diagnostics.py)
- [README diagnostics section](../../../README.md#diagnostics-and-logging)
- [Quality strategy](../../quality.md)

## Fitness functions

- Diagnostics tests place addresses and sensitive names in nested structured and
  free-form fields and assert their absence from serialized output.
- Tests preserve a safe error category while sanitizing details.
- Any new diagnostic field is reviewed for identifiers and included in recursive
  sanitization tests.

## Review triggers

- Diagnostics add protocol captures, advertisements, serial numbers, network
  addresses, or other new identifiers.
- Home Assistant changes its diagnostics-redaction API or privacy guidance.
- Stable correlation across reports becomes a requirement and a privacy design is
  approved.

## Related decisions

- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
- [0013](0013-persist-only-configuration-and-validated-options.md)
