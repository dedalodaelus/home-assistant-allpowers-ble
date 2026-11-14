# Guided HIL CLI: preflight, private traces and scenarios

The `doctor` and `run` commands collect non-destructive environment provenance and existing Home Assistant entity states. The separate administrator-only `trace` command in #229 **does capture private runtime GATT API data**, using the integration's existing BLE connection and no extra BLE commands. The private trace v2 identifies the established local BlueZ adapter or ESPHome active proxy only when the connected backend exposes sufficient evidence; otherwise its route is unverified. The separately authorized `probe-trace` command also captures Home Assistant's existing config-flow GATT probe, including flows without a config entry. Neither capture mode provides independent RF evidence or physical hardware qualification. See [#227](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/227) and #229-#232.

## Installation

Run the CLI from a checkout of this repository with Python **3.14** (the project CI baseline). Dependencies are isolated from the production integration/HACS:

```bash
python3.14 -m venv .venv-hil
source .venv-hil/bin/activate
python -m pip install -r requirements_hil.txt
python -m scripts.hil --help
```

The offline `transaction-safety --simulate` runner is optional and uses the
repository's fake-GATT test harness. To enable it in this isolated environment,
install the development/test dependencies separately:

```bash
python -m pip install -r requirements_test.txt
python -m scripts.hil run --suite transaction-safety --simulate
```

This installs test tooling only in `.venv-hil`; it does not change
`requirements_hil.txt` or the HACS integration dependencies. Without the
optional dependencies, the command exits inconclusive and prints this install
command.

Set `HA_HIL_TOKEN` in the process environment first, or omit `--non-interactive` to enter a long-lived access token via a hidden prompt. Create the token from the Home Assistant user profile. The CLI **does not** accept `--token`, save credentials, load `.storage` files, use the Docker socket, access D-Bus or open an independent BLE connection. Use a dedicated testing HA account with the read access needed for entity/device registries. For ordinary data, authentication is required; restricted registry permissions result in a safe error.

`HA_HIL_URL` may be used instead of `--ha-url`. Only the HA origin is valid, without `/api`, user info, query strings or embedded tokens. HTTPS uses normal certificate verification; there is **no** option to disable it. Plain HTTP to a non-loopback address requires `--allow-insecure-http` and exposes the token on the network; only use it on an explicitly trusted, isolated test network. **The CLI deliberately ignores environment proxy variables** (`http_proxy`, `https_proxy`, `ALL_PROXY`, etc.) for both REST and WebSocket transport: this prevents a token intended for a loopback HA server from leaking to an ambient proxy. A reverse proxy at the explicitly selected HA URL still works; upstream HTTP CONNECT proxies are not supported. Do not expose the token in shell history or screenshots.

For a local HA Container installation:

```bash
export HA_HIL_URL=http://127.0.0.1:8123
read -rs -p "HA long-lived token: " HA_HIL_TOKEN; echo
export HA_HIL_TOKEN
python -m scripts.hil doctor --non-interactive
python -m scripts.hil run --device 1 --route auto --non-interactive
```

A remote host and HA OS are also supported using the REST/WebSocket APIs. `network_mode: host` in Home Assistant does **not** give the CLI access to the HA machine's Bluetooth or Docker host data. If the host is elsewhere, its kernel/CPU/RAM/BlueZ/container runtime remain explicitly unavailable until an authorized host collector is added. The local `runner_host` section describes the computer running the CLI, not the HA host.

## Commands

- `doctor`: authenticate, verify HA Core version, read the integration manifest and registries, and independently check each selected station's integration-entry state, BLE-connected diagnostic state and (if enabled) status telemetry freshness. No filesystem output. Readiness is **ready**, **incomplete** (observed disconnection/staleness or unloaded entry) or **inconclusive** (missing/disabled/unreadable diagnostic). Only all three `ready` checks yield exit 0; incomplete/inconclusive yield exit 2 without claiming the physical hardware is faulty. `binary_sensor.telemetry_available` is disabled by default; enable this diagnostic entity in HA if you need a conclusive freshness check. Registry discovery and `run` remain available even if optional entry-state or sensor data are missing.
- `run`: do the same and read existing battery, power, remaining time, hardware/firmware and connectivity entities where available. No service calls, commands, BLE writes, host reconfiguration or HA reloads.
- `--device 1`: select an indexed device (or a model name if unambiguous). If there is one device, selection is automatic. In non-interactive mode, multiple devices require an explicit selection.
- `--route auto|local_adapter|active_proxy`: record the **requested** route. Runtime trace v2 determines the **observed** route from the connected backend, not from the requested route or available scanner advertisements.
- `--output-root`: choose the output directory. The default is `./hil-results/`, ignored by Git.

Each `run` creates a permission-restricted folder:

```text
hil-results/<UTC-time>_<random-run-id>/
  private/                  # raw local evidence and restart checkpoints
    environment.json
    session.json
  public/                   # generated allowlisted projection
```

The private environment and session sources use schema version 1, UTC timestamps, structured `value/source/verified` metadata and explicit `unavailable` reasons. The session records the sanitized associated config-entry state (`loaded`, `not_loaded`, `setup_error`, etc.) and a separate `preflight.checks` object with `entry_loaded`, `connected` and `telemetry_fresh`. Unknown is **not** treated as failure or success. `run` is always allowed to save a degraded read-only snapshot; `qualification_status` remains `inconclusive`. Registry device IDs, entity IDs, BLE addresses, tokens, IP addresses, SSIDs, hostnames, full HA configuration and device-provided arbitrary attributes are **never written**. The private source files are not themselves an attachment; use `report` or `export` below to build the public projection.

