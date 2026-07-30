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

## Code and metadata

- `manifest.json` version is valid SemVer.
- Release tags use plain `X.Y.Z` (no `v` prefix).
- Minimum Home Assistant version is still justified.
- English and Spanish translation trees match `strings.json`.
- Compatibility documentation distinguishes verified and experimental hardware.
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
- Update `tests/hil/qualification_matrix.json` for every verified hardware revision.
- Include sanitized fixtures under `tests/hil/fixtures/<revision-id>/` for both
  `local_adapter` and `active_proxy` lanes.
- Run `python scripts/validate_hil_qualification.py` before opening the release PR.
- For stable promotion where HIL is mandatory, enforce the gate with
  `REQUIRE_HIL_STABLE_GATE=true` and run
  `python scripts/validate_hil_qualification.py --require-pass --max-age-days 30`.

## Publishing

Release publication is handled only by Release Please on `main`.

- Use CI or workflow-dispatch dry-run paths for release validation without
  creating stable tags.
- When Release Please creates a release, both `allpowers_ble.zip` and
  `allpowers_ble.zip.sha256` must be uploaded as release assets.
