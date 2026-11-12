"""Mark explicitly resolved issues as fixed in devel, pending a stable release."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

LABEL = "status: fixed-in-devel"
LABEL_COLOR = "5319e7"
LABEL_DESCRIPTION = "Fixed in devel; not yet included in a stable release."
KEYWORD_REFERENCE = re.compile(
    r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE
)


def explicit_issue_numbers(body: str | None) -> list[int]:
    """Return unique local issue numbers following GitHub closing keywords."""
    if not body:
        return []
    matches = (int(match.group(1)) for match in KEYWORD_REFERENCE.finditer(body))
    return [number for number in dict.fromkeys(matches) if number > 0]


def is_eligible_merge(merged: str, base_branch: str) -> bool:
    """Only successfully merged PRs targeting devel may update issues."""
    return merged.lower() == "true" and base_branch == "devel"


class GitHubAPI:
    """Small GitHub REST client using the workflow token."""

    def __init__(self, repository: str, token: str) -> None:
        self.base_url = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method: str, path: str, data: Any = None) -> Any:
        payload = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Content-Type": "application/json"} if payload else {}),
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                result = response.read()
                return json.loads(result) if result else None
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 422:
                # Label creation can race with another delivery of this workflow.
                return {"_http_status": 422}
            raise

    def pages(self, path: str) -> Iterator[Any]:
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            result = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not result:
                return
            yield from result
            if len(result) < 100:
                return
            page += 1


def mark_issue(api: GitHubAPI, issue_number: int, pr_number: int, pr_url: str) -> None:
    issue = api.request("GET", f"/issues/{issue_number}")
    if issue is None or "pull_request" in issue:
        print(f"Skipping #{issue_number}: not an issue in this repository")
        return

    existing_labels = {label["name"] for label in issue.get("labels", [])}
    if LABEL not in existing_labels:
        labels = {label["name"] for label in api.pages("/labels")}
        if LABEL not in labels:
            api.request(
                "POST",
                "/labels",
                {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
            )
        api.request("POST", f"/issues/{issue_number}/labels", {"labels": [LABEL]})

    marker = f"<!-- fixed-in-devel:pr-{pr_number} -->"
    comments = api.pages(f"/issues/{issue_number}/comments")
    if any(marker in comment["body"] for comment in comments):
        return

    api.request(
        "POST",
        f"/issues/{issue_number}/comments",
        {
            "body": (
                f"Fixed by #{pr_number} (merged into `devel`): {pr_url}\n\n"
                "This fix is **not yet included in a stable release**. "
                "The issue will remain open until a maintainer confirms the fix "
                "has reached `main`.\n\n"
                f"{marker}"
            )
        },
    )


def main() -> int:
    if not is_eligible_merge(os.environ["PR_MERGED"], os.environ["PR_BASE_REF"]):
        print("Skipping PR: it was not merged into devel")
        return 0

    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    pr_number = int(os.environ["PR_NUMBER"])
    pr_url = os.environ["PR_URL"]
    body = os.environ.get("PR_BODY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must be an owner/repository pair")
    if pr_number < 1:
        raise ValueError("PR_NUMBER must be positive")
    api = GitHubAPI(repository, token)
    for issue_number in explicit_issue_numbers(body):
        mark_issue(api, issue_number, pr_number, pr_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
