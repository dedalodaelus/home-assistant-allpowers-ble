# Roadmap and known risks

## Release status

**Published stable:** 1.2.0 (2026-10-31). **Pending publication:** 1.3.0 is the
next target; work on `devel` is not a published release. Release Please and the
reviewed promotion process determine the published version and package.

The product roadmap is maintained in [issue #57](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/57),
with cross-version HIL program scope and evidence history in [issue #227](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/227).
The release-planning epics are [#391](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/391)
(1.3.2), [#392](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/392)
(1.4.0), and [#393](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/393)
(1.5.0). Those issues carry detailed scope and acceptance criteria; this page
summarizes the sequence. Versions 1.4.0 and later are planning targets, not
release commitments or dates.

## Planned releases

| Target | Focus | Planning and scope |
|---|---|---|
| **1.3.0** | HIL Gold and physical R600 qualification | #227 and [#353](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/353). Gold evidence is a release requirement for R600 HW 0.3 / observed firmware on both `local_adapter` and `active_proxy`, with exact build, route, safe restoration, and reviewed evidence. Bronze PR [#383](https://github.com/dedalodaelus/home-assistant-allpowers-ble/pull/383) is merged; Silver PR [#390](https://github.com/dedalodaelus/home-assistant-allpowers-ble/pull/390) is open; Gold remains pending. The existing Bronze evidence is operator-attested, not physical Gold. Also reconcile [#367](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/367), [#211](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/211), and [#239](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/239); do not close either promotion issue until all acceptance criteria are verified. No Platinum or structural refactor is required for this release. |
| **1.3.1** | Bug fixes and reliability | [#384](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/384), [#385](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/385), [#140](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/140), and [#240](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/240). Fix demonstrated defects, preserve valid fragmented frames, classify watchdog states correctly, and validate affected BLE paths. |
| **1.3.2** | Modularization, typing, and repository quality | Planning epic [#391](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/391): progressive module-size budgets and typing enforcement; modular HIL evidence, CLI, and qualification rules; behavior-based test organization; independent device-state simulation before BLE client modularization. Preserve public schemas, CLI behavior, safety, and coverage. |
| **1.4.0** | HIL UX and contributor experience | Planning epic [#392](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/392): improve progress, prompts, warnings, recovery, result presentation, and contribution handoff while retaining safety guidance and machine-readable contracts. |
| **1.5.0** | HIL Platinum | Planning epic [#393](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/393): extend the reviewed evidence pipeline for Platinum and verify the uninterrupted 24-hour soak contract. Platinum follows Gold; it is not a 1.3.0 requirement. |
| **1.6.0** | Command reliability and transaction safety | [#387](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/387), [#388](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/388), and [#389](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/389). Retain authoritative write guards, explicit confirmations, and safe recovery. |
| **1.7.0** | Power management and Home Assistant UX | [#138](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/138), [#139](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/139), [#141](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/141), [#142](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/142), [#143](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/143), and [#137](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/137). Any default or control change needs explicit review and applicable physical validation. |
| **1.8.0** | Community HIL and hardware expansion | [#355](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/355), [#356](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/356), [#357](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/357), [#358](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/358), with candidate hardware reports [#248](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/248) and [#189](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/189) conditional on device availability. |
| **2.0.0** | Conditional architecture and protocol expansion | [#56](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/56), [#207](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/207), [#208](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/208), and [#91](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/91). Architecture evaluation alone does not require a major release. A 2.0.0 release is justified only if approved public changes are incompatible; otherwise compatible work can remain in 1.x. |

Later-version scopes and milestones are deferred until their planning and
dependencies are ready. No dates are set here. Shipping a release does not by
itself close an umbrella or planning issue; only verified acceptance criteria
can do that.

## HIL evidence, write authorization, and support

Synthetic tests validate software behavior and evidence-processing logic; they
do not demonstrate device behavior or qualify hardware. Physical qualification
requires reviewed evidence tied to the exact model, hardware revision, firmware,
build, session, and observed route. Application-level GATT traces are not
independent RF captures. Missing hardware or incomplete evidence stays
unqualified rather than being inferred from CI.

HIL support tiers describe reviewed evidence scope: Bronze is read-only, Silver
covers applicable basic output controls, Gold covers applicable settings and
functions plus recovery, and Platinum adds an uninterrupted 86,400-second
observation. Tier and write authorization are separate: a tier does not grant
runtime permission. Production writes require per-feature authorization and
fresh authoritative state from the current GATT session. Experimental writes
require an individually reviewed, attended, temporary grant and explicit
consent. Preserve existing transaction, freshness, session, and restoration
gates. See the [HIL program](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/227),
[compatibility guide](compatibility.md#write-authorization-and-hardware-support-tiers),
and [HIL runbook](hil-qualification.md).

## Known risks

### Hardware revision ambiguity

The same commercial name may use another controller or firmware. The active
probe reduces false positives but cannot prove every write field is identical.

### Single active BLE client behavior

Some stations may allow only one active connection. The vendor app or another
Home Assistant instance can cause connection churn.

### Reverse-engineered settings semantics

Unknown bits are preserved, but undocumented interactions may still exist. The
car charger and periodic settings keepalive remain opt-in.

### Proxy and radio variability

Wi-Fi quality, BLE placement, USB interference, and controller limits can
dominate reliability independently of code. Diagnostics expose counters but
cannot repair a poor RF path.

### Home Assistant API evolution

The integration pins a minimum Home Assistant version and runs a real-package
test harness in CI. Behavioral API changes may still require adaptation.

## Release and quality flow

- Changes land on `devel` with the current CI, lint, typing, and repository checks.
- Stable releases are promoted through reviewed pull requests; Release Please owns published versions, tags, and changelog updates.
- Version 1.0 readiness is evaluated against the [quality contract](quality.md) (`IQS-*` criteria). A milestone is not complete while a criterion is `Blocked` or a `release-blocker` remains open.
- Update documentation guarantees only after matching implementation and tests are merged. Roadmap readiness claims must cite the same criteria and evidence used by the quality contract.
- This project tracks practical quality targets for a community integration; it does not claim official Home Assistant Core certification tiers.

## Non-goals

- cloud account integration;
- direct BlueZ management;
- bypassing Home Assistant Bluetooth routing;
- guessed support based only on product name;
- arbitrary raw command services;
- persistent storage of high-frequency telemetry;
- silently issuing writes from stale or assumed state.
