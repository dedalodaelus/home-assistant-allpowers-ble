#!/usr/bin/env python3
"""Build a deterministic HACS zip with integration files at archive root."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = ROOT / "custom_components" / "allpowers_ble"
DEFAULT_OUTPUT = ROOT / "dist" / "allpowers_ble.zip"
EXCLUDED_PARTS = {"__pycache__", ".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
DETERMINISTIC_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def iter_release_files() -> list[Path]:
    """Return sorted integration files that are safe to ship."""
    files: list[Path] = []
    for path in INTEGRATION_DIR.rglob("*"):
        relative = path.relative_to(INTEGRATION_DIR)
        if not path.is_file():
            continue
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.as_posix())


def build_release(output: Path, *, clean: bool) -> tuple[Path, str]:
    """Create the HACS archive and return its path and SHA-256 digest."""
    if not INTEGRATION_DIR.is_dir():
        raise FileNotFoundError(f"Integration directory not found: {INTEGRATION_DIR}")
    if clean and output.parent.exists():
        shutil.rmtree(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)

    files = iter_release_files()
    required = {"__init__.py", "manifest.json", "strings.json"}
    names = {path.relative_to(INTEGRATION_DIR).as_posix() for path in files}
    missing = required - names
    if missing:
        raise RuntimeError(f"Release is missing required files: {sorted(missing)}")

    with ZipFile(
        output,
        mode="w",
        compression=ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for source in files:
            arcname = source.relative_to(INTEGRATION_DIR).as_posix()
            info = ZipInfo(arcname, DETERMINISTIC_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, source.read_bytes(), compresslevel=9)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return output, digest


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"archive path (default: {DEFAULT_OUTPUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove the output directory before building",
    )
    return parser.parse_args()


def main() -> int:
    """Build the archive and print machine-readable output details."""
    args = parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    archive, digest = build_release(output, clean=args.clean)
    print(f"archive={archive}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
