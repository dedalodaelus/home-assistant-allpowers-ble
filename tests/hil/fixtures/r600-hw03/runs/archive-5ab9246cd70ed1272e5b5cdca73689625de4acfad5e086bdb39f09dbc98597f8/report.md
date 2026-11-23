# HIL evidence report

Run: `8a3432e9cac457e2`

Acquisition status: pre-review evidence snapshot; hardware qualification pending.

## Environment

- Home Assistant: 2026.10.4
- Integration manifest version: 0.3.0 (installed build identity verified)
- Installed integration commit: 5101ee98f9ec9128eac73442d0caf05fd8f6b0d9
- Device: R600; hardware 0.3; firmware 0.2
- Observed GATT route: active_proxy

## Initial read-only preflight snapshot

- Snapshot type: `read_only_preflight`
- Requested route: `auto`
- Preflight: `ready`; connected `ready`, entry_loaded `ready`, telemetry_fresh `ready`
- Telemetry: battery `89`, connected `on`, firmware_version `0.2`, hardware_version `0.3`, input_power `0`, output_power `0`, remaining_time `5464`, telemetry_available `on`
- Runtime capabilities available: `True`; connected `True`, status fresh `True`, settings fresh `True`; capabilities car_charger_enabled `False`, read_telemetry `True`, write_output_controls `True`, write_settings_controls `True`, write_settings_keepalive `True`

## Guided tier acquisition

- Requested tier: `bronze`; executed tier: `bronze`
- Requested BLE route: `auto`; observed route: `active_proxy`
- Installed build identity: `verified`
- Capture summary: 1/1 complete; 0 dropped events; 0 dropped frames.
- Acquisition status: pre-review snapshot; hardware qualification pending.

- `read_only_probe`: **pass**; artifact outcome `inconclusive`; operator-reported physical plausibility `plausible`; fresh verified telemetry samples present; read-only protocol probe `verified`
- `physical_plausibility_observation`: **pass**; artifact outcome `inconclusive`; operator-reported physical plausibility `plausible`; fresh verified telemetry samples present; read-only protocol probe `verified`

No correlated scenario steps were recorded; guided tier steps above are a separate acquisition record.

## Scenario results

No correlated scenario steps were recorded.

GATT evidence: 7 allowlisted events across 1 captures; observed route `active_proxy`.

## Limitations

- Event payloads and frame bytes are omitted by policy.
- Missing or uncorrelated observations remain inconclusive.
- Review `hil-evidence.zip` locally before attaching it manually to an issue or PR.
