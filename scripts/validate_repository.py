#!/usr/bin/env python3
"""Validate repository, HACS, translation, brand, and release invariants."""

from __future__ import annotations

import json
from pathlib import Path
import re
import struct
import sys
import tomllib
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
    "scripts/validate_release_metadata.py",
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
README_REQUIRED_HEADINGS = {
    "README.md": {
        "## Safety model",
        "## Conservative automation examples",
        "## Removal and recovery",
        "## BLE trust boundary",
        "## Project status",
    },
    "README.es.md": {
        "## Seguridad de las escrituras",
        "## Ejemplos de automatización conservadora",
        "## Retirada y recuperación",
        "## Límite de confianza BLE",
        "## Estado del proyecto",
    },
}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
QUALITY_CONTRACT_HEADING = "## 1.0 readiness contract"
QUALITY_CADENCE_HEADING = "## Stable release review cadence"
QUALITY_TABLE_COLUMNS = (
    "Criterion",
    "How it is measured",
    "Evidence",
    "Status",
    "Blocking issues",
)
QUALITY_ALLOWED_STATUSES = {"Done", "Blocked", "N/A"}
QUALITY_ISSUE_REFERENCE = re.compile(
    r"(?:#\d+|https://github\.com/[^/\s]+/[^/\s]+/issues/\d+)"
)
DEPENDABOT_TARGET_BRANCH = re.compile(r"^\s*target-branch:\s*(\S+)\s*$", re.MULTILINE)
RELEASE_MAIN_PUSH_BRANCH = re.compile(
    r"^\s*branches:\s*\n\s*-\s*main\s*$", re.MULTILINE
)
RELEASE_MAIN_IF_GUARD = re.compile(
    r"^\s*if:\s*github\.ref\s*==\s*['\"]refs/heads/main['\"]\s*$",
    re.MULTILINE,
)
RELEASE_TAG_TRIGGER = re.compile(r"^\s*tags:\s*$", re.MULTILINE)
RELEASE_TAGGED_JOB = re.compile(r"^\s*tagged-release:\s*$", re.MULTILINE)
RELEASE_METADATA_VALIDATION_STEP = re.compile(r"scripts/validate_release_metadata\.py")
RELEASE_CHECKSUM_ASSET = re.compile(r"dist/allpowers_ble\.zip\.sha256")
BLOCKED_RUFF_IGNORE_RULES = {"ALL"}
BLOCKED_REPO_CODE_GLOBS = {
    "*",
    "custom_components",
    "custom_components.*",
    "scripts",
    "scripts.*",
    "tests",
    "tests.*",
}


def normalize_markdown_anchor(value: str) -> str:
    """Approximate GitHub markdown anchor generation for local checks."""
    normalized = value.strip().lower()
    normalized = re.sub(r"[\\`*_~\[\]().,!?:;\"']", "", normalized)
    normalized = re.sub(r"\s+", "-", normalized)
    return normalized


def heading_set(markdown: str) -> set[str]:
    """Collect normalized heading anchors from markdown content."""
    anchors: set[str] = set()
    for line in markdown.splitlines():
        match = MARKDOWN_HEADING.match(line)
        if match is None:
            continue
        anchors.add(normalize_markdown_anchor(match.group(2)))
    return anchors


def split_link_target(target: str) -> tuple[str, str]:
    """Split markdown link target into path and optional fragment."""
    if "#" not in target:
        return target, ""
    link_path, fragment = target.split("#", 1)
    return link_path, fragment


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


