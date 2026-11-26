"""Small standard-library GitHub REST client for repository automation."""

from __future__ import annotations

import base64
import json
from typing import Any, Literal, overload
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class GitHubApiError(RuntimeError):
    """A GitHub request or response could not be safely used."""


class GitHubApiClient:
    """Read-only, repository-scoped REST client used by the Merge gate."""

    def __init__(self, owner: str, repo: str, token: str, api_url: str) -> None:
        if not token:
            raise GitHubApiError(
                "GITHUB_TOKEN or GH_TOKEN is required for GitHub API access."
            )
        self.owner = owner
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.repository_path = f"/repos/{quote(owner)}/{quote(repo)}"

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "home-assistant-allpowers-ble-automation",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                return (json.loads(raw) if raw else None), dict(
                    response.headers.items()
                )
        except HTTPError as error:
            # Never include request headers or payload; both can contain credentials.
            raise GitHubApiError(
                f"GitHub API request failed for {path}: HTTP {error.code}."
            ) from error
        except (
            URLError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise GitHubApiError(
                f"GitHub API request failed for {path}: {type(error).__name__}."
            ) from error

    def _get(self, path: str, **query: str | int) -> Any:
        suffix = f"?{urlencode(query)}" if query else ""
        return self._request("GET", f"{path}{suffix}")[0]

    def _all_pages(
        self, path: str, key: str, **query: str | int
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._get(path, per_page=100, page=page, **query)
            if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                raise GitHubApiError(
                    f"Unexpected GitHub API response while reading {path}."
                )
            batch = payload[key]
            if any(not isinstance(item, dict) for item in batch):
                raise GitHubApiError(
                    f"Malformed GitHub API entries while reading {path}."
                )
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1

    def pull_request(self, number: int) -> dict[str, Any]:
        value = self._get(f"{self.repository_path}/pulls/{number}")
        if not isinstance(value, dict):
            raise GitHubApiError("GitHub returned an invalid pull request object.")
        return value

    def pull_request_files(self, number: int) -> list[dict[str, Any]]:
        # This endpoint returns an array rather than an object keyed by `files`.
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            values = self._get(
                f"{self.repository_path}/pulls/{number}/files", per_page=100, page=page
            )
            if not isinstance(values, list) or any(
                not isinstance(value, dict) for value in values
            ):
                raise GitHubApiError(
                    "GitHub returned an invalid pull request file list."
                )
            files.extend(values)
            if len(values) < 100:
                return files
            page += 1

    def compare(self, base: str, head: str) -> dict[str, Any]:
        value = self._get(
            f"{self.repository_path}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        )
        if not isinstance(value, dict):
            raise GitHubApiError("GitHub returned an invalid commit comparison.")
        return value

    def content(
        self, path: str, ref: str, repository_full_name: str | None = None
    ) -> str:
        repository_path = self.repository_path
        if repository_full_name is not None:
            parts = repository_full_name.split("/")
            if len(parts) != 2 or not all(parts):
                raise GitHubApiError(
                    "GitHub returned an invalid head repository identity."
                )
            repository_path = f"/repos/{quote(parts[0])}/{quote(parts[1])}"
        value = self._get(
            f"{repository_path}/contents/{quote(path, safe='/')}", ref=ref
        )
        if not isinstance(value, dict) or value.get("type") != "file":
            raise GitHubApiError(f"Expected a file at {path} for {ref}.")
        encoded = value.get("content")
        if value.get("encoding") != "base64" or not isinstance(encoded, str):
            raise GitHubApiError(
                f"GitHub returned missing or unsupported content encoding for {path} at {ref}."
            )
        try:
            return base64.b64decode(
                encoded.replace("\r", "").replace("\n", ""), validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHubApiError(f"Could not decode {path} at {ref}.") from error

    def check_runs(self, ref: str) -> list[dict[str, Any]]:
        return self._all_pages(
            f"{self.repository_path}/commits/{quote(ref, safe='')}/check-runs",
            "check_runs",
            filter="all",
        )

    def workflow_run(self, run_id: int) -> dict[str, Any]:
        value = self._get(f"{self.repository_path}/actions/runs/{run_id}")
        if not isinstance(value, dict):
            raise GitHubApiError("GitHub returned an invalid Actions workflow run.")
        return value

    def workflow(self, workflow_id: int) -> dict[str, Any]:
        value = self._get(f"{self.repository_path}/actions/workflows/{workflow_id}")
        if not isinstance(value, dict):
            raise GitHubApiError("GitHub returned an invalid Actions workflow.")
        return value

    def workflow_run_attempt_jobs(
        self, run_id: int, attempt: int
    ) -> list[dict[str, Any]]:
        if run_id <= 0 or attempt <= 0:
            raise GitHubApiError(
                "Actions run ID and attempt must be positive integers."
            )
        return self._all_pages(
            f"{self.repository_path}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "jobs",
        )

    def list_pull_requests(
        self, *, state: str, base: str, head: str | None = None
    ) -> list[dict[str, Any]]:
        if state not in {"open", "closed", "all"}:
            raise GitHubApiError("Pull request state must be open, closed, or all.")
        # The pulls endpoint returns a top-level array.
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            values = self._get(
                f"{self.repository_path}/pulls",
                state=state,
                base=base,
                per_page=100,
                page=page,
                **({"head": f"{self.owner}:{head}"} if head else {}),
            )
            if not isinstance(values, list) or any(
                not isinstance(value, dict) for value in values
            ):
                raise GitHubApiError("GitHub returned an invalid pull request list.")
            results.extend(values)
            if len(values) < 100:
                return results
            page += 1


class GitHubPullRequestApi(GitHubApiClient):
    """REST client adding the PR creation operation used by authorized workflows."""

    def create_pull_request(
        self, *, base: str, head: str, title: str, body: str, draft: bool = False
    ) -> dict[str, Any]:
        value, _ = self._request(
            "POST",
            f"{self.repository_path}/pulls",
            {"base": base, "head": head, "title": title, "body": body, "draft": draft},
        )
        if not isinstance(value, dict):
            raise GitHubApiError("GitHub returned an invalid created pull request.")
        return value


@overload
def client_from_env(
    *, allow_pull_request_creation: Literal[True]
) -> GitHubPullRequestApi: ...


@overload
def client_from_env(
    *, allow_pull_request_creation: Literal[False] = False
) -> GitHubApiClient: ...


def client_from_env(*, allow_pull_request_creation: bool = False) -> GitHubApiClient:
    """Build a client using the Actions and local CLI token conventions."""
    import os

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubApiError("GITHUB_REPOSITORY must be owner/repository.")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    client_type = (
        GitHubPullRequestApi if allow_pull_request_creation else GitHubApiClient
    )
    return client_type(parts[0], parts[1], token, api_url)
