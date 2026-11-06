# Guided HIL CLI: read-only preflight and opt-in private GATT trace

The `doctor` and `run` commands collect non-destructive environment provenance and existing Home Assistant entity states. The separate administrator-only `trace` command in #229 **does capture private runtime GATT API data**, using the integration's existing BLE connection and no extra BLE commands. The private trace v2 identifies the established local BlueZ adapter or ESPHome active proxy only when the connected backend exposes sufficient evidence; otherwise its route is unverified. The separately authorized `probe-trace` command also captures Home Assistant's existing config-flow GATT probe, including flows without a config entry. Neither capture mode provides independent RF evidence or physical hardware qualification. See [#227](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/227) and #229-#232.

## Installation

Run the CLI from a checkout of this repository with Python **3.14** (the project CI baseline). Dependencies are isolated from the production integration/HACS:

```bash
python3.14 -m venv .venv-hil
source .venv-hil/bin/activate
python -m pip install -r requirements_hil.txt
python -m scripts.hil --help
```

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
  environment.json
  session.json
```

Both files use schema version 1, UTC timestamps, structured `value/source/verified` metadata and explicit `unavailable` reasons. The session records the sanitized associated config-entry state (`loaded`, `not_loaded`, `setup_error`, etc.) and a separate `preflight.checks` object with `entry_loaded`, `connected` and `telemetry_fresh`. Unknown is **not** treated as failure or success. `run` is always allowed to save a degraded read-only snapshot; `qualification_status` remains `inconclusive`. Registry device IDs, entity IDs, BLE addresses, tokens, IP addresses, SSIDs, hostnames, full HA configuration and device-provided arbitrary attributes are **never written**. Environment and session schemas will be extended in #229-#232; these files are not yet a publishable evidence ZIP.

## What can and cannot be verified

The CLI reads `/api/config`, `config/entity_registry/list`, `config/device_registry/list`, `manifest/get` and optionally `config_entries/get`, plus selected `/api/states/<entity_id>` GETs. Registry `model`, hardware and firmware are **last recorded metadata**, not proof of live BLE freshness. The `binary_sensor.connected` state reflects the integration's coordinator-reported connection; `binary_sensor.telemetry_available`, when enabled, reflects its fresh-status check, *not independent hardware causality*. Unavailable states are not replaced with guessed values, and translated entity names are never used to infer functionality.

### Bluetooth adapter/proxy candidates in Home Assistant 2026.10.4

Home Assistant 2026.10.4 registers the **admin-only** WebSocket command `bluetooth/subscribe_scanner_details` ([server implementation](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/bluetooth/websocket_api.py#L249-L294); [upstream tests](https://github.com/home-assistant/core/blob/2026.10.4/tests/components/bluetooth/test_websocket_api.py#L310-L373)). This is a short-lived *subscription*: it returns a command acknowledgement, then an initial `event.add` list of scanner objects, if any. That list includes **addresses, names and source IDs**. The CLI requests the initial event only when permitted, collects four **counts only** (`total_scanners`, `connectable_scanners`, `remote_scanners`, `connectable_remote_scanners`), and closes the subscription. No raw scanner record or identifier enters the JSON. With non-admin accounts, Bluetooth not loaded, unsupported API, or an absent initial event, `bluetooth_scanner_candidates` is explicitly `unknown`.

`scanner_type=remote` means a candidate **remote scanner**, not necessarily an ESPHome active proxy or a proven connection route. The API cannot establish the ALLPOWERS station's active GATT source, proxy firmware, local adapter chipset or BlueZ version. Accordingly `doctor` / `run` leave `esphome_active_proxy` and the actual route **unknown/unverified**; passing `--route active_proxy` does not change that. The separate administrator-only `trace` command uses connected-backend evidence in schema v2 to report the observed GATT route when verifiable, without establishing RF or physical-output proof. The scanner command is not part of the unprivileged registry API: it needs an HA administrator account. **Do not make admin access a prerequisite** for ordinary `doctor`/`run`. A separate authorized HA host collector is still needed for host metadata unavailable through HA's API.

`manifest/get` can establish the **reported installed integration version**, but does **not** expose the installed files' exact Git SHA. The CLI records the runner checkout commit when available while marking the *installed* commit and build match as unverified (or mismatched if manifest versions differ). Firmware/hardware versions from HA may use a constrained decimal representation or hexadecimal notation such as `0xF1`; arbitrary strings are rejected. The run is always **inconclusive for HIL qualification** until later instrumentation provides route-specific trace/build evidence and physical checks.

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
suites are never implicitly enabled; an empty suite reports as blocked until
its declarations and executor are implemented. The ordinary `run` command
remains the #228 read-only snapshot. Checkpoint resume is not available yet.

`run --suite read-only` also requests an admin-only runtime capability snapshot
for the selected loaded entry. Without admin access, capability evidence is
recorded as unavailable and write eligibility stays false. Control entities
are matched by integration ownership, device, platform and unique-ID suffix;
disabled, ambiguous, missing or unavailable entities are ineligible. The
snapshot stores only allowlisted states and options, never HA entity or entry
IDs. This does not execute controls or establish physical qualification.

```bash
python -m pytest tests/test_hil_cli.py
ruff format --check scripts tests
ruff check scripts tests
```

See [the full qualification runbook](hil-qualification.md). Do not update `qualification_matrix.json` or mark any control/route passed based on the read-only preflight alone.


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
