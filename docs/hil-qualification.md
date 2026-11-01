# Hardware-in-the-loop qualification runbook

This runbook explains how to execute and document real hardware qualification for
verified profiles.

The runtime `verified` classification is a software model/revision decision, not a
claim that physical qualification is complete. Track that separately in the
qualification matrix, and leave routes, scenarios, or write capabilities pending
until their required evidence is captured. For example, the current VOLIX P1800
report covers one unit running firmware `0xF1`; it does not identify a Bluetooth
route or establish results for the required scenario and write-capability set.

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

The default command validates structure and privacy while allowing `pending`
results. It is not a hardware qualification pass. Use `--require-pass` to reject
pending or failed results for all required routes, scenarios, and capabilities.

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

## Transaction safety regression

Issue #197 has deterministic simulation coverage, not new physical qualification.
For R600 hardware 0.3 / raw `0x03`, execute this checklist on both routes with
noncritical test loads. Do not mark matrix scenarios passed without sanitized
capture evidence, and do not automatically retry an ambiguous power command.

- Send AC on followed immediately by DC on without intervening matching telemetry:
  verify rejection of DC and absence of a second combined write on the wire.
- After matching AC status, repeat DC and verify the AC/DC/light bitmap, including
  explicit off transitions and every documented sibling output state.
- Repeat ECO, work mode, timeout, and optional car-charger changes; verify that
  dependent settings writes remain blocked and unknown flag bits are preserved.
- Capture when the station emits settings `0x03` relative to command `0x02`, both
  for actual mutations and unchanged keepalive. Do not assume an acknowledgement
  cadence or promise a bounded physical-confirmation time without this evidence.
- While settings are pending, invoke manual and automatic keepalive and verify
  that no unconfirmed target is replayed. With only `0x01`, verify session renewal
  by the next keepalive interval, no write before the new session supplies `0x03`,
  and no rapid reconnect loop. Repeat multiple intervals and measure actual cutoff
  behavior; session renewal alone is not a qualified cutoff-prevention strategy.
- Capture contradictory, missing, and late telemetry, cancellation, route loss,
  and reload. Confirm recovery requires new session-appropriate safe telemetry.
- Repeat with vendor-app contention. A matching snapshot is convergence only;
  without transaction IDs the responsible actor cannot be authenticated.

### ESPHome comparison

The working reference [esphome-allpowers-ble at c0bfd88](https://github.com/dedalodaelus/esphome-allpowers-ble/blob/c0bfd88a78726087dc0c309bdb5b3a6faa4c7c01/components/allpowers_ble/allpowers_ble.cpp)
re-sends the last complete settings snapshot learned in the current connection
without requiring a new `0x03` after each keepalive. Its control paths also use
optimistic requested values. It avoids redundant ECO/work-mode/timeout/car-charger
commands and renews unhealthy sessions. Those implementation choices do not prove
that unchanged settings frames emit `0x03` or that status requests produce it.
This integration deliberately keeps confirmed and requested state separate and
requires fresh settings after session renewal instead of adopting optimistic
replay. Physical capture must compare both implementations on the same R600
revision and routes, including whether reconnect reacquires settings.

## Issue #213: frozen telemetry retest

Use a dedicated Home Assistant instance with R600 V2 hardware 0.3 and the exact
build under test. Record the loaded integration path and `manifest.json` version
from inside the Home Assistant process/container; record the source commit or
release archive checksum. A repository checkout alone does not identify the
code Home Assistant imported. Keep settings keepalive enabled to reproduce the
reported setup, but do not attribute the freeze to it without measurements.

Run the following procedure separately on `local_adapter` and `active_proxy`.
Make only the named route available for the run and confirm the selected route
in Home Assistant Bluetooth logs. Keep the GATT connection entity and the
disabled-by-default `telemetry_available` entity distinct: enable the latter
for this test. Do not modify the HIL matrix until both routes have measured
evidence.

1. Start a timestamped, 10-second REST poll of battery and connection states.
   Record `state`, `last_updated`, and `last_reported` for each. Record
   `telemetry_available` with the same fields. Use a monotonic test clock for
   elapsed time and UTC timestamps for correlation.
2. Download two sanitized integration diagnostics snapshots 60–120 seconds
   apart while `last_reported` is stationary. Compare
   `publication.live_client_statistics` (including rejected session/client
   callbacks, notifications, valid packets, parser errors, and emitted updates),
   `publication.coordinator_client_updates`, snapshot packet timestamps, and
   status/settings monotonic timestamps. Inspect `client_task_health` to spot
   a stopped connection or maintenance loop. The live counters may advance even
   when a rejected callback cannot publish a new coordinator snapshot.
   Compare `publication.entity_state_reports` for battery and connected with
   REST `last_reported`. These counters show actual entity state-write calls,
   without exposing entity IDs.
   Record `publication.coordinator_listener_count` in both snapshots to detect
   a listener lifecycle change while the client remains active.
3. On `local_adapter`, capture HCI traffic with `btmon` on the selected
   adapter. Count ATT Handle Value Notifications for the R600 connection handle
   and attribute handle `0x0006` during exactly the REST/diagnostics interval;
   separate status `0x01` from settings `0x03`. Confirm the handle is for this
   session. On `active_proxy`, collect timestamped notification counts from
   the proxy's BLE transport/debug logs. An HCI capture on the Home Assistant
   host cannot establish notification receipt at a remote ESPHome proxy.
4. Reconnect once through the integration and repeat the same interval.
   Record whether callbacks, accepted packets, coordinator updates, and
   `last_reported` resume together or separate again. Also record the behavior
   after telemetry becomes stale: battery should become unavailable while
   GATT connectivity can remain on.

Interpret the first counter that stops advancing as the failure boundary.
HCI/proxy notifications with stationary live `notifications` need subscription,
callback ownership, or competing-client investigation. Rising notifications
with stationary `valid_packets` need decoder/error inspection. Rising valid
packets with stationary coordinator updates need callback/exception inspection.
Rising coordinator updates with stationary entity report counts indicate a
listener/platform lifecycle problem. Rising entity report counts with stationary
REST `last_reported` indicate an entity-ID mismatch or Home Assistant state
machine problem. A passing simulation or CI run does
not establish physical HIL success.

Before sharing evidence, remove Bluetooth addresses, device names, serial
numbers, Home Assistant IDs, tokens, and unrelated traffic. Share only counts,
timestamps, sanitized diagnostics, and the two already published frame shapes.
