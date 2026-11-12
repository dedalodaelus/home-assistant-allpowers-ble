# Contributing

Contributions are welcome when they keep protocol safety and compatibility
explicit.

## First-time contributors: start here

- Start from the latest `devel` and target `devel` with your pull request. `main`
  is for releases. This keeps your diff aligned with current development.
- [Open the GitHub comparison page](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare)
  and set the base repository to
  `dedalodaelus/home-assistant-allpowers-ble` and the base branch to `devel`.
  For a fork, choose **compare across forks**, then select your fork and feature
  branch as the head. Confirm all four selections before submitting; GitHub may
  initially select `main` as the base branch.
- Check for an existing issue and pull request before starting, then link the
  issue in your PR description.
- Check for the `status: fixed-in-devel` label before starting work. It means a
  merged PR addressed the issue in `devel`, but the fix is not yet in a stable
  release. Keep the issue open while it is pending release.
- This label automation becomes active only after its workflow has reached
  `main` through the normal `devel` to `main` promotion. Until then, maintainers
  must apply the label and add the pending-release comment manually for fixes
  merged into `devel`.
- Use a Conventional Commits PR title, such as
  `test: cover status freshness timeout boundary`.
- Descriptive source branch names such as `fix/issue-123-description`,
  `test/...`, or `docs/...` are helpful but optional. An unfamiliar branch name
  alone is not a reason to reject useful work.
- GitHub's **Verified** badge indicates a verified commit signature. Signing
  commits is optional; see [GitHub's signing instructions](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits).
  Do not rewrite otherwise valid work just to obtain the badge; maintainers can
  integrate it while preserving attribution.

See the [pull request template](.github/PULL_REQUEST_TEMPLATE.md) for the
repository's safety and validation checklist.

## Support channel

- Use the public issue forms for bugs and compatibility reports:
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/new/choose>
- Use private vulnerability reporting for security issues:
  <https://github.com/dedalodaelus/home-assistant-allpowers-ble/security/advisories/new>

## Before opening a change

- Search existing issues and pull requests.
- For another hardware model, provide the exact model/revision, advertised name,
  GATT service and characteristics, and sanitized packet evidence.
- Do not infer compatibility from product appearance or marketing name.
- Never test experimental output commands with a critical load connected.

## Development setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements_test.txt
pre-commit install
```

Run the full local validation:

```bash
make all
pre-commit run --all-files
```

Mandatory pre-commit policy:

- Every contributor must run `pre-commit run --all-files` before push.
- Commits that skip local hooks (for example with `--no-verify`) are not accepted.
- The CI workflow runs the same pre-commit hooks and blocks merging when any hook fails.

Build and inspect the HACS asset:

```bash
python scripts/build_release.py --clean
unzip -l dist/allpowers_ble.zip
```

The archive must place `manifest.json` and `__init__.py` at its root.

## Pull requests

Use Conventional Commit syntax in the pull request title, for example:

```text
feat: add verified support for S300 revision 2
fix: invalidate settings shadow after disconnect
```

### Branch and promotion workflow

The repository uses an explicit promotion model:

- Feature, bugfix, docs, and dependency work must target `devel`.
- `main` is release-only and receives reviewed pull requests only from `devel` or
  `hotfix/*` branches.
- Release Please runs from `main` and is the only automation that updates
  `CHANGELOG.md` and release tags.
- To prepare a promotion, run **Actions → Promote devel to main → Run workflow**
  from `main`. It runs preflight checks and opens/reuses a draft PR; it never
  merges, pushes a branch, tags, or publishes a release. The repository setting
  **Allow GitHub Actions to create and approve pull requests** must be enabled.
- A PR created with `GITHUB_TOKEN` may require a maintainer with write access to
  select **Approve workflows to run** before its CI starts. Review the PR and
  wait for the existing `Gate all required checks` before merging manually.
- Direct commits into `devel` and `main` are not permitted.
- Urgent production fixes may target `main` only from a `hotfix/*` branch cut
  from `main`; the same fix must be propagated back to `devel` in a follow-up PR.
- When a fix tracked by `status: fixed-in-devel` reaches `main`, a maintainer
  must confirm the fix is present, remove that label, and close the issue. A
  merge into `devel` alone does not mean the fix has been released.
- Rulesets require a single status check: `Merge gate`. That workflow enforces
  downstream validation checks transitively.

When opening a pull request:

- use `devel` as the base branch;
- include the issue reference and validation evidence;
- document any release-process impact in the same pull request.

A pull request should include:

- the problem and design rationale;
- tests for normal, malformed, stale, disconnected, and retry paths as relevant;
- documentation and translations for user-visible changes;
- aligned updates for `README.md` and `README.es.md` when safety guarantees,
  trust-boundary assumptions, removal steps, or automation guidance change;
- a changelog fragment in the pull request description;
- no generated caches, local configuration, diagnostics, or packet captures with
  identifying data.

## Protocol changes

Keep protocol parsing and encoding under
`custom_components/allpowers_ble/protocol/`. That package must remain independent
from Home Assistant and BLE transport.

Any writable bit or byte requires evidence. Preserve unknown fields during
read-modify-write operations. When a safe current snapshot is unavailable, reject
the write instead of assuming defaults.

Do not document a behavior as guaranteed unless the same pull request (or a
referenced merged pull request) contains the matching implementation and tests.

See [Adding models](docs/adding-models.md) for the verification process.

## Testing expectations

- New behavior must have unit tests.
- Parser changes must include valid and invalid vectors.
- Connection changes must cover cancellation, timeout, retry, and disconnect paths.
- Entity changes must cover availability with fresh, stale, and missing data.
- Overall coverage must remain above the configured threshold.

## Licensing

By submitting a contribution, you agree that it may be distributed under the MIT
license in this repository.
