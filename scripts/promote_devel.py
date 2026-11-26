#!/usr/bin/env python3
"""Prepare a safe draft promotion PR from devel to main."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from scripts.git import (
    GIT_COMMAND_TIMEOUT_SECONDS,
    GIT_NETWORK_TIMEOUT_SECONDS,
    GitExecutionError,
    decode_git_output,
    require_success,
    run_git,
)
from scripts.github_api import (
    GitHubApiError,
    client_from_env,
    validate_pull_request_url,
)
from scripts.promotion_metadata import validate_revisions

REMOTE = "origin"


class PromotionError(RuntimeError):
    """A preflight or GitHub API failure that must stop PR creation."""


def run(command: list[str], *, check: bool = True) -> str:
    """Run a command and return stdout with useful error details."""
    if not command or command[0] != "git":
        raise PromotionError("Only native Git commands are supported here.")
    args = command[1:]
    try:
        timeout = (
            GIT_NETWORK_TIMEOUT_SECONDS
            if args and args[0] in {"fetch", "ls-remote"}
            else GIT_COMMAND_TIMEOUT_SECONDS
        )
        result = run_git(args, cwd=Path.cwd(), timeout=timeout)
        output = require_success(result, args) if check else result.stdout
        return decode_git_output(output).strip()
    except GitExecutionError as error:
        raise PromotionError(str(error)) from error


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


def _same_repo_pr(pr: dict[str, object], owner: str, repo: str) -> bool:
    base = pr.get("base")
    head = pr.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise PromotionError("GitHub returned a PR without base/head refs.")
    repositories: list[str] = []
    for ref in (base, head):
        repository = ref.get("repo")
        full_name = (
            repository.get("full_name") if isinstance(repository, dict) else None
        )
        if not isinstance(full_name, str):
            raise PromotionError(
                "GitHub returned a PR with incomplete repository identity."
            )
        parts = full_name.split("/")
        if len(parts) != 2 or not all(parts):
            raise PromotionError(
                "GitHub returned a PR with incomplete repository identity."
            )
        repositories.append(full_name)
    expected = f"{owner}/{repo}"
    return all(repository == expected for repository in repositories)


def open_promotion() -> dict[str, object] | None:
    """Return an existing open same-repository devel-to-main PR, including drafts."""
    try:
        api = client_from_env()
        pulls = api.list_pull_requests(state="open", base="main", head="devel")
        for pr in pulls:
            base, head = pr.get("base"), pr.get("head")
            if not isinstance(base, dict) or not isinstance(head, dict):
                raise PromotionError("GitHub returned a PR without base/head refs.")
            if (
                base.get("ref") == "main"
                and head.get("ref") == "devel"
                and _same_repo_pr(pr, api.owner, api.repo)
            ):
                url = pr.get("html_url")
                draft = pr.get("draft")
                base_sha, head_sha = base.get("sha"), head.get("sha")
                number = pr.get("number")
                url = validate_pull_request_url(
                    url,
                    number,
                    owner=api.owner,
                    repo=api.repo,
                    api_url=api.api_url,
                )
                if (
                    not isinstance(draft, bool)
                    or not isinstance(base_sha, str)
                    or not base_sha
                    or not isinstance(head_sha, str)
                    or not head_sha
                ):
                    raise PromotionError(
                        "GitHub returned incomplete promotion PR details."
                    )
                return {
                    "url": url,
                    "number": number,
                    "isDraft": draft,
                    "baseSha": base_sha,
                    "headSha": head_sha,
                }
        return None
    except GitHubApiError as error:
        raise PromotionError(str(error)) from error


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
    diff_args = ["diff", "--name-only", "-z", base_sha, head_sha]
    try:
        raw_paths = require_success(run_git(diff_args, cwd=Path.cwd()), diff_args)
        changed = [path.decode("utf-8") for path in raw_paths.split(b"\0") if path]
    except (GitExecutionError, UnicodeDecodeError) as error:
        raise PromotionError("Could not decode changed paths from Git.") from error
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
    ancestry = run_git(
        ["merge-base", "--is-ancestor", base_sha, head_sha], cwd=Path.cwd()
    )
    if ancestry.returncode not in (0, 1):
        raise PromotionError("Could not check branch ancestry.")
    if ancestry.returncode == 1:
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
        api = client_from_env(allow_pull_request_creation=True)
        created = api.create_pull_request(
            base="main",
            head="devel",
            title="chore: promote devel to main",
            body=body,
            draft=True,
        )
        url = created.get("html_url")
        number = created.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise PromotionError(
                "GitHub returned incomplete created promotion PR details."
            )
        url = validate_pull_request_url(
            url,
            number,
            owner=api.owner,
            repo=api.repo,
            api_url=api.api_url,
        )
        pull = api.pull_request(number)
    except (PromotionError, GitHubApiError) as create_error:
        existing = open_promotion()
        if existing:
            if (
                existing.get("isDraft") is True
                and existing.get("baseSha") == base_sha
                and existing.get("headSha") == head_sha
            ):
                return str(existing["url"])
            raise PromotionError(
                "A promotion PR exists after the ambiguous create, but its draft or "
                "validated refs do not match this run; inspect it before retrying."
            ) from create_error
        if isinstance(create_error, PromotionError):
            raise
        raise PromotionError(str(create_error)) from create_error
    detail_url = validate_pull_request_url(
        pull.get("html_url"),
        pull.get("number"),
        owner=api.owner,
        repo=api.repo,
        api_url=api.api_url,
    )
    if pull.get("number") != number or detail_url != url:
        raise PromotionError("GitHub returned inconsistent PR URL/number details.")
    base, head = pull.get("base"), pull.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise PromotionError("GitHub returned a promotion PR without base/head refs.")
    if (
        pull.get("draft") is not True
        or (head.get("sha"), base.get("sha")) != (head_sha, base_sha)
        or base.get("ref") != "main"
        or head.get("ref") != "devel"
        or not _same_repo_pr(pull, api.owner, api.repo)
    ):
        raise PromotionError(
            f"Draft {url} was not created as a draft at the validated revisions. "
            "Keep it draft, close it, and rerun."
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
