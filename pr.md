## Before submitting

- [x] For normal changes, the base branch is `devel` (`main` is release-only). If this PR comes from a fork, confirm the base repository and branch before submitting; GitHub may default to `main`.
- [x] The PR title uses Conventional Commits syntax, for example `docs: clarify contributor workflow` or `test: cover status freshness timeout boundary`.
- [x] The related issue is linked, and existing issues and PRs were checked before starting duplicate work.

## Summary

**Proposed title:** `feat(hil): gate onboarding with verified evidence and PR handoff`

Add a fail-closed onboarding gate that verifies the completed HIL run, artifacts, session, build, route, and requested tier before export. The supported Bronze path displays the public report, requires explicit export/import confirmation, preserves imported results as inconclusive until a distinct reviewer completes the review wizard, validates the matrix and exact-scope tier, then writes a deterministic PR body and prints an allowlisted command handoff. Add a reusable JSON-to-Markdown renderer that preserves the complete repository PR template and all checkbox labels. Update contributor, HIL, and English/Spanish README guidance. No protocol implementation or HA UI strings changed; CHANGELOG.md remains release-tooling owned. Checklist notes: the physical hardware-evidence item is not applicable because this change adds software workflow only; no HA UI strings changed, so translation updates are not applicable; the source-branch restriction for main-target PRs is not applicable because the target is devel.

Related issue: #330.


## Protocol and safety impact

The change adds no BLE writes, output controls, writable flags, or connection behavior. The onboarding gate requires fresh run-bound evidence and blocks export on missing, inconsistent, unsafe, or downgraded results. Imported evidence remains inconclusive until independent review. Unknown protocol fields and current-session freshness requirements are unaffected.


- [x] No protocol write behavior changes.
- [x] Unknown bits remain preserved.
- [x] Writes still require fresh state from the current GATT session.
- [ ] Hardware model/revision evidence is attached or linked when required.

## Validation

Local CI workflow validation passed: HA dependency sync and candidate checks; uv pip check; pre-commit; Ruff format and lint; Mypy; Pylint errors-only; full unit/simulated transport suite (1,458 passed, 100% branch coverage); config-flow coverage suite (58 passed, 100%); real Home Assistant harness (94 passed); HIL transaction-safety simulation (41 cases passed); HIL qualification validator; standalone HIL CLI smoke in .venv-hil; repository validation; release archive build, metadata/checksum dry-run, and archive layout validation. The deterministic PR renderer, onboarding gate, shell command quoting, importer, export, and report tests are included in the full suite. No remote CI was run. The transaction-safety run was a simulation, not physical hardware evidence.

Behavioral evidence and limits: the onboarding gate tests assert that failed runners, stale or mismatched session/build/route/tier evidence, malformed artifacts, and unsafe paths stop before export or PR guidance; these cases catch regressions that could turn incomplete or cross-session evidence into a pass. The passing Bronze fixture exercises report/export/import and reviewer handoff, while tier and renderer tests catch scope promotion and template drift. Preconditions are a completed verified Bronze run, explicit export/import consent, and a separate reviewer. All automated HIL evidence here is synthetic; it proves gate behavior for fixtures, not physical BLE behavior or hardware qualification.

- [x] Unit tests added or updated.
- [x] Malformed, timeout, stale, disconnected, and cancellation paths considered.
- [x] Ruff, Mypy, Pylint, tests, coverage, and repository validation pass.
- [ ] English and Spanish translations updated for user-visible changes.
- [x] Documentation and changelog impact addressed.
- [x] Logs, diagnostics, addresses, serial numbers, and packet captures are sanitized.

## Branch workflow

- [x] For non-hotfix work, base branch is `devel`.
- [ ] For `main` targets, source branch is `devel` or `hotfix/*` (cut from `main`) only.
- [x] This change does not require bypassing branch protections on `devel` or `main`.

## Hardware results

No physical hardware qualification was performed for this software-only change. The HIL transaction-safety job ran a fake-GATT simulation with 41 synthetic regression cases; it provides no device, firmware, route, or write-capability qualification.
