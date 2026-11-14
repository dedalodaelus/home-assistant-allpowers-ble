#!/usr/bin/env python3
"""Synchronize test requirements with the installed Home Assistant manifests."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import sys
from typing import Sequence

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = ROOT / "requirements_test.txt"
COMPONENTS = ("bluetooth", "esphome", "usb")
START_MARKER = "# BEGIN HA TEST STACK (managed by scripts/sync_ha_test_deps.py)"
END_MARKER = "# END HA TEST STACK"
STACK_HEADER = (
    "# These pins come from the selected HA component manifests, plus the explicitly "
    "reviewed pytest fixture."
)
HA_DISTRIBUTION = "homeassistant"
FIXTURE_DISTRIBUTION = "pytest-homeassistant-custom-component"


class SyncError(ValueError):
    """Raised when the selected HA environment and test requirements disagree."""


def _validated_version(value: str, *, label: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion as err:
        raise SyncError(f"invalid {label} version {value!r}") from err


def _requirement_map(requirements: Sequence[str], *, source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as err:
            raise SyncError(f"invalid requirement in {source}: {raw!r}") from err
        name = canonicalize_name(requirement.name)
        normalized = str(requirement)
        previous = result.get(name)
        if previous is not None and previous != normalized:
            raise SyncError(
                f"conflicting requirements for {requirement.name} in {source}: "
                f"{previous!r} and {normalized!r}"
            )
        result[name] = normalized
    return result


def read_manifest_requirements(components_root: Path) -> dict[str, str]:
    """Read direct requirements from the selected HA component manifests."""
    requirements: dict[str, str] = {}
    for component in COMPONENTS:
        manifest_path = components_root / component / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise SyncError(f"cannot read HA {component} manifest: {err}") from err
        if not isinstance(manifest, dict):
            raise SyncError(f"HA {component} manifest root must be an object")
        if manifest.get("domain") != component:
            raise SyncError(
                f"HA {component} manifest has unexpected domain "
                f"{manifest.get('domain')!r}"
            )
        raw_requirements = manifest.get("requirements", [])
        if not isinstance(raw_requirements, list) or not all(
            isinstance(item, str) for item in raw_requirements
        ):
            raise SyncError(f"HA {component} manifest requirements must be strings")
        component_requirements = _requirement_map(
            raw_requirements, source=f"HA {component} manifest"
        )
        for name, requirement in component_requirements.items():
            previous = requirements.get(name)
            if previous is not None and previous != requirement:
                raise SyncError(
                    f"HA component manifests disagree on {name}: "
                    f"{previous!r} and {requirement!r}"
                )
            requirements[name] = requirement
    return requirements


def expected_stack(
    components_root: Path, *, ha_version: str, fixture_version: str
) -> dict[str, str]:
    """Return the complete generated requirement set for one HA test stack."""
    ha_version = _validated_version(ha_version, label="Home Assistant")
    fixture_version = _validated_version(fixture_version, label="pytest fixture")
    result = read_manifest_requirements(components_root)
    for distribution, version in (
        (HA_DISTRIBUTION, ha_version),
        (FIXTURE_DISTRIBUTION, fixture_version),
    ):
        name = canonicalize_name(distribution)
        if name in result:
            raise SyncError(
                f"HA component manifests unexpectedly manage {distribution}"
            )
        result[name] = f"{distribution}=={version}"
    return dict(sorted(result.items()))


def _block_bounds(lines: list[str]) -> tuple[int, int]:
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == START_MARKER]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END_MARKER]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise SyncError(
            "requirements_test.txt must contain exactly one ordered HA test stack block"
        )
    return starts[0], ends[0]


def render_stack_block(expected: dict[str, str]) -> str:
    """Render the deterministic, script-managed requirements block."""
    lines = [START_MARKER, STACK_HEADER]
    lines.extend(requirement for _, requirement in sorted(expected.items()))
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def synchronize_text(text: str, expected: dict[str, str]) -> str:
    """Replace the managed stack block, preserving the rest of the file."""
    lines = text.splitlines(keepends=True)
    start, end = _block_bounds(lines)
    prefix = "".join(lines[:start])
    suffix = "".join(lines[end + 1 :])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if suffix and not suffix.startswith("\n"):
        suffix = "\n" + suffix
    return prefix + render_stack_block(expected) + suffix


def validate_requirements_text(text: str, expected: dict[str, str]) -> list[str]:
    """Report stale, missing, duplicate, or out-of-block stack requirements."""
    lines = text.splitlines()
    try:
        start, end = _block_bounds(lines)
    except SyncError as err:
        return [str(err)]

    errors: list[str] = []
    actual: dict[str, str] = {}
    for line_number, raw in enumerate(lines[start + 1 : end], start=start + 2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            requirement = Requirement(stripped)
        except InvalidRequirement:
            errors.append(f"invalid managed requirement on line {line_number}: {raw!r}")
            continue
        name = canonicalize_name(requirement.name)
        if name in actual:
            errors.append(f"duplicate managed requirement for {requirement.name}")
        actual[name] = str(requirement)

    for requirement_name in sorted(set(expected) | set(actual)):
        expected_requirement = expected.get(requirement_name)
        actual_requirement = actual.get(requirement_name)
        if expected_requirement != actual_requirement:
            errors.append(
                f"{requirement_name}: expected {expected_requirement or 'no requirement'}, "
                f"found {actual_requirement or 'no requirement'}"
            )

    managed_names = set(expected)
    for raw in (*lines[:start], *lines[end + 1 :]):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        try:
            requirement = Requirement(stripped)
        except InvalidRequirement:
            continue
        name = canonicalize_name(requirement.name)
        if name in managed_names:
            errors.append(
                f"{requirement.name} is also specified outside the managed block"
            )
    return errors


def installed_version(distribution: str) -> str:
    """Get an installed distribution version with a useful failure message."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError as err:
        raise SyncError(
            f"{distribution} is not installed in this Python environment"
        ) from err


