#!/usr/bin/env python3
"""Prepare or reuse a reviewed PR propagating main into devel."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from scripts.github_api import GitHubApiError, client_from_env

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
    if not command or command[0] != "git":
        raise SyncError("Only native Git commands are supported here.")
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, cwd=Path.cwd()
    )
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
        cwd=Path.cwd(),
    )
    if result.returncode == 1:
        return False
    if result.returncode:
        raise SyncError("Could not check branch ancestry.")
    return True


def _same_repository(pr: dict[str, object], repository: str) -> bool:
    base, head = pr.get("base"), pr.get("head")
    return (
        isinstance(base, dict)
        and isinstance(head, dict)
        and isinstance(base.get("repo"), dict)
        and isinstance(head.get("repo"), dict)
        and base["repo"].get("full_name") == repository
        and head["repo"].get("full_name") == repository
    )


def _pr_number(url: str) -> int:
    try:
        number = int(urlparse(url).path.rstrip("/").split("/")[-1])
    except (ValueError, IndexError) as error:
        raise SyncError("GitHub returned an invalid pull request URL.") from error
    if number <= 0:
        raise SyncError("GitHub returned an invalid pull request URL.")
    return number


def open_sync_pr() -> dict[str, object] | None:
    """Find an open same-repository PR with exactly main → devel direction."""
    try:
        api = client_from_env()
        repository = f"{api.owner}/{api.repo}"
        for pr in api.list_pull_requests(state="open", base="devel", head="main"):
            base, head = pr.get("base"), pr.get("head")
            if not isinstance(base, dict) or not isinstance(head, dict):
                raise SyncError("GitHub returned a PR without base/head refs.")
            if (
                base.get("ref") == "devel"
                and head.get("ref") == "main"
                and _same_repository(pr, repository)
            ):
                url = pr.get("html_url")
                if not isinstance(url, str):
                    raise SyncError("GitHub returned a sync PR without a URL.")
                return {"url": url}
        return None
    except GitHubApiError as error:
        raise SyncError(str(error)) from error


def inspect_pr(url: str) -> dict[str, object]:
    """Read refs and mergeability for a PR URL without changing it."""
    try:
        api = client_from_env()
        pr = api.pull_request(_pr_number(url))
    except GitHubApiError as error:
        raise SyncError(str(error)) from error
    base, head = pr.get("base"), pr.get("head")
    repository = f"{api.owner}/{api.repo}"
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise SyncError("GitHub returned a PR without base/head refs.")
    if (
        base.get("ref") != "devel"
        or head.get("ref") != "main"
        or not _same_repository(pr, repository)
        or pr.get("html_url") != url
    ):
        raise SyncError(f"Refusing unrelated or cross-repository PR: {url}")
    for name, ref in (("base", base), ("head", head)):
        if not isinstance(ref.get("sha"), str) or not ref.get("sha"):
            raise SyncError(f"GitHub returned a PR with missing {name}.sha.")
    mergeable = pr.get("mergeable")
    if mergeable is not None and not isinstance(mergeable, bool):
        raise SyncError("GitHub returned an invalid mergeable value.")
    merge_state = pr.get("mergeable_state")
    if merge_state is not None and not isinstance(merge_state, str):
        raise SyncError("GitHub returned an invalid mergeable_state value.")
    return {
        "headRefOid": head["sha"],
        "baseRefOid": base["sha"],
        "mergeable": "UNKNOWN"
        if mergeable is None
        else ("MERGEABLE" if mergeable else "CONFLICTING"),
        "mergeStateStatus": "UNKNOWN"
        if merge_state in (None, "unknown")
        else str(merge_state).upper(),
    }


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
        api = client_from_env(allow_pull_request_creation=True)
        created = api.create_pull_request(
            base="devel",
            head="main",
            title="chore(ci): synchronize main into devel",
            body=body,
        )
        url = created.get("html_url")
        number = created.get("number")
        if (
            not isinstance(url, str)
            or not isinstance(number, int)
            or isinstance(number, bool)
        ):
            raise SyncError("GitHub returned incomplete created sync PR details.")
    except (SyncError, GitHubApiError) as create_error:
        # A timed-out create is ambiguous; reuse only the exact same-repository PR.
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
            raise SyncError(str(create_error)) from create_error
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
