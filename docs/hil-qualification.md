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

For a non-destructive first step, use the [guided HIL CLI](hil-cli.md) to collect
read-only environment and entity snapshots. It does not produce qualifying BLE
or physical evidence until later guided-capture stages are implemented.

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

## Issue #213: REST/live state observation

A stationary REST `last_reported` does not establish a telemetry freeze. In Core
2026.10.4, identical writes advance the live `State.last_reported` while cached
serialization can retain an older timestamp. This behavior is tracked in
[Core #181392](https://github.com/home-assistant/core/issues/181392) and
[Core PR #181577](https://github.com/home-assistant/core/pull/181577).
The upstream PR remains open; maintainers have raised cache-invalidation overhead
and API-semantics concerns. This integration does not patch Core or force state
changes to refresh its serialization.

### Physical comparison received 2026-11-01

The maintainer's sanitized capture from the dedicated R600 instance is retained
as [`issue213-live-rest-2026-11-01.json`](../tests/hil/fixtures/r600-hw03/issue213-live-rest-2026-11-01.json).
Live samples span 01:07:59.285001Z–01:09:29.303712Z (90.018711 seconds).

| Measurement | Observed change |
| --- | --- |
| Notifications / valid packets | 3176 → 3332 each (+156) |
| Status / settings monotonic freshness | +89.583945 s / +89.587918 s |
| Battery live report | +89.587951 s |
| Connected live report | +89.587902 s |
| Telemetry-availability live report | +89.587904 s |
| All three REST report timestamps | Stationary near 00:37:28Z |

All three targets were present and belonged to the same loaded ALLPOWERS entry.
Battery stayed at 78%; connected and telemetry availability stayed on. Parser
errors/discards, disconnects, and reconnects remained zero. The second live
reports were about 0.507 seconds old. These observations support a stale REST
serialization boundary for this interval, rather than an R600 BLE publication
freeze. They do not establish a Core repair or exclude other historical faults.

The retained historical capture includes null investigation-only counters.
Those counters were not measured on hardware and have been removed from the
final integration changes; null is not zero. Existing snapshot statistics and
freshness timestamps remain available without adding runtime instrumentation.

### Repeat the live/REST comparison

On a machine with Python 3 and access to the dedicated HA instance, run:

```bash
python3 scripts/capture_telemetry_publication.py \
  --interval 90 --output /tmp/telemetry-publication.json
```

The script prompts for the HA URL, exact battery/connected/enabled
telemetry-availability IDs, and an existing admin token using hidden input.
It brackets direct scalar `/api/template` readings and existing integration
diagnostics with REST reads, twice 90 seconds apart, and checks target ownership.
Shared output contains logical aliases and whitelisted states, timestamps, and
snapshot counters; credentials, URLs, IDs, names, attributes, and raw diagnostics
are excluded. Diagnostics HTTP failures are recorded by status only. The script
sends no state/device writes, reconnects, reloads, or BLE commands.

`cached_rest_discrepancy_observed` reports an observation, not a required runtime
behavior. It is false when REST advances. Integration tests require valid packet
processing, coordinator propagation, actual live reports, rejection of old-session
callbacks, malformed-frame freshness protection, and listener cleanup after reload.
They do not require the upstream serialization behavior to remain unfixed.
Parser errors may publish diagnostics without refreshing valid telemetry.

If live reports also stop, compare existing valid-packet counters and status/
settings freshness before investigating target listeners and platform lifecycle.
For BLE qualification, verify the loaded build and selected route; correlate
synchronized diagnostics/live reports with HCI counts on the local adapter or
proxy-side counts on an active ESPHome proxy. Follow the route/scenario and
recovery requirements above. Keep #213 open for the upstream follow-up and keep
the general HIL matrix pending; this scoped capture is not a qualification pass.
