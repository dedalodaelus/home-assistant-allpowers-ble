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
- `not_applicable`: a write capability is absent on this model, with a recorded
  rationale and evidence for that absence;
- `inconclusive`: evidence is missing, incomplete, or cannot establish an outcome;
- `pass`: the scoped result is supported by its linked evidence.

Strict mode selects the newest complete, dated, build-verified `pass` or `fail`
run for each revision/firmware/route group. An inconclusive imported archive is
kept as history and does not supersede a qualification run; a newer eligible
physical failure supersedes an older pass. Every required scenario must pass.
Each write capability must pass or have a justified `not_applicable` result
with evidence for its absence. PASS/N/A evidence must contain a route- and
operation-bound capture row; a generic note or a legacy `*.sample.jsonl` file
cannot satisfy the gate.

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
current results using the selection policy above.

The runtime `verified` classification is a software model/revision decision, not a
claim that physical qualification is complete. Track that separately in the
qualification matrix, and leave routes, scenarios, or write capabilities pending
until their required evidence is captured. The R600 pilot reported in #227
includes guided `active_proxy` AC on/off and ECO-timeout runs; these have not been
independently reviewed or entered as scoped matrix runs and do not qualify these
controls. The current VOLIX P1800 report covers one unit running firmware `0xF1`;
it does not identify a Bluetooth route or establish results for the required
scenario and write-capability set.

The full matrix supports hardware support-tier and release-gate decisions. It is
not an all-or-nothing prerequisite for each individual production write. One
verified, actually observed route is sufficient to collect operation-specific
evidence for an individual control; record that route with the tested firmware.
Under [ADR 0017](architecture/decisions/0017-reconcile-hil-qualification-with-write-capability-policy.md),
adding or newly enabling a control requires reviewed, operation-specific physical
evidence for its exact model/revision, firmware, and observed route. Bronze is
evidence-backed read-only; Silver is physically verified applicable basic output
control; Gold adds verified declared settings/functions and recovery; Platinum
adds optional uninterrupted 24-hour reliability. These tiers classify coverage
and never authorize writes. Candidate testing is intended to use a separate
attended, temporary #275 grant and does not enable normal Home Assistant controls.

These are accepted policy targets, not implemented behavior yet. The integration
still uses grouped R600/VOLIX write flags. Follow-up work #266/#267 implements
per-feature enforcement, #268 implements per-route qualification, #269 implements
tier calculation, and #275 implements candidate grants. Until those changes land,
the strict schema-v2 validator continues to require both supported routes for
each firmware and supports the `--max-age-days 30` freshness option.

## Scope

Use this process when you need to:

- promote or keep a profile in the verified lane;
- refresh stale HIL evidence before a stable release;
- qualify a new hardware revision for write-capable support.

For one newly proposed control, collect operation-specific evidence on one
verified, actually observed route as required by ADR 0017. The two-route,
full-scenario checklist below describes the current legacy strict-v2
matrix/release procedure pending #268. It is not a prerequisite for each
individual control or the accepted future support tiers.

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
- For one control's evidence, one verified, actually observed route is sufficient.
- The current legacy strict-v2 matrix/release procedure requires both a local
  Bluetooth adapter route and an active ESPHome Bluetooth Proxy route.
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

## 2. Capture evidence for the current strict-v2 matrix/release procedure

The existing strict schema-v2 validator requires the full scenario set in both
lanes for each firmware. This is the current matrix/release procedure pending
#268; it does not change ADR 0017's one-observed-route evidence boundary for an
individual write control.

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

- tests/hil/fixtures/<revision-id>/local-adapter-status.jsonl
- tests/hil/fixtures/<revision-id>/local-adapter-settings.jsonl
- tests/hil/fixtures/<revision-id>/proxy-status.jsonl
- tests/hil/fixtures/<revision-id>/proxy-settings.jsonl

Each independently sourced JSONL row should be a sanitized object with:

- route: local_adapter or active_proxy
- revision_id, firmware_version, run_id, and timezone-aware captured_at
- scenario: one required scenario name
- direction: write or notify
- frame_hex: payload bytes in hex
- verdict: ok, timeout, reconnect, or rejected
- physical_outcome: pass, fail, or not_applicable, based on the observed result
- capability: required for write-capability evidence; use the matrix key

For strict qualification, each row must exactly match the matrix revision,
firmware, run ID, and capture timestamp as well as its route. A scenario row
must match the scenario key, have a `write` or `notify` direction, a valid
frame, and `verdict: "ok"` plus `physical_outcome: "pass"`. A capability PASS
row must match its capability key and use `direction: "write"`; a justified
`not_applicable` row must use `direction: "observe"`,
`verdict: "not_applicable"`, `physical_outcome: "not_applicable"`, and a matrix
rationale. Changing the matrix timestamp cannot make an older capture current
because the capture row retains its own timestamp. These checks bind a claim to
a specific run and capture record. They do not cryptographically authenticate
where a file came from; independent physical evidence review remains required.

Example row:

```json
{"route":"local_adapter","revision_id":"r600-hw03","firmware_version":"1.1","run_id":"r600-hw03-fw11-local-20261109","captured_at":"2026-11-09T12:00:00+00:00","scenario":"rapid_consecutive_writes","direction":"write","frame_hex":"EE010B0000000000000000000000","verdict":"ok","physical_outcome":"pass"}
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
inconclusive results. It does not qualify hardware. `--require-pass` selects the
newest eligible run for each firmware and route, checks freshness and capture
bindings, and requires both supported routes for every non-empty firmware
version recorded in the revision's scoped runs. Incomplete or inconclusive runs
still put their reported firmware in scope, but do not count as route evidence.
A newer eligible failure supersedes an older pass; incomplete or inconclusive
imports do not supersede eligible results. Scenarios require PASS. Write
capabilities allow PASS or evidence-backed `not_applicable` with a rationale.
Unknown, partial, expired, or legacy results do not satisfy the gate.

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

### Installed integration build identity

`doctor`, `run`, and `trace` request the admin-only HIL WebSocket build fingerprint and compare it with the CLI checkout. The SHA-256 stream includes sorted relative paths and file bytes for the integration package, excluding `__pycache__` and `.pyc`. A matching fingerprint is `verified` only when those checkout files are clean in Git; the matching commit SHA is then recorded. Different fingerprints are `mismatch`, even when both manifests say `0.3.0`. Missing/unsupported endpoints, invalid responses, failed scans, or dirty checkouts are `unverified` with a reason. This metadata does not award a scenario PASS or prove that Home Assistant's already-loaded Python modules match its on-disk files. Install the desired development checkout and restart HA before HIL execution, then confirm the exact `Installed integration commit` reported by the CLI. The CLI does not modify installed files or restart/reload HA.
