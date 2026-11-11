# Compatibility

## Home Assistant version

The project's declared support minimum is Home Assistant Core **2026.10.4**.
The owner set this HACS installation floor to align supported releases with the
repository's current real-Core test baseline (`requirements_test.txt`). This is
a project support policy: earlier Core releases are outside declared support,
but are not asserted to be technically incompatible.

The read-only HIL preflight CLI has a separate **2026.8+** requirement for its
remote registry contract; see the [HIL CLI guide](hil-cli.md). That tool-specific
minimum does not lower the integration requirement.

## Support levels

The repository distinguishes three runtime code-profile levels:

- **Verified code profile**: the model identity and hardware revision match a
  supported profile, so the integration can expose its capabilities after the
  runtime safety checks. This label does not mean that the full physical HIL
  qualification is complete.
- **Experimental**: the device advertises a candidate name and passes the active
  GATT/protocol probe, but the exact hardware revision has not been fully
  validated. These devices run in read-only mode.
- **Rejected**: available evidence indicates another protocol revision; setup is
  stopped before a persistent entry is created.

Physical HIL qualification is tracked separately in
[`tests/hil/qualification_matrix.json`](../tests/hil/qualification_matrix.json).
Schema v2 keeps results scoped to a model/hardware revision, firmware, observed
route, and run. Only linked, independently reviewed physical evidence can
support a `pass`; a successful default matrix-validator run checks structure and
privacy, not qualification. The current opt-in strict schema-v2 gate accepts only
fresh, complete, attributed results and requires both supported routes for each
firmware. Strict mode enforces a 30-day maximum age by default; `--max-age-days N`
overrides that limit. Issue #273 tracks the planned opt-in, non-expiring policy.
It selects the newest eligible run per firmware and route, so a later complete
failure supersedes an older pass while an inconclusive archive import does not.
Scenario results must pass; a write capability may be `not_applicable` only with
scoped supporting evidence and a rationale. Capture records must also match the
matrix revision, firmware, run ID, and timestamp. Independent physical evidence
review remains necessary.

## Write authorization and hardware support tiers

Production write authorization is reviewed per model/revision and individual
control. Before adding or newly enabling a control, require an understood command
frame and masks, representative read/write vectors and negative regression tests,
fresh pre/post GATT observations, operator-confirmed physical effect in both
directions (or a safe reversible change and restoration), preservation of
unrelated/unknown bits, and transaction/reconnect checks. Record the tested
firmware and observed BLE route. Evidence does not automatically generalize to
other firmware or routes; maintainers must review an equivalence rationale.
Full matrix completion, both routes, vendor-app contention, and a 24-hour soak
are not prerequisites for each individual control.

