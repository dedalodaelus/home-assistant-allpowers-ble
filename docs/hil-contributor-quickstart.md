# HIL contributor quickstart

| Tier | Purpose and physical evidence |
|---|---|
| **Bronze** | Reviewed, read-only telemetry and physical observations for the exact station and observed route. |
| **Silver** | Bronze plus physically verified on/off and safe restoration for each applicable basic output. |
| **Gold** | Silver plus physically verified declared settings/functions and applicable recovery behavior, including restoration. |
| **Platinum** | Gold plus a verified, uninterrupted 24-hour soak. Optional; it is not a next-release target, and the current public handoff cannot qualify it. |

Use this short path to collect and submit evidence for one physical station.
The full
[qualification guide](hil-qualification.md#calculated-hardware-support-tiers)
is the normative source for tier rules; this table is only a summary.

A physically confirmed absent feature does not need an impossible control test
and does not by itself block Silver or Gold. Unknown features, or present
applicable features that have not been tested and reviewed, can leave the tier
unearned. Every result is scoped to the exact model, hardware revision, firmware,
and **observed** BLE route. One actually observed local-adapter or active-proxy
route is enough for its own scope; do not invent evidence for another route.
`operator_attested` records the operator's own inspection; `independently_reviewed`
means a different person inspected the evidence. Neither assurance label nor any
support tier authorizes production writes.

## 1. Prepare a current checkout and HIL environment

Start from current `devel` and use a topic branch. In an existing clone, for
example:

```bash
git fetch origin devel
git switch -c hil/device-route-evidence origin/devel
```

Install the isolated CLI dependencies from the repository root.

Linux/macOS:

```bash
python3.14 -m venv .venv-hil
source .venv-hil/bin/activate
python -m pip install -r requirements_hil.txt
```

Windows PowerShell:

```powershell
py -3.14 -m venv .venv-hil
.\.venv-hil\Scripts\Activate.ps1
python -m pip install -r requirements_hil.txt
```

These commands use Python 3.14, matching the documented HIL CLI environment.
See [HIL CLI installation and token handling](hil-cli.md#installation) for
details. The CLI prompts for a missing Home Assistant token without echoing it;
do not put a token in a command or shell history.

## 2. Install and verify the same integration build

Install the `custom_components/allpowers_ble/` files from this checkout into
Home Assistant's configuration directory at
`custom_components/allpowers_ble/`, then restart Home Assistant. A released
HACS installation may differ from this development checkout. The manifest
version (including `0.3.0`) alone does not prove that installed files match.
The read-only doctor reports build verification; `onboard` also checks the
installed integration fingerprint against the checkout before qualification.
If the fingerprints do not match, stop and install the matching files before
continuing.

Run the same doctor command on Linux/macOS or Windows PowerShell:

```console
python -m scripts.hil doctor --ha-url https://home-assistant.example:8123
```

The doctor needs the HA origin from `--ha-url` or `HA_HIL_URL`; unlike
`onboard`, it does not prompt for a missing origin. It prompts securely for a
missing token.

`doctor` is a read-only preflight. It does not establish a connected BLE route;
onboarding verifies route evidence from the connected backend.

## 3. Run the guided acquisition

From the repository root with `.venv-hil` active, start the same interactive
entry point on all supported platforms:

```console
python -m scripts.hil onboard --ha-url https://home-assistant.example:8123
```

The wizard selects the station and asks whether Bronze, Silver, or Gold evidence
is requested. Silver and Gold use this same command: Silver adds real on/off and
restoration checks for applicable outputs; Gold adds declared setting/function
and recovery checks. Remain present, disconnect critical loads, and separately
authorize every state change and restoration. The wizard cannot turn missing,
unsafe, or failed results into PASS.

The optional `--route local_adapter` or `--route active_proxy` records a requested
route for the run; it does not force Home Assistant to use it. The connected
backend must establish the observed route. Use whichever route is actually
connected and verified. A contributor with only a local adapter needs no proxy
evidence; one with only an active ESPHome proxy can submit that observed route.

## 4. Review the evidence and calculate the attained tier

On success, inspect the generated report and privacy preview. Follow the wizard's
explicit confirmations to export the public ZIP, review the import dry-run, apply
the import only after reviewing its proposed changes, and preview/apply the
physical review as prompted. A ZIP import, software test result, operator
consent, or synthetic fixture is not physical PASS evidence. Never edit the
qualification matrix to invent results. The tier evaluator reports the attained
tier and remaining requirements from reviewed evidence.

If review is interrupted after import, resume offline using the exact command
printed by `onboard`. The general form for Bronze is:

```console
python -m scripts.hil review --run-id archive-... --interactive
```

For a pending higher-tier review, use the same command with `--tier silver` or
`--tier gold` as applicable. Review provenance is explicit: operator attestation
records the operator's own inspection; independent review requires a different
person to inspect the evidence. The name alone does not prove independence.

## 5. Inspect the allowlist and open the PR

After the exact requested tier is validated, `onboard` generates a
template-compliant PR body and prints exact Git and GitHub CLI commands for the
matrix and verified public evidence files in that run. Inspect each named public
file and review `git status --short`, the generated body, and the staged diff.
Use only those printed allowlisted paths; never add private `hil-results/`, raw
captures, secrets, or the original ZIP. Keep the PR base at `devel` and link the
relevant issue. The wizard does not commit, push, or open a PR automatically.

The handoff prints signed commit and verification commands. Agents must run
them as printed, as required by [`AGENTS.md`](../AGENTS.md). Human contributors
may remove `-S` from the printed commit command and omit the
`git verify-commit HEAD` command when signing is not configured. Signing is
optional under [`CONTRIBUTING.md`](../CONTRIBUTING.md). Keep commit hooks
enabled and never use `--no-verify`.

## Troubleshooting

- **Fingerprint mismatch:** stop. Copy the matching checkout's integration
  files into Home Assistant, restart it, and rerun `doctor`/`onboard`. A
  matching manifest version is not a fingerprint match.
- **Output step failed:** do not retry automatically or proceed to import.
  Follow the wizard's exact diagnostic and recovery instructions; retain the
  local run for diagnosis and inspect safe restoration before another run.
- **No proxy route:** `--route active_proxy` cannot create or force one. Check
  that an active ESPHome Bluetooth Proxy is available to Home Assistant, or
  continue only with another route that is actually observed and verified.
- **Review interrupted:** run the printed offline `review --run-id ...
  --interactive` command (adding `--tier silver` or `--tier gold` when printed).
  This resumes review of imported evidence without repeating BLE acquisition.

For advanced capture, reporting, and reviewer details, continue to the
[complete HIL CLI guide](hil-cli.md) and the normative
[qualification guide](hil-qualification.md).
