# Release checklist

## Repository settings

- Public GitHub repository with a concise description.
- Topics: `home-assistant`, `hacs`, `custom-component`, `bluetooth`, `ble`,
  `esphome-bluetooth-proxy`, and `allpowers`.
- Vulnerability reporting enabled.
- Branch protection requires CI, HACS, Hassfest, conventional title, and dependency
  review checks.
- Workflow permissions allow Release Please to create pull requests and releases.

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
