#!/usr/bin/env python3
"""Validate release metadata invariants and optional ZIP checksum."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "custom_components" / "allpowers_ble" / "manifest.json"
RELEASE_PLEASE_MANIFEST_PATH = ROOT / ".release-please-manifest.json"
RELEASE_PLEASE_CONFIG_PATH = ROOT / "release-please-config.json"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
DEFAULT_ZIP_PATH = ROOT / "dist" / "allpowers_ble.zip"
DEFAULT_CHECKSUM_PATH = ROOT / "dist" / "allpowers_ble.zip.sha256"
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$"
)
VERSION_HEADING = re.compile(
    r"^## \[(?P<version>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?)\]"
    r"\((?P<link>https://github\.com/[^/]+/[^/]+/compare/(?P<from>.+?)\.\.\.(?P<to>[^)]+))\) "
    r"\((?P<date>\d{4}-\d{2}-\d{2})\)$"
)
UNRELEASED_LINK = re.compile(
    r"^\[Unreleased\]:\s+https://github\.com/[^/]+/[^/]+/compare/(?P<from>.+?)\.\.\.HEAD$"
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.relative_to(ROOT)}")
    return value


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag to validate (for example 0.2.0)")
    parser.add_argument(
        "--zip-path",
        type=Path,
        default=DEFAULT_ZIP_PATH,
        help=f"release ZIP path (default: {DEFAULT_ZIP_PATH.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--checksum-path",
        type=Path,
        default=DEFAULT_CHECKSUM_PATH,
        help=f"checksum file path (default: {DEFAULT_CHECKSUM_PATH.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--write-checksum",
        action="store_true",
        help="write <sha256><space><space><filename> to --checksum-path",
    )
    parser.add_argument(
        "--verify-checksum",
        action="store_true",
        help="verify --checksum-path matches the ZIP digest",
    )
    parser.add_argument(
        "--strict-unreleased-base",
        action="store_true",
        help="require [Unreleased] compare base to match the current manifest version",
    )
    return parser.parse_args()


def validate_manifest_and_tag(tag: str | None, errors: list[str]) -> str:
    manifest = _load_json(MANIFEST_PATH)
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        errors.append(f"manifest version is not SemVer: {version!r}")
        return ""

    if tag is not None:
        if tag.startswith("v"):
            errors.append(f"release tags must not start with 'v': received {tag!r}")
        if tag != version:
            errors.append(
                f"tag version {tag!r} does not match manifest version {version!r}"
            )

    return version


def validate_release_please_manifest(manifest_version: str, errors: list[str]) -> None:
    release_manifest = _load_json(RELEASE_PLEASE_MANIFEST_PATH)
    release_version = release_manifest.get(".")
    if (
        not isinstance(release_version, str)
        or SEMVER.fullmatch(release_version) is None
    ):
        errors.append(
            ".release-please-manifest.json must define a SemVer string for '.'"
        )
        return
    if release_version != manifest_version:
        errors.append(
            "release-please manifest version does not match manifest.json version: "
            f"{release_version!r} != {manifest_version!r}"
        )


def validate_release_please_config(errors: list[str]) -> None:
    config = _load_json(RELEASE_PLEASE_CONFIG_PATH)
    packages = config.get("packages")
    if not isinstance(packages, dict):
        errors.append("release-please-config.json must define a 'packages' object")
        return
    package_root = packages.get(".")
    if not isinstance(package_root, dict):
        errors.append("release-please-config.json must define packages['.']")
        return

    if package_root.get("include-v-in-tag") is not False:
        errors.append("release-please config must set include-v-in-tag to false")

    extra_files = package_root.get("extra-files")
    expected = {
        "jsonpath": "$.version",
        "path": "custom_components/allpowers_ble/manifest.json",
        "type": "json",
    }
    if not isinstance(extra_files, list) or expected not in extra_files:
        errors.append(
            "release-please config must sync custom_components/allpowers_ble/manifest.json "
            "version through extra-files"
        )


def validate_changelog(
    manifest_version: str,
    strict_unreleased_base: bool,
    errors: list[str],
) -> None:
    lines = CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()

    heading_versions: dict[str, tuple[str, str]] = {}
    unreleased_base: str | None = None
    for line in lines:
        heading = VERSION_HEADING.match(line.strip())
        if heading is not None:
            version = heading.group("version")
            compare_from = heading.group("from")
            compare_to = heading.group("to")
            heading_versions[version] = (compare_from, compare_to)
            if compare_to != version:
                errors.append(
                    "changelog compare target does not match heading version: "
                    f"{compare_to!r} != {version!r}"
                )
            if SEMVER.fullmatch(compare_from) is None:
                errors.append(
                    "changelog compare base is not SemVer for heading "
                    f"{version!r}: {compare_from!r}"
                )
            continue

        unreleased = UNRELEASED_LINK.match(line.strip())
        if unreleased is not None:
            unreleased_base = unreleased.group("from")

    if manifest_version not in heading_versions:
        errors.append(
            "CHANGELOG.md must contain a release heading for the current manifest "
            f"version {manifest_version!r}"
        )

    if unreleased_base is None:
        errors.append("CHANGELOG.md must define an [Unreleased] compare link")
    elif SEMVER.fullmatch(unreleased_base) is None:
        errors.append(f"[Unreleased] compare base is not SemVer: {unreleased_base!r}")
    elif strict_unreleased_base and unreleased_base != manifest_version:
        errors.append(
            "[Unreleased] compare base does not match current manifest version: "
            f"{unreleased_base!r} != {manifest_version!r}"
        )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_checksum(path: Path, filename: str, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{digest}  {filename}\n", encoding="utf-8")


def parse_checksum(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    parts = text.split()
    if len(parts) < 2:
        raise ValueError(
            f"checksum file must contain '<sha256>  <filename>': {path.relative_to(ROOT)}"
        )
    return parts[0], parts[-1]


def validate_checksum(
    zip_path: Path,
    checksum_path: Path,
    write: bool,
    verify: bool,
    errors: list[str],
) -> None:
    if not zip_path.is_file():
        errors.append(f"release ZIP not found: {zip_path.relative_to(ROOT)}")
        return

    digest = sha256(zip_path)

    if write:
        write_checksum(checksum_path, zip_path.name, digest)

    if verify:
        if not checksum_path.is_file():
            errors.append(f"checksum file not found: {checksum_path.relative_to(ROOT)}")
            return
        try:
            expected_digest, filename = parse_checksum(checksum_path)
        except (OSError, UnicodeDecodeError, ValueError) as ex:
            errors.append(str(ex))
            return
        if filename != zip_path.name:
            errors.append(
                f"checksum filename mismatch: {filename!r} != {zip_path.name!r}"
            )
        if expected_digest != digest:
            errors.append(
                "checksum digest mismatch for release ZIP: "
                f"{expected_digest!r} != {digest!r}"
            )

    print(f"zip_sha256={digest}")


def main() -> int:
    args = parse_args()
    errors: list[str] = []

    try:
        manifest_version = validate_manifest_and_tag(args.tag, errors)
        if manifest_version:
            validate_release_please_manifest(manifest_version, errors)
            validate_release_please_config(errors)
            validate_changelog(
                manifest_version,
                strict_unreleased_base=args.strict_unreleased_base,
                errors=errors,
            )
        validate_checksum(
            _resolve(args.zip_path),
            _resolve(args.checksum_path),
            write=args.write_checksum,
            verify=args.verify_checksum,
            errors=errors,
        )
    except (OSError, json.JSONDecodeError, ValueError) as ex:
        errors.append(str(ex))

    if errors:
        print("Release metadata validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Release metadata validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
