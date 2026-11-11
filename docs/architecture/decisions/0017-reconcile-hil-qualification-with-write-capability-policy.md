# 0017. Reconcile HIL qualification with write-capability policy

- Status: Accepted
- Recorded: 2026-11-11
- Decision state in code: Transitional compatibility; follow-up implementation
  remains open
- Supersedes: [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- Superseded by: None

## Context

ADR 0008 made full hardware-in-the-loop (HIL) validation a prerequisite before a
profile gains write capabilities. The current runtime already grants the R600
hardware 0.3 code profile to canonical R600 and VOLIX P1800 identities. The HIL
matrix has no scoped runs for either profile and retains pending route, scenario,
and write-capability results. A software `verified` classification establishes
identity/revision matching; it does not establish physical qualification.

The current capability inventory is:

| Profile | Current runtime capabilities | Physical evidence and user impact |
|---|---|---|
| Canonical R600, hardware 0.3 / raw `0x03` | Output controls and settings controls enabled; settings keepalive supported; experimental car-charger switch is separately opt-in and off by default. | Required routes, scenarios, and all write-capability results remain pending in the matrix. Existing users have writable controls. |
| Canonical VOLIX P1800, hardware 0.3 / raw `0x03` | Same R600 code profile and capability flags. | PR #183 reports one physical unit on firmware `0xF1`; it does not identify an observed BLE route or enumerate writes/scenarios. Matrix results remain pending. Existing users have writable controls. |
| Other revisions, unresolved identities, and identity conflicts | Telemetry only; no production write capabilities. | No new profile or write authorization is approved by this decision. |

These are current code and evidence facts. The continued write access for the two
shipped profiles is a narrow transitional compatibility allowance, not a physical
qualification result or evidence that every control, firmware, or route is safe.
Removing those existing controls is not part of this policy change; changing
runtime eligibility requires a separately reviewed implementation and migration
plan. Evidence collection and tier computation remain owned by #206 and #266–#269.

## Decision

Production write authorization, HIL candidate testing, and hardware support tiers
are independent decisions:

1. **Profile and candidate eligibility.** A reviewed protocol profile describes
   the exact model/revision and known physical features. Candidate eligibility
   requires a maintainer-reviewed command, frame and masks, readback behavior,
   and profile-specific semantic safety validator. Unknown, absent, conflicting,
   or unsafe operations are not eligible. An absent feature is not applicable;
   a decoded or echoed bit alone does not prove physical presence.
2. **Production write authorization.** Before adding or newly enabling an
   individual production control, maintainers require operation-specific evidence
   for that model/revision: understood protocol frame and masks; representative
   read/write vectors plus negative and regression tests; fresh pre- and post-write
   GATT observations; operator-confirmed physical effect in both directions, or
   one safe reversible change and restoration; preservation of unrelated and
   unknown bits; and transaction/reconnect safety checks. Record the tested
   firmware and actually observed BLE route. Do not generalize across firmware or
   routes without an explicitly reviewed equivalence rationale. Human confirmation
   alone is insufficient.
3. **Attended HIL candidate testing.** A candidate whose production writes are
   blocked may be exercised only through the separate #275 mechanism, after the
   candidate command and semantic guard have been reviewed. The grant is
   administrator-only, short-lived, single-use, and bound to device, session,
   operation, and action. It requires an attended risk acknowledgment and separate
   consent for each action, and is revoked on disconnect, reload, cancellation, or
   timeout. It does not create normal Home Assistant entities/services, bypass
   freshness, semantic, transaction, or bit-preservation checks, or authorize
   arbitrary raw GATT writes. Contributor consent never unlocks an unknown opcode,
   absent feature, identity conflict, or unsafe command.
4. **HIL support tier.** Independently classify evidence coverage: Bronze means
   evidence-backed read-only support; Silver means physically verified applicable
   basic output controls; Gold means verified declared settings/functions and
   recovery. Platinum is an optional tier; earning it requires Gold plus a
   verified, uninterrupted 86,400-second soak. The soak is not required for
   Bronze or Gold, or for authorizing an individual control. Tiers describe
   evidence and never grant production writes. One observed route may qualify
   that route; both routes, vendor-app contention, and the full scenario matrix
   are not prerequisites for each individual safe control.

The five runtime safety invariants from ADR 0008 remain mandatory for every
production write and HIL candidate action:

1. the exact authorized model and hardware capability must match;
2. the GATT session must be active and connected;
3. the source snapshot must be present and fresh;
4. profile-specific semantic validation must authorize the requested operation;
   and
5. no pending or ambiguous transaction may make the source unsafe to reuse.

The shipped R600 0.3 and VOLIX P1800 0.3 production profiles retain their current
capabilities under the transitional compatibility allowance while targeted,
control-by-control physical review is outstanding. This does not approve new
controls or firmware/route generalization. The VOLIX firmware `0xF1` report applies
only to the reported unit; it does not qualify every control, firmware, or route.
The qualification matrix remains unchanged and pending until evidence is captured
and independently reviewed. Do not infer physical qualification from CI, packet
structure, successful telemetry, captured frames without physical outcomes, or
support-tier labels.

ADR 0012 remains in force: unknown or unverified production controls are read-only,
and opt-in remains required for the existing experimental car-charger control.
The #275 HIL grant is a separate attended test path and cannot alter this rule.

## Alternatives considered

### Require the full HIL matrix before any individual production control

Rejected. It prevents safe, operation-specific evidence collection without first
completing unrelated routes and scenarios, creating a qualification deadlock.

### Treat software verification or a successful frame as physical qualification

Rejected. Neither establishes physical effect, preservation, or route-specific
behavior.

### Disable all existing R600 and VOLIX writes immediately

Rejected as an automatic documentation-side effect. It would create a user-visible
regression without a reviewed compatibility and migration decision. The existing
capabilities remain explicitly transitional and evidence-limited.

### Let consent or support tier grant writes

Rejected. Consent is necessary for attended candidate testing but does not
establish command safety. Support tiers classify evidence and never authorize
controls.

## Consequences

### Positive

- Individual controls can be qualified with proportionate, reviewable physical
  evidence without waiting for unrelated matrix scenarios.
- Candidate experiments have a narrow, auditable test-only authorization boundary.
- Runtime authorization, physical qualification, and support tiers have distinct
  meanings.
- Current shipped access and the outstanding R600/VOLIX evidence gap are visible
  without inventing a PASS or causing a surprise permission change.

### Negative and trade-offs

- Existing shipped R600/VOLIX controls remain available during a documented
  evidence gap; runtime guards reduce but do not eliminate that residual risk.
- Reviewers must assess evidence per capability and scope rather than rely on one
  profile-wide qualification result.
- Candidate authorization adds an independent security-sensitive implementation
  boundary in #275.

## Evidence and follow-up

- [ADR 0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [ADR 0012](0012-keep-unverified-controls-read-only-or-opt-in.md)
- [Model support implementation](../../../custom_components/allpowers_ble/model_support.py)
- [HIL qualification matrix](../../../tests/hil/qualification_matrix.json)
- [R600 and VOLIX evidence notes](../../../tests/hil/fixtures/volix-p1800-hw03/README.md)
- [Compatibility policy](../../compatibility.md)
- [Quality contract](../../quality.md)
- Issues [#227](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/227), [#206](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/206), [#248](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/248), [#266–#269](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/266), [#273](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/273), and [#275](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/275)

## Fitness functions

- Tests enforce all five runtime gates and preserve read-only behavior for
  unsupported revisions and identity conflicts.
- HIL candidate-grant lifecycle and enforcement are implemented and tested only
  in #275, coordinated with #266 and #267; #270 and #274 consume that boundary.
- Qualification matrix results remain pending until scoped physical evidence is
  independently reviewed under #206/#232.
- The default HIL validator is not physical qualification. The strict matrix gate
  applies to the release policy and support-tier claims that require it; it is not
  a prerequisite for each individual operation's production authorization.

## Review triggers

- Reviewed HIL evidence changes the authorization decision for an individual
  control, firmware, or route.
- Maintainers propose changing the transitional R600/VOLIX allowance.
- Candidate grant implementation or tier definitions diverge from this policy.
