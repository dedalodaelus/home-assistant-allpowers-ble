#!/usr/bin/env python3
"""Check that a release tag matches the custom integration version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "custom_components" / "allpowers_ble" / "manifest.json"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+].+)?$")


def main() -> int:
    """Compare the supplied tag with ``manifest.json``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, for example 0.1.0")
    args = parser.parse_args()

    manifest_version = str(json.loads(MANIFEST.read_text())["version"])
    if args.tag.startswith("v"):
        raise SystemExit(f"release tags must not start with 'v': received {args.tag!r}")
    tag_version = args.tag
    if SEMVER.fullmatch(manifest_version) is None:
        raise SystemExit(f"manifest version is not SemVer: {manifest_version}")
    if tag_version != manifest_version:
        raise SystemExit(
            f"tag version {tag_version!r} does not match manifest "
            f"version {manifest_version!r}"
        )
    print(f"version={manifest_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
