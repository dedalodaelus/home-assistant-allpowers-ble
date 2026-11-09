# Hardware-in-the-loop qualification runbook

This runbook explains how to execute and document real hardware qualification for
verified profiles.

## Qualification matrix schema v2

`tests/hil/qualification_matrix.json` uses schema v2. A revision describes the
model and hardware revision; each item in its `runs` list is scoped to one
firmware version, one observed BLE route, and one HIL run. A run records its
timestamp when available, tool and installed integration versions, capture
completeness, build verification (`verified`, `unverified`, or `mismatch`), an
overall qualification status, scenario results, write-capability results, and
links to sanitized evidence. Use `null` for firmware only when it is unknown;
unknown firmware cannot qualify a run.

Result statuses have distinct meanings:

- `pending`: not yet evaluated;
- `fail`: evidence demonstrates a failed result;
- `not_applicable`: explicitly exempt, with a recorded rationale;
- `inconclusive`: evidence is missing, incomplete, or cannot establish an outcome;
- `pass`: the scoped result is supported by its linked evidence.

Only a complete, fresh run with a verified installed build, known firmware, an
observed supported route, linked evidence, and `pass` results can satisfy
`--require-pass`. Neither `not_applicable` nor `inconclusive` counts as a pass
in strict mode.

The v1 migration preserves each old revision and all its route, scenario, and
write-capability values under `legacy_results`. These records are marked
`unscoped_legacy`: v1 shared scenario/control results cannot be assigned to a
route or firmware after the fact. Historical `na` values are retained verbatim
in the source snapshot and normalized to `not_applicable` in the legacy outcome
summary. They remain unscoped and never satisfy the strict gate. Migration
creates no scoped runs and therefore cannot add qualification or make the strict
gate pass. To migrate another v1 matrix without overwriting it, write to a new
path:

```bash
python -m scripts.migrate_hil_qualification old-matrix.json migrated-matrix.json
```

The default validator checks structure and privacy while allowing unfinished
runs. Add `--require-pass --max-age-days 30` to require fresh, fully scoped
passes.

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

Use the [guided HIL CLI](hil-cli.md) to collect a run and make a public evidence
ZIP. Read-only snapshots and guided software steps are useful review evidence;
they do not by themselves qualify a physical scenario or write capability.

The `report` and `export` commands described in [hil-cli.md](hil-cli.md) produce
a local, privacy-filtered ZIP. An imported ZIP becomes an `inconclusive` run
record until separately reviewed physical evidence supports each result. A
software step marked `pass`, a complete GATT API capture, or a verified route
must not be used to mark a physical qualification result `pass`.

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

