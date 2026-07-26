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
