# 0009. Preserve unrelated and unknown protocol bits

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from protocol models, codecs, and safe command construction.

## Context

The vendor protocol combines multiple controls into shared flag fields. It is also
reverse engineered, so hardware revisions may use bits that the integration does
not yet understand. Constructing a command from guessed defaults or only the one
visible control can silently alter another output or setting.

## Decision

Construct shared-field commands by starting from the latest authorized raw device
state, changing only the documented mask, and preserving every unrelated bit.

For verified output commands, preserve all documented sibling output states and do
not claim preservation of undocumented output-command semantics. For settings,
preserve unknown raw flag bits through read-modify-write. Reject the operation when
semantic validation shows that safe preservation cannot be proved.

## Alternatives considered

### Build commands from zero or fixed defaults

This is deterministic but can clear unknown or unrelated device state.

### Send only the requested bit

The GATT protocol expects a complete shared field, so an isolated bit value may
implicitly disable sibling controls.

### Normalize unknown bits to known values

This hides revision differences and changes behavior without evidence.

### Never write any shared field

This is safest but would remove controls that have been verified on the supported
R600 revision.

## Consequences

### Positive

- A one-control change does not intentionally overwrite sibling state.
- Revision-specific unknown settings bits survive supported writes.
- Raw protocol evidence remains available for future analysis.

### Negative and trade-offs

- Every shared-field write depends on a fresh source snapshot.
- Output preservation is limited to semantics demonstrated for the verified
  command mapping.
- Unknown status bits can force conservative rejection instead of a best-effort
  write.

## Evidence

- [`protocol/codec.py`](../../../custom_components/allpowers_ble/protocol/codec.py)
- [`protocol/models.py`](../../../custom_components/allpowers_ble/protocol/models.py)
- [`protocol/semantics.py`](../../../custom_components/allpowers_ble/protocol/semantics.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Protocol](../../protocol.md)
- [Existing architecture summary](../implementation-architecture.md#safe-command-construction)

## Fitness functions

- Protocol vectors verify bit-preserving settings mutations.
- Output-command tests change one output while asserting documented sibling states
  remain represented in the command.
- Semantic tests deny writes when unknown status bits make preservation unsafe.
- No command encoder may fall back to a guessed default shared field.

## Review triggers

- Captures identify semantics for currently unknown bits.
- A new model uses independent command fields.
- The device exposes a compare-and-set or atomic single-control command.
- Hardware evidence contradicts the current output-command mapping.

## Related decisions

- [0005](0005-keep-the-protocol-core-in-repository-and-home-assistant-independent.md)
- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [0011](0011-serialize-writes-and-require-device-confirmation.md)
