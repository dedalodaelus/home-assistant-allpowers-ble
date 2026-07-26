# 0004. Require an active protocol probe before setup

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from config flow and compatibility policy.

## Context

Bluetooth names and 16-bit vendor service UUIDs are weak compatibility signals.
Similar ALLPOWERS names can identify different protocol revisions, and accepting a
candidate from advertisement data alone could expose incorrect entities or send a
command to an incompatible product.

A stronger setup gate can inspect the actual GATT contract and decode a real status
response before a persistent config entry exists.

## Decision

Before creating a config entry, resolve a connectable route, open a temporary GATT
session, validate service FFF0 plus FFF1 notification and FFF2 write properties,
subscribe, send the known status request, and require a checksum-valid decoded
status frame.

An advertisement remains a discovery candidate, not proof of support.

## Alternatives considered

### Trust advertised name patterns

This is fast and passive but cannot distinguish incompatible revisions or renamed
devices.

### Trust the FFF0 service UUID

This is stronger than a name but still does not prove characteristic properties,
framing, checksum behavior, or payload compatibility.

### Create the entry and validate during first runtime setup

Home Assistant could retry failures, but an unsupported device would already be
represented as configured and could produce confusing partial state.

## Consequences

### Positive

- Setup fails before persistent configuration when the GATT or protocol contract is
  incompatible.
- Generic candidates can be accepted conservatively as read-only only after real
  protocol evidence.
- The entry starts with a device that has demonstrated a valid status path.

### Negative and trade-offs

- Setup consumes an active BLE connection slot and can fail on a weak route.
- The device must be powered, in range, and available during setup.
- A valid read probe does not by itself prove any writable command safe.

## Evidence

- [`config_flow.py`](../../../custom_components/allpowers_ble/config_flow.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Protocol](../../protocol.md)
- [Compatibility](../../compatibility.md)
- [Adding a model](../../adding-models.md)

## Fitness functions

- Config-flow tests cover missing routes, GATT mismatches, invalid frames,
  unsupported models, timeouts, and successful active probes.
- A config entry is asserted to be absent on probe failure.
- Model onboarding requires read-only protocol vectors before write evidence.

## Review triggers

- Pairing or authentication makes temporary probing materially different from
  runtime setup.
- The protocol gains a passive cryptographic or revision advertisement that is
  equally strong and independently verified.
- Home Assistant changes discovery-flow connection guidance.

## Related decisions

- [0001](0001-route-all-ble-access-through-home-assistant.md)
- [0006](0006-decode-notifications-with-a-bounded-incremental-stream-parser.md)
- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [0012](0012-keep-unverified-controls-read-only-or-opt-in.md)
