"""Publish an independent, trusted GitHub App release-metadata check.

Only the protected default-branch pull_request_target workflow may execute this
module with the release guard App's environment-restricted credentials.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHECK_NAME = "Trusted release metadata"
API_ROOT = "https://api.github.com"
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z")
MERGEABILITY_MAX_POLLS = 10
MERGEABILITY_POLL_INTERVAL_SECONDS = 3


class GuardError(RuntimeError):
    """An API or validation failure must never produce a passing check."""


@dataclass(frozen=True)
class GuardContext:
    repository: str
    number: int
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    run_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GuardContext:
        repository = env.get("GITHUB_REPOSITORY", "")
        if REPOSITORY_RE.fullmatch(repository) is None:
            raise GuardError("GITHUB_REPOSITORY must be owner/repository.")
        try:
            number = int(env.get("PR_NUMBER", ""))
        except ValueError as error:
            raise GuardError("PR_NUMBER must be a positive integer.") from error
        if number < 1:
            raise GuardError("PR_NUMBER must be a positive integer.")
        base_sha = env.get("PR_BASE_SHA", "")
        head_sha = env.get("PR_HEAD_SHA", "")
        if SHA_RE.fullmatch(base_sha) is None or SHA_RE.fullmatch(head_sha) is None:
            raise GuardError("PR base/head must be full commit SHAs.")
        base_ref, head_ref = env.get("PR_BASE_REF", ""), env.get("PR_HEAD_REF", "")
        if not base_ref or not head_ref:
            raise GuardError("PR base/head branch names are required.")
        run_id = env.get("GITHUB_RUN_ID", "")
        if not run_id.isdecimal() or int(run_id) < 1:
            raise GuardError("GITHUB_RUN_ID must be a positive integer.")
        return cls(
            repository=repository,
            number=number,
            base_ref=base_ref,
            base_sha=base_sha,
            head_ref=head_ref,
            head_sha=head_sha,
            run_url=f"https://github.com/{repository}/actions/runs/{run_id}",
        )


class GitHubAPI:
    """Small REST client with an App installation token, no untrusted code."""

    def __init__(self, token: str) -> None:
        if not token:
            raise GuardError("RELEASE_GUARD_TOKEN is missing.")
        self._token = token

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = Request(
            f"{API_ROOT}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            raise GuardError(
                f"GitHub API request failed ({error.code}): {error.reason}"
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise GuardError(f"GitHub API request failed: {error}") from error
        if not isinstance(result, dict):
            raise GuardError("GitHub API returned an unexpected response type.")
        return result


def create_check(ctx: GuardContext, api: GitHubAPI, sha: str) -> int:
    """Post the real in-progress validation on one immutable commit."""
    result = api.request(
        "POST",
        f"/repos/{ctx.repository}/check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": sha,
            "status": "in_progress",
            "details_url": ctx.run_url,
            "external_id": (
                f"pr={ctx.number};base={ctx.base_sha};head={ctx.head_sha};checked={sha}"
            ),
            "output": {
                "title": "Validating release metadata",
                "summary": (
                    f"PR #{ctx.number}; base {ctx.base_ref}@{ctx.base_sha}; "
                    f"head {ctx.head_ref}@{ctx.head_sha}; checked {sha}."
                ),
            },
        },
    )
    check_id = result.get("id")
    if not isinstance(check_id, int) or check_id < 1:
        raise GuardError("GitHub did not return a valid check run ID.")
    return check_id


def complete_check(
    ctx: GuardContext, api: GitHubAPI, check_id: int, conclusion: str, summary: str
) -> None:
    api.request(
        "PATCH",
        f"/repos/{ctx.repository}/check-runs/{check_id}",
        {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "output": {
                "title": (
                    "Release metadata passed"
                    if conclusion == "success"
                    else "Release metadata failed"
                ),
                "summary": summary,
            },
        },
    )


def start_checks(
    ctx: GuardContext, api: GitHubAPI
) -> tuple[int, int | None, str | None]:
    """Cover both the PR head and the test-merge commit used by branch rulesets."""
    pull_path = f"/repos/{ctx.repository}/pulls/{ctx.number}"
    head_check_id = create_check(ctx, api, ctx.head_sha)
    try:
        merge_sha: str | None = None
        for attempt in range(MERGEABILITY_MAX_POLLS):
            pull = api.request("GET", pull_path)
            if not current_revision_matches(ctx, pull):
                raise GuardError(
                    "PR base/head changed before starting trusted validation."
                )
            if pull.get("mergeable") is False:
                raise GuardError(
                    "PR has merge conflicts; trusted validation cannot proceed."
                )
            candidate = pull.get("merge_commit_sha")
            if (
                pull.get("mergeable") is True
                and isinstance(candidate, str)
                and SHA_RE.fullmatch(candidate)
            ):
                merge_sha = candidate
                break
            if attempt + 1 < MERGEABILITY_MAX_POLLS:
                time.sleep(MERGEABILITY_POLL_INTERVAL_SECONDS)
        else:
            raise GuardError(
                "GitHub did not produce a stable test-merge commit after "
                f"{MERGEABILITY_MAX_POLLS} checks. Retry trusted validation."
            )

        merge_commit = api.request(
            "GET", f"/repos/{ctx.repository}/commits/{merge_sha}"
        )
        parents = merge_commit.get("parents")
        parent_shas = (
            [parent.get("sha") for parent in parents if isinstance(parent, dict)]
            if isinstance(parents, list)
            else []
        )
        if merge_commit.get("sha") != merge_sha or parent_shas != [
            ctx.base_sha,
            ctx.head_sha,
        ]:
            raise GuardError(
                "GitHub's test-merge commit does not have the expected current base/head parents."
            )

        merge_check_id = (
            create_check(ctx, api, merge_sha) if merge_sha != ctx.head_sha else None
        )
    except GuardError as error:
        complete_check(ctx, api, head_check_id, "failure", str(error))
        raise

    return head_check_id, merge_check_id, merge_sha


def authorized_release_source(ctx: GuardContext, pull: Mapping[str, Any]) -> bool:
    """Reject fork branch-name impersonation and unauthorized release writers."""
    is_promotion = ctx.base_ref == "main" and ctx.head_ref == "devel"
    is_sync = ctx.base_ref == "devel" and ctx.head_ref == "main"
    is_release = ctx.base_ref == "main" and ctx.head_ref.startswith("release-please--")
    if not (is_promotion or is_sync or is_release):
        return True
    try:
        if pull["head"]["repo"]["full_name"] != ctx.repository:
            return False
        if pull["base"]["repo"]["full_name"] != ctx.repository:
            return False
        if is_release:
            author = pull["user"]
            return author["type"] == "Bot" and author["login"] in {
                "allpowers-release-please[bot]",
                "github-actions[bot]",
                "release-please[bot]",
            }
        return True
    except KeyError, TypeError:
        return False


def current_revision_matches(ctx: GuardContext, pull: Mapping[str, Any]) -> bool:
    """Reject stale base/head SHAs, retargeted PRs, or closed PRs."""
    try:
        return (
            pull["state"] == "open"
            and pull["number"] == ctx.number
            and pull["base"]["ref"] == ctx.base_ref
            and pull["base"]["sha"] == ctx.base_sha
            and pull["head"]["ref"] == ctx.head_ref
            and pull["head"]["sha"] == ctx.head_sha
            and authorized_release_source(ctx, pull)
        )
    except KeyError, TypeError:
        return False


def finish_checks(
    ctx: GuardContext,
    api: GitHubAPI,
    head_check_id: int,
    merge_check_id: int | None,
    merge_sha: str | None,
    validation_outcome: str,
) -> None:
    """Report success only for validated, current head/base/test-merge SHAs."""
    if head_check_id < 1 or (merge_check_id is not None and merge_check_id < 1):
        raise GuardError("Invalid check run ID.")
    reason = "Trusted metadata validation did not succeed."
    current = False
    try:
        pull = api.request("GET", f"/repos/{ctx.repository}/pulls/{ctx.number}")
        current = (
            current_revision_matches(ctx, pull)
            and pull.get("merge_commit_sha") == merge_sha
        )
        if not current:
            reason = "PR base/head or test-merge commit changed during validation."
    except GuardError:
        reason = "Could not re-read the current PR revisions from GitHub."
    success = validation_outcome == "success" and current
    if success:
        reason = "Trusted default-branch validator passed for current PR revisions."

    summary = (
        f"{reason}\n\nPR #{ctx.number}; "
        f"base {ctx.base_ref}@{ctx.base_sha}; "
        f"head {ctx.head_ref}@{ctx.head_sha}; "
        f"test merge {merge_sha or 'unavailable'}.\n"
        f"Validator step outcome: {validation_outcome}."
    )
    for check_id in (head_check_id, merge_check_id):
        if check_id is not None:
            complete_check(
                ctx,
                api,
                check_id,
                "success" if success else "failure",
                summary,
            )
    if not success:
        raise GuardError(reason)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "finish"))
    args = parser.parse_args()
    try:
        ctx = GuardContext.from_env(os.environ)
        api = GitHubAPI(os.environ.get("RELEASE_GUARD_TOKEN", ""))
        if args.command == "start":
            head_id, merge_id, merge_sha = start_checks(ctx, api)
            print(f"head_check_id={head_id}")
            print(f"merge_check_id={merge_id or ''}")
            print(f"merge_sha={merge_sha or ''}")
        else:
            head_id = int(os.environ.get("RELEASE_GUARD_HEAD_CHECK_ID", "0"))
            merge_id_raw = os.environ.get("RELEASE_GUARD_MERGE_CHECK_ID", "")
            merge_id = int(merge_id_raw) if merge_id_raw else None
            merge_sha = os.environ.get("RELEASE_GUARD_MERGE_SHA") or None
            finish_checks(
                ctx,
                api,
                head_id,
                merge_id,
                merge_sha,
                os.environ.get("VALIDATION_OUTCOME", ""),
            )
    except (GuardError, ValueError) as error:
        print(f"Release guard failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
