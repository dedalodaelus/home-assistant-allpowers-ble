# 0011. Serialize writes and require device confirmation

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from versioned command transactions and tests.

## Context

Output and settings commands are derived from shared device state. If two writes
are constructed concurrently from the same snapshot, the second can overwrite the
first. A successful GATT write only confirms that bytes reached the transport; it
does not prove that the station applied the intended state.

Delayed, duplicate, contradictory, or missing notifications must not allow stale
state to authorize later commands.

## Decision

Serialize all commands for a config entry with one write lock. Before sending a
shared-state command, open a versioned transaction containing the active session
generation, source state version, expected result, and confirmation deadline.
Complete the transaction only when a matching device notification arrives from the
same session. Block reuse of the source state while confirmation is pending, and
invalidate transactions on disconnect, new session, or timeout.

Issue #197 tightens the earlier optimistic chaining contract from issue #24:
reject dependent commands without queuing and never use a requested target as a
snapshot. Publish `SENDING` before the awaited write, retain matching observations
until transport return, and enter `AWAITING_CONFIRMATION` if not converged. A
contradictory observation during sending revokes provisional convergence. Failed
or canceled writes require newer telemetry even if a matching packet was seen
during the await. An unknown group is represented by its blocked version threshold,
not a rolled-back requested state.

Manual keepalive obeys the same guard. Automatic keepalive defers pending settings
until the next keepalive interval, then renews an unsafe session rather than
replaying user mutations or relying indefinitely on status requests to yield
settings. The new session requires its own fresh `0x03`. Early initial-keepalive
recovery uses bounded status requests. Without settings across sessions, controls
remain unavailable and session renewal is paced by the keepalive interval; no
physical inactivity-cutoff prevention is promised. Genuine no-op commands create
no transaction, but cannot bypass any existing guard. Explicit keepalive remains
an intentional unchanged write.
Existing freshness timeouts are conservative guard deadlines, not qualified R600
`0x03` response timing. The protocol has no transaction ID; matching same-session
telemetry proves observed convergence, not which write or actor caused it.

## Alternatives considered

### Treat the GATT write call as success

This measures transport acceptance, not device state, and can produce optimistic
state that the station never applied.

### Allow parallel writes and let later commands win

This improves throughput but loses deterministic read-modify-write ordering and can
revert sibling bits.

### Update a local optimistic shadow immediately

This improves UI responsiveness but makes local assumptions authoritative and
creates reconciliation complexity after rejection or disconnect.

### Sleep for a fixed delay after every write

This reduces overlap but does not identify the confirming state or handle variable
radio latency reliably.

## Consequences

### Positive

- Commands have deterministic ordering.
- Transaction confirmation is tied to observed device-state convergence. Service
  completion reports transport submission, not physical acknowledgement.
- A timed-out or disconnected operation cannot silently seed the next command.
- Race behavior can be tested with deterministic notification sequences.

### Negative and trade-offs

- GATT writes are serialized per station; dependent writes are blocked separately
  for the output and settings groups until their confirmation arrives.
- A command can fail even when the device applied it but confirmation was lost.
- More transaction and timeout state must be maintained and cleared correctly.

## Evidence

- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Existing architecture summary](../implementation-architecture.md#safe-command-construction)
- [Quality strategy](../../quality.md#deterministic-sequence-regression-fixtures)
- [README safety model](../../../README.md#safety-model)

## Fitness functions

- Tests hold one write, start another, and assert serialization.
- Sequence fixtures cover delayed, duplicate, contradictory, missing, and
  old-session confirmations.
- Tests assert pending transactions are invalidated by disconnect, new session,
  shutdown, and timeout.
- Completion requires a newer matching status or settings version from the active
  generation.

## Review triggers

- The protocol adds command acknowledgements with transaction identifiers.
- Hardware evidence proves independent commands can execute safely in parallel.
- Home Assistant service timeout expectations require a different confirmation
  contract.

## Related decisions

- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [0009](0009-preserve-unrelated-and-unknown-protocol-bits.md)
- [0010](0010-re-resolve-routes-and-isolate-session-generations.md)
