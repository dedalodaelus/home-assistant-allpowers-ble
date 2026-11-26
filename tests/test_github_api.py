"""Contract tests for the shared repository-scoped GitHub REST adapter."""

from __future__ import annotations

from http.client import IncompleteRead, RemoteDisconnected
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import github_api
from scripts.github_api import GitHubApiClient, GitHubApiError, GitHubPullRequestApi


class Response:
    headers: dict[str, str] = {}

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_pull_listing_paginates_and_scopes_repository_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []
    pages = [[{"number": index} for index in range(100)], [{"number": 100}]]

    def open_request(request: Any, timeout: float) -> Response:
        assert timeout == 30
        requests.append(request)
        return Response(pages.pop(0))

    monkeypatch.setattr(github_api, "urlopen", open_request)
    api = GitHubApiClient("owner", "repo", "secret-token", "https://api.github.com")
    pulls = api.list_pull_requests(state="open", base="devel", head="main")

    assert len(pulls) == 101
    assert [request.get_method() for request in requests] == ["GET", "GET"]
    assert all(
        request.full_url.startswith("https://api.github.com/repos/owner/repo/pulls?")
        for request in requests
    )
    assert [
        parse_qs(urlparse(request.full_url).query)["page"] for request in requests
    ] == [["1"], ["2"]]
    query = parse_qs(urlparse(requests[0].full_url).query)
    assert query == {
        "state": ["open"],
        "base": ["devel"],
        "per_page": ["100"],
        "page": ["1"],
        "head": ["owner:main"],
    }
    assert all(
        request.get_header("Authorization") == "Bearer secret-token"
        for request in requests
    )
    assert all(
        request.get_header("X-github-api-version") == "2022-11-28"
        for request in requests
    )


def test_pull_creation_posts_expected_body_and_write_api_is_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []

    def open_request(request: Any, timeout: float) -> Response:
        requests.append(request)
        return Response(
            {"number": 8, "html_url": "https://github.com/owner/repo/pull/8"}
        )

    monkeypatch.setattr(github_api, "urlopen", open_request)
    assert not hasattr(GitHubApiClient, "create_pull_request")
    api = GitHubPullRequestApi("owner", "repo", "token", "https://api.github.com")
    result = api.create_pull_request(
        base="main", head="devel", title="title", body="body", draft=True
    )

    assert result["number"] == 8
    request = requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.github.com/repos/owner/repo/pulls"
    assert json.loads(request.data) == {
        "base": "main",
        "head": "devel",
        "title": "title",
        "body": "body",
        "draft": True,
    }


def test_pull_request_url_validation_covers_github_and_enterprise_hosts() -> None:
    assert (
        github_api.validate_pull_request_url(
            "https://github.com/owner/repo/pull/8",
            8,
            owner="owner",
            repo="repo",
            api_url="https://api.github.com",
        )
        == "https://github.com/owner/repo/pull/8"
    )
    assert (
        github_api.validate_pull_request_url(
            "https://ghe.example/owner/repo/pull/9",
            9,
            owner="owner",
            repo="repo",
            api_url="https://ghe.example/api/v3",
        )
        == "https://ghe.example/owner/repo/pull/9"
    )

    with pytest.raises(GitHubApiError, match="mismatched identity"):
        github_api.validate_pull_request_url(
            "https://github.com/fork/repo/pull/8",
            8,
            owner="owner",
            repo="repo",
            api_url="https://api.github.com",
        )


def test_malformed_lists_and_objects_fail_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = GitHubApiClient("owner", "repo", "token", "https://api.github.com")
    monkeypatch.setattr(api, "_get", lambda *_args, **_kwargs: {"items": []})
    with pytest.raises(GitHubApiError, match="invalid pull request list"):
        api.list_pull_requests(state="open", base="main")
    monkeypatch.setattr(api, "_get", lambda *_args, **_kwargs: [])
    with pytest.raises(GitHubApiError, match="invalid pull request object"):
        api.pull_request(1)


def test_api_failure_and_timeout_do_not_expose_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "secret-token-sentinel"
    api = GitHubApiClient("owner", "repo", token, "https://api.github.com")

    def denied(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            "https://api.github.com/repos/owner/repo/pulls", 403, "Forbidden", {}, None
        )

    monkeypatch.setattr(github_api, "urlopen", denied)
    with pytest.raises(GitHubApiError, match="HTTP 403") as error:
        api.list_pull_requests(state="open", base="main")
    assert token not in str(error.value)

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise URLError(TimeoutError())

    monkeypatch.setattr(github_api, "urlopen", timeout)
    with pytest.raises(GitHubApiError, match="URLError"):
        api.pull_request(1)


def test_interrupted_response_body_is_normalized_without_partial_data_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "secret-token-sentinel"
    api = GitHubApiClient("owner", "repo", token, "https://api.github.com")

    class InterruptedBody(Response):
        def read(self) -> bytes:
            raise IncompleteRead(b"partial-secret-response", 100)

    monkeypatch.setattr(
        github_api, "urlopen", lambda *_args, **_kwargs: InterruptedBody({})
    )
    with pytest.raises(GitHubApiError, match="request failed.*IncompleteRead") as error:
        api.pull_request(42)

    assert token not in str(error.value)
    assert "partial-secret-response" not in str(error.value)
    assert "(GET)" in str(error.value)
    assert "/repos/owner/repo/pulls/42" in str(error.value)


def test_disconnected_response_is_normalized_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "secret-token-sentinel"
    api = GitHubApiClient("owner", "repo", token, "https://api.github.com")

    def disconnected(*_args: object, **_kwargs: object) -> None:
        raise RemoteDisconnected("peer disconnected")

    monkeypatch.setattr(github_api, "urlopen", disconnected)
    with pytest.raises(
        GitHubApiError, match="request failed.*RemoteDisconnected"
    ) as error:
        api.pull_request(42)

    assert token not in str(error.value)
    assert "peer disconnected" not in str(error.value)
    assert "(GET)" in str(error.value)


def test_client_uses_existing_actions_and_local_cli_token_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert github_api.client_from_env().token == "gh-token"
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    assert github_api.client_from_env().token == "actions-token"
    monkeypatch.setenv("GITHUB_REPOSITORY", "invalid")
    with pytest.raises(GitHubApiError, match="GITHUB_REPOSITORY"):
        github_api.client_from_env()
