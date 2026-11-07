#!/usr/bin/env python3
"""Prepare a safe draft promotion PR from devel to main using Git and gh."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.promotion_metadata import validate_revisions

REMOTE = "origin"


class PromotionError(RuntimeError):
    """A preflight or GitHub CLI failure that must stop PR creation."""


def run(command: list[str], *, check: bool = True) -> str:
    """Run a command and return stdout with useful error details."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise PromotionError(f"{' '.join(command)} failed: {message}")
    return result.stdout.strip()


def remote_refs() -> tuple[str, str]:
    """Read current main/devel SHAs directly from origin."""
    output = run(["git", "ls-remote", REMOTE, "refs/heads/main", "refs/heads/devel"])
    refs = {line.split()[1]: line.split()[0] for line in output.splitlines() if line}
    try:
        return refs["refs/heads/main"], refs["refs/heads/devel"]
    except KeyError as error:
        raise PromotionError(
            "Could not read both main and devel refs from origin."
        ) from error


def open_promotion() -> dict[str, object] | None:
    """Return an existing open devel-to-main PR, including drafts."""
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            "main",
            "--head",
            "devel",
            "--json",
            "url,headRefName,baseRefName,draft",
        ]
    )
    try:
        pulls = json.loads(result)
    except json.JSONDecodeError as error:
        raise PromotionError("GitHub CLI returned invalid PR data.") from error
    return pulls[0] if pulls else None


def render_body(template: str, base_sha: str, head_sha: str) -> str:
    """Render a traceable commit list and changed test paths from real Git data."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        raise PromotionError("GITHUB_REPOSITORY must be owner/repository.")
    commit_lines = run(
        ["git", "log", "--reverse", "--format=%H%x09%s", f"{base_sha}..{head_sha}"]
    ).splitlines()
    if not commit_lines:
        raise PromotionError("Branches differ but contain no promotable commits.")
    commits = []
    for line in commit_lines:
        sha, _, subject = line.partition("\t")
        commits.append(
            f"* {subject} ([`{sha[:7]}`](https://github.com/{repo}/commit/{sha}))"
        )
    changed = run(["git", "diff", "--name-only", "-z", base_sha, head_sha]).split("\0")
    tests = [path for path in changed if path.startswith("tests/")]
    test_summary = (
        "\n".join(f"* `{path}`" for path in tests)
        if tests
        else "* No test files changed. This describes changed files, not test results."
    )
    replacements = {
        "<!-- GENERATED_COMMITS -->": "\n".join(commits),
        "<!-- GENERATED_TESTS -->": test_summary,
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise PromotionError(
                f"Promotion template must contain exactly one {marker} marker."
            )
        template = template.replace(marker, value)
    return template


def prepare_promotion(template: str) -> str | None:
    """Preflight current refs and create or reuse a draft promotion PR."""
    existing = open_promotion()
    if existing:
        return str(existing["url"])

    run(["git", "fetch", "--no-tags", REMOTE, "main", "devel"])
    base_sha, head_sha = remote_refs()
    run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"])
    run(["git", "cat-file", "-e", f"{head_sha}^{{commit}}"])
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        if base_sha == head_sha:
            return None
        raise PromotionError(
            "main and devel have diverged; synchronize them through a reviewed PR before retrying."
        )
    if base_sha == head_sha:
        return None
    if run(["git", "rev-parse", f"{base_sha}^{{tree}}"]) == run(
        ["git", "rev-parse", f"{head_sha}^{{tree}}"]
    ):
        return None

    try:
        validate_revisions(base_sha, head_sha, "main", "devel")
    except ValueError as error:
        raise PromotionError(str(error)) from error
    body = render_body(template, base_sha, head_sha)
    if remote_refs() != (base_sha, head_sha):
        raise PromotionError(
            "main or devel moved during preflight; rerun the workflow."
        )
    existing = open_promotion()
    if existing:
        return str(existing["url"])
    try:
        url = run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                "main",
                "--head",
                "devel",
                "--title",
                "chore: promote devel to main",
                "--body",
                body,
            ]
        )
    except PromotionError:
        existing = open_promotion()
        if existing:
            return str(existing["url"])
        raise
    pull = json.loads(
        run(["gh", "pr", "view", url, "--json", "url,isDraft,headRefOid,baseRefOid"])
    )
    if (pull.get("headRefOid"), pull.get("baseRefOid")) != (head_sha, base_sha):
        raise PromotionError(
            f"Draft {url} was created against moved refs. Keep it draft, close it, and rerun."
        )
    return url


def main() -> int:
    template_path = Path(
        os.environ.get(
            "PROMOTION_TEMPLATE", ".github/PULL_REQUEST_TEMPLATE/promotion.md"
        )
    )
    try:
        url = prepare_promotion(template_path.read_text(encoding="utf-8"))
    except (OSError, PromotionError, ValueError) as error:
        print(f"Promotion preflight failed: {error}", file=sys.stderr)
        return 1
    if url:
        print(f"Promotion PR: {url}")
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8") as stream:
                stream.write(f"pull-request-url={url}\n")
    else:
        print("No changes to promote: main and devel are identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
