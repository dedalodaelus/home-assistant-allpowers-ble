# R600 HW12 HIL fixture lane

This folder stores sanitized hardware evidence for revision `R600 hw=1.2 raw=0x12`.

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
