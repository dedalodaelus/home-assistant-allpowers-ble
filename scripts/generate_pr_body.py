"""Render structured PR facts using the complete repository PR template."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from scripts.pr_body import render_pr_body_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="UTF-8 JSON facts")
    parser.add_argument(
        "--output", type=Path, required=True, help="Markdown output path"
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="PR template path (defaults to .github/PULL_REQUEST_TEMPLATE.md)",
    )
    args = parser.parse_args(argv)
    try:
        output = render_pr_body_file(
            args.data,
            args.output,
            **({"template_path": args.template} if args.template is not None else {}),
        )
    except (OSError, ValueError) as exc:
        print(f"Unable to generate PR body: {exc}", file=sys.stderr)
        return 2
    print(f"PR body written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
