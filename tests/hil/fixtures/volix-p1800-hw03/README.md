# VOLIX P1800 HW03 evidence notes

This file records the limits of the currently available physical report. It is
not a sanitized packet capture and does not qualify either Bluetooth route.

## Reported evidence

PR [#183](https://github.com/dedalodaelus/home-assistant-allpowers-ble/pull/183)
reports one physical VOLIX P1800 unit with hardware version `0.3`, raw hardware
version `0x03`, and firmware `0xF1` validated using the integration and the
ESPHome component.

The report does not identify whether the integration used a local adapter or an
active ESPHome Bluetooth Proxy. It does not enumerate tested writes or HIL
scenarios, state a test duration, or provide sanitized route-specific capture
files. PR #195 merged the software support and unit tests; those tests do not
expand the physical evidence scope.

## Qualification status

The report is limited to that single unit and firmware. Do not infer qualification
for other firmware versions, naming variants, routes, scenarios, or write
capabilities. Both required route results, all required scenarios, and all
writable-capability results remain `pending` in
`tests/hil/qualification_matrix.json` until runbook evidence is captured.
