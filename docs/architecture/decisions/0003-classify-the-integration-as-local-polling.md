# 0003. Classify the integration as local polling

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from the manifest and runtime behavior.

## Context

Telemetry arrives locally through BLE notifications and the coordinator publishes
state to entities without a Home Assistant polling interval. However, the runtime
periodically writes an explicit status request to stimulate fresh state. The IoT
classification should describe how state is obtained, not only how the coordinator
notifies entities.

## Decision

Declare `iot_class` as `local_polling` in the integration manifest while retaining
push publication from the BLE client to the coordinator.

## Alternatives considered

### Declare local push

Notifications are push-like, but periodic explicit status requests mean state
acquisition is not purely unsolicited.

### Implement coordinator polling

This would make the classification visually obvious but duplicate the client
maintenance loop and split connection ownership across two schedulers.

### Omit the IoT class

This would make integration metadata less precise and fail expected Home Assistant
manifest conventions.

## Consequences

### Positive

- Metadata reflects that all communication is local and state is actively
  requested.
- The client can keep one coherent timing and watchdog loop.
- Entities still receive low-latency push updates from notifications.

### Negative and trade-offs

- Readers may incorrectly assume the coordinator calls a conventional update
  method on a fixed interval.
- The distinction between acquisition and publication needs explicit
  documentation.

## Evidence

- [`manifest.json`](../../../custom_components/allpowers_ble/manifest.json)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [`coordinator.py`](../../../custom_components/allpowers_ble/coordinator.py)
- [Existing design decisions](../../design-decisions.md#classify-the-integration-as-local-polling)

## Fitness functions

- Manifest validation confirms a valid IoT class.
- Coordinator tests confirm `update_interval` is not used for transport polling.
- Runtime tests confirm periodic status requests are owned by the BLE client.

## Review triggers

- The station begins sending all required state without explicit requests.
- Home Assistant changes IoT-class definitions or manifest requirements.
- Polling responsibility moves from the client to the coordinator.

## Related decisions

- [0002](0002-maintain-one-persistent-session-per-config-entry.md)
- [0007](0007-publish-immutable-snapshots-and-gate-operations-by-freshness.md)
