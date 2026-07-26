# 0001. Route all BLE access through Home Assistant

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented
- Supersedes: None
- Superseded by: None
- Provenance: Retrospective reconstruction from existing implementation and documentation.

## Context

A Home Assistant installation may expose several local adapters and connectable
ESPHome Bluetooth Proxies. Home Assistant owns discovery, availability, and route
selection across those transports. Direct BlueZ access would bypass that model,
reduce portability, and pin the integration to host-specific behavior.

The integration still needs a real `BLEDevice` and an active connection path for
GATT notifications and writes.

## Decision

Resolve connectable BLE devices through Home Assistant Bluetooth APIs and establish
connections through `bleak-retry-connector`. Do not open BlueZ directly and do not
implement a separate proxy-routing layer inside the integration.

## Alternatives considered

### Use direct BlueZ or Bleak discovery

This could work on a Linux host with a local adapter, but it would bypass Home
Assistant ownership, conflict with proxy routing, and create a second source of
adapter lifecycle truth.

### Pin a config entry to the adapter or proxy used during discovery

This simplifies a connection attempt but prevents transparent recovery when route
quality or proxy availability changes.

### Implement a custom network bridge to a proxy

This duplicates Home Assistant Bluetooth functionality and expands the security,
compatibility, and maintenance surface.

## Consequences

### Positive

- Local adapters and connectable ESPHome proxies use the same integration code.
- Home Assistant remains the authority for route availability and selection.
- Proxy failover can occur without changing the config entry.

### Negative and trade-offs

- The transport runtime cannot operate as a generic standalone Python program.
- Behavior depends on Home Assistant Bluetooth API contracts.
- A passive-only proxy can advertise a station but cannot satisfy setup.

## Evidence

- [`__init__.py`](../../../custom_components/allpowers_ble/__init__.py)
- [`config_flow.py`](../../../custom_components/allpowers_ble/config_flow.py)
- [`client.py`](../../../custom_components/allpowers_ble/client.py)
- [Existing architecture summary](../../architecture.md)
- [README Bluetooth Proxy section](../../../README.md#bluetooth-proxy)

## Fitness functions

- Setup and config-flow tests require a Home Assistant-resolved connectable device.
- Runtime tests model route loss and reconnection through a fresh device lookup.
- Repository checks should reject direct BlueZ transport code outside Home
  Assistant abstractions if such code is proposed.

## Review triggers

- Home Assistant replaces or materially changes its Bluetooth routing APIs.
- A supported deployment must run without Home Assistant.
- Device authentication requires transport behavior unavailable through the Home
  Assistant route abstraction.

## Related decisions

- [0002](0002-maintain-one-persistent-session-per-config-entry.md)
- [0004](0004-require-an-active-protocol-probe-before-setup.md)
- [0010](0010-re-resolve-routes-and-isolate-session-generations.md)
