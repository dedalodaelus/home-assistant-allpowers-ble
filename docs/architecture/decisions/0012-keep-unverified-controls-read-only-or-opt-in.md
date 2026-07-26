# 0012. Keep unverified controls read-only or opt-in

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from model capabilities, entity creation, and defaults.

## Context

The repository has strong evidence for writable controls only on the verified R600
hardware signature. Other R600 revisions, AP S candidates, and generic ALLPOWERS or
FFF0 candidates may decode telemetry after the active probe, but matching reads do
not prove command compatibility. Even on the verified profile, settings keepalive
and car-charger behavior have a narrower evidence base than core telemetry and
outputs.

## Decision

Expose unverified model profiles as read-only and omit their writable entities.
Reject known incompatible model families. Keep settings keepalive and the
experimental car-charger control disabled by default and require explicit user
opt-in where the verified profile permits them.

## Alternatives considered

### Enable all decoded controls for every candidate

This maximizes features but turns incomplete reverse engineering into device-write
risk.

### Reject every device except the exact verified R600 revision

This is simpler and conservative, but prevents useful read-only telemetry and
compatibility evidence from candidate devices that pass the active probe.

### Enable experimental controls by default with a warning

A warning does not prevent automations or users from invoking behavior they did not
intend to test.

### Hide all experimental features from the codebase

This minimizes risk but makes controlled validation and gradual evidence collection
harder.

## Consequences

### Positive

- Read-only support can grow without automatically expanding the write boundary.
- Known incompatible products fail explicitly.
- Users must make a deliberate choice before enabling narrower experimental
  behavior.

### Negative and trade-offs

- Similar-looking stations can expose different entity sets.
- Experimental candidate users may expect controls that are intentionally absent.
- Option flags and profile capabilities add test combinations.

## Evidence

- [`model_support.py`](../../../custom_components/allpowers_ble/model_support.py)
- [`options.py`](../../../custom_components/allpowers_ble/options.py)
- [`entity.py`](../../../custom_components/allpowers_ble/entity.py)
- [Compatibility](../../compatibility.md)
- [README compatibility table](../../../README.md#compatibility)

## Fitness functions

- Model-support tests distinguish the verified R600 signature, unverified R600,
  AP S candidates, generic candidates, and explicit S500/S700 rejection.
- Entity tests assert write entities are absent or unavailable when a capability is
  false.
- Defaults tests assert keepalive and car-charger options are off.
- Hardware-in-the-loop validation is required before changing a profile from
  read-only to writable.

## Review triggers

- Hardware captures and prolonged tests verify a new revision or command.
- A feature moves from experimental to the core supported contract.
- The project decides that read-only candidate support creates too much user
  ambiguity.

## Related decisions

- [0004](0004-require-an-active-protocol-probe-before-setup.md)
- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [0014](0014-apply-runtime-options-without-reloading-the-entry.md)
