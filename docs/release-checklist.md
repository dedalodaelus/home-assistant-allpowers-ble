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
python scripts/validate_repository.py
python scripts/check_version.py X.Y.Z
```

Inspect the archive:

```bash
unzip -l dist/allpowers_ble.zip
sha256sum dist/allpowers_ble.zip
```

`manifest.json` must be at archive root and no cache file may be present.

## Hardware

- Re-test discovery and setup.
- Re-test status refresh and stale availability.
- Re-test AC, DC, and light preservation.
- Re-test settings preservation when settings changed.
- Re-test reconnect, Home Assistant restart, and proxy route change.
- Run without unexplained protocol or watchdog errors for a representative period.

## Publishing

For the initial `0.1.0`, push the matching tag; the tag job creates the GitHub
release and attaches `allpowers_ble.zip`. Thereafter, merge the Release Please pull
request to create semantic releases and the HACS asset automatically.
