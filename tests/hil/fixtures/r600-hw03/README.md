# R600 HW03 HIL fixture lane

This folder contains legacy example JSONL rows for `R600 hw=0.3 raw=0x03`. These
sample rows are not independently sourced physical captures and do not qualify
any route, scenario, write capability, or protocol golden vector.

## Privacy rules

- Never commit Bluetooth MAC addresses.
- Never commit serial numbers or human-identifiable labels.
- Never commit Home Assistant IDs, room names, GPS coordinates, or local network topology.
- Replace sensitive values with `REDACTED` before saving captures.

## Legacy sample files

- `local-adapter-status.sample.jsonl`
- `local-adapter-settings.sample.jsonl`
- `proxy-status.sample.jsonl`
- `proxy-settings.sample.jsonl`

These files predate the v2 per-run/per-firmware/per-route matrix. Do not count
their statuses as physical qualification or transfer them into a v2 run as
`pass`. When a real sanitized capture is available, record its actual scope and
provenance in a separate run record.

An independently reviewed capture row should be a sanitized JSON object and
include:

- route (`local_adapter` or `active_proxy`)
- scenario key for a scenario result
- capability key for a write-capability result
- direction (`write` or `notify`)
- frame payload in hex (`frame_hex`)
- normalized verdict (`ok`, `timeout`, `reconnect`, `rejected`)
- observed physical outcome (`pass`, `fail`, or `not_applicable`)

For a justified unsupported control, use the capability key, `direction` of
`observe`, and `verdict` of `not_applicable`; the matrix entry must include a
rationale. Strict validation requires a matching route and operation row for
every PASS or `not_applicable` claim, including matching
`physical_outcome: "pass"` or `physical_outcome: "not_applicable"`. Generic
notes and the legacy `*.sample` files do not qualify.

## Validation command

```bash
python scripts/validate_hil_qualification.py
python scripts/validate_hil_qualification.py --require-pass --max-age-days 30
```

The matrix validator confirms metadata and redaction. It cannot turn these
legacy samples into physical evidence.
