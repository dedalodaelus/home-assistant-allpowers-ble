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

### Check the observation path first

The pinned Home Assistant 2026.10.4 runtime reproduces stationary REST
`last_reported` with advancing state-machine reports. `StateMachine.async_set_internal`
updates an existing `State.last_reported` in place for identical state/attributes,
but the REST entity endpoint serves the cached `State.as_dict_json`. That JSON
retains its earlier `last_reported`. A changed state creates a new `State` and
REST catches up. This is reproduced both without BLE and through the captured
R600 status/settings notification path for battery, connected, and telemetry
availability. No integration workaround, forced reports, or Core patch is applied.

The earlier tests inspected `hass.states.get(...).last_reported` without making
HTTP requests. They therefore missed this difference between live reports and
their REST representation. A stationary REST timestamp alone cannot establish
that the integration stopped publishing. The physical comparison below now
confirms this observation discrepancy on the affected host for the captured
interval; it does not establish that every historical symptom has this cause.

### Physical comparison received 2026-11-01

The maintainer ran the capture script from commit `4df29a2` against the existing
dedicated instance. The sanitized output is retained as
[`issue213-live-rest-2026-11-01.json`](../tests/hil/fixtures/r600-hw03/issue213-live-rest-2026-11-01.json).
Live samples span 01:07:59.285001Z–01:09:29.303712Z (90.018711 seconds).

| Measurement | First sample | Second sample | Change |
| --- | --- | --- | --- |
| Notifications / valid packets | 3176 / 3176 | 3332 / 3332 | +156 / +156 |
| Status freshness, monotonic seconds | 18911.097463115 | 19000.681408032 | +89.583945 s |
| Settings freshness, monotonic seconds | 18911.197428297 | 19000.785346211 | +89.587918 s |
| Battery live `last_reported`, UTC | 01:07:59.208379 | 01:09:28.796330 | +89.587951 s |
| Connected live `last_reported`, UTC | 01:07:59.208793 | 01:09:28.796695 | +89.587902 s |
| Telemetry availability live `last_reported`, UTC | 01:07:59.208828 | 01:09:28.796732 | +89.587904 s |
| Battery REST `last_reported`, UTC | 00:37:28.719187 | 00:37:28.719187 | 0 s |
| Connected REST `last_reported`, UTC | 00:37:28.719326 | 00:37:28.719326 | 0 s |
| Telemetry availability REST `last_reported`, UTC | 00:37:28.719339 | 00:37:28.719339 | 0 s |

Battery stays at 78%; connected and telemetry availability stay on. All target
ownership/presence/loaded-entry checks are true in both samples. Parser discards,
protocol errors, disconnects, and reconnects remain zero. The second live reports
are approximately 0.507 seconds old, despite REST being about 32 minutes behind.
The capture confirms advancing valid telemetry and actual state-machine reports
while the REST representation remains stale, matching the Core cache behavior
reproduced by the HTTP tests. No BLE/session recovery was needed to obtain this
evidence, and no repair of Core's serialization has been demonstrated.

The installed build does not expose the PR's instrumentation: live statistics,
coordinator callback/listener counts, and per-entity callback counts are `null`.
These are unmeasured, not zero. This capture independently observes publication
through scalar template reads; it does not validate the new counters on hardware.
It is evidence for the specific observation boundary, not a physical qualification
pass for all routes/scenarios. The HIL matrix stays pending and #213 stays open.

### Repeat the live/REST comparison

On any machine with Python 3 and access to the dedicated HA instance, run:

```bash
python3 scripts/capture_telemetry_publication.py \
  --interval 90 --output /tmp/telemetry-publication.json
```

The script prompts for the HA URL, the exact battery, connected, and enabled
telemetry-availability entity IDs, and an existing admin token (hidden input).
It reads each REST state before/after a live `/api/template` query and fetches
integration diagnostics between them, twice 90 seconds apart. The template
reads scalar timestamps directly and verifies that all three entities belong
to one loaded ALLPOWERS config entry. Shared output uses logical aliases and a
whitelist of timestamps, states, counters, and ownership booleans; it excludes
tokens, URLs, IDs, attributes, device names, and raw diagnostics. New diagnostic
fields are `null` on an older integration build. A diagnostics HTTP error is
recorded by status only. The script issues no state or device writes, reloads,
reconnects, or BLE commands. It can be used before installing this PR.

If `cached_rest_discrepancy_observed` is true, live state-machine reports advanced
for the verified entity despite its stationary REST representation. If live
reports also stop, continue tracing the publication chain below. This does not
infer telemetry freshness from publication: compare valid packet counters and
status/settings freshness separately. Do not classify a missing/foreign entity
or an unloaded config entry as a confirmed cache discrepancy.

### Controlled BLE publication capture

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

1. Privately verify in the entity registry that the sampled battery, connected,
   and `telemetry_available` IDs are enabled, use the `allpowers_ble` platform,
   and belong to the active config entry. Retain the exact ID-to-alias mapping
   locally; use consistent logical aliases in shared evidence. Repeat this
   verification after reload/reconnect so retired entity IDs are not sampled.
   Start a timestamped, 10-second REST poll of battery and connection states.
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
   For each snapshot, record UTC start/end times and sample REST immediately
   before and after the diagnostics request. Compare
   `publication.entity_update_callbacks` for `AllpowersSensor.battery`,
   `AllpowersBinarySensor.connected`, and
   `AllpowersBinarySensor.telemetry_available` with REST `last_reported`.
   These counters count completed coordinator callbacks by static entity key.
   They do not prove publication: Home Assistant can return from a state-write
   request without reaching the state machine when the entity is not ADDED.
   Read live `last_reported` via the scalar template query as well as REST;
   cached REST alone is not evidence of missing state-machine reports. No
   entity IDs are added to diagnostics.
   Record `publication.coordinator_listener_count` in both snapshots to detect
   a listener lifecycle change while the client remains active. This is an
   aggregate; a nonzero count does not prove any particular entity is subscribed.
   Use each target's callback deltas to check whether its callback runs.
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
Rising coordinator updates with stationary target callback counts narrow the
investigation to target subscription, callback exceptions, or platform lifecycle.
Rising target callback counts with stationary live template `last_reported`
require checking entity lifecycle/platform registration and the private ID mapping;
callback completion alone does not establish a state-machine write. Correlate
all deltas over the same interval, including each target's actual state-machine
reports. A passing simulation or CI run does not establish physical HIL success.

Malformed frames intentionally emit client/coordinator diagnostic updates when
parser error counters change. Active entities may therefore advance
`last_reported` even though no valid telemetry arrived. Such frames must not
advance `valid_packets`, `last_packet_at`, `status_monotonic`, or
`settings_monotonic`; compare these separately from HA publication timestamps.
Repeated captured-frame, reload, and simulated elapsed-time tests cover the
existing publication path. HTTP tests reproduce the observation discrepancy on
the pinned runtime; the retained physical capture confirms it on the affected
host for its interval. Keep #213 open while Core serialization follow-up remains
unresolved, and keep general HIL qualification pending until the required route,
scenario, and recovery evidence exists.

Before sharing evidence, remove Bluetooth addresses, device names, serial
numbers, Home Assistant IDs, tokens, and unrelated traffic. Share only counts,
timestamps, sanitized diagnostics, and the two already published frame shapes.
