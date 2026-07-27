# Architecture documentation

This directory is the structured architecture entry point for the ALLPOWERS BLE
Home Assistant integration.

## Baseline and intent

- Repository baseline: `main`, reviewed on 2026-07-26.
- Integration version observed in `manifest.json`: `0.2.0`.
- Scope: as-is architecture and architecturally significant decisions.
- Change type: documentation only. No runtime behavior is changed.

The records in `decisions/` are retrospective. They reconstruct decisions already
expressed by the implementation and the existing project documentation. The
recorded date is the date the decision was documented, not necessarily the date
on which the original implementation choice was made.

## Document map

| Document | Purpose |
|---|---|
| [System architecture](system-architecture.md) | Context, components, deployment, runtime flows, trust boundaries, data lifecycle, quality attributes, and known limits. |
| [Architecture decision log](decisions/README.md) | Indexed list of architecture decision records and their relationships. |
| [ADR template](decisions/template.md) | Template for future architecture decisions. |
| [Implementation architecture](implementation-architecture.md) | Concise implementation-oriented architecture summary. |
| [Existing design decisions](../design-decisions.md) | Original informal decision list retained as source evidence. |
| [Protocol](../protocol.md) | BLE framing, commands, state fields, and protocol safety notes. |
| [Compatibility](../compatibility.md) | Verified, experimental, and rejected device profiles. |
| [Quality strategy](../quality.md) | Automated test layers and remaining hardware-validation boundary. |

## Architecture principles

1. Route BLE access through Home Assistant rather than bypassing its adapter and
   proxy selection.
2. Treat protocol compatibility and write safety as evidence-based capabilities,
   not as consequences of a similar product name.
3. Keep stale, disconnected, or semantically unsafe state from authorizing a
   command.
4. Preserve information that the reverse-engineered protocol does not yet
   understand.
5. Bound asynchronous ownership by config entry and invalidate work at session
   boundaries.
6. Keep ephemeral radio and protocol state in memory; persist only validated
   configuration.
7. Expose enough redacted diagnostics and deterministic tests to make failure
   behavior observable.

## ADR workflow

The workflow follows the conventions described by the
[Architecture Decision Record reference repository](https://github.com/architecture-decision-record/architecture-decision-record):

1. Copy `decisions/template.md` to the next numeric file.
2. Use one present-tense imperative decision per ADR.
3. Use lowercase, dash-separated Markdown filenames.
4. Record context, alternatives, consequences, evidence, and review triggers.
5. Start a new decision as `Proposed`; merge it as `Accepted` when the pull
   request establishes agreement.
6. Do not silently rewrite an accepted decision when the architecture changes.
   Create a new ADR and mark the previous record `Superseded`.
7. Link tests, repository checks, or other fitness functions that demonstrate the
   decision remains implemented.

## Status vocabulary

| Status | Meaning |
|---|---|
| `Proposed` | Under review and not yet part of the architecture contract. |
| `Accepted` | Current architecture contract. |
| `Deprecated` | Retained for compatibility but no longer preferred. |
| `Superseded` | Replaced by a later ADR, which must be linked. |
| `Rejected` | Considered and intentionally not adopted. |

## Maintenance rule

Architecture documentation is evidence, not a safety claim by itself. Any change
that expands model support, writable capabilities, protocol semantics, persistent
state, or trust boundaries must update the relevant ADR and add matching tests or
hardware evidence before the new behavior is described as supported.
