# Release checklist

## Repository settings

- Public GitHub repository with a concise description.
- Topics: `home-assistant`, `hacs`, `custom-component`, `bluetooth`, `ble`,
  `esphome-bluetooth-proxy`, and `allpowers`.
- Vulnerability reporting enabled.
- Branch protection/ruleset requires exactly one status check: `Merge gate`.
- `Merge gate` transitively enforces CI, HACS, Hassfest, title, dependency review,
  and CodeQL checks.
- Workflow permissions allow Release Please to create pull requests and releases.
- All third-party GitHub Actions are pinned to full immutable commit SHAs with
  version comments and updated via Dependabot pull requests.
- Workflows default to no token permissions; each job grants only the read or
  write scopes it needs. Release publication is the only job with write access.

## Branch workflow and promotion

- Day-to-day work merges into `devel` through reviewed pull requests.
- Promotion into `main` happens through reviewed pull requests from `devel`.
- Urgent production fixes may target `main` only from `hotfix/*` branches cut
  from `main`, then must be propagated back to `devel`.
- Release Please runs only on `main` and remains the single writer for
  `CHANGELOG.md` and release tags.
- Dependabot pull requests target `devel`.
- Direct commits into `devel` and `main` are not permitted. Pull requests into `main` are only allowed from `devel` or from `hotfix/*` branches cut from `main`.
- Prepare routine promotion by manually dispatching **Promote devel to main** on
  `main`. It creates or reuses a draft PR and never merges or publishes.
- After **Sync main to devel** reaches default `main`, pushes to `main` prepare
  or reuse a reviewed return-sync PR when `main` is not already an ancestor of
  `devel`. A manual dispatch from `main` recovers missed events. Enable **Allow
  GitHub Actions to create and approve pull requests**; a maintainer may need to
  approve the created PR's workflow runs before required checks execute.
- Review the exact `main` → `devel` PR and merge with a merge commit. The sync
  carries production commits and Release Please metadata; it neither publishes
  nor verifies a release. Confirm publication and HACS assets through the
  Release Please workflow separately.
- The current `devel` ruleset permits squash merges only. Do not deploy or merge
  the return-sync workflow until a maintainer approves an ancestry-preserving
  merge policy and current-base revalidation procedure. The Merge gate requires
  an Actions run display title that binds each required check to its workflow/job
  identity, PR number, repository, exact base/head SHAs, tested `github.sha`
  merge revision. The latest run attempt is read from Actions and tied to the
  named job through that attempt's jobs endpoint and matching check-run ID;
  the display title does not claim to identify a rerun attempt. Mutable PR
  association fields do not establish this evidence. If `devel` advances, close and reopen the PR to trigger fresh
  `reopened` runs; verify each required check links to a successful run with the
  current revision evidence and confirm the Merge gate completed for that merge
  revision. This manual procedure is not enforced by branch protection.
- If the sync PR conflicts, branch protection requires updating `main` from
  `devel`, or linear history prevents an ancestry-preserving merge, stop for a
  maintainer-reviewed recovery. Do not update the `main` head from `devel`,
  bypass protection, or resolve conflicts automatically. Identical trees do not
  prove synchronized histories.
- Enable **Allow GitHub Actions to create and approve pull requests**. A
  maintainer with write access may need to select **Approve workflows to run**
  for CI on the `GITHUB_TOKEN`-created PR.
- The workflow is dispatchable only after its file reaches default `main`.
  Resolve branch synchronization and release manifest differences through a
  separate reviewed bootstrap before dispatching it.

## Code and metadata

- `manifest.json` version is valid SemVer.
- Release tags use plain `X.Y.Z` (no `v` prefix).
- Minimum Home Assistant version is still justified.
- English and Spanish translation trees match `strings.json`.
- Compatibility documentation distinguishes code-profile recognition, feature
  inventory, per-feature evidence, and project hardware support tiers. Tier
  claims are scoped to model, hardware revision, firmware, and observed route;
  they are not Home Assistant IQS ratings or write permissions.
- Changelog describes user-visible and protocol-safety changes.

## Validation

```bash
make clean
make all
python scripts/build_release.py --clean
python scripts/validate_release_metadata.py --tag X.Y.Z --zip-path dist/allpowers_ble.zip --checksum-path dist/allpowers_ble.zip.sha256 --write-checksum --verify-checksum
python scripts/validate_repository.py
python scripts/check_version.py X.Y.Z
```

Inspect the archive:

```bash
unzip -l dist/allpowers_ble.zip
sha256sum dist/allpowers_ble.zip
sha256sum --check dist/allpowers_ble.zip.sha256
```

`manifest.json` must be at archive root and no cache file may be present.

## Hardware

- Re-test discovery and setup.
- Re-test status refresh and stale availability.
- Re-test AC, DC, and light preservation.
- Re-test settings preservation when settings changed.
- Re-test reconnect, Home Assistant restart, and proxy route change.
- Run without unexplained protocol or watchdog errors for a representative period.
- Keep a v2 revision record for every verified hardware revision and separate run
  records for each firmware and observed route. Do not share results across
  firmware versions or routes.
- Review new or newly enabled production controls against ADR 0017's per-control
  physical evidence and runtime safety requirements. A support tier never grants
  write access; the complete matrix and 24-hour soak are separate release/support
  evidence gates.
- Import reviewed public guided-run evidence with
  `python -m scripts.hil import <zip> --dry-run`, inspect the proposed profile,
  scope, evidence paths, and limitations, then use `--apply` to write it.
- Keep results inconclusive when the archive does not include independent
  physical evidence. Store sanitized imported evidence under
  `tests/hil/fixtures/<revision-id>/`.
- Run `python scripts/validate_hil_qualification.py` before opening the release PR.
- For stable promotion where HIL is mandatory, enforce the gate with
  `REQUIRE_HIL_STABLE_GATE=true` and run
  `python scripts/validate_hil_qualification.py --require-pass`. Add
  `--max-age-days N` only when the reviewer requests a finite freshness audit.
- The ordinary CI validator checks matrix structure and evidence reviews offline.
  The no-scope strict gate checks the newest eligible, complete, verified-build
  result for every reported firmware and every route with eligible evidence; it
  does not itself require both supported routes. If this release requires both
  routes, verify that each firmware has eligible results on `local_adapter` and
  `active_proxy` before approval. A newer eligible failure supersedes an older
  pass; incomplete and inconclusive imports do not. Scenarios require PASS;
  write capabilities may use evidence-backed `not_applicable` with a rationale.
  Legacy results do not qualify.

## Publishing

Release publication is handled only by Release Please on `main`.

- Use CI or workflow-dispatch dry-run paths for release validation without
  creating stable tags.
- When Release Please creates a release, both `allpowers_ble.zip` and
  `allpowers_ble.zip.sha256` must be uploaded as release assets.