def validate_markdown_links_and_headings(errors: list[str]) -> None:
    """Validate required README headings and local markdown links/fragments."""
    for relative, required_headings in README_REQUIRED_HEADINGS.items():
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"missing required README file: {relative}")
            continue
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError as ex:
            errors.append(f"failed reading markdown file {relative}: {ex}")
            continue

        lines = set(markdown.splitlines())
        for heading in sorted(required_headings):
            if heading not in lines:
                errors.append(f"{relative} missing required heading: {heading}")

    checked_files: dict[Path, set[str]] = {}
    markdown_files = [ROOT / "README.md", ROOT / "README.es.md"]

    for source in markdown_files:
        if not source.is_file():
            continue
        try:
            source_text = source.read_text(encoding="utf-8")
        except OSError as ex:
            errors.append(
                f"failed reading markdown file {source.relative_to(ROOT)}: {ex}"
            )
            continue

        source_anchors = heading_set(source_text)
        for match in MARKDOWN_LINK.finditer(source_text):
            target = match.group(1).strip()
            if (
                not target
                or target.startswith(("http://", "https://", "mailto:"))
                or target.startswith("<")
            ):
                continue

            link_path, fragment = split_link_target(target)
            if link_path.startswith("#"):
                fragment = link_path[1:] + (f"-{fragment}" if fragment else "")
                link_path = ""

            resolved = source if not link_path else (ROOT / link_path).resolve()
            if not resolved.is_file():
                errors.append(
                    f"broken local markdown link in {source.relative_to(ROOT)}: {target}"
                )
                continue

            if fragment:
                if resolved not in checked_files:
                    try:
                        checked_files[resolved] = heading_set(
                            resolved.read_text(encoding="utf-8")
                        )
                    except OSError as ex:
                        errors.append(
                            f"failed reading markdown target {resolved.relative_to(ROOT)}: {ex}"
                        )
                        continue
                normalized_fragment = normalize_markdown_anchor(fragment)
                anchors = (
                    source_anchors if resolved == source else checked_files[resolved]
                )
                if normalized_fragment not in anchors:
                    errors.append(
                        "broken markdown fragment in "
                        f"{source.relative_to(ROOT)}: {target}"
                    )


