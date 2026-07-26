# 0005. Keep the protocol core in repository and Home Assistant independent

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from package boundaries and existing documentation.

## Context

Protocol framing, decoding, semantic values, and command construction need fast
iteration with the integration while the verified hardware scope remains narrow.
At the same time, those rules are easier to test and reason about when they do not
depend on Home Assistant or BLE transport objects.

Publishing a separate package would add release coordination and compatibility
management before a second consumer or independent versioning need exists.

## Decision

Keep the protocol implementation under
`custom_components/allpowers_ble/protocol/`, but make that package pure Python and
independent from Home Assistant and transport APIs. Transport and entity layers may
depend on the protocol package; the protocol package must not depend on them.

## Alternatives considered

### Mix decoding and encoding into the BLE client

This reduces file count but couples byte-level rules to asynchronous transport and
makes protocol vectors harder to test independently.

### Publish a separate PyPI package now

This creates a reusable artifact, but adds release ordering, dependency pinning,
and compatibility burden without a demonstrated external consumer.

### Generate protocol code from a formal schema

A schema could improve rigor, but the reverse-engineered protocol and semantic
write constraints are not currently expressed in a suitable authoritative schema.

## Consequences

### Positive

- Protocol tests run without Home Assistant or radio dependencies.
- Framing and command behavior remain reusable and deterministic.
- Integration and protocol changes can land atomically in one repository.

### Negative and trade-offs

- The protocol has no independent semantic version or distribution artifact.
- Repository layout alone does not enforce dependency direction unless tests or
  static checks inspect imports.
- Extraction later will require defining a public compatibility contract.

## Evidence

- [`protocol/__init__.py`](../../../custom_components/allpowers_ble/protocol/__init__.py)
- [`protocol/codec.py`](../../../custom_components/allpowers_ble/protocol/codec.py)
- [`protocol/models.py`](../../../custom_components/allpowers_ble/protocol/models.py)
- [`protocol/semantics.py`](../../../custom_components/allpowers_ble/protocol/semantics.py)
- [Existing design decisions](../../design-decisions.md#keep-protocol-code-inside-the-integration-but-independent-from-home-assistant)

## Fitness functions

- Protocol tests import and exercise the package without bootstrapping Home
  Assistant.
- A repository invariant or import-boundary test should fail if a protocol module
  imports `homeassistant`, client, coordinator, or entity modules.
- Protocol inputs and outputs remain immutable value objects or bytes.

## Review triggers

- A second independent consumer needs the protocol.
- Multiple protocol families require independent release cadence.
- External contributors need a stable public protocol API separate from Home
  Assistant releases.

## Related decisions

- [0006](0006-decode-notifications-with-a-bounded-incremental-stream-parser.md)
- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [0009](0009-preserve-unrelated-and-unknown-protocol-bits.md)