For each observed route, create a separate guided run and export its public ZIP
using the steps in the [archive handoff example](#complete-archive-handoff-example).
The ZIP records available route/capture metadata but omits raw frame payloads.
It is an importable review record, not a physical qualification result.

The older `*.sample.jsonl` files under `tests/hil/fixtures/` are retained as
legacy examples. They are not independent hardware golden vectors and do not
qualify a route, scenario, or write capability. Do not turn their contents into
PASS results.

When independently reviewed physical byte captures are available, keep the
sanitized fixture files under:

- tests/hil/fixtures/<revision-id>/local-adapter-status.sample.jsonl
- tests/hil/fixtures/<revision-id>/local-adapter-settings.sample.jsonl
- tests/hil/fixtures/<revision-id>/proxy-status.sample.jsonl
- tests/hil/fixtures/<revision-id>/proxy-settings.sample.jsonl

Each independently sourced JSONL row should be a sanitized object with:

- route: local_adapter or active_proxy
- scenario: one required scenario name
- direction: write or notify
- frame_hex: payload bytes in hex
- verdict: ok, timeout, reconnect, or rejected

Example row:

```json
{"route":"local_adapter","scenario":"rapid_consecutive_writes","direction":"write","frame_hex":"EE010B...","verdict":"ok"}
```

Do not describe a synthetic frame made with the project's checksum helper or a
legacy sample row as an independent hardware capture. A reviewed status/settings
pair may become an offline protocol regression vector when physical evidence is
available; otherwise leave that requirement pending.

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

Add one run record per firmware and observed route in
`tests/hil/qualification_matrix.json`. Prefer the archive importer for public
guided-run evidence, then review the generated record and add only independently
supported physical verdicts:

- set `run_id`, `firmware_version`, and timezone-aware `captured_at`;
- record the observed `route`, capture completeness, tool/integration versions,
  build verification, and sanitized evidence paths;
- record each required scenario and write capability independently, with its
  status and evidence links;
- leave statuses `inconclusive` when the ZIP lacks the physical observation,
  complete capture, or verified build needed to support a verdict;
- keep revision `privacy_review` as `pass`.

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

The default command validates structure and privacy while allowing pending and
inconclusive results. It does not qualify hardware. `--require-pass` requires
fresh, complete, firmware- and route-scoped `pass` results, a verified build, and
both supported routes for each qualified firmware. `not_applicable`, inconclusive,
unknown, partial, expired, or legacy results do not satisfy that gate.

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

## Complete archive handoff example

The contributor creates and attaches a public ZIP from their run directory:

```bash
python -m scripts.hil report hil-results/<run-directory>
python -m scripts.hil export hil-results/<run-directory>
```

The ZIP is written to `hil-results/<run-directory>/hil-evidence.zip`. The
contributor attaches that file to the qualification PR. After downloading it,
the maintainer reviews the proposed profile, firmware, observed route, capture
completeness, build provenance, guided step records, and evidence paths without
changing repository files:

```bash
python -m scripts.hil import /path/to/hil-evidence.zip --dry-run
```

If the archive and proposed paths are appropriate, the maintainer explicitly
applies the import and validates the resulting matrix:

```bash
python -m scripts.hil import /path/to/hil-evidence.zip --apply
python scripts/validate_hil_qualification.py
```

The imported record remains `inconclusive`; current ZIP exports omit frame
payloads and report unverified or mismatched installed-build provenance. The
import is reviewable evidence, not a physical qualification. Only separately
reviewed evidence can support per-route, per-firmware PASS results.

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

## Guided scenarios (#230 status)

The CLI exposes `run --list-suites` and side-effect-free dry-run planning.
Read-only collection is available, as are attended AC, DC and station-light
on/off scenarios, the opt-in gated car-charger cycle, and ECO-mode, work-mode
and ECO-timeout setting checks with per-action authorization and private trace
evidence. A
recovery guide lets the operator perform a route interruption/restoration,
vendor-app contention, or HA reload/upgrade while the CLI observes
reconnection and current route evidence. No write, settings, or recovery
scenario has been piloted on hardware;
missing route or physical evidence remains inconclusive. Checkpoint resume is
available for interrupted recovery and soak observation on the same
fingerprinted device. Resume starts a new soak segment and records a
restart discontinuity; it never repeats the manual action. Output checkpoint
resume is refused because an interrupted write can be ambiguous. A bounded
24-hour soak collector is implemented with periodic telemetry, route traces,
gap detection, private storage limits, and restart-aware checkpoints. The
collector has offline simulation coverage, but no hardware soak has been run.
The output cycle can perform one separately authorized, traced restoration
after an ambiguous final write when fresh same-session telemetry confirms the
known intermediate state and no transaction is pending. Settings changes use
the same guarded restoration rule. If the changed state cannot be proven, the
session changed, or a transaction remains pending, the CLI does not issue
another write and instructs the operator to inspect and restore manually.
The CLI repeats the live preflight after arming the restoration trace, records
the last observed state and cleanup status in the private checkpoint, and
never retries a cleanup write whose acknowledgement is ambiguous. Write
checkpoints are deliberately not resumable: after interruption, inspect a new
read-only snapshot and the station before starting another write scenario.
Restoration never upgrades an inconclusive original action to a pass.

`run --suite transaction-safety --simulate` runs selected offline fake-GATT
regression cases against the production client for pending-write rejection,
same-generation confirmation, combined output-state preservation,
keepalive/settings non-replay, and ambiguous evidence without automatic
retries. Its private report contains test names and outcomes only, is explicitly
marked synthetic, and cannot qualify an integration build or hardware. Live
transaction-safety execution and all hardware qualification remain outstanding.
No build-level physical qualification is claimed.
