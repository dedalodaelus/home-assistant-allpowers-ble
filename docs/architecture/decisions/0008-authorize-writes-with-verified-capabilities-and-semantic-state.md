# 0008. Authorize writes with verified capabilities and semantic state

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from model policy, semantics, and client write gates.

## Context

Passing a checksum proves frame integrity, not that a payload is meaningful for a
specific hardware revision. Similar models may use different flag layouts, and a
structurally valid state can contain reserved modes, unsupported values, or unknown
bits that make a read-modify-write unsafe.

Write capability therefore needs stronger evidence than discovery or successful
telemetry decoding.

## Decision

Authorize each write only when all of the following are true:

1. the detected model and exact hardware profile expose the required capability;
2. the client is connected in the active GATT session;
3. the relevant source snapshot is present and fresh;
4. profile-specific semantic validation says that snapshot can safely authorize
   the requested command; and
5. no previous transaction makes that source version unsafe to reuse.

Keep capability flags in immutable model-profile data. Continue exposing decoded
invalid or unknown values to diagnostics while denying dependent writes.

## Alternatives considered

### Enable writes for every device that passes the read probe

This treats protocol framing as command compatibility and can modify an unsupported
bit layout.

### Gate only by product-name family

Names do not prove hardware revision or command semantics.

### Clamp or normalize reserved values before writing

This invents device state and may change unrelated behavior.

### Hide semantically invalid frames entirely

This prevents unsafe writes but removes evidence needed to understand a new
revision or device fault.

## Consequences

### Positive

- Writable behavior is explicit, profile-specific, and evidence driven.
- Structurally valid but semantically unsafe state cannot authorize a command.
- Unknown devices can still contribute read-only telemetry and diagnostics.

### Negative and trade-offs

- New writable profiles require hardware evidence, policy changes, semantic rules,
  and regression tests.
- Users may see telemetry for a candidate device while controls remain absent.
- Conservative rejection can block a command that the device might have accepted.

## Evidence

- [`model_support.py`](../../../custom_components/allpowers_ble/model_support.py)
- [`protocol/semantics.py`](../../../custom_components/allpowers_ble/protocol/semantics.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Compatibility](../../compatibility.md)
- [Adding a model](../../adding-models.md)

## Fitness functions

- Model-support tests cover verified revisions, unverified revisions, broad family
  candidates, and explicit incompatible products.
- Semantic tests reject unknown output bits, reserved work modes, and unsupported
  ECO timeout values for the verified profile.
- Entity and client tests confirm that capability-denied controls are unavailable
  and cannot write.
- Hardware-in-the-loop validation remains mandatory before a profile gains write
  capabilities.

## Review triggers

- A new hardware revision or protocol family is proposed for writable support.
- Captures prove different semantic constraints for an existing profile.
- Device authentication or negotiated capabilities become available.
- Read-only diagnostics reveal values currently rejected by the semantic policy.

## Related decisions

- [0004](0004-require-an-active-protocol-probe-before-setup.md)
- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
- [0009](0009-preserve-unrelated-and-unknown-protocol-bits.md)
- [0011](0011-serialize-writes-and-require-device-confirmation.md)
- [0012](0012-keep-unverified-controls-read-only-or-opt-in.md)