Bronze (evidence-backed read-only), Silver (physically verified applicable basic
output controls), and Gold (verified declared settings/functions and recovery).
Platinum is an optional tier; earning it requires Gold plus a verified,
uninterrupted 86,400-second soak. The soak is not required for Bronze or Gold, or
for authorizing an individual control. These are the accepted support tiers;
tier calculation is not implemented yet (#269). Per-feature
production enforcement (#266/#267), per-route qualification (#268), and temporary
candidate HIL grants (#275) are also follow-up implementation work. Current code
still uses grouped R600/VOLIX write-capability flags, and the current strict
schema-v2 validator still requires both routes per firmware. The one-route and
tier rules above describe the accepted target policy, not current code or
validator behavior. A support tier never grants a write capability. Candidate HIL
experiments are intended to use a separate attended, temporary, single-use path;
they do not expose normal Home Assistant write entities or authorize production
writes. Unknown, absent, conflicting, or unsafe operations remain blocked. See
accepted [ADR 0017](architecture/decisions/0017-reconcile-hil-qualification-with-write-capability-policy.md)
and [ADR 0012](architecture/decisions/0012-keep-unverified-controls-read-only-or-opt-in.md).

### Transitional inventory: currently writable code profiles

| Profile | Current code permissions | Evidence status and compatibility impact |
|---|---|---|
| Canonical R600, hardware 0.3 / raw `0x03` | Output and settings controls enabled; settings keepalive supported; experimental car-charger control is separately opt-in and defaults off. | No independently reviewed scoped runs are recorded in the matrix (`runs: []`). #227 reports guided `active_proxy` AC on/off and ECO-timeout pilot runs; these remain unqualified and pending review. Other routes, scenarios, and matrix write capabilities remain pending. Existing controls remain enabled under a documented transitional allowance while targeted review is outstanding. |
| Canonical VOLIX P1800, hardware 0.3 / raw `0x03` | Same R600 profile permissions. | PR #183 reports one unit on firmware `0xF1`, without observed route or per-control results. All scoped routes, scenarios, and write capabilities remain pending. Current access remains transitional; the report does not qualify every control, firmware, or route. |

This allowance describes current shipped behavior; it is not a HIL PASS, does not
qualify the profiles physically, and does not approve additional write
capabilities. Runtime authorization still applies the five safety gates in ADR
0008/0017. The matrix stays unchanged until independently reviewed evidence
supports a scoped result. Follow-up: #206, #227, #248, #266–#269, #273, and #275.

## Current matrix

| Model or advertised family | Level | Setup behavior |
|---|---|---|
| Canonical R600 identity recognized by the integration with revision signature (`hardware_version=0.3`, `raw_hardware_version=0x03`) | Verified code profile; HIL pending | Accepted after active probe with writable controls enabled. Output writes are rejected if status includes unknown output-related bits; settings writes are rejected when semantic safety checks fail. |
| R600 discovery candidate with an unrecognized identity (for example, `R6000` or `AP R1500`) | Experimental | A discovery-prefix match is not canonical identity verification. Accepted candidates remain telemetry-only; writable entities are not created. |
| Canonical VOLIX P1800 identity with verified revision signature (`hardware_version=0.3`, `raw_hardware_version=0x03`) | Verified code profile; HIL pending | Accepted after active probe using the R600 protocol profile and the same runtime write safety checks. PR #183 reports one physical unit with firmware `0xF1`; route-specific and full HIL qualification remain pending. |
| VOLIX P1800 discovery candidate with an unrecognized identity or missing/unverified revision | Experimental | A broad discovery match is not identity verification. Accepted candidates remain telemetry-only; writable entities are not created. |
| AP S300 / other `AP S*` candidates | Experimental | Accepted only if FFF0/FFF1/FFF2 and a valid status response are confirmed. Telemetry only; writable entities are not created. |
| AP S500 | Rejected | Aborted as a known different protocol family. |
| AP S700 V2 | Rejected | Aborted as a known different protocol family. |
| Generic `ALLPOWERS*` | Experimental candidate | Requires active probe. Telemetry only; writable entities are not created. |
| Service UUID FFF0 without a useful name | Experimental candidate | Requires active probe. Telemetry only; writable entities are not created. |

## Why advertisement matching is broad

Bluetooth advertisements can vary by firmware and proxy. Broad discovery filters
such as `R600*`, `AP R*`, and `VOLIX P1800*`, together with the FFF0 service UUID,
route candidates to the config flow. This is only the first filter: names such as
`R6000`, `AP R1500`, and `VOLIX P1800-ABCD` can match discovery without establishing
a canonical model identity.

Model identity is resolved separately with stricter canonical-name matching.
Writable R600 and VOLIX P1800 profiles require a recognized canonical identity and
the exact verified hardware signature. Unrecognized identities and other or
missing revisions stay experimental/read-only, even if they match a discovery
prefix or pass the probe. A persisted canonical identity can survive generic or
missing advertisements; a conflicting specific identity blocks writes until the
conflict is resolved using a later identity observation and current-session
settings telemetry.

The final protocol filter is the active probe, which requires:

1. a connectable route selected by Home Assistant;
2. service FFF0;
3. notify characteristic FFF1;
4. write characteristic FFF2;
5. a valid status response after the known request;
6. a frame that passes header, length, checksum, and payload validation.

## Hardware revisions

A commercial model name can cover more than one controller, BLE module, or firmware
family. A report must include the exact label/revision and advertised name. Do not
change a rejected family to supported merely because it exposes one matching UUID.

## Verified revision evidence lane

Every profile classified as a **Verified code profile** must have a revision
record in `tests/hil/qualification_matrix.json`. Each physical run is recorded
separately for one firmware and one observed route. The matrix supports scoped
support-tier and release-gate decisions; it is not an all-or-nothing prerequisite
for each individual production write authorization. A newly enabled control
instead requires the operation-specific evidence above. A run records:

- route results for the routes actually observed (`local_adapter` and/or
  `active_proxy`);
- scenario status for replay, reconnect/failover, rapid writes, stale/timeout,
  upgrade/reload, vendor-app contention, and soak;
- write-capability status for every exposed writable control.

Capture artifacts must stay sanitized and stored under
`tests/hil/fixtures/<revision-id>/`. Review public archives with:

```bash
python -m scripts.hil import <zip> --dry-run
```

Add one only with `--apply`. Imported archives remain inconclusive until
physical results are independently reviewed.
Do not publish Bluetooth addresses, serial numbers, Home Assistant identifiers,
or topology details.

The matrix records qualification progress, not the software classifier result.
Unexecuted results remain pending; missing or incomplete evidence is inconclusive.
Neither status counts as a strict pass, and legacy v1 results remain unscoped.
Do not infer coverage from unit tests, an undifferentiated hardware report, or a
result for another firmware or route. The current VOLIX P1800 evidence scope and
its missing route-specific captures are recorded under
`tests/hil/fixtures/volix-p1800-hw03/`.

Use the [HIL qualification runbook](hil-qualification.md) for the end-to-end
capture, matrix update, and validation flow.

## Reporting a result

Use the public issue forms to report compatibility and bugs:

- Compatibility reports:
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/new/choose>
- Bug reports:
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/new/choose>
- Security reports (private):
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/security/advisories/new>

For a compatibility report, provide:

- product model and revision from the physical label;
- advertised Bluetooth name;
- Home Assistant and integration versions;
- adapter/proxy model and ESPHome version when relevant;
- whether discovery, probe, telemetry, and each control succeeded;
- sanitized diagnostics;
- packet evidence only after removing addresses and identifying information.

A positive status probe does not automatically make write features safe. Each
shared bit field and hardware revision must be verified independently before
write controls are enabled.

For verified profiles, the hardware revision must be observed in the active GATT
session. That verification remains valid for the session even when no new settings
notification arrives; a disconnect or new session invalidates it. Output writes
still require a fresh status snapshot and semantic validation. Settings mutations
still require a fresh settings snapshot and semantic validation. Structurally valid
packets can still be shown for diagnostics even when they are not trusted for writes.

Model identities are persisted as canonical family names, while the latest
advertised name remains available separately for diagnostics. A conflicting
specific identity latches the entry into read-only mode without replacing its last
known identity. MAC-only advertisements do not clear that state. Recovery requires
a later identifying observation and current-session settings telemetry that either
verify the observed revision or confirm the preserved supported identity. A
persisted rejected-family identity that conflicts with the entry's original model
name is allowed to reconnect for telemetry, but remains read-only until that
recovery check succeeds.

## Security and trust model

Write authorization is a local trust decision and depends on multiple layers:

- Home Assistant user permissions control who can issue entity writes.
- BLE proximity constrains range but does not remove local radio risk.
- ESPHome proxies and local adapters are part of the trusted transport boundary.
- Experimental/read-only profiles are intentionally prevented from creating write entities.

For safety, avoid unattended automation of critical loads. Prefer notifications,
manual acknowledgements, and explicit conditions for connectivity, telemetry
freshness, and battery thresholds before issuing non-critical control actions.

## Entity semantics

Binary sensors for power flow use measured power direction only:

- `Input active`: `input_power_w > 0`
- `Output active`: `output_power_w > 0`

These entities do not claim battery charging/discharging direction because the
current protocol evidence does not provide a verified battery-flow field.

### Why profile verification matters for write access

The BLE protocol used by ALLPOWERS devices does not provide cryptographic device
authentication. Write authorization therefore depends on:

1. **Hardware identity verification**: The canonical model identity and exact
  hardware revision must match a supported code profile. Firmware version is
  recorded as qualification evidence, but the current runtime authorization code
  does not enforce a firmware allowlist.
2. **Protocol stability**: Flag bits, reserved fields, and payload structure must
   be fully understood to perform safe read-modify-write operations without
   corrupting device state.
3. **Semantic validation**: Detected inconsistencies in state (e.g., unknown flag
   bits in status) trigger write rejection rather than guessing at safe behavior.

For VOLIX P1800, the only reported physical evidence is one unit running firmware
`0xF1` (PR #183). Other firmware versions are not physically qualified by that
report, but the current code does not enforce a firmware allowlist: the shipped
transitional profile may grant existing writes when canonical identity and the
current-session hardware revision match. This is an evidence-scope limitation,
not evidence that other firmware is qualified or a newly approved firmware policy.

### Read-only mode for experimental devices

Devices that pass active protocol validation but cannot be matched to a verified
hardware revision profile operate in read-only mode. This includes:

- R600 devices with an unverified hardware revision signature.
- AP S* protocol family candidates.
- Generic ALLPOWERS advertisements without a specific model match.

Even if such a device successfully responds to status requests, writable control
entities are not created. Telemetry collection continues, allowing long-term
monitoring and data gathering to support future hardware verification.

### Adding a new profile with write capabilities

To add or newly enable an individual production write control requires:

1. Capturing representative traffic from the exact hardware revision on your
   physical device.
2. Documenting all behavior: status flag bit meanings, settings byte layout,
   output command encoding.
3. Adding regression tests that validate the protocol behavior for that revision.
4. Fresh pre/post GATT observations and operator-confirmed physical effect for
   both directions (or a safe reversible change and restoration), preserving
   unrelated and unknown bits.
5. Recording tested firmware and observed route, with an explicit reviewed
   rationale before generalizing to other firmware or routes.
6. Explicit maintainer review of the evidence, profile policy, and regression
   tests before enabling the control.

Completing every HIL route, scenario, or 24-hour soak is not required to authorize
one individually evidenced control. Qualification and support-tier claims remain
separately scoped to the matrix.

Contributing a new unverified device that merely exposes the FFF0 service will
not enable writes. The integration distinguishes between "device is working" and
"device is safe to write to."

When a verified capability profile is discovered later from settings telemetry,
the integration adds writable control entities exactly once without requiring a
config-entry reload. If capabilities later downgrade to read-only, existing
control entities remain in the registry but become unavailable.

### Trust boundary assumptions

Write authorization assumes:

- **Local network isolation**: Only devices on the local network can be discovered
  or issued commands.
- **Physical proximity**: BLE range limits attack surface to nearby locations.
- **Home Assistant access control**: Only users with configuration or entity
  control permissions can issue commands.
- **No credential compromise**: The protocol uses no shared secrets; there is no
  token or key to leak remotely.
