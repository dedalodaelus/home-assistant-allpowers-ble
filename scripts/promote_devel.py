#!/usr/bin/env python3
"""Prepare a safe draft promotion PR from devel to main."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.promotion_metadata import semver_key

API_ROOT = "https://api.github.com"
MANIFEST_PATH = "custom_components/allpowers_ble/manifest.json"
CHANGELOG_PATH = "CHANGELOG.md"
RELEASE_MANIFEST_PATH = ".release-please-manifest.json"
PER_PAGE = 100


class PromotionError(RuntimeError):
    """A preflight or GitHub API failure that must stop PR creation."""


class GitHubAPI:
    """Small GitHub REST client with bounded retries for rate limits."""

    def __init__(self, owner: str, repo: str, token: str) -> None:
        if not token:
            raise PromotionError(
                "GitHub App token is missing; check promotion App secrets."
            )
        self.owner = owner
        self.repo = repo
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{API_ROOT}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(4):
            request = Request(
                url,
                data=data,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except HTTPError as error:
                message = error.read().decode("utf-8", errors="replace")[:1000]
                retry_after = error.headers.get("Retry-After")
                rate_limited = error.code == 429 or (
                    error.code == 403
                    and (
                        retry_after or error.headers.get("X-RateLimit-Remaining") == "0"
                    )
                )
                if rate_limited and attempt < 3:
                    delay = min(int(retry_after or 2**attempt), 30)
                    time.sleep(delay)
                    continue
                if error.code in {401, 403}:
                    raise PromotionError(
                        f"GitHub authentication/permission failure ({error.code}): {message}"
                    ) from error
                raise PromotionError(
                    f"GitHub API failure ({error.code}): {message}"
                ) from error
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                raise PromotionError(f"GitHub API request failed: {error}") from error
        raise PromotionError("GitHub API remained rate limited after retries.")

    def paginated(self, path: str, query: dict[str, Any] | None = None) -> list[Any]:
        values: list[Any] = []
        page = 1
        while True:
            response = self.request(
                "GET", path, query={**(query or {}), "per_page": PER_PAGE, "page": page}
            )
            if not isinstance(response, list):
                raise PromotionError(f"Unexpected paginated response from {path}.")
            values.extend(response)
            if len(response) < PER_PAGE:
                return values
            page += 1

    def open_promotions(self) -> list[dict[str, Any]]:
        return self.paginated(
            f"/repos/{self.owner}/{self.repo}/pulls",
            {"state": "open", "base": "main", "head": f"{self.owner}:devel"},
        )

    def refs(self) -> tuple[str, str]:
        base = self.request(
            "GET", f"/repos/{self.owner}/{self.repo}/git/ref/heads/main"
        )
        head = self.request(
            "GET", f"/repos/{self.owner}/{self.repo}/git/ref/heads/devel"
        )
        return base["object"]["sha"], head["object"]["sha"]

    def compare_page(self, base_sha: str, head_sha: str, page: int) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/compare/{base_sha}...{head_sha}",
            query={"per_page": PER_PAGE, "page": page},
        )

    def tree(self, commit_sha: str) -> dict[str, dict[str, Any]]:
        commit = self.request(
            "GET", f"/repos/{self.owner}/{self.repo}/git/commits/{commit_sha}"
        )
        tree = self.request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/git/trees/{commit['tree']['sha']}",
            query={"recursive": 1},
        )
        if tree.get("truncated"):
            raise PromotionError(
                "GitHub returned a truncated tree; refusing an incomplete preflight."
            )
        return {entry["path"]: entry for entry in tree["tree"]}

    def blob_text(self, sha: str) -> str:
        blob = self.request("GET", f"/repos/{self.owner}/{self.repo}/git/blobs/{sha}")
        if blob.get("encoding") != "base64":
            raise PromotionError("GitHub returned an unsupported blob encoding.")
        return base64.b64decode(blob["content"]).decode("utf-8")

    def associated_prs(self, commit_sha: str) -> list[dict[str, Any]]:
        return self.paginated(
            f"/repos/{self.owner}/{self.repo}/commits/{commit_sha}/pulls"
        )

    def recent_promotions(self) -> list[dict[str, Any]]:
        pulls = self.paginated(
            f"/repos/{self.owner}/{self.repo}/pulls",
            {"state": "closed", "base": "main", "head": f"{self.owner}:devel"},
        )
        promotions = [
            pull
            for pull in pulls
            if pull.get("merged_at")
            and (
                pull.get("title", "").startswith("chore(release):")
                or re.search(
                    r"promot(?:e|ion).*devel.*main", pull.get("title", ""), re.I
                )
            )
        ]
        return sorted(promotions, key=lambda pull: pull["merged_at"], reverse=True)[:4]

    def create_draft(self, body: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            payload={
                "head": "devel",
                "base": "main",
                "draft": True,
                "title": "chore: promote devel to main",
                "body": body,
            },
        )


def load_version(api: GitHubAPI, tree: dict[str, dict[str, Any]], ref: str) -> str:
    entry = tree.get(MANIFEST_PATH)
    if entry is None or entry.get("type") != "blob":
        raise PromotionError(
            f"{MANIFEST_PATH} is missing or is not a regular file at {ref}."
        )
    try:
        manifest = json.loads(api.blob_text(entry["sha"]))
    except (json.JSONDecodeError, KeyError) as error:
        raise PromotionError(
            f"Cannot read {MANIFEST_PATH} at {ref}: {error}"
        ) from error
    version = manifest.get("version")
    if not isinstance(version, str):
        raise PromotionError(f"{MANIFEST_PATH} has no string version at {ref}.")
    semver_key(version)
    return version


def change_summary(commits: list[dict[str, Any]], api: GitHubAPI) -> str:
    with ThreadPoolExecutor(max_workers=3) as executor:
        associated = list(
            executor.map(lambda commit: api.associated_prs(commit["sha"]), commits)
        )

    categories: dict[str, list[str]] = {
        "Features": [],
        "Bug Fixes": [],
        "Documentation": [],
        "Maintenance": [],
        "Other Changes": [],
    }
    seen: set[int] = set()
    for commit, pull_requests in zip(commits, associated, strict=True):
        merged_prs = [pull for pull in pull_requests if pull.get("merged_at")]
        entries: list[dict[str, Any] | None] = merged_prs or [None]
        for pull in entries:
            if pull and pull["number"] in seen:
                continue
            if pull:
                seen.add(pull["number"])
            subject = (commit.get("commit", {}).get("message") or "").split("\n", 1)[0]
            title = pull.get("title", subject) if pull else subject
            match = re.match(
                r"^(feat|fix|docs|test|build|ci|chore|refactor|perf)(?:\([^)]*\))?:\s*",
                title,
                re.I,
            )
            kind = match.group(1).lower() if match else "other"
            category = {
                "feat": "Features",
                "fix": "Bug Fixes",
                "docs": "Documentation",
                "test": "Maintenance",
                "build": "Maintenance",
                "ci": "Maintenance",
                "chore": "Maintenance",
                "refactor": "Maintenance",
                "perf": "Maintenance",
            }.get(kind, "Other Changes")
            display = title[match.end() :] if match else title
            link = (
                f"([#{pull['number']}]({pull['html_url']}))"
                if pull
                else f"([`{commit['sha'][:7]}`]({commit['html_url']}))"
            )
            categories[category].append(f"* {display} {link}")
    return (
        "\n\n".join(
            f"#### {name}\n\n" + "\n".join(lines)
            for name, lines in categories.items()
            if lines
        )
        or "* No included commits were reported."
    )


def render_template(
    template: str,
    commits: list[dict[str, Any]],
    changed_paths: list[str],
    api: GitHubAPI,
) -> str:
    tests = [path for path in changed_paths if path.startswith("tests/")]
    test_summary = (
        "\n".join(f"* `{path}`" for path in tests)
        if tests
        else "* No test files changed. This list describes changed files, not test results."
    )
    promotions = api.recent_promotions()
    history = (
        f"The repository has {len(promotions)} recent previous promotions from `devel` to `main`:\n\n"
        + "\n".join(f"* [#{pr['number']}]({pr['html_url']})" for pr in promotions)
        if promotions
        else "No merged promotion PRs from `devel` to `main` were found."
    )
    replacements = {
        "<!-- GENERATED_COMMITS -->": change_summary(commits, api),
        "<!-- GENERATED_TESTS -->": test_summary,
        "<!-- GENERATED_PREVIOUS_PROMOTIONS -->": history,
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise PromotionError(
                f"Promotion template must contain exactly one {marker} marker."
            )
        template = template.replace(marker, value)
    return template


def prepare_promotion(api: GitHubAPI, template: str) -> str | None:
    """Run all preflight checks and create one draft PR, or return an existing URL."""
    existing = api.open_promotions()
    if existing:
        return existing[0]["html_url"]

    base_sha, head_sha = api.refs()
    first_page = api.compare_page(base_sha, head_sha, 1)
    status = first_page.get("status")
    if status == "identical":
        return None
    if status != "ahead":
        raise PromotionError(
            f"Branches are not in a promotable ancestry relationship (status: {status}). "
            "Create a reviewed main-to-devel synchronization PR using a merge commit, then retry."
        )

    commits: list[dict[str, Any]] = []
    page = 1
    comparison = first_page
    while True:
        page_commits = comparison.get("commits")
        if not isinstance(page_commits, list):
            raise PromotionError("GitHub returned an incomplete commit comparison.")
        commits.extend(page_commits)
        if len(page_commits) < PER_PAGE:
            break
        page += 1
        comparison = api.compare_page(base_sha, head_sha, page)
    if not commits:
        raise PromotionError(
            "Branches differ but GitHub returned no commits; refusing an empty PR."
        )

    base_tree = api.tree(base_sha)
    head_tree = api.tree(head_sha)
    changed_paths = sorted(
        path
        for path in base_tree.keys() | head_tree.keys()
        if (
            path not in base_tree
            or path not in head_tree
            or (
                base_tree[path].get("sha"),
                base_tree[path].get("mode"),
                base_tree[path].get("type"),
            )
            != (
                head_tree[path].get("sha"),
                head_tree[path].get("mode"),
                head_tree[path].get("type"),
            )
        )
    )
    if not changed_paths:
        raise PromotionError(
            "Branches contain commits but no tree changes; refusing incomplete comparison."
        )

    protected_history = {CHANGELOG_PATH, RELEASE_MANIFEST_PATH}
    affected_history = sorted(protected_history.intersection(changed_paths))
    if affected_history:
        raise PromotionError(
            "Release Please managed files changed: "
            + ", ".join(affected_history)
            + ". Reconcile them through the reviewed release process before promoting."
        )

    base_manifest_version = load_version(api, base_tree, base_sha)
    head_manifest_version = load_version(api, head_tree, head_sha)
    if semver_key(head_manifest_version) < semver_key(base_manifest_version):
        raise PromotionError(
            "Integration manifest version would regress from "
            f"{base_manifest_version} to {head_manifest_version}."
        )

    body = render_template(template, commits, changed_paths, api)
    if api.refs() != (base_sha, head_sha):
        raise PromotionError(
            "main or devel moved during preflight; rerun to regenerate the promotion summary."
        )
    # Close the race with an independently opened promotion PR where possible.
    existing = api.open_promotions()
    if existing:
        return existing[0]["html_url"]
    try:
        pull = api.create_draft(body)
    except PromotionError:
        # A concurrent invocation may win the PR creation race. Reuse its result.
        existing = api.open_promotions()
        if existing:
            return existing[0]["html_url"]
        raise
    return pull["html_url"]


def main() -> int:
    owner = os.environ.get("GITHUB_REPOSITORY", "").split("/", 1)
    if len(owner) != 2:
        print("GITHUB_REPOSITORY must be owner/repository.", file=sys.stderr)
        return 1
    try:
        template_path = Path(os.environ.get("PROMOTION_TEMPLATE", ""))
        template = template_path.read_text(encoding="utf-8")
        api = GitHubAPI(owner[0], owner[1], os.environ.get("PROMOTION_TOKEN", ""))
        url = prepare_promotion(api, template)
    except (OSError, PromotionError, ValueError) as error:
        print(f"Promotion preflight failed: {error}", file=sys.stderr)
        return 1
    if url:
        print(f"Promotion PR: {url}")
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open(
                "a", encoding="utf-8"
            ) as output:
                output.write(f"pull-request-url={url}\n")
    else:
        print("No changes to promote: main and devel are identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
