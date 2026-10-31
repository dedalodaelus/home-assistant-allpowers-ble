# 0017. Reconcile HIL qualification with write-capability policy

- Status: Proposed
- Recorded: 2026-10-31
- Decision state in code: Partially implemented
- Supersedes: None
- Superseded by: None

## Context

Accepted [ADR 0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
requires hardware-in-the-loop validation before a profile gains write capabilities.
The current implementation grants the R600 hardware 0.3 profile to canonical
VOLIX P1800 identities with hardware revision `0.3` / raw `0x03`, while the
qualification matrix records pending routes, scenarios, and write capabilities.
The same matrix currently has pending physical qualification results for R600.

PR #183 reports one physical VOLIX P1800 unit running firmware `0xF1`, validated
with the integration and ESPHome component. It does not identify a Bluetooth
route or enumerate the required HIL scenarios and writable controls. The report
does not establish qualification for other firmware versions. The runtime
authorization logic does not use firmware version as an allowlist.

These are facts about current code and evidence, not an accepted exception to
ADR 0008. The compatibility classification `Verified code profile` describes
software identity/revision matching; it does not mean physical qualification has
completed.

## Decision

This proposal does not amend or supersede ADR 0008 and does not approve an
exception. ADR 0008 remains the accepted policy while the current conformance gap
is unresolved. Maintainers must either complete the HIL evidence it requires or
formally propose and accept a superseding ADR before changing the policy. Until
then, pending matrix results remain pending and must not be described as physical
qualification.

## Alternatives considered

### Treat a successful default matrix validation as physical qualification

Rejected. The default validator checks structure, file references, and redaction;
it allows pending results and cannot prove radio behavior.

### Silently reinterpret ADR 0008 as a release-only HIL requirement

Rejected. This changes an accepted architectural requirement without review,
evidence, or a superseding decision.

### Complete the required hardware qualification

This satisfies the existing accepted policy without changing it. It remains an
open operational action and does not resolve whether future profiles may be
enabled before their full HIL matrix passes.

## Consequences

### Positive

- Runtime classification, physical evidence, and architectural policy remain
  separately visible.
- Pending results cannot be mistaken for a passed HIL qualification.
- Any policy exception or change receives explicit review and traceability.

### Negative and trade-offs

- The current code/policy mismatch remains unresolved until qualification or a
  superseding ADR is completed.
- The proposal does not itself disable current code capabilities or provide
  physical evidence.

## Evidence

- [ADR 0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
- [Model support implementation](../../../custom_components/allpowers_ble/model_support.py)
- [HIL qualification matrix](../../../tests/hil/qualification_matrix.json)
- [VOLIX P1800 evidence scope](../../../tests/hil/fixtures/volix-p1800-hw03/README.md)
- [Compatibility policy](../../compatibility.md)
- [Quality contract](../../quality.md)
- [PR #183](https://github.com/dedalodaelus/home-assistant-allpowers-ble/pull/183)

## Fitness functions

- Keep route, scenario, and write-capability results explicit in the HIL matrix.
- Run `python scripts/validate_hil_qualification.py --require-pass` when claiming
  the required HIL qualification is complete.
- Before changing the policy, accept a new ADR that links to and supersedes ADR
  0008; update the compatibility and quality documents to match.

## Review triggers

- The VOLIX P1800 or R600 required HIL results are completed.
- Maintainers propose a firmware allowlist or a different firmware-evidence policy.
- A new writable model or hardware revision is enabled before completing its HIL
  qualification.

## Related decisions

- [0008](0008-authorize-writes-with-verified-capabilities-and-semantic-state.md)
  remains accepted and unchanged; this proposal records a current conformance gap.
- [0012](0012-keep-unverified-controls-read-only-or-opt-in.md) keeps unverified
  controls read-only or explicitly opt-in.