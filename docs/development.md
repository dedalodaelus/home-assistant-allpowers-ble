# Development guide

## Environment

The targeted Home Assistant release requires Python 3.14.2 or newer.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements_test.txt
pre-commit install
```

The custom integration itself does not install a separate protocol package. At
runtime it uses the Bleak and retry connector versions supplied by Home Assistant.

## Fast unit suite

The normal unit suite installs lightweight API-shaped Home Assistant and BLE stubs
before importing the integration. This keeps protocol, task, flow, entity, and
error-path tests fast and deterministic:

```bash
USE_REAL_HOMEASSISTANT=0 pytest --ignore=tests/homeassistant
```

When adding lifecycle or command-transaction tests, prefer deterministic stateful
fakes over ad-hoc mocks or sleeps. Keep transport sequencing explicit by modeling
blocked awaits, delayed callbacks, and ordered notifications so regressions remain
reproducible across environments.

Coverage:

```bash
USE_REAL_HOMEASSISTANT=0 pytest \
  --ignore=tests/homeassistant \
  --cov=custom_components/allpowers_ble \
  --cov-branch \
  --cov-report=term-missing \
  --cov-report=xml
```

## Real Home Assistant smoke test

CI installs the pinned Home Assistant release and imports every Home
Assistant-facing module without stubs:

```bash
USE_REAL_HOMEASSISTANT=1 pytest tests/homeassistant
```

This catches renamed or removed imports while keeping device-independent tests
fast. Hardware-in-the-loop validation remains necessary before marking another
model verified.

## Static analysis

```bash
ruff format --check custom_components scripts tests
ruff check custom_components scripts tests
mypy custom_components/allpowers_ble
pylint --errors-only custom_components/allpowers_ble
```

`make all` runs the standard local sequence.

## Repository validation

```bash
python scripts/validate_repository.py
```

The validator checks:

- one HACS integration under `custom_components`;
- required metadata and brand files;
- manifest/HACS contracts and semantic version;
- translation-key parity;
- PNG dimensions;
- text encoding, line endings, whitespace, and generated caches;
- Python syntax;
- HACS archive layout when a release ZIP exists.

## Building a release

```bash
python scripts/build_release.py --clean
python scripts/check_version.py 0.1.0
```

The builder sorts files, normalizes timestamps and permissions, excludes caches,
and creates a reproducible archive with integration files at its root. The script
prints a SHA-256 digest.

## Release process

Conventional commits feed Release Please. Merging its release pull request updates
the changelog and manifest version, creates a GitHub release, builds
`allpowers_ble.zip`, and uploads the HACS asset.

A release is valid only when:

- the tag, manifest version, and Release Please manifest agree;
- CI, HACS, and Hassfest succeed;
- the release contains `allpowers_ble.zip`;
- the ZIP has `manifest.json` at archive root;
- compatibility claims match tested hardware.

## Manual Home Assistant development

For interactive work, copy or symlink `custom_components/allpowers_ble` into a test
Home Assistant configuration. Do not use a production instance for reverse
engineering or early write tests. Enable debug logging only for the shortest useful
period.

## Coding rules

- Keep the protocol package free of Home Assistant imports.
- Use monotonic time for freshness and timeout relationships.
- Use UTC datetimes only for human-readable diagnostics.
- Own, name, cancel, and await every background task.
- Never authorize a write from data belonging to a previous GATT session.
- Treat unknown bits and enum values as information to preserve.
- Avoid persistent writes for telemetry and counters.
