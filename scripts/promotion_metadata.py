"""Validation helpers shared by promotion preflight and the Merge gate."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
INTEGRATION_MANIFEST = "custom_components/allpowers_ble/manifest.json"
CHANGELOG = "CHANGELOG.md"
RELEASE_MANIFEST = ".release-please-manifest.json"


class MetadataError(ValueError):
    """Release metadata would be unsafe to promote."""


def semver_key(value: str) -> tuple[Any, ...]:
    """Return a SemVer precedence key, rejecting non-SemVer values."""
    match = SEMVER.fullmatch(value)
    if not match:
        raise MetadataError(
            f"Invalid semantic version in integration manifest: {value!r}"
        )
    major, minor, patch, prerelease = match.groups()
    if prerelease is None:
        pre_key: tuple[Any, ...] = (1,)
    else:
        identifiers: list[tuple[int, Any]] = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    raise MetadataError(
                        f"Invalid SemVer prerelease identifier: {value!r}"
                    )
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
        pre_key = (0, tuple(identifiers))
    return int(major), int(minor), int(patch), pre_key


def validate_version_change(base_version: str, head_version: str) -> None:
    """Reject invalid or regressing integration manifest versions."""
    if semver_key(head_version) < semver_key(base_version):
        raise MetadataError(
            f"Integration version regressed from {base_version} to {head_version}."
        )


def validate_release_please_versions(
    base_versions: dict[str, Any], head_versions: dict[str, Any]
) -> None:
    """Prevent a release manifest from dropping package history or lowering a version."""
    missing = sorted(base_versions.keys() - head_versions.keys())
    if missing:
        raise MetadataError(
            "Release Please manifest removed package history for: " + ", ".join(missing)
        )
    for package, base_version in base_versions.items():
        head_version = head_versions[package]
        if not isinstance(base_version, str) or not isinstance(head_version, str):
            raise MetadataError("Release Please manifest versions must be strings.")
        try:
            validate_version_change(base_version, head_version)
        except MetadataError as error:
            raise MetadataError(f"Release Please package {package}: {error}") from error


def validate_release_file_changes(
    changed_paths: set[str], base_ref: str, head_ref: str
) -> None:
    """Enforce Release Please file ownership, including rename source paths."""
    is_main_to_devel_sync = base_ref == "devel" and head_ref == "main"
    is_release_please = base_ref == "main" and head_ref.startswith("release-please--")
    if (
        CHANGELOG in changed_paths
        and not is_main_to_devel_sync
        and not is_release_please
    ):
        raise MetadataError(
            "CHANGELOG.md is managed by Release Please and cannot be changed in this PR."
        )
    if (
        RELEASE_MANIFEST in changed_paths
        and not is_main_to_devel_sync
        and not is_release_please
    ):
        raise MetadataError(
            f"{RELEASE_MANIFEST} is managed by Release Please and cannot be changed in this PR."
        )
    if f"{INTEGRATION_MANIFEST} (removed)" in changed_paths:
        raise MetadataError(f"{INTEGRATION_MANIFEST} was deleted or renamed.")


def _git_text(revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _changed_paths(base_sha: str, head_sha: str) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "-M", "-z", base_sha, head_sha, "--"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    paths: set[str] = set()
    fields = result.stdout.split("\0")
    index = 0
    while index < len(fields) - 1:
        status = fields[index]
        index += 1
        count = 2 if status.startswith(("R", "C")) else 1
        changed = fields[index : index + count]
        if len(changed) != count or any(not path for path in changed):
            raise MetadataError("Unexpected git diff name-status output.")
        index += count
        paths.update(changed)
        if status.startswith(("D", "R")) and INTEGRATION_MANIFEST in changed:
            paths.add(f"{INTEGRATION_MANIFEST} (removed)")
    return paths


def validate_revisions(
    base_sha: str, head_sha: str, base_ref: str, head_ref: str
) -> None:
    """Validate PR metadata at the exact immutable revisions supplied by GitHub."""
    paths = _changed_paths(base_sha, head_sha)
    validate_release_file_changes(paths, base_ref, head_ref)
    try:
        base_manifest = json.loads(_git_text(base_sha, INTEGRATION_MANIFEST))
        head_manifest = json.loads(_git_text(head_sha, INTEGRATION_MANIFEST))
        base_release_versions = json.loads(_git_text(base_sha, RELEASE_MANIFEST))
        head_release_versions = json.loads(_git_text(head_sha, RELEASE_MANIFEST))
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise MetadataError(
            f"Cannot read integration manifest at PR base/head revisions: {error}"
        ) from error
    base_version = base_manifest.get("version")
    head_version = head_manifest.get("version")
    if not isinstance(base_version, str) or not isinstance(head_version, str):
        raise MetadataError(
            "Integration manifest must have a string version at both revisions."
        )
    validate_version_change(base_version, head_version)
    if not isinstance(base_release_versions, dict) or not isinstance(
        head_release_versions, dict
    ):
        raise MetadataError(
            "Release Please manifest must contain package version objects."
        )
    validate_release_please_versions(base_release_versions, head_release_versions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    args = parser.parse_args()
    try:
        validate_revisions(args.base_sha, args.head_sha, args.base_ref, args.head_ref)
    except MetadataError as error:
        print(f"Release metadata validation failed: {error}", file=sys.stderr)
        return 1
    print("Release metadata validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