def selected_versions_from_requirements(text: str) -> tuple[str, str]:
    """Read the exact HA and fixture pins selected in the managed block."""
    lines = text.splitlines()
    start, end = _block_bounds(lines)
    requirements = _requirement_map(
        [
            line.strip()
            for line in lines[start + 1 : end]
            if line.strip() and not line.strip().startswith("#")
        ],
        source="managed requirements block",
    )
    versions: list[str] = []
    for distribution, label in (
        (HA_DISTRIBUTION, "Home Assistant"),
        (FIXTURE_DISTRIBUTION, "pytest fixture"),
    ):
        raw = requirements.get(canonicalize_name(distribution))
        if raw is None:
            raise SyncError(f"managed requirements block is missing {distribution}")
        requirement = Requirement(raw)
        specifiers = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise SyncError(f"managed {label} requirement must use one exact == pin")
        versions.append(_validated_version(specifiers[0].version, label=label))
    return versions[0], versions[1]


def components_root_for_ha() -> Path:
    """Locate manifests from the installed Home Assistant distribution."""
    try:
        distribution = metadata.distribution(HA_DISTRIBUTION)
    except metadata.PackageNotFoundError as err:
        raise SyncError(
            "Home Assistant is not installed in this Python environment"
        ) from err
    return Path(str(distribution.locate_file("homeassistant/components")))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--sync",
        action="store_true",
        help="write pins from the explicitly selected, installed HA release",
    )
    mode.add_argument(
        "--check", action="store_true", help="check pins without modifying files"
    )
    parser.add_argument(
        "--ha-version",
        help="explicit HA release selected for --sync",
    )
    parser.add_argument(
        "--fixture-version",
        help="explicit compatible pytest fixture selection for --sync",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=DEFAULT_REQUIREMENTS,
        help="requirements file to update or check (default: requirements_test.txt)",
    )
    args = parser.parse_args(argv)
    if args.sync and (args.ha_version is None or args.fixture_version is None):
        parser.error("--sync requires both --ha-version and --fixture-version")
    if args.check and (args.ha_version is not None or args.fixture_version is not None):
        parser.error("--check reads HA and fixture versions from the requirements file")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.sync:
            installed_ha = installed_version(HA_DISTRIBUTION)
            installed_fixture = installed_version(FIXTURE_DISTRIBUTION)
            selected_ha = _validated_version(args.ha_version, label="Home Assistant")
            selected_fixture = _validated_version(
                args.fixture_version, label="pytest fixture"
            )
            if installed_ha != selected_ha:
                raise SyncError(
                    f"selected HA {selected_ha} is not installed (found {installed_ha}); "
                    "install the selected release before synchronizing"
                )
            if installed_fixture != selected_fixture:
                raise SyncError(
                    f"selected pytest fixture {selected_fixture} is not installed "
                    f"(found {installed_fixture}); resolve the selected fixture first"
                )
        else:
            text = args.requirements.read_text(encoding="utf-8")
            selected_ha, selected_fixture = selected_versions_from_requirements(text)
            installed_ha = installed_version(HA_DISTRIBUTION)
            installed_fixture = installed_version(FIXTURE_DISTRIBUTION)
            if installed_ha != selected_ha or installed_fixture != selected_fixture:
                raise SyncError(
                    "installed Home Assistant/fixture versions do not match "
                    f"{args.requirements} (HA {selected_ha}, fixture {selected_fixture}; "
                    f"installed HA {installed_ha}, fixture {installed_fixture})"
                )
        expected = expected_stack(
            components_root_for_ha(),
            ha_version=selected_ha,
            fixture_version=selected_fixture,
        )
        if args.sync:
            current = args.requirements.read_text(encoding="utf-8")
            updated = synchronize_text(current, expected)
            errors = validate_requirements_text(updated, expected)
            if errors:
                raise SyncError("; ".join(errors))
            if updated != current:
                args.requirements.write_text(updated, encoding="utf-8")
                print(
                    f"Synchronized HA {selected_ha} test dependencies in {args.requirements}"
                )
            else:
                print(f"HA {selected_ha} test dependencies are already synchronized")
            return 0

        errors = validate_requirements_text(text, expected)
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        print(
            f"HA {selected_ha}, fixture {selected_fixture}, and component manifest pins are synchronized"
        )
    except (OSError, SyncError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
