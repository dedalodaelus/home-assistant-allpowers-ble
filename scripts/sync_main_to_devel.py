#!/usr/bin/env python3
"""Prepare or reuse a reviewed PR propagating main into devel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

REMOTE = "origin"


class SyncError(RuntimeError):
    """A Git or GitHub failure that must stop synchronization preparation."""


@dataclass(frozen=True)
class SyncResult:
    """Outcome and immutable remote refs observed by one workflow run."""

    url: str | None
    main_sha: str
    devel_sha: str


def run(command: list[str], *, check: bool = True) -> str:
    """Run a command and return stdout with useful error details."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise SyncError(f"{' '.join(command)} failed: {message}")
    return result.stdout.strip()


def fetch_refs() -> tuple[str, str]:
    """Fetch current protected branch tips into remote-tracking refs."""
    run(
        [
            "git",
            "fetch",
            "--no-tags",
            REMOTE,
            "+refs/heads/main:refs/remotes/origin/main",
            "+refs/heads/devel:refs/remotes/origin/devel",
        ]
    )
    try:
        main_sha = run(["git", "rev-parse", "--verify", "origin/main"])
        devel_sha = run(["git", "rev-parse", "--verify", "origin/devel"])
    except SyncError as error:
        raise SyncError("Could not read fetched main and devel commits.") from error
    return main_sha, devel_sha


def is_ancestor(main_sha: str, devel_sha: str) -> bool:
    """Return whether main is already included in devel."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", main_sha, devel_sha],
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        message = result.stderr.decode(errors="replace").strip()
        raise SyncError(f"Could not check branch ancestry: {message}")
    return result.returncode == 0


def open_sync_pr() -> dict[str, object] | None:
    """Find an open same-repository PR with exactly main → devel direction."""
    output = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            "devel",
            "--json",
            "url,headRefName,baseRefName,isCrossRepository",
        ]
    )
    try:
        pulls = json.loads(output)
    except json.JSONDecodeError as error:
        raise SyncError("GitHub CLI returned invalid PR data.") from error
    if not isinstance(pulls, list) or any(not isinstance(pr, dict) for pr in pulls):
        raise SyncError("GitHub CLI returned an unexpected PR list shape.")
    for pr in pulls:
        if (
            pr.get("headRefName") == "main"
            and pr.get("baseRefName") == "devel"
            and pr.get("isCrossRepository") is False
            and isinstance(pr.get("url"), str)
        ):
            return pr
    return None


def inspect_pr(url: str) -> dict[str, object]:
    """Read refs and mergeability without changing the PR or its metadata."""
    output = run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--json",
            "url,headRefName,baseRefName,isCrossRepository,headRefOid,baseRefOid,mergeable,mergeStateStatus",
        ]
    )
    try:
        pr = json.loads(output)
    except json.JSONDecodeError as error:
        raise SyncError("GitHub CLI returned invalid PR details.") from error
    if not isinstance(pr, dict):
        raise SyncError("GitHub CLI returned an unexpected PR detail shape.")
    if (
        pr.get("headRefName") != "main"
        or pr.get("baseRefName") != "devel"
        or pr.get("isCrossRepository") is not False
        or pr.get("url") != url
    ):
        raise SyncError(f"Refusing unrelated or cross-repository PR: {url}")
    return pr


def render_body(main_sha: str, devel_sha: str) -> str:
    """Describe actual commits and immutable refs without claiming validation."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        raise SyncError("GITHUB_REPOSITORY must be owner/repository.")
    commits = run(
        [
            "git",
            "log",
            "--reverse",
            "--format=%H%x09%s",
            f"{devel_sha}..{main_sha}",
        ]
    ).splitlines()
    commit_list = "\n".join(
        f"- {subject} ([`{sha[:7]}`](https://github.com/{repository}/commit/{sha}))"
        for line in commits
        for sha, _, subject in [line.partition("\t")]
    )
    if not commit_list:
        commit_list = "- No main-only commits (ancestry-only divergence)."
    return (
        "## Purpose\n\n"
        "Propagate the reviewed commits from `main` into `devel` while preserving "
        "both branch histories. This PR does not publish a release or change "
        "Release Please ownership.\n\n"
        "## Source commits\n\n"
        f"{commit_list}\n\n"
        "## Refs observed by the sync workflow\n\n"
        f"- `main`: `{main_sha}`\n"
        f"- `devel`: `{devel_sha}`\n\n"
        "These refs identify the fetched inputs; they are not CI or merge-validation results. "
        "Review the actual proposed merge and required checks before merging."
    )


