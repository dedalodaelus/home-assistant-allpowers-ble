# Roadmap and known risks

## Current release scope

Version 0.1.x focuses on a safe, maintainable R600 implementation:

- discovery and active protocol validation;
- local adapter and ESPHome Bluetooth Proxy transport;
- telemetry, output controls, settings controls, and diagnostics;
- reconnection, watchdog, data freshness, and optional keepalive;
- HACS packaging and automated quality checks.

## Known risks

### Hardware revision ambiguity

The same commercial name may use another controller or firmware. The active probe
reduces false positives but cannot prove every write field is identical.

### Single active BLE client behavior

Some stations may allow only one active connection. The vendor app or another Home
Assistant instance can cause connection churn.

### Reverse-engineered settings semantics

Unknown bits are preserved, but undocumented interactions may still exist. The car
charger and periodic settings keepalive remain opt-in.

### Proxy and radio variability

Wi-Fi quality, BLE placement, USB interference, and controller limits can dominate
reliability independently of code. Diagnostics expose counters but cannot repair a
poor RF path.

### Home Assistant API evolution

The integration pins a minimum Home Assistant version and runs a real-package
import smoke test in CI. Behavioral API changes may still require adaptation.

## Planned improvements

1. Collect sanitized hardware reports for S300 and other protocol-compatible
   revisions.
2. Add hardware-in-the-loop regression tooling for long-duration reconnect and
   proxy-failover testing.
3. Expand decoded telemetry only when packet meaning is verified.
4. Add Repairs issues for persistent no-route or watchdog conditions if those can
   be made actionable without noise.
5. Consider extracting the pure protocol package when a second integration or tool
   needs it.
6. Evaluate Bluetooth connection-slot coordination metrics as Home Assistant APIs
   evolve.

## Non-goals

- cloud account integration;
- direct BlueZ management;
- bypassing Home Assistant Bluetooth routing;
- guessed support based only on product name;
- arbitrary raw command services;
- persistent storage of high-frequency telemetry;
- silently issuing writes from stale or assumed state.
