# Hardware-in-the-loop qualification runbook

This runbook explains how to execute and document real hardware qualification for
verified profiles.

## Scope

Use this process when you need to:

- promote or keep a profile in the verified lane;
- refresh stale HIL evidence before a stable release;
- qualify a new hardware revision for write-capable support.

## Prerequisites

- One physical ALLPOWERS device for the target revision.
- One local Bluetooth adapter route.
- One active ESPHome Bluetooth Proxy route.
- A Home Assistant instance dedicated to testing (not production).
- A local checkout of this repository with test dependencies installed.

## 1. Prepare the environment

From repository root:

```bash
source .venv/bin/activate
USE_REAL_HOMEASSISTANT=0 pytest --ignore=tests/homeassistant
USE_REAL_HOMEASSISTANT=1 pytest tests/homeassistant
```

If either lane fails, fix that first. Do not start HIL capture with a broken base.

Enable temporary debug logs in Home Assistant while capturing evidence:

```yaml
logger:
  logs:
    custom_components.allpowers_ble: debug
    bleak_retry_connector: debug
```

## 2. Capture evidence for each required route

Run every scenario in both lanes:

- local_adapter
- active_proxy

For each lane, capture at least one status and one settings fixture file under:

- tests/hil/fixtures/<revision-id>/local-adapter-status.sample.jsonl
- tests/hil/fixtures/<revision-id>/local-adapter-settings.sample.jsonl
- tests/hil/fixtures/<revision-id>/proxy-status.sample.jsonl
- tests/hil/fixtures/<revision-id>/proxy-settings.sample.jsonl

Each JSONL row should be a sanitized object with:

- route: local_adapter or active_proxy
- scenario: one required scenario name
- direction: write or notify
- frame_hex: payload bytes in hex
- verdict: ok, timeout, reconnect, or rejected

Example row:

```json
{"route":"local_adapter","scenario":"rapid_consecutive_writes","direction":"write","frame_hex":"EE010B...","verdict":"ok"}
```

## 3. Execute the required scenario set

Mark results in the matrix for all required scenarios:

- golden_vector_replay
- reconnect_failover
- rapid_consecutive_writes
- stale_timeout_recovery
- soak_24h
- upgrade_reload
- vendor_app_contention

Recommended execution pattern:

1. Start with telemetry-only validation and verify stable status frames.
2. Exercise output and settings writes with fresh snapshots.
3. Force disconnects, route loss, and reconnect behavior.
4. Run a long soak test for at least 24h.
5. Repeat key writes after integration reload and Home Assistant restart.

## 4. Update the qualification matrix

Edit tests/hil/qualification_matrix.json:

- set route status for local_adapter and active_proxy;
- update last_tested to current date (YYYY-MM-DD);
- set each scenario status;
- set each write_capabilities status;
- keep privacy_review as pass.

Allowed values are pass, fail, pending, and na.

## 5. Validate matrix and fixture hygiene

Run:

```bash
python scripts/validate_hil_qualification.py
python scripts/validate_hil_qualification.py --require-pass --max-age-days 30
```

The validator checks:

- required schema keys and status fields;
- required route/scenario/capability coverage;
- fixture file existence;
- stale evidence in require-pass mode;
- redaction failures such as raw Bluetooth MAC addresses.

## 6. Privacy and publication rules

Before committing evidence:

- remove MAC addresses, serial numbers, Home Assistant IDs, and local topology;
- replace sensitive values with REDACTED;
- keep only protocol-relevant payload and verdict context.

Do not publish raw packet captures that still contain identifying metadata.

## 7. PR expectations

For a qualification PR, include:

- updated tests/hil/qualification_matrix.json;
- sanitized fixtures under tests/hil/fixtures/<revision-id>/;
- validation command output from scripts/validate_hil_qualification.py;
- notes on any failed or not-applicable scenarios.

For stable promotion that requires strict HIL gating, ensure repository variable
REQUIRE_HIL_STABLE_GATE is true so CI enforces require-pass mode.
