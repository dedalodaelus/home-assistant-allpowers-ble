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

`HA_HIL_URL` may be used instead of `--ha-url`. Only the HA origin is valid, without `/api`, user info, query strings or embedded tokens. HTTPS uses normal certificate verification; there is **no** option to disable it. Plain HTTP to a non-loopback address requires `--allow-insecure-http` and exposes the token on the network; only use it on an explicitly trusted, isolated test network. Do not expose the token in shell history or screenshots.

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

- `doctor`: authenticate, verify HA Core version, read integration manifest and registries, list ALLPOWERS devices and check the associated integration config-entry state; no filesystem output. Returns a nonzero status if an entry is not loaded or its state cannot be verified. Registry discovery still works even when optional entry-state access is denied.
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

Both files use schema version 1, UTC timestamps, structured `value/source/verified` metadata and explicit `unavailable` reasons. The session records the sanitized associated config-entry readiness (`loaded`, `not_loaded`, `setup_error`, etc.), if the HA API exposes it; absent or ambiguous states remain unknown. Registry device IDs, entity IDs, BLE addresses, tokens, IP addresses, SSIDs, hostnames, full HA configuration and device-provided arbitrary attributes are **never written**. Environment and session schemas will be extended in #229-#232; these files are not yet a publishable evidence ZIP.

## What can and cannot be verified

The CLI reads `/api/config`, `config/entity_registry/list`, `config/device_registry/list`, `manifest/get` and optionally `config_entries/get`, plus selected `/api/states/<entity_id>` GETs. Registry `model`, hardware and firmware are **last recorded metadata**, not proof of live BLE freshness. Unavailable states are not replaced with guessed values, and translated entity names are never used to infer functionality.

`manifest/get` can establish the **reported installed integration version**, but does **not** expose the installed files' exact Git SHA. The CLI records the runner checkout commit when available while marking the *installed* commit and build match as unverified (or mismatched if manifest versions differ). Firmware/hardware versions from HA may use a constrained decimal representation or hexadecimal notation such as `0xF1`; arbitrary strings are rejected. The run is always **inconclusive for HIL qualification** until later instrumentation provides route-specific trace/build evidence and physical checks.

The CLI currently requires Home Assistant Core **2026.8 or later** for the registry contract. It was designed around 2026.10.4 but a live connection to a physical HA host has not been made as part of the automated tests; volunteer verification is still required.

## Verification

```bash
python -m pytest tests/test_hil_cli.py
ruff format --check scripts tests
ruff check scripts tests
```

See [the full qualification runbook](hil-qualification.md). Do not update `qualification_matrix.json` or mark any control/route passed based on the read-only preflight alone.
