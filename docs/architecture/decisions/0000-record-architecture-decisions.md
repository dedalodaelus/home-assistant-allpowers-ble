# 0000. Record architecture decisions

- Status: Accepted
- Recorded: 2026-07-26
- Decision state in code: Implemented by this documentation set
- Supersedes: None
- Superseded by: None
- Provenance: New governance decision; acceptance is established by merging the documentation pull request.

## Context

The repository already contains `docs/architecture.md` and
`docs/design-decisions.md`, but the latter groups several decisions without
individual status, alternatives, supersession links, or review triggers. Safety-
relevant BLE behavior evolves across model profiles, protocol evidence, Home
Assistant APIs, and asynchronous failure paths. The reasons behind those choices
need to remain reviewable with the code that implements them.

The reference ADR guidance recommends one version-controlled text file per
significant decision, present-tense imperative filenames, explicit rationale,
timestamps, consequences, and supersession rather than silent historical edits.

## Decision

Record each architecturally significant decision as a numbered Markdown ADR under
`docs/architecture/decisions/`. Keep one decision per record. New decisions start
as `Proposed`, become `Accepted` through pull-request review, and are replaced by a
new linked ADR rather than rewriting history.

Retrospective records must identify themselves as retrospective and must not invent
an original date, decider, or rationale unsupported by repository evidence.

## Alternatives considered

### Keep only the existing narrative documents

This is concise, but it makes individual decisions difficult to supersede, trace,
or review independently.

### Put decisions only in issues and pull requests

Discussion history is useful but is distributed, can be closed or retitled, and is
not guaranteed to remain aligned with the released source tree.

### Adopt an external ADR management service

A service could add workflow features, but it would introduce a dependency and
access boundary that is unnecessary for a small open-source repository.

## Consequences

### Positive

- Decisions are versioned and reviewed beside implementation changes.
- Each decision can link to evidence, tests, and later superseding records.
- Retrospective uncertainty is explicit instead of presented as fact.

### Negative and trade-offs

- Contributors must update the ADR log for architecture-affecting changes.
- Some overlap with concise user/developer documentation is intentional.
- Documentation review cannot by itself prove radio or hardware behavior.

## Evidence

- [Existing architecture summary](../../architecture.md)
- [Existing informal design decisions](../../design-decisions.md)
- [Quality and test strategy](../../quality.md)
- [Architecture documentation entry point](../README.md)

## Fitness functions

- Every ADR filename begins with a unique four-digit number.
- Every ADR contains status, recorded date, context, decision, alternatives,
  consequences, evidence, fitness functions, and review triggers.
- New architecture-changing pull requests link an existing ADR or add a new one.

## Review triggers

- The project adopts a different architecture knowledge-management format.
- Repository governance moves authoritative documentation outside Git.
- ADR volume requires automated indexing or validation.

## Related decisions

All later ADRs depend on this governance record.