def validate_current_pr(
    pr: dict[str, object], main_sha: str, devel_sha: str, *, url: str
) -> None:
    """Reject stale PR refs and report conflicts without attempting resolution."""
    if (pr.get("headRefOid"), pr.get("baseRefOid")) != (main_sha, devel_sha):
        raise SyncError(
            f"{url} exists, but its refs moved during preflight; rerun the workflow."
        )
    if pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY":
        raise SyncError(
            f"{url} has merge conflicts. Leave it for maintainer review; do not "
            "update the main head from devel or resolve conflicts automatically."
        )


def prepare_sync() -> SyncResult:
    """Create or reuse one reviewed PR, never writing either protected branch."""
    main_sha, devel_sha = fetch_refs()
    if is_ancestor(main_sha, devel_sha):
        return SyncResult(None, main_sha, devel_sha)

    existing = open_sync_pr()
    if existing:
        url = str(existing["url"])
        pr = inspect_pr(url)
        latest_main, latest_devel = fetch_refs()
        if (latest_main, latest_devel) != (main_sha, devel_sha):
            raise SyncError("main or devel moved during preflight; rerun the workflow.")
        validate_current_pr(pr, main_sha, devel_sha, url=url)
        return SyncResult(url, main_sha, devel_sha)

    body = render_body(main_sha, devel_sha)
    latest_main, latest_devel = fetch_refs()
    if (latest_main, latest_devel) != (main_sha, devel_sha):
        raise SyncError("main or devel moved during preflight; rerun the workflow.")

    try:
        url = run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "devel",
                "--head",
                "main",
                "--title",
                "chore(ci): synchronize main into devel",
                "--body",
                body,
            ]
        )
    except SyncError:
        # Another run may have won the create race. Reuse only the exact same-repo PR.
        existing = open_sync_pr()
        if not existing:
            identical_trees = run(["git", "rev-parse", f"{main_sha}^{{tree}}"]) == run(
                ["git", "rev-parse", f"{devel_sha}^{{tree}}"]
            )
            if identical_trees:
                raise SyncError(
                    "GitHub could not create the ancestry-only sync PR (branch trees "
                    "are identical). No branch was changed; defer until a production "
                    "change exists or ask a maintainer to define a reviewed, "
                    "ancestry-preserving recovery."
                ) from None
            raise
        url = str(existing["url"])

    pr = inspect_pr(url)
    latest_main, latest_devel = fetch_refs()
    if (latest_main, latest_devel) != (main_sha, devel_sha):
        raise SyncError(
            f"{url} was created/reused, but a branch moved during the operation; "
            "the PR tracks current branch tips, rerun to verify them."
        )
    validate_current_pr(pr, main_sha, devel_sha, url=url)
    return SyncResult(url, main_sha, devel_sha)


def main() -> int:
    if os.environ.get("GITHUB_REPOSITORY", "").count("/") != 1:
        print(
            "Sync failed: GITHUB_REPOSITORY must be owner/repository.", file=sys.stderr
        )
        return 1
    try:
        result = prepare_sync()
    except (OSError, SyncError) as error:
        print(f"Sync failed: {error}", file=sys.stderr)
        return 1
    print(f"Observed main ref: {result.main_sha}")
    print(f"Observed devel ref: {result.devel_sha}")
    if result.url:
        print(f"Reviewed main-to-devel sync PR: {result.url}")
    else:
        print("main is already an ancestor of devel; no sync PR needed.")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"pull-request-url={result.url or ''}\n")
            stream.write(f"main-sha={result.main_sha}\n")
            stream.write(f"devel-sha={result.devel_sha}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
