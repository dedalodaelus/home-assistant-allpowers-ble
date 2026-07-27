# 0006. Decode notifications with a bounded incremental stream parser

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from protocol codec and tests.

## Context

BLE notification boundaries are not a reliable application-frame boundary. One
notification may contain a partial frame, several frames, leading noise, or bytes
from a malformed candidate. An unbounded buffer would allow corrupted length data
or prolonged noise to consume memory indefinitely.

## Decision

Feed notification bytes into a stateful incremental decoder that can retain partial
frames, emit multiple complete frames, discard invalid leading bytes to
resynchronize, validate frame boundaries and checksums, and enforce explicit
maximum payload and buffer bounds. Reset the decoder at every GATT session boundary.

## Alternatives considered

### Decode each notification as one complete frame

This is simple but fails under fragmentation or concatenation and turns transport
packetization into an undocumented protocol requirement.

### Append bytes without a maximum bound

This handles fragmentation but risks unbounded growth when length fields or input
streams are corrupt.

### Disconnect on the first malformed byte

This is fail-fast but makes recoverable radio noise cause avoidable connection
churn and reduces diagnostic information about stream quality.

## Consequences

### Positive

- The protocol layer tolerates realistic fragmentation, concatenation, and noise.
- Buffer growth is bounded.
- Parser discards and protocol failures can be counted separately from transport
  disconnects.

### Negative and trade-offs

- Resynchronization logic is stateful and requires extensive sequence tests.
- Discarding bytes can hide repeated upstream corruption unless counters and logs
  are monitored.
- Parser state must never cross a GATT session generation.

## Evidence

- [`protocol/codec.py`](../../../custom_components/allpowers_ble/protocol/codec.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Protocol](../../protocol.md)
- [Quality strategy](../../quality.md)

## Fitness functions

- Stream-decoder tests cover partial frames, concatenated frames, leading noise,
  invalid headers, invalid lengths, checksums, oversized candidates, bounded
  buffers, and reset behavior.
- Client tests verify decoder reset when a new GATT session begins.
- Diagnostics preserve separate discard and protocol-error counters.

## Review triggers

- Captures demonstrate a different framing or escaping mechanism.
- Maximum payload assumptions change for a new model family.
- The transport provides guaranteed message boundaries that can be proved for all
  supported routes.

## Related decisions

- [0004](0004-require-an-active-protocol-probe-before-setup.md)
- [0005](0005-keep-the-protocol-core-in-repository-and-home-assistant-independent.md)
- [0010](0010-re-resolve-routes-and-isolate-session-generations.md)