## What can and cannot be verified

The CLI reads `/api/config`, `config/entity_registry/list`, `config/device_registry/list`, `manifest/get` and optionally `config_entries/get`, plus selected `/api/states/<entity_id>` GETs. Registry `model`, hardware and firmware are **last recorded metadata**, not proof of live BLE freshness. The `binary_sensor.connected` state reflects the integration's coordinator-reported connection; `binary_sensor.telemetry_available`, when enabled, reflects its fresh-status check, *not independent hardware causality*. Unavailable states are not replaced with guessed values, and translated entity names are never used to infer functionality.

## Build and attach a privacy-safe report (#231)

Reporting and export operate offline on an existing run; they do not use a Home
Assistant URL/token or upload anything. Point them at the visible run directory
(legacy flat runs are also accepted):

```bash
python -m scripts.hil report hil-results/<UTC-time>_<run-id>
python -m scripts.hil export hil-results/<UTC-time>_<run-id>
```

`report` rebuilds `public/` from an allowlisted projection and writes
`public/report.md`. Review the report and the export preview. In a terminal,
type `EXPORT` when prompted; in automation, use
`export <run-directory> --non-interactive --confirm-export` only after review.
The archive `hil-evidence.zip` contains `public/` files only. Attach it
manually to the issue or PR; there is no automatic posting. Unknown frame
payloads and uncorrelated trace data are omitted. A step result or complete
capture does not establish physical HIL qualification, and the generated
qualification status remains inconclusive where evidence is insufficient.
Guided-tier archives also carry verified, allowlisted child checkpoints, GATT
event/frame metadata, and operator-observation summaries in
`tier-artifacts.json`. Opaque artifact references bind these files to the
requested steps; local paths, HA identifiers, BLE addresses, grant tokens, and
raw notification payloads are excluded. Outbound transaction metadata remains
review evidence, not an automatic qualification result.
After interruption, run `report` on the same directory; it does not resume or
repeat HIL actions.

For `run --suite recovery`, choosing vendor-app contention asks for one
operator-reported result: could not connect, connected, or unable/unwilling to
test. The runner never opens the app or changes the BLE route. It reports
`ha_stable` for an unchanged verified session or `ha_recovered` after an
observed session change only when the bounded trace contains a fresh decoded
status packet and complete verified route evidence. The app answer is stored
separately from machine observations. A resume gap, missing status packet,
unverified route, or incomplete trace stays inconclusive; the result cannot
establish why the app connected or failed, or prove uninterrupted behavior
outside the observed interval.

### Review and import a public evidence ZIP

An exported ZIP can be reviewed and added to the repository without upgrading
qualification results:

```bash
python -m scripts.hil import path/to/hil-evidence.zip --dry-run
python -m scripts.hil import path/to/hil-evidence.zip --apply
```

Import defaults to a dry run. Review the reported profile, firmware, observed
route, capture completeness, guided scenario records, and missing qualification
evidence before rerunning with `--apply`. The importer verifies archive members,
schemas, privacy allowlist and file hashes, then copies only public ZIP members
into `tests/hil/fixtures/` and records the run in the v3 matrix. Imported run,
scenario, and write-capability results remain `inconclusive`; a guided software
step or route trace does not become a physical qualification PASS. The original
session run ID is pseudonymized before export; the archive does not expose that
source ID. The importer uses the pseudonym and verified contents for logical
run identity. ZIP SHA-256 is retained separately as an artifact integrity
value. Repackaging or re-exporting the same public members is
idempotent. If a known session ID arrives with different verified member
contents or scope, import fails without changing the matrix or fixtures. When
the source session ID is unavailable, a canonical hash of the verified member
names and contents supplies a deterministic fallback independent of ZIP
timestamps and packaging. Conflicting existing evidence is rejected.
Archives without verified model or hardware metadata are kept under an
unattributed revision identifier. Missing capture timestamps or firmware remain
unknown and cannot satisfy strict qualification.

For independent review, inspect the imported `tier-execution.json` and its
referenced artifacts in `tier-artifacts.json`. Record only physically reviewed
claims in the schema-v3 qualification matrix, including the reviewer, timestamp,
evidence digest map, and scope-bound capture rows required by the
[qualification guide](hil-qualification.md). Then compute the attained tier:

```bash
python -m scripts.hil tiers --matrix tests/hil/qualification_matrix.json
```

Imported consent, software PASS labels, synthetic captures, or hashes without
their verified child artifacts cannot create a qualification PASS.

For imported Bronze archives, the reviewer CLI can apply a digest-bound
independent review. Create a JSON file that names the archive digest and
imported run ID, and set each claim to `pass` only after inspecting its
supporting public artifacts; otherwise use `inconclusive`:

```json
{
  "schema_version": 1,
  "archive_sha256": "<imported archive sha256>",
  "run_id": "<archive-... run ID>",
  "reviewed_by": "Reviewer name",
  "reviewed_at": "2026-11-13T12:00:00+00:00",
  "claims": {
    "identity": "pass",
    "protocol_probe": "pass",
    "fresh_telemetry": "pass"
  }
}
```

