#!/usr/bin/env python3
"""Migrate a v1 HIL qualification matrix to the conservative v2 format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scripts.hil.qualification import migrate_v1_to_v2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="v1 matrix JSON")
    parser.add_argument("output", type=Path, help="new v2 matrix JSON path")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("input and output must be different paths")
    try:
        source = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise ValueError("matrix root must be an object")
        migrated = migrate_v1_to_v2(source)
        rendered = json.dumps(migrated, indent=2, ensure_ascii=False) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"HIL matrix migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"Migrated v1 matrix to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
