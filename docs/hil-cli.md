# Guided HIL CLI: environment and read-only preflight

This is the **first, non-destructive step** of the guided HIL work in [#227](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/227). It collects environment provenance and a snapshot of existing ALLPOWERS entities. It does **not** implement BLE packet capture, route proof, write scenarios or hardware qualification; these belong to #229-#232.

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
- `--route auto|local_adapter|active_proxy`: record the **requested** route. The **observed** route is always `unverified` at this stage, even if a proxy is connected.
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

`scanner_type=remote` means a candidate **remote scanner**, not necessarily an ESPHome active proxy or a proven connection route. The API cannot establish the ALLPOWERS station's active GATT source, proxy firmware, local adapter chipset or BlueZ version. Accordingly `esphome_active_proxy` and the actual route remain **unknown/unverified**; passing `--route active_proxy` does not change that. The command is not part of the unprivileged registry API: it needs an HA administrator account. **Do not make admin access a prerequisite** for ordinary `doctor`/`run`. A separate authorized HA host collector or future connection-layer instrumentation is needed for the missing metadata.

`manifest/get` can establish the **reported installed integration version**, but does **not** expose the installed files' exact Git SHA. The CLI records the runner checkout commit when available while marking the *installed* commit and build match as unverified (or mismatched if manifest versions differ). Firmware/hardware versions from HA may use a constrained decimal representation or hexadecimal notation such as `0xF1`; arbitrary strings are rejected. The run is always **inconclusive for HIL qualification** until later instrumentation provides route-specific trace/build evidence and physical checks.

The CLI currently requires Home Assistant Core **2026.8 or later** for the registry contract. Its scanner response contract has been checked against the **2026.10.4 Home Assistant source and upstream tests**, and is covered by local mock-based regression tests. **A real HA Container 2026.10.4 smoke test has not been performed**: no live HA endpoint, login token, or attached BLE station is available to this development environment. Do not mark #228 physically validated on the basis of the source review or automated tests.

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

```bash
python -m pytest tests/test_hil_cli.py
ruff format --check scripts tests
ruff check scripts tests
```

See [the full qualification runbook](hil-qualification.md). Do not update `qualification_matrix.json` or mark any control/route passed based on the read-only preflight alone.
