# Compatibility

## Support levels

The repository distinguishes three levels:

- **Verified**: protocol behavior has been exercised on the named hardware and is
  covered by known vectors and integration behavior.
- **Experimental**: the device advertises a candidate name and passes the active
  GATT/protocol probe, but the exact hardware revision has not been fully
  validated. These devices run in read-only mode.
- **Rejected**: available evidence indicates another protocol revision; setup is
  stopped before a persistent entry is created.

## Current matrix

| Model or advertised family | Level | Setup behavior |
|---|---|---|
| ALLPOWERS R600 (`R600*`, `AP R*`) with verified revision signature (`hardware_version=1.2`, `raw_hardware_version=0x12`) | Verified | Accepted after active probe with writable controls enabled. Output writes are rejected if status includes unknown output-related bits; settings writes are rejected when semantic safety checks fail. |
| AP S300 / other `AP S*` candidates | Experimental | Accepted only if FFF0/FFF1/FFF2 and a valid status response are confirmed. Telemetry only; writable entities are not created. |
| AP S500 | Rejected | Aborted as a known different protocol family. |
| AP S700 V2 | Rejected | Aborted as a known different protocol family. |
| Generic `ALLPOWERS*` | Experimental candidate | Requires active probe. Telemetry only; writable entities are not created. |
| Service UUID FFF0 without a useful name | Experimental candidate | Requires active probe. Telemetry only; writable entities are not created. |

## Why advertisement matching is broad

Bluetooth advertisements can vary by firmware and proxy. The manifest includes
candidate local-name patterns and the FFF0 service UUID so Home Assistant can route
discovery to the config flow. This is only the first filter.

The second filter is model policy. The final and authoritative filter is the active
probe, which requires:

1. a connectable route selected by Home Assistant;
2. service FFF0;
3. notify characteristic FFF1;
4. write characteristic FFF2;
5. a valid status response after the known request;
6. a frame that passes header, length, checksum, and payload validation.

## Hardware revisions

A commercial model name can cover more than one controller, BLE module, or firmware
family. A report must include the exact label/revision and advertised name. Do not
change a rejected family to supported merely because it exposes one matching UUID.

## Verified revision evidence lane

Every profile that remains marked as **Verified** must keep an evidence record in
`tests/hil/qualification_matrix.json` with:

- a route result for `local_adapter`;
- a route result for `active_proxy`;
- scenario status for replay, reconnect/failover, rapid writes, stale/timeout,
  upgrade/reload, vendor-app contention, and soak;
- write-capability status for every exposed writable control.

Capture artifacts must stay sanitized and stored under
`tests/hil/fixtures/<revision-id>/`. Do not publish Bluetooth addresses, serial
numbers, Home Assistant identifiers, or topology details.

Use the [HIL qualification runbook](hil-qualification.md) for the end-to-end
capture, matrix update, and validation flow.

## Reporting a result

Use the public issue forms to report compatibility and bugs:

- Compatibility reports:
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/new/choose>
- Bug reports:
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/new/choose>
- Security reports (private):
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/security/advisories/new>

For a compatibility report, provide:

- product model and revision from the physical label;
- advertised Bluetooth name;
- Home Assistant and integration versions;
- adapter/proxy model and ESPHome version when relevant;
- whether discovery, probe, telemetry, and each control succeeded;
- sanitized diagnostics;
- packet evidence only after removing addresses and identifying information.

A positive status probe does not automatically make write features safe. Each
shared bit field and hardware revision must be verified independently before
write controls are enabled.

For verified profiles, write authorization depends on both freshness and semantic
validation of the latest snapshot. Structurally valid packets can still be shown
for diagnostics even when they are not trusted for writes.

## Security and trust model

Write authorization is a local trust decision and depends on multiple layers:

- Home Assistant user permissions control who can issue entity writes.
- BLE proximity constrains range but does not remove local radio risk.
- ESPHome proxies and local adapters are part of the trusted transport boundary.
- Experimental/read-only profiles are intentionally prevented from creating write entities.

For safety, avoid unattended automation of critical loads. Prefer notifications,
manual acknowledgements, and explicit conditions for connectivity, telemetry
freshness, and battery thresholds before issuing non-critical control actions.

## Entity semantics

Binary sensors for power flow use measured power direction only:

- `Input active`: `input_power_w > 0`
- `Output active`: `output_power_w > 0`

These entities do not claim battery charging/discharging direction because the
current protocol evidence does not provide a verified battery-flow field.

### Why profile verification matters for write access

The BLE protocol used by ALLPOWERS devices does not provide cryptographic device
authentication. Write authorization therefore depends on:

1. **Hardware identity verification**: The exact hardware revision and firmware
   must be confirmed to match the integration's tested and documented capability
   profile.
2. **Protocol stability**: Flag bits, reserved fields, and payload structure must
   be fully understood to perform safe read-modify-write operations without
   corrupting device state.
3. **Semantic validation**: Detected inconsistencies in state (e.g., unknown flag
   bits in status) trigger write rejection rather than guessing at safe behavior.

### Read-only mode for experimental devices

Devices that pass active protocol validation but cannot be matched to a verified
hardware revision profile operate in read-only mode. This includes:

- R600 devices with an unverified hardware revision signature.
- AP S* protocol family candidates.
- Generic ALLPOWERS advertisements without a specific model match.

Even if such a device successfully responds to status requests, writable control
entities are not created. Telemetry collection continues, allowing long-term
monitoring and data gathering to support future hardware verification.

### Adding a new profile with write capabilities

To enable write controls for a new hardware model requires:

1. Capturing representative traffic from the exact hardware revision on your
   physical device.
2. Documenting all behavior: status flag bit meanings, settings byte layout,
   output command encoding.
3. Adding regression tests that validate the protocol behavior for that revision.
4. Explicitly merging a model profile change to the integration with a `verified`
   classification.

Contributing a new unverified device that merely exposes the FFF0 service will
not enable writes. The integration distinguishes between "device is working" and
"device is safe to write to."

When a verified capability profile is discovered later from settings telemetry,
the integration adds writable control entities exactly once without requiring a
config-entry reload. If capabilities later downgrade to read-only, existing
control entities remain in the registry but become unavailable.

### Trust boundary assumptions

Write authorization assumes:

- **Local network isolation**: Only devices on the local network can be discovered
  or issued commands.
- **Physical proximity**: BLE range limits attack surface to nearby locations.
- **Home Assistant access control**: Only users with configuration or entity
  control permissions can issue commands.
- **No credential compromise**: The protocol uses no shared secrets; there is no
  token or key to leak remotely.
