#!/usr/bin/env python3
"""Validate repository, HACS, translation, brand, and release invariants."""

from __future__ import annotations

import json
from pathlib import Path
import re
import struct
import sys
from typing import Any
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "allpowers_ble"
INTEGRATION_DIR = ROOT / "custom_components" / DOMAIN
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+].+)?$")
REQUIRED_REPOSITORY_FILES = {
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/hacs.yml",
    ".github/workflows/hassfest.yml",
    ".github/workflows/release.yml",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "hacs.json",
    "pyproject.toml",
}
REQUIRED_INTEGRATION_FILES = {
    "__init__.py",
    "brand/icon.png",
    "brand/icon@2x.png",
    "config_flow.py",
    "diagnostics.py",
    "manifest.json",
    "strings.json",
    "translations/en.json",
    "translations/es.json",
}
IGNORED_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist"}


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object or fail with a useful file name."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.relative_to(ROOT)}")
    return value


def leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return all JSON leaf paths for translation-key comparisons."""
    if isinstance(value, dict):
        paths: set[tuple[str, ...]] = set()
        for key, child in value.items():
            paths.update(leaf_paths(child, (*prefix, str(key))))
        return paths
    return {prefix}


def png_size(path: Path) -> tuple[int, int]:
    """Read PNG dimensions without an imaging dependency."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"Invalid PNG: {path.relative_to(ROOT)}")
    return struct.unpack(">II", data[16:24])


def validate_required_files(errors: list[str]) -> None:
    """Check repository and integration file layout."""
    for relative in sorted(REQUIRED_REPOSITORY_FILES):
        if not (ROOT / relative).is_file():
            errors.append(f"missing repository file: {relative}")
    for relative in sorted(REQUIRED_INTEGRATION_FILES):
        if not (INTEGRATION_DIR / relative).is_file():
            errors.append(f"missing integration file: {relative}")

    component_dirs = sorted(
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )
    if component_dirs != [DOMAIN]:
        errors.append(
            "custom_components must contain exactly one integration directory; "
            f"found {component_dirs}"
        )


def validate_json_contracts(errors: list[str]) -> None:
    """Validate manifest, HACS metadata, and translation parity."""
    try:
        manifest = load_json(INTEGRATION_DIR / "manifest.json")
        hacs = load_json(ROOT / "hacs.json")
        strings = load_json(INTEGRATION_DIR / "strings.json")
        english = load_json(INTEGRATION_DIR / "translations" / "en.json")
        spanish = load_json(INTEGRATION_DIR / "translations" / "es.json")
    except (OSError, ValueError, json.JSONDecodeError) as ex:
        errors.append(f"JSON validation failed: {ex}")
        return

    expected_manifest = {
        "domain": DOMAIN,
        "config_flow": True,
        "integration_type": "device",
        "iot_class": "local_polling",
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            errors.append(
                f"manifest {key!r} must be {expected!r}; got {manifest.get(key)!r}"
            )
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        errors.append(f"manifest version is not SemVer: {version!r}")
    if manifest.get("requirements") != []:
        errors.append(
            "manifest requirements must stay empty; Core provides BLE libraries"
        )
    if hacs.get("zip_release") is not True:
        errors.append("hacs.json must enable zip_release")
    if hacs.get("filename") != "allpowers_ble.zip":
        errors.append("hacs.json filename must be allpowers_ble.zip")

    reference_paths = leaf_paths(strings)
    for label, translation in (("en", english), ("es", spanish)):
        paths = leaf_paths(translation)
        missing = sorted(reference_paths - paths)
        extra = sorted(paths - reference_paths)
        if missing:
            errors.append(f"{label} translation missing keys: {missing}")
        if extra:
            errors.append(f"{label} translation has extra keys: {extra}")


def validate_brand(errors: list[str]) -> None:
    """Check local Home Assistant brand asset dimensions."""
    expected = {
        INTEGRATION_DIR / "brand" / "icon.png": (256, 256),
        INTEGRATION_DIR / "brand" / "icon@2x.png": (512, 512),
    }
    for path, size in expected.items():
        if not path.is_file():
            continue
        try:
            actual = png_size(path)
        except (OSError, ValueError) as ex:
            errors.append(str(ex))
            continue
        if actual != size:
            errors.append(
                f"{path.relative_to(ROOT)} must be {size[0]}x{size[1]}; got {actual}"
            )


def validate_text_files(errors: list[str]) -> None:
    """Reject tabs, trailing whitespace, and non-LF line endings in text files."""
    text_suffixes = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
    text_names = {
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".yamllint",
        "Makefile",
    }
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix not in text_suffixes and path.name not in text_names:
            continue
        data = path.read_bytes()
        relative = path.relative_to(ROOT)
        if b"\r\n" in data or b"\r" in data:
            errors.append(f"non-LF line ending: {relative}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"non-UTF-8 text file: {relative}")
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if line.endswith((" ", "\t")):
                errors.append(f"trailing whitespace: {relative}:{number}")
            if "\t" in line and path.name != "Makefile":
                errors.append(f"tab character: {relative}:{number}")


def validate_clean_tree(errors: list[str]) -> None:
    """Reject generated caches from the distributable source tree."""
    for path in ROOT.rglob("*"):
        if ".git" in path.parts or "dist" in path.parts:
            continue
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}:
            errors.append(f"generated Python cache present: {path.relative_to(ROOT)}")


def validate_release_if_present(errors: list[str]) -> None:
    """Verify the HACS release archive layout when it has been built."""
    archive_path = ROOT / "dist" / "allpowers_ble.zip"
    if not archive_path.is_file():
        return
    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    for required in ("__init__.py", "manifest.json", "strings.json", "brand/icon.png"):
        if required not in names:
            errors.append(f"release archive missing {required}")
    if any(name.startswith("custom_components/") for name in names):
        errors.append("release files must be at archive root for HACS zip_release")
    if any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names):
        errors.append("release archive contains Python cache files")


def main() -> int:
    """Run all validation checks."""
    errors: list[str] = []
    validate_required_files(errors)
    validate_json_contracts(errors)
    validate_brand(errors)
    validate_text_files(errors)
    validate_clean_tree(errors)
    validate_release_if_present(errors)

    for source in sorted(ROOT.rglob("*.py")):
        if any(part in IGNORED_DIRS for part in source.parts):
            continue
        try:
            compile(
                source.read_text(encoding="utf-8"),
                str(source.relative_to(ROOT)),
                "exec",
                dont_inherit=True,
            )
        except (OSError, SyntaxError, UnicodeDecodeError) as ex:
            errors.append(f"Python syntax validation failed for {source}: {ex}")

    if errors:
        print("Repository validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Repository validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