Preview, then explicitly apply the reviewed claims:

```bash
python -m scripts.hil review \
  --matrix tests/hil/qualification_matrix.json \
  --run-id archive-... \
  --review-file bronze-review.json
python -m scripts.hil review \
  --matrix tests/hil/qualification_matrix.json \
  --run-id archive-... \
  --review-file bronze-review.json \
  --apply
```

The command preserves a claim as inconclusive unless its imported archive
digest, manifest, route, build, runtime session, and claim-specific evidence
verify. It invokes the schema-v3 tier evaluator after applying the review;
review input and operator consent alone cannot create Bronze PASS.

### Bluetooth adapter/proxy candidates in Home Assistant 2026.10.4

Home Assistant 2026.10.4 registers the **admin-only** WebSocket command `bluetooth/subscribe_scanner_details` ([server implementation](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/bluetooth/websocket_api.py#L249-L294); [upstream tests](https://github.com/home-assistant/core/blob/2026.10.4/tests/components/bluetooth/test_websocket_api.py#L310-L373)). This is a short-lived *subscription*: it returns a command acknowledgement, then an initial `event.add` list of scanner objects, if any. That list includes **addresses, names and source IDs**. The CLI requests the initial event only when permitted, collects four **counts only** (`total_scanners`, `connectable_scanners`, `remote_scanners`, `connectable_remote_scanners`), and closes the subscription. No raw scanner record or identifier enters the JSON. With non-admin accounts, Bluetooth not loaded, unsupported API, or an absent initial event, `bluetooth_scanner_candidates` is explicitly `unknown`.

`scanner_type=remote` means a candidate **remote scanner**, not necessarily an ESPHome active proxy or a proven connection route. The API cannot establish the ALLPOWERS station's active GATT source, proxy firmware, local adapter chipset or BlueZ version. Accordingly `doctor` / `run` leave `esphome_active_proxy` and the actual route **unknown/unverified**; passing `--route active_proxy` does not change that. The separate administrator-only `trace` command uses connected-backend evidence in schema v2 to report the observed GATT route when verifiable, without establishing RF or physical-output proof. The scanner command is not part of the unprivileged registry API: it needs an HA administrator account. **Do not make admin access a prerequisite** for ordinary `doctor`/`run`. A separate authorized HA host collector is still needed for host metadata unavailable through HA's API.

`manifest/get` reports the installed integration version. The admin-only `allpowers_ble/hil/build` WebSocket command also hashes the installed integration files; the CLI computes the same sorted relative-path and file-byte SHA-256 over its checkout, excluding `__pycache__` and `.pyc`. The scan is bounded and fails closed if the file set changes while it runs. `verified` means the fingerprints match, the tracked integration subtree is clean, and Git supplies a valid commit SHA. `mismatch` means the file fingerprints differ (a matching manifest version does not change this). `unverified` means the endpoint is unavailable/unsupported, scanning failed, the checkout is dirty, or its Git commit cannot be established; the reason is recorded. Older HA installations remain usable with an unverified result.

Install the development build from the exact clean checkout used by the CLI into HA's `custom_components/allpowers_ble`, then restart Home Assistant. Run `allpowers-ble-hil doctor` (or `run`/`trace`) and confirm `Installed integration build: verified` plus the reported installed commit SHA. Matching files on disk does not prove which Python modules are already loaded in memory; restart (or an explicitly performed integration reload where supported) after deployment before HIL execution. The toolkit never copies files or restarts/reloads HA automatically.

The CLI currently requires Home Assistant Core **2026.8 or later** for the registry contract. Its scanner response contract has been checked against the **2026.10.4 Home Assistant source and upstream tests**, and is covered by local mock-based regression tests.

Operator-run tests recorded in PR #236 used Home Assistant Core **2026.10.4 in a Container** with an R600 (hardware 0.3, firmware 0.2), from test checkout `a4f2887782ad10b322772d81d4cf76c184c9fb9d`. The initial config-flow `probe-trace` succeeded over both local BlueZ and an ESPHome active proxy: each report records 16 events, one status-request TX, one RX, and zero dropped events or frames. Runtime route tracing was also exercised on both transports (26 GATT frames on local BlueZ; 27 on the active proxy, including 26 RX and one TX). These are operator-run hardware observations, separate from automated simulated GATT E2E tests; they do not establish that the current PR head was physically retested.

The installed full integration build SHA remains **unverified**. A reported matching `hil_probe.py` file hash identifies only that file, not the complete installed build. The manual scanner-inventory preflight has not been demonstrated on a live instance, including its non-admin behavior. Do not mark #228 physically qualified from these tests: they provide no independent RF or physical-output evidence.

### Live read-only pilot checklist (on a volunteer's HA 2026.10.4 machine)

1. Install the *exact PR head* in a checkout with the isolated HIL requirements. Verify `doctor` can enumerate the ALLPOWERS R600 model/revisions without showing MACs, entity IDs or tokens. Use a dedicated HA test account; a normal (non-admin) account must still work for the core discovery APIs when permitted.
2. Run `doctor --device 1`. Observe the separate integration/BLE/telemetry verdicts. If the `telemetry_available` diagnostic is disabled, record `inconclusive` and optionally enable **only that diagnostic entity** through HA's UI; do not change BLE routes or power outputs.
3. Run `run --device 1 --route auto` and inspect the generated `environment.json` and `session.json`. Confirm the reported HA and firmware versions, status/telemetry, no raw registry identifiers, and `qualification_status: inconclusive`. Confirm `route.observed` and `integration.qualification_allowed` still deny qualification.
4. With an administrator test account, compare the **aggregate** scanner inventory with HA's Bluetooth UI: optional remote/connectable candidates can appear. Confirm that the same CLI with a non-admin account merely records `unknown` (not a hard failure) for the scanner inventory. Do not publish addresses, tokens or the raw inventory.
5. Review JSON for privacy before attaching a **sanitized, redacted summary** to issue #228. Document HA version, HIL-tool commit, exit codes and deviations; do not invent a PASS or upload the raw files. Hardware qualification requires #229–#232.

Example (run where HA is actually reachable at loopback; otherwise supply the trusted HTTPS origin):

```bash
export HA_HIL_URL=http://127.0.0.1:8123
read -rs -p "HA test token: " HA_HIL_TOKEN; echo
export HA_HIL_TOKEN
python -m scripts.hil doctor --device 1 --non-interactive
python -m scripts.hil run --device 1 --route auto --non-interactive
unset HA_HIL_TOKEN
```

If `doctor` exits 2 because the optional freshness sensor is disabled, that means **insufficient evidence**, not a failed device. `run` still saves the read-only snapshot.

## Keep a 24-hour soak observable across terminal disconnects

The soak command runs in the foreground. Keep it in a terminal multiplexer on
the **same machine where Python and the CLI process run**. For a remote Debian
host, that means `tmux` on the Debian host, inside the SSH session. Detaching
or losing the SSH client is harmless only while the server-side tmux session
and soak process continue running. It does not protect against a process exit,
host shutdown/reboot, host sleep, or Home Assistant outage.

### Linux, macOS, or remote Debian with tmux

Install `tmux` using the operating system's package manager if it is not already
installed. From the repository checkout, create a named session:

```bash
tmux new -s allpowers-soak
```

In the tmux shell, enter the checkout and prepare the isolated CLI environment
(skip the venv and install commands if it is already prepared):

```bash
cd /path/to/home-assistant-allpowers-ble
python3.14 -m venv .venv-hil
source .venv-hil/bin/activate
python -m pip install -r requirements_hil.txt
export HA_HIL_URL=https://home-assistant.example:8123
HA_HIL_TOKEN="$(python -c 'import getpass; print(getpass.getpass("HA long-lived token: "))')"
export HA_HIL_TOKEN
python -m scripts.hil run --non-interactive --device 1 --suite soak-24h
unset HA_HIL_TOKEN
```

The hidden prompt does not echo the token or put it in shell history. Do not
replace it with a literal in a command, `.env` file, or shell startup file. Once
the soak starts, detach with **Ctrl-b**, then **d**. Reconnect to the same host
and run `tmux attach -t allpowers-soak` to inspect or reattach. Closing the SSH
client after detaching leaves the remote process running; closing the local
terminal before detaching may interrupt it. Keep the host awake and the tmux
session alive for the entire segment.

### Windows using WSL2 (not yet runtime-tested)

Use WSL2 with the repository checkout, Python 3.14 environment, and `tmux` all
inside the same Linux distribution. Do not run Python in Windows while trying
to keep it alive with a tmux server in WSL. Install tmux in the distribution
(for Ubuntu, `sudo apt install tmux`), open the repository in WSL, and follow
the Linux workflow above. Start `tmux new -s allpowers-soak` from that WSL
shell. Detach with **Ctrl-b**, then **d**; later open the same distribution and
use `tmux attach -t allpowers-soak` to reconnect.

This is the recommended Windows setup, but its lifecycle has not yet been
runtime-verified on Windows with WSL2. Validate that a detached tmux process
survives closing and reopening Windows Terminal on the target machine before
relying on it for a soak.

Closing Windows Terminal after detaching is safe only while the WSL2
distribution remains running. `wsl --shutdown`, shutting down or restarting
Windows, or otherwise stopping the distribution kills the process; Windows
sleep pauses execution and breaks wall-clock continuity. Keep the computer
awake, on power, and the WSL2 distribution running. A process exit or any such
interruption ends that segment; resuming starts another segment and cannot
make one continuous 24-hour PASS. The supported Windows instructions here are
for WSL2; native PowerShell background execution is not documented as a
supported persistent-run method.

### Progress, checkpoint, and report

Reattaching to tmux lets you confirm the session is still running and see any
terminal output, but the soak currently prints no periodic progress metrics.
Long periods of terminal silence are expected. For progress, inspect the local
checkpoint, which is updated as samples and traces are collected. The default
output root is `hil-results/` in the checkout. A run has a UTC timestamp and
random identifier, and its checkpoint is
`hil-results/<run-name>/private/checkpoint.json`; the adjacent `scenarios.json`
records segment outcomes. Pretty-print the checkpoint locally with:

```bash
python -m json.tool hil-results/<run-name>/private/checkpoint.json
```

In the last object in `steps`, check `sample_count`, `trace_count`, and
`measured_seconds`; `last_sample_utc` appears after the first successful sample.
The counters advance when their corresponding samples or traces are collected,
so they do not all change at the same interval. The checkpoint and its output
are private local evidence: do not commit `hil-results/`, copy checkpoint data
into a public issue, or share its contents.

After completion or interruption, generate the sanitized report and optional
public ZIP from the run's private directory:

```bash
python -m scripts.hil report hil-results/<run-name>/private
python -m scripts.hil export hil-results/<run-name>/private
```

Review `public/report.md` and the export preview before manually sharing the
public ZIP. If continuing an interrupted soak, first use a fresh, verified HA
connection and the same selected device. In the new shell, load the HA URL and
enter the token again without echo or shell history, then explicitly resume:

```bash
export HA_HIL_URL=https://home-assistant.example:8123
HA_HIL_TOKEN="$(python -c 'import getpass; print(getpass.getpass("HA long-lived token: "))')"
export HA_HIL_TOKEN
python -m scripts.hil run --non-interactive --device 1 --suite soak-24h --resume hil-results/<run-name>/private
unset HA_HIL_TOKEN
```

The checkpoint records a restart discontinuity and the resumed observation is
a new segment. SSH client disconnection alone does not create a discontinuity
when tmux and the process remain alive on the remote host. A stopped process,
sleep/reboot, or HA interruption does; no resumed segments can be combined to
claim one uninterrupted 24-hour PASS. The observer is read-only, and surviving
an SSH disconnect or producing a report does not itself imply HIL PASS.

## Verification

## Guided scenario runner (#230)

The scenario interface is being introduced incrementally. List declarations and
inspect a side-effect-free plan with:

```bash
python -m scripts.hil run --list-suites
python -m scripts.hil run --suite read-only --dry-run
python -m scripts.hil run --suite all --dry-run
```

`all` currently selects only read-only scenarios. Write, recovery and soak
suites are never implicitly enabled. The `transaction-safety` suite is offline
only; `--simulate` runs selected existing fake-GATT regression tests against the
production client. A failing assertion reports `fail`; missing, skipped or
unavailable cases report `inconclusive`. This simulation does not contact HA,
write to hardware, or qualify a build. The ordinary `run` command remains the
#228 read-only snapshot. Run the bounded 24-hour observer explicitly with
`python -m scripts.hil run --device 1 --suite soak-24h`.
It samples every five minutes, allows at most a ten-minute observation gap,
collects an hourly 15-second route trace, and caps private run data at 10 MiB.
Only a full uninterrupted 24-hour segment can pass; a process restart starts a
new segment and records the missing time as a discontinuity. Recovery and soak
can resume a private schema v2 checkpoint for the same selected device. Resume
continues observation only; it never repeats an operator action or replays an
output write. Output checkpoints are not resumable because an interrupted
service call may be ambiguous.

### Qualify station stability before a 24-hour soak

`run --suite soak-24h` is a read-only observer. It never enables or changes
settings keepalive. Keepalive must be enabled before the soak starts; if it is
disabled, the CLI shows the Home Assistant settings path, waits for you to
enable it, then rereads the option before continuing. In non-interactive mode,
it exits by default. To proceed anyway, explicitly pass
`--allow-keepalive-disabled`; the CLI warns that a station cutoff can interrupt
the soak. The default is disabled. Keepalive is not proven to prevent a
physical station cutoff around ten minutes.

```bash
python -m scripts.hil run --device 1 --suite soak-24h --non-interactive \
  --allow-keepalive-disabled
```

Before starting the 24-hour clock, inspect the integration options in Home
Assistant at **Settings → Devices & services → Integrations → ALLPOWERS BLE →
Configure → Advanced timing → Enable settings keepalive**. The keepalive
interval is in the same section. Current defaults are 540 seconds for the
interval and 600 seconds for the settings-stale timeout; when keepalive is
enabled, validation requires the interval to be below the stale timeout.
Shorter intervals increase BLE traffic.

Before the soak, run an attended, short physical preflight under comparable,
safe conditions for longer than the previously reported ten-minute cutoff.
First record behavior using the current keepalive setting. If it is disabled,
record that preflight separately, then explicitly enable keepalive before
starting the soak. Record the enabled configuration and timestamps as a
separate test; enabling keepalive is not claimed to prevent a cutoff. Keep
output changes manual and separately authorized; use only a safe noncritical
load. Do not automate output writes, change BLE routes, or retry ambiguous
commands. Record each observation,
including:

- device model, hardware revision, firmware, Home Assistant Core, and
  integration build/revision;
- actual observed BLE route (local adapter or active proxy), not just the
  requested route;
- keepalive enabled/disabled state, interval, and settings-stale timeout;
- noncritical load and station auto-off settings;
- test conditions and timestamps, station power/activity, and any cutoff or
  interruption;
- route-specific settings TX/RX evidence when available.

Start the 24-hour clock only after the selected configuration stays stable
throughout preflight. On a cutoff, stale telemetry, or missing route proof,
stop and document the reason; do not record a completed soak or PASS. A second
attempt that stays on does not by itself prove keepalive prevented cutoff.
Record unknown or inconclusive when observations or captures are insufficient.
Keep private observations private; review the privacy-filtered `report` and
`export` output before sharing it, and keep identifiers and tokens out of public
evidence. The keepalive/cutoff relationship remains unresolved until repeated,
route-specific physical testing establishes it.

`run --suite read-only` also requests an admin-only runtime capability snapshot
for the selected loaded entry. Without admin access, capability evidence is
recorded as unavailable and write eligibility stays false. Control entities
are matched by integration ownership, device, platform and unique-ID suffix;
disabled, ambiguous, missing or unavailable entities are ineligible. The
snapshot stores only allowlisted states and options, never HA entity or entry
IDs. This does not execute controls or establish physical qualification.

The `outputs` suite offers separately selectable AC, DC and station-light
on/off cycles, plus the optional car charger when its integration opt-in is
enabled. AC remains the default; choose another declared scenario with
`--scenario-id output_dc_cycle`, `--scenario-id output_light_cycle`, or
`--scenario-id output_car_charger_cycle`. The car charger also requires fresh
settings evidence and its explicit integration capability flag. Each scenario
checks the active profile and state before each write, requires separate
numeric consent, records short private traces and asks for the physical
result. Run only with a safe noncritical load and no hazardous
equipment connected. If the operator authorized the final off step but trace
setup failed before its service call, the runner rechecks the same fresh
session and may perform one traced restoration. After an ambiguous service
call, one separately authorized, traced restoration may be offered only when
fresh same-session telemetry shows the known intermediate state and no
transaction is pending. Otherwise inspect and restore the station manually.
A restoration does not turn an incomplete run into a pass. These scenarios
have not been piloted on hardware.

The `settings` suite currently provides ECO-mode, work-mode and ECO-timeout
checks. Each change is restricted to a declared value exposed by HA, requires
fresh settings and a separate authorization, and is followed by a separately
authorized attempt to restore the original value. After an ambiguous service
call, the runner may offer one traced restoration only when fresh same-session
telemetry confirms the changed value and no transaction is pending. Missing
confirmation or route evidence remains inconclusive. Settings checks have not
been piloted on hardware.

The `recovery` suite is a guided observation. It asks the operator to choose
and perform a route interruption/restoration, vendor-app contention, or HA
reload/upgrade; the CLI does not initiate any of those actions. For route
interruption/restoration and HA reload/upgrade, successful recovery requires a
verified connected session with a new generation; an observed disconnect
alone is insufficient. The optional vendor-app branch can instead report
`ha_stable` for the same verified session when it captures genuinely new
status telemetry and complete verified GATT route evidence. It reports
`ha_recovered` when a changed session is observed and that evidence is
complete. It captures a bounded route trace and asks for a numeric physical
outcome where applicable.
Missing route proof, incomplete trace, or missing recovery confirmation
remains inconclusive. The evidence is not build-level HIL qualification.

```bash
python -m scripts.hil run --device 1 --suite recovery
python -m scripts.hil run --device 1 --suite outputs
python -m scripts.hil run --device 1 --suite outputs --scenario-id output_dc_cycle
python -m scripts.hil run --device 1 --suite outputs --scenario-id output_light_cycle
python -m scripts.hil run --device 1 --suite outputs --scenario-id output_car_charger_cycle
python -m scripts.hil run --device 1 --suite settings --scenario-id settings_work_mode
python -m scripts.hil run --device 1 --suite settings --scenario-id settings_eco_timeout
```

Each guided run writes private `scenarios.json` and `checkpoint.json` files
after steps. To resume a recovery observation, pass the existing run folder:

```bash
python -m scripts.hil run --device 1 --suite recovery \
  --resume hil-results/2026-11-07T01-00-00Z_0123456789abcdef
```

Resume validates the checkpoint schema, matching session/run identity, private
file permissions, and a run-scoped fingerprint of the selected HA origin and
device. It refuses old checkpoint versions, symlinks, mismatched devices,
output runs, and completed or failed recovery scenarios. It records the
restart as an unobserved clock interval and never repeats the disruptive
operator action; that gap cannot count as continuous qualification time.

```bash
python -m pytest tests/test_hil_cli.py
ruff format --check scripts tests
ruff check scripts tests
```

See [the full qualification runbook](hil-qualification.md) for matrix schema v3. A strict feature verdict selects one exact scope, for example `python scripts/validate_hil_qualification.py --require-pass --revision r600-hw03 --firmware 1.1 --route active_proxy --feature ac_output`. Do not mark a control or route passed based on a read-only preflight alone.


## Experimental private GATT trace (#229)

An administrator can record bounded runtime GATT activity without creating a second
Bluetooth connection. Enable explicitly with:

```bash
HA_HIL_URL=https://homeassistant.example HA_HIL_TOKEN="$TOKEN" \\
  python -m scripts.hil trace --non-interactive --duration 20
```

Use `--device 1` for multi-device installations and `--route auto` (or a
requested route). Do not put the token in shell command arguments. The command
creates `trace.json`, `events.jsonl` and `frames.jsonl` inside the private
run directory (`0600`). Only a HA administrator can start, stop or fetch
captures; capture access remains bound to that HA account and selected config
entry. Only one capture is active at a time; active TTL is 120 seconds. Up to
**four independently addressable sealed captures** remain privately retrievable
for 30 seconds each, so starting the next scenario does not erase the previous
one. A fifth sealed result evicts the oldest immediately (FIFO), after which
the previous owner cannot fetch it. Entry unload/reload erases all captures
for that entry. Each capture stores at most 256 events and 128 frames of at
most 256 bytes. Dropped counters and expiry make evidence incomplete. Raw packet contents may contain private information: **never
publish `frames.jsonl` without a separate protocol-aware privacy review.**

The trace records *GATT API observations*, not independent RF data or
physical output measurements. A trace v2 route can be `local_adapter`, `active_proxy`, `mixed` or `unverified`, based exclusively on the actually connected backend. An explicitly armed config-flow active probe can also be captured at the GATT/API layer with `probe-trace`. Independent RF and physical-output evidence remain unavailable; neither runtime nor probe captures establish physical HIL qualification.

Trace acquisition defaults to 15 seconds and is limited to 30 seconds per
scenario (`--scenario-id safe-label`). Run separate scenarios instead of
silently extending a trace beyond the bounded buffer capacity. A stopped trace
may be fetched again for up to 30 seconds if the stop response is lost.
`complete` only certifies *capture integrity* (no dropped records), not
hardware qualification or successful commands. Zero GATT frames, drops or
missing final acknowledgement produce nonzero CLI exit status; the baseline
`session.json` always remains inconclusive.

Safe, non-identifying `model_profile_selected` and
`notification_decoded.inferred_model_profile` events provide the selected
compatibility profile and current session hardware revision when available;
these are inferences from application telemetry, **not proof of physical BLE
route or device authenticity**. Known characteristic UUIDs accompany GATT
write/notify metadata; raw RX chunks remain unclassified until decoding.


### Legacy private trace v1 schema and interpretation

`trace.json` describes the authenticated, entry-scoped capture. Required fields:
`schema_version=1`, `capture_id`, `run_id`, `scenario_id`,
`requested_route`, `observed_route="unverified"`,
`capture_layer="gatt_api_not_rf"`, `private_raw_bytes=true`,
`qualification_allowed=false`, `closed`, `expired`,
`termination_reason`, `complete`, `has_gatt_frames`,
`event_count`, `frame_count`, `dropped_events`, `dropped_frames`.
The `events` and `frames` arrays are stored only in the private JSONL files,
not copied into `trace.json`. Counters describe **stored**, not total observed,
records. Missing or overwritten records contribute to drop counters.

Each line in `events.jsonl` has `schema_version=1`, increasing positive
`seq` (shared with any associated frame), UTC ISO-8601 `utc`,
nonnegative monotonic `elapsed_ms`, `run_id`, `scenario_id`, a machine-readable
`event`, `session_generation` (positive integer or null),
`layer="gatt_api"`, `observed_route="unverified"`, and optional
event-specific *safe* fields. The sequence may have gaps when events are
dropped; when `dropped_events=0`, every frame `seq` must match one stored
event with the same `event` name.

Each line in `frames.jsonl` has those same common keys plus
`direction=tx|rx`, lowercase even-length `hex` (maximum 256 bytes),
`command_type=status_request|output_control|settings_control|unknown`, and
`frame_validation=validated_tx|unvalidated_chunk`. `validated_tx` means
known write framing/XOR, **not** successful GATT submission or a physical
effect. RX chunks are always marked `unknown/unvalidated_chunk`; separate
`notification_decoded` events expose valid decoded packet types. For TX
`write_attempt/write_ok/write_error`, optional `transaction_id` joins to
`transaction_accepted/submitted/confirmed/unresolved/timed_out/abandoned`
events; standalone status requests carry null. Transaction submission means
the GATT operation returned, not that the station applied a setting.

`termination_reason` is `requested_stop` (normal WebSocket stop),
`runtime_stopped` (integration shutdown/unload), or `expired` (active
capture timed out). The original reason is immutable across retries.
`complete=true` requires `requested_stop`, no expiry and zero drops,
but **does not imply device confirmation or HIL qualification**. A silent,
pending write at stop becomes `transaction_unresolved` before its deadline
or `transaction_timed_out` after it, without modifying production state or
sending another BLE command. On a genuine disconnect, the trace instead
records `transaction_abandoned` or `transaction_timed_out` before
`disconnected`. The `disconnected.reason_category` is a finite safe label
(`manual`, `telemetry_watchdog`, `transport_watchdog`,
`settings_recovery`, `unsolicited_disconnect`, `write_failure`), never a
raw exception, address or caller-provided reason.

The CLI rejects missing/malformed schemas, sequences, timestamps, lengths,
private field mismatches and unjoined frames **before saving raw data**. A
runtime-stopped or empty capture exits 2 even with no drops. A sealed capture
remains accessible to its original admin account for up to 30 seconds, then
in-memory private bytes are erased; entry unload erases immediately.


### Cross-scenario causality and fail-closed collection

Transaction IDs use an independent random namespace and a sequence counter,
not the private capture-ID retrieval capability. They are distinct across
overlapping or subsequent HIL runs. When a new run is
attached to an already-connected entry with an outstanding output/settings
transaction, `transaction_inherited` records the **original** correlation
ID, transaction kind, generation, phase and `origin=preexisting`. It does
not fabricate a corresponding TX frame or rerun the BLE write. If the write
occurred while capture was disabled, the inherited ID stays null with
`attribution=unattributed`: the new run must not claim to have initiated it.

The trace emits `control_rejected` for pre-GATT safety guards with only a
control kind (`output`, `settings`, `keepalive`) and a finite safe reason
(`unsupported_capability`, `disconnected`, `stale_telemetry`,
`semantic_validation`, `ambiguous_state`, `confirmation_pending` or
`confirmation_timed_out`). Existing transaction-specific rejection events
may additionally identify the pending transaction, but no false GATT TX row
is created. Hardware status and addresses never appear in the guard reason.

For every frame with a matching event, the CLI requires exact agreement
on schema version, sequence, UTC timestamp, elapsed time, run/scenario,
session generation, layer, observed route, event name and transaction ID
(if present). Elapsed time must not go backwards; ties are valid. Orphan
frames are accepted only when `dropped_events>0`, and such an incomplete
trace exits with status 2. A malformed trace is rejected before writing
private JSONL. If a private file write fails, the CLI attempts to delete
**all** partial trace-only files and reports a sanitized failure; it leaves
the already-created inconclusive `environment.json` and `session.json`
untouched. Disk I/O errors cannot establish physical HIL qualification.


### Opt-in GATT tracing of config-flow probes

The `probe-trace` command uses authenticated administrator-only WebSocket
commands to arm **one exact existing Home Assistant config flow and candidate**.
Open the standard Bluetooth confirmation, manual or reconfigure form in HA
before running it. The tool lists only safe ordinal choices and opaque
short-lived candidate tokens; it never prints Bluetooth addresses or proxies.

```bash
python -m scripts.hil probe-trace --ha-url https://ha.example --non-interactive
```

A single unambiguous candidate is selected automatically. Otherwise supply
`--flow-id` and `--candidate` (server-issued token or candidate ordinal), or
choose interactively. Non-interactive ambiguity fails instead of guessing.

The operator still presses the **ordinary form submit/confirm button in HA**.
The CLI never submits config forms, starts a competing Bleak connection or
sends a second GATT command. An explicitly armed probe records its actual
connection route, status request TX, RX chunks, decoder results, failures
and teardown; reconfiguration reusing a fresh runtime session is recorded
as `probe_skipped` with no fabricated TX.

Probe outcomes (`success`, `failure`, `cancelled`, `skipped`, `interrupted`)
are separate from `capture_complete` and from physical HIL qualification.
A complete failed probe is useful diagnostic evidence but yields exit 2;
even successful private v2 GATT evidence is never physical/RF/output PASS.
Private results use `trace.json`, `events.jsonl`, and `frames.jsonl` with
0700 directory / 0600 file permissions; environment/session JSON remains
inconclusive and excludes MACs, candidate tokens, flow IDs and proxy IDs.
### Connection-route private trace v2 (new runtime captures)

New admin-armed runtime captures use **`schema_version=2`**. The CLI continues
to validate legacy **v1** private evidence with `observed_route=unverified`;
it does not reinterpret old v1 records as route proof. Event, frame and
`trace.json` schema versions must match; a mismatched server response is
rejected before private bytes are saved. The independent
`environment.json`/`session.json` formats are unchanged.

The private `trace.json` now contains `route_observations`, one record per
successfully established connected generation. Each observation records a
positive `session_generation`, the associated `route_observed` event `seq`,
UTC `utc`, monotonic `elapsed_ms`, finite `observed_route`, `backend_kind`
and `evidence`. The optional `adapter_id=hciN` or `proxy_id` is restricted
to private `trace.json`. Proxy identifiers can identify the operator's
network devices: do **not** publish raw trace files or full snapshots.

A `local_adapter` proof requires a connected
`HaBleakClientWrapper`, its actual `HaScanner`, a connected BlueZ backend,
and exact agreement among the scanner's `hciN` adapter name, numeric index
and backend's BlueZ device path. The path itself is **never stored**.
An `active_proxy` proof requires that same connected HA wrapper, an actual
`ESPHomeScanner` and connected `ESPHomeClient`, the **same scanner object**
and an identical, nonempty proxy source. Advertisement provenance, RSSI,
the requested `--route`, an unpatched Bleak client, or the mere existence
of a remote scanner never establishes the route. Missing/version-changed
backend internals are recorded as `unverified`, not guessed.

The route in each event/frame is the route **known when that row was emitted**.
Earlier rows remain `unverified`; reconnect/failover adds a new generation
and never overwrites old frames. A homogeneous set of verified connected
generations produces `local_adapter` or `active_proxy`. Distinct verified
local and proxy generations yield `mixed`. **Any unverified connected
generation forces the summary to `unverified`**; failed pre-connect attempts
do not influence it. Attaching a capture to an already connected client
records a read-only route snapshot with **zero additional BLE operations**.

The CLI validates per-generation uniqueness, route/backend/evidence
consistency, safe private-ID placement, UTC timing, ordering, association
with the first `route_observed` sequence, every event/frame route at its
original sequence, frame/event joins and the recomputed summary. Invalid
or contradictory backend claims are rejected before writing `frames.jsonl`,
`events.jsonl` or `trace.json`. The CLI never copies adapter/proxy IDs into
`environment.json`, `session.json` or event/frame JSONL, and never prints
them. A requested-local/observed-proxy mismatch remains visible in private
metadata but does not authorize qualification.

**`qualification_allowed=false` for every v1/v2 trace and every route**,
even with `complete=true` or `mixed`. A connected backend is not independent
RF evidence or proof that the station changed a physical output. The config-flow
active-probe capture and automated real-HA flow/WebSocket/GATT E2E are implemented.
Operator reports document runtime route tracing and initial config-flow probes
over both local BlueZ and ESPHome active proxy on the older checkout described
above; the current PR head has not been physically retested. Remaining #229
acceptance work includes independent RF/physical-output confirmation and
protocol-aware privacy review/redaction. The disabled-tracing comparison against
`devel` is documented separately in [the benchmark report](hil-disabled-trace-benchmark.md).
