# HIL evidence report

Run: `13ddd4b00b65a964`

Acquisition status: pre-review evidence snapshot; hardware qualification pending.

## Environment

- Home Assistant: 2026.10.4
- Integration manifest version: 0.3.0 (installed build identity verified)
- Installed integration commit: 8aae2f9610acc405cf9803a6379e635e18b2a8ae
- Device: R600; hardware 0.3; firmware 0.2
- Observed GATT route: local_adapter

## Initial read-only preflight snapshot

- Snapshot type: `read_only_preflight`
- Requested route: `auto`
- Preflight: `ready`; connected `ready`, entry_loaded `ready`, telemetry_fresh `ready`
- Telemetry: battery `79`, connected `on`, firmware_version `0.2`, hardware_version `0.3`, input_power `0`, output_power `0`, remaining_time `4850`, telemetry_available `on`
- Runtime capabilities available: `True`; connected `True`, status fresh `True`, settings fresh `True`; capabilities car_charger_enabled `False`, read_telemetry `True`, write_output_controls `True`, write_settings_controls `True`, write_settings_keepalive `True`

## Guided tier acquisition

- Requested tier: `gold`; executed tier: `gold`
- Requested BLE route: `auto`; observed route: `local_adapter`
- Installed build identity: `verified`
- Capture summary: 1/1 complete; 0 dropped events; 0 dropped frames.
- Acquisition status: pre-review snapshot; hardware qualification pending.

- `read_only_probe`: **pass**; artifact outcome `inconclusive`; operator-reported physical plausibility `plausible`; fresh verified telemetry samples present; read-only protocol probe `verified`
- `physical_plausibility_observation`: **pass**; artifact outcome `inconclusive`; operator-reported physical plausibility `plausible`; fresh verified telemetry samples present; read-only protocol probe `verified`
- `ac_output_cycle`: **pass**; artifact outcome `pass`; physical change outcome `correct`; physical restore outcome `correct`; final restore outcome `correct`; output cycle 1 (off to on): physical change `correct`; physical restoration `correct`; output cycle 2 (on to off): physical change `correct`; physical restoration `correct`
- `dc_output_cycle`: **pass**; artifact outcome `pass`; physical change outcome `correct`; physical restore outcome `correct`; final restore outcome `correct`; output cycle 1 (off to on): physical change `correct`; physical restoration `correct`; output cycle 2 (on to off): physical change `correct`; physical restoration `correct`
- `light_output_cycle`: **pass**; artifact outcome `pass`; physical change outcome `correct`; physical restore outcome `correct`; final restore outcome `correct`; output cycle 1 (off to on): physical change `correct`; physical restoration `correct`; output cycle 2 (on to off): physical change `correct`; physical restoration `correct`
- `basic_reconnect`: **pass**; supporting details unavailable
- `eco_mode_reversal`: **pass**; artifact outcome `pass`; physical change outcome `correct`; physical restore outcome `correct`
- `work_mode_reversal`: **pass**; artifact outcome `pass`; physical change outcome `correct`; physical restore outcome `correct`
- `eco_timeout_reversal`: **pass**; artifact outcome `pass`; physical change outcome `correct`; physical restore outcome `correct`
- `car_charger_reversal`: **pending_review**; supporting details unavailable
- `guarded_recovery`: **shared_evidence**; supporting details unavailable
- `minimum_output_requirement`: **pending_review**; supporting details unavailable

No correlated scenario steps were recorded; guided tier steps above are a separate acquisition record.

## Scenario results

No correlated scenario steps were recorded.

GATT evidence: 13 allowlisted events across 1 captures; observed route `local_adapter`.

## Limitations

- Event payloads and frame bytes are omitted by policy.
- Missing or uncorrelated observations remain inconclusive.
- Review `hil-evidence.zip` locally before attaching it manually to an issue or PR.
