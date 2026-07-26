# Contributing

Contributions are welcome when they keep protocol safety and compatibility
explicit.

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
```

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
- Direct commits into `devel` and `main` are not permitted.
- Urgent production fixes may target `main` only from a `hotfix/*` branch cut
  from `main`; the same fix must be propagated back to `devel` in a follow-up PR.
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
