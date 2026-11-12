## Before submitting

- [ ] For normal changes, the base branch is `devel` (`main` is release-only). If this PR comes from a fork, confirm the base repository and branch before submitting; GitHub may default to `main`.
- [ ] The PR title uses Conventional Commits syntax, for example `docs: clarify contributor workflow` or `test: cover status freshness timeout boundary`.
- [ ] The related issue is linked, and existing issues and PRs were checked before starting duplicate work.

## Summary

<!-- What changes and why? -->

## Protocol and safety impact

<!-- State whether protocol bytes, writable flags, timing, or connection behavior change. -->

- [ ] No protocol write behavior changes.
- [ ] Unknown bits remain preserved.
- [ ] Writes still require fresh state from the current GATT session.
- [ ] Hardware model/revision evidence is attached or linked when required.

## Validation

- [ ] Unit tests added or updated.
- [ ] Malformed, timeout, stale, disconnected, and cancellation paths considered.
- [ ] Ruff, Mypy, Pylint, tests, coverage, and repository validation pass.
- [ ] English and Spanish translations updated for user-visible changes.
- [ ] Documentation and changelog impact addressed.
- [ ] Logs, diagnostics, addresses, serial numbers, and packet captures are sanitized.

## Branch workflow

- [ ] For non-hotfix work, base branch is `devel`.
- [ ] For `main` targets, source branch is `devel` or `hotfix/*` (cut from `main`) only.
- [ ] This change does not require bypassing branch protections on `devel` or `main`.

## Hardware results

<!-- Exact model/revision, firmware, adapter/proxy, operations tested, and duration. -->
