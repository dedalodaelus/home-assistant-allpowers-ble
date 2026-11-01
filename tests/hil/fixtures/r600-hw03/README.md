# R600 HW03 HIL fixture lane

This folder stores sanitized hardware evidence for revision `R600 hw=0.3 raw=0x03`.

## Privacy rules

- Never commit Bluetooth MAC addresses.
- Never commit serial numbers or human-identifiable labels.
- Never commit Home Assistant IDs, room names, GPS coordinates, or local network topology.
- Replace sensitive values with `REDACTED` before saving captures.

## Expected fixture files

- `local-adapter-status.sample.jsonl`
- `local-adapter-settings.sample.jsonl`
- `proxy-status.sample.jsonl`
- `proxy-settings.sample.jsonl`

Each line must be a sanitized JSON object and should include:

- route (`local_adapter` or `active_proxy`)
- scenario name
- direction (`write` or `notify`)
- frame payload in hex (`frame_hex`)
- normalized verdict (`ok`, `timeout`, `reconnect`, `rejected`)

## Validation command

```bash
python scripts/validate_hil_qualification.py
python scripts/validate_hil_qualification.py --require-pass --max-age-days 30
```

## Issue #213 publication observation

[`issue213-live-rest-2026-11-01.json`](issue213-live-rest-2026-11-01.json) is the
maintainer-supplied, sanitized two-sample output of
`scripts/capture_telemetry_publication.py` from commit `4df29a2`. It compares the
same privately supplied battery, connected, and telemetry-availability IDs via
REST and scalar template reads, together with integration diagnostics.

Across 90.018711 seconds, valid packets increase by 156, status/settings freshness
advances, and all three live report timestamps advance by about 89.588 seconds.
REST report timestamps remain fixed. All ownership checks pass and there are no
parser/protocol errors or reconnects. The installed integration does not expose
the PR's additional counters: `null` means unmeasured, not zero.

This is observation-boundary evidence for #213, not a golden-frame notification
fixture, recovery test, or local-adapter/active-proxy qualification pass. The
qualification matrix is unchanged. See the interpretation in the
[HIL runbook](../../../../docs/hil-qualification.md#physical-comparison-received-2026-11-01).
