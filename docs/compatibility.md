# Compatibility

## Support levels

The repository distinguishes three levels:

- **Verified**: protocol behavior has been exercised on the named hardware and is
  covered by known vectors and integration behavior.
- **Experimental**: the device advertises a candidate name and passes the active
  GATT/protocol probe, but the exact hardware has not been fully validated.
- **Rejected**: available evidence indicates another protocol revision; setup is
  stopped before a persistent entry is created.

## Current matrix

| Model or advertised family | Level | Setup behavior |
|---|---|---|
| ALLPOWERS R600 (`R600*`, `AP R*`) | Verified | Accepted after active probe. |
| AP S300 / other `AP S*` candidates | Experimental | Accepted only if FFF0/FFF1/FFF2 and a valid status response are confirmed. |
| AP S500 | Rejected | Aborted as a known different protocol family. |
| AP S700 V2 | Rejected | Aborted as a known different protocol family. |
| Generic `ALLPOWERS*` | Experimental candidate | Requires active probe. |
| Service UUID FFF0 without a useful name | Experimental candidate | Requires active probe. |

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

## Reporting a result

Use the compatibility issue template and provide:

- product model and revision from the physical label;
- advertised Bluetooth name;
- Home Assistant and integration versions;
- adapter/proxy model and ESPHome version when relevant;
- whether discovery, probe, telemetry, and each control succeeded;
- sanitized diagnostics;
- packet evidence only after removing addresses and identifying information.

A positive report does not automatically make a write feature safe. Each shared
bit field must be verified independently.