def validate_quality_readiness_contract(errors: list[str]) -> None:
    """Validate measurable quality readiness criteria and blocker references."""
    path = ROOT / "docs" / "quality.md"
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as ex:
        errors.append(f"failed reading docs/quality.md: {ex}")
        return

    if QUALITY_CONTRACT_HEADING not in markdown:
        errors.append(
            "docs/quality.md must define a '## 1.0 readiness contract' section"
        )
        return
    if QUALITY_CADENCE_HEADING not in markdown:
        errors.append(
            "docs/quality.md must define a '## Stable release review cadence' section"
        )

    lines = markdown.splitlines()
    heading_index = lines.index(QUALITY_CONTRACT_HEADING)
    table_lines: list[str] = []
    for line in lines[heading_index + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            table_lines.append(line)

    if len(table_lines) < 3:
        errors.append("docs/quality.md readiness contract must include a table")
        return

    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    if tuple(header) != QUALITY_TABLE_COLUMNS:
        errors.append(
            "docs/quality.md readiness table columns must be exactly: "
            + ", ".join(QUALITY_TABLE_COLUMNS)
        )
        return

    for row_number, raw_row in enumerate(table_lines[2:], start=1):
        cells = [cell.strip() for cell in raw_row.strip("|").split("|")]
        if len(cells) != len(QUALITY_TABLE_COLUMNS):
            errors.append(
                "docs/quality.md readiness row has wrong column count: "
                f"row {row_number}"
            )
            continue

        row = dict(zip(QUALITY_TABLE_COLUMNS, cells, strict=True))
        criterion = row["Criterion"]
        measure = row["How it is measured"]
        evidence = row["Evidence"]
        status = row["Status"]
        blocking = row["Blocking issues"]

        if not criterion.startswith("IQS-"):
            errors.append(
                "docs/quality.md readiness rows must use IQS-* criterion ids: "
                f"{criterion!r}"
            )
        if not measure or measure == "-":
            errors.append(
                "docs/quality.md readiness rows must define a measurable check: "
                f"{criterion!r}"
            )
        if "](" not in evidence:
            errors.append(
                "docs/quality.md readiness rows must include evidence links: "
                f"{criterion!r}"
            )
        if status not in QUALITY_ALLOWED_STATUSES:
            errors.append(
                "docs/quality.md readiness status must be one of "
                f"{sorted(QUALITY_ALLOWED_STATUSES)}; got {status!r} in {criterion!r}"
            )
        if status == "Blocked":
            if (
                QUALITY_ISSUE_REFERENCE.search(blocking) is None
                and "](" not in blocking
            ):
                errors.append(
                    "blocked readiness rows must reference an issue or evidence: "
                    f"{criterion!r}"
                )
        elif blocking not in {"", "-", "None", "Not applicable by scope"}:
            if (
                QUALITY_ISSUE_REFERENCE.search(blocking) is None
                and "](" not in blocking
            ):
                errors.append(
                    "non-blocked readiness rows may only use issue/evidence references "
                    "or explicit empty values: "
                    f"{criterion!r}"
                )


def validate_branch_workflow_contract(errors: list[str]) -> None:
    """Enforce repository branch workflow invariants used by CI/release."""
    try:
        dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    except OSError as ex:
        errors.append(f"failed reading .github/dependabot.yml: {ex}")
        dependabot = ""

    if dependabot:
        targets = DEPENDABOT_TARGET_BRANCH.findall(dependabot)
        if not targets:
            errors.append("dependabot.yml must define at least one target-branch")
        for index, target in enumerate(targets, start=1):
            if target != "devel":
                errors.append(
                    "dependabot target-branch must be devel "
                    f"(entry {index} is {target!r})"
                )

    try:
        release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
    except OSError as ex:
        errors.append(f"failed reading .github/workflows/release.yml: {ex}")
        release_workflow = ""

    if release_workflow:
        if RELEASE_MAIN_PUSH_BRANCH.search(release_workflow) is None:
            errors.append("release workflow must trigger push branch on main")
        if RELEASE_MAIN_IF_GUARD.search(release_workflow) is None:
            errors.append("release-please job must remain guarded to refs/heads/main")
        if RELEASE_TAG_TRIGGER.search(release_workflow) is not None:
            errors.append("release workflow must not publish from pushed tags")
        if RELEASE_TAGGED_JOB.search(release_workflow) is not None:
            errors.append("release workflow must not define a tagged-release job")
        if RELEASE_METADATA_VALIDATION_STEP.search(release_workflow) is None:
            errors.append(
                "release workflow must validate release metadata before upload"
            )
        if RELEASE_CHECKSUM_ASSET.search(release_workflow) is None:
            errors.append(
                "release workflow must upload checksum asset for allpowers_ble.zip"
            )


def _module_list(value: Any) -> list[str]:
    """Normalize mypy module override values to a string list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def validate_static_analysis_contract(errors: list[str]) -> None:
    """Reject broad static-analysis ignores that hide integration-owned code."""
    pyproject_path = ROOT / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as ex:
        errors.append(f"failed reading pyproject.toml for static-analysis checks: {ex}")
        return

    tool_config = pyproject.get("tool")
    if not isinstance(tool_config, dict):
        errors.append("pyproject.toml must define a [tool] table")
        return

    mypy_config = tool_config.get("mypy")
    if not isinstance(mypy_config, dict):
        errors.append("pyproject.toml must define a [tool.mypy] table")
        return

    if mypy_config.get("ignore_missing_imports") is True:
        errors.append(
            "tool.mypy.ignore_missing_imports must stay false; "
            "use explicit per-module overrides"
        )
    if mypy_config.get("follow_imports") == "skip":
        errors.append(
            "tool.mypy.follow_imports must not be 'skip'; "
            "integration-owned imports must stay type-checked"
        )

    overrides = mypy_config.get("overrides", [])
    if isinstance(overrides, dict):
        overrides = [overrides]
    if not isinstance(overrides, list):
        errors.append("tool.mypy.overrides must be a table or array of tables")
        overrides = []

    for index, override in enumerate(overrides, start=1):
        if not isinstance(override, dict):
            errors.append(f"tool.mypy.overrides entry {index} must be a table")
            continue
        if override.get("ignore_missing_imports") is not True:
            continue
        for module in _module_list(override.get("module")):
            if module in BLOCKED_REPO_CODE_GLOBS:
                errors.append(
                    "tool.mypy.overrides uses ignore_missing_imports on "
                    f"integration-owned scope {module!r} (entry {index})"
                )

    ruff_config = tool_config.get("ruff")
    if not isinstance(ruff_config, dict):
        return
    lint_config = ruff_config.get("lint")
    if not isinstance(lint_config, dict):
        return

    ignored_rules = lint_config.get("ignore", [])
    if isinstance(ignored_rules, str):
        ignored_rules = [ignored_rules]
    if isinstance(ignored_rules, list):
        forbidden = sorted(
            rule
            for rule in ignored_rules
            if isinstance(rule, str) and rule in BLOCKED_RUFF_IGNORE_RULES
        )
        if forbidden:
            errors.append(
                f"tool.ruff.lint.ignore contains forbidden broad ignores: {forbidden}"
            )

    per_file_ignores = lint_config.get("per-file-ignores", {})
    if not isinstance(per_file_ignores, dict):
        errors.append("tool.ruff.lint.per-file-ignores must be a table")
        return
    for pattern in per_file_ignores:
        if not isinstance(pattern, str):
            continue
        if pattern in {"*", "custom_components/*", "custom_components/**"}:
            errors.append(
                "tool.ruff.lint.per-file-ignores must not disable checks "
                f"for integration code via pattern {pattern!r}"
            )


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
    validate_quality_readiness_contract(errors)
    validate_markdown_links_and_headings(errors)
    validate_branch_workflow_contract(errors)
    validate_static_analysis_contract(errors)
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
