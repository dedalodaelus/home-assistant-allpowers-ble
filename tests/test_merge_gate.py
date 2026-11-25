"""Behavioral tests for the Python policy module used by Merge gate."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from scripts import merge_gate, promotion_metadata
from scripts.merge_gate import (
    GateError,
    REQUIRED_CHECKS,
    REQUIRED_EVIDENCE_IDS,
    REQUIRED_WORKFLOWS,
    RestGitHubApi,
    run_gate,
)

BASE_SHA = "base-current"
HEAD_SHA = "head-current"
TESTED_SHA = "merge-current"
PR_NUMBER = 396
OWNER = "owner"
REPO = "repo"


def _heading(version: str) -> str:
    return f"## [{version}](https://example.test/compare/previous...{version})"


class FakeApi:
    def __init__(
        self,
        *,
        base_ref: str = "devel",
        head_ref: str = "main",
        base_sha: str = BASE_SHA,
        head_sha: str = HEAD_SHA,
        base_version: str = "1.0.0",
        head_version: str = "1.2.0",
        base_repo: str | None = f"{OWNER}/{REPO}",
        head_repo: str | None = f"{OWNER}/{REPO}",
        files: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_ref = base_ref
        self.head_ref = head_ref
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.base_version = base_version
        self.head_version = head_version
        self.base_repo = base_repo
        self.head_repo = head_repo
        self.files = files or []
        self.runs = self._success_runs()
        self.contents = self._metadata_contents()
        self.current_pr_overrides: dict[str, Any] = {}
        self.run_pages: list[list[dict[str, Any]]] | None = None

    def _metadata_contents(self) -> dict[tuple[str, str], str]:
        base_changelog = f"# Changelog\n\n{_heading(self.base_version)}\n"
        head_changelog = (
            f"# Changelog\n\n{_heading(self.head_version)}\n"
            f"\n{_heading(self.base_version)}\n"
        )
        return {
            (
                self.base_sha,
                "custom_components/allpowers_ble/manifest.json",
            ): json.dumps({"version": self.base_version}),
            (
                self.head_sha,
                "custom_components/allpowers_ble/manifest.json",
            ): json.dumps({"version": self.head_version}),
            (self.base_sha, ".release-please-manifest.json"): json.dumps(
                {".": self.base_version}
            ),
            (self.head_sha, ".release-please-manifest.json"): json.dumps(
                {".": self.head_version}
            ),
            (self.base_sha, "CHANGELOG.md"): base_changelog,
            (self.head_sha, "CHANGELOG.md"): head_changelog,
        }

    def _success_runs(self) -> list[dict[str, Any]]:
        values = []
        self.workflow_runs = {}
        for index, name in enumerate(REQUIRED_CHECKS, start=10):
            workflow_id = index + 1000
            association = {
                "number": PR_NUMBER,
                "base": {"sha": self.base_sha},
                "head": {"sha": self.head_sha},
            }
            self.workflow_runs[workflow_id] = {
                "event": "pull_request",
                "name": REQUIRED_WORKFLOWS[name],
                "run_attempt": 1,
                "head_sha": self.head_sha,
                "display_title": (
                    f"evidence w={REQUIRED_EVIDENCE_IDS[name][0]} "
                    f"j={REQUIRED_EVIDENCE_IDS[name][1]} pr={PR_NUMBER} "
                    f"r={OWNER}/{REPO} b={self.base_sha} h={self.head_sha} "
                    f"s={TESTED_SHA} a=1"
                ),
            }
            values.append(
                {
                    "id": index,
                    "name": name,
                    "head_sha": self.head_sha,
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [association],
                    "details_url": (
                        f"https://github.com/{OWNER}/{REPO}/actions/runs/"
                        f"{workflow_id}/job/1"
                    ),
                }
            )
        return values

    def pull_request(self, number: int) -> dict[str, Any]:
        assert number == PR_NUMBER
        value = {
            "number": number,
            "merge_commit_sha": TESTED_SHA,
            "title": "chore(ci): synchronize main into devel",
            "draft": False,
            "user": {"login": "maintainer", "type": "User"},
            "base": {
                "ref": self.base_ref,
                "sha": self.base_sha,
                "repo": {"full_name": self.base_repo},
            },
            "head": {
                "ref": self.head_ref,
                "sha": self.head_sha,
                "repo": {"full_name": self.head_repo},
            },
        }
        value.update(self.current_pr_overrides)
        return value

    def pull_request_files(self, number: int) -> list[dict[str, Any]]:
        assert number == PR_NUMBER
        return self.files

    def compare(self, base: str, head: str) -> dict[str, Any]:
        assert (base, head) == ("main", self.head_sha)
        return {"status": "ahead"}

    def content(
        self, path: str, ref: str, repository_full_name: str | None = None
    ) -> str:
        try:
            return self.contents[(ref, path)]
        except KeyError as error:
            raise GateError(f"GitHub returned no file at {ref}:{path}") from error

    def check_runs(self, ref: str) -> list[dict[str, Any]]:
        assert ref == self.head_sha
        if self.run_pages is not None:
            return (
                self.run_pages.pop(0) if len(self.run_pages) > 1 else self.run_pages[0]
            )
        return self.runs

    def workflow_run(self, run_id: int) -> dict[str, Any]:
        return self.workflow_runs[run_id]


def _event(api: FakeApi, *, action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": api.pull_request(PR_NUMBER),
        "changes": {"title": {"from": "old", "to": "new"}}
        if action == "edited"
        else {},
    }


def _run(api: FakeApi, *, action: str = "opened") -> list[str]:
    messages: list[str] = []
    run_gate(_event(api, action=action), api, OWNER, REPO, log=messages.append)
    return messages


def test_same_repository_sync_accepts_new_release_metadata_and_checks() -> None:
    api = FakeApi(files=[{"filename": "CHANGELOG.md"}])

    messages = _run(api)

    assert messages[-1] == "All required checks completed successfully."


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda api: api.contents.__setitem__(
                (HEAD_SHA, "custom_components/allpowers_ble/manifest.json"),
                json.dumps({"version": "0.9.0"}),
            ),
            "Integration version regressed",
        ),
        (
            lambda api: api.contents.__setitem__(
                (HEAD_SHA, ".release-please-manifest.json"), json.dumps({})
            ),
            "removed package history",
        ),
        (
            lambda api: api.contents.__setitem__(
                (HEAD_SHA, ".release-please-manifest.json"),
                json.dumps({".": "not-a-version"}),
            ),
            "Invalid semantic version",
        ),
    ],
)
def test_sync_rejects_release_metadata_regressions_and_malformed_versions(
    mutate: Any, message: str
) -> None:
    api = FakeApi(files=[{"filename": "CHANGELOG.md"}])
    mutate(api)

    with pytest.raises(GateError, match=message):
        _run(api)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            "custom_components/allpowers_ble/manifest.json",
            "{}",
            "string versions",
        ),
        (
            "custom_components/allpowers_ble/manifest.json",
            json.dumps({"version": 12}),
            "string versions",
        ),
        (
            "custom_components/allpowers_ble/manifest.json",
            "[]",
            "JSON objects",
        ),
        (".release-please-manifest.json", "[]", "version objects"),
        (
            ".release-please-manifest.json",
            json.dumps({".": None}),
            "versions must be strings",
        ),
    ],
)
def test_sync_rejects_missing_non_string_or_malformed_metadata(
    path: str, value: str, message: str
) -> None:
    api = FakeApi(files=[{"filename": "CHANGELOG.md"}])
    api.contents[(HEAD_SHA, path)] = value

    with pytest.raises(GateError, match=message):
        _run(api)


def test_sync_rejects_missing_manifest_content() -> None:
    api = FakeApi(files=[{"filename": "CHANGELOG.md"}])
    api.contents.pop((HEAD_SHA, "custom_components/allpowers_ble/manifest.json"))

    with pytest.raises(GateError, match="no file at head-current"):
        _run(api)


def test_release_precedence_handles_prerelease_and_rejects_leading_zero() -> None:
    assert promotion_metadata.semver_key("1.2.3-rc.2") < promotion_metadata.semver_key(
        "1.2.3-rc.10"
    )
    assert promotion_metadata.semver_key("1.2.3-rc.10") < promotion_metadata.semver_key(
        "1.2.3"
    )
    api = FakeApi(
        base_version="1.2.3-rc.2",
        head_version="1.2.3",
        files=[{"filename": "CHANGELOG.md"}],
    )
    assert _run(api)[-1] == "All required checks completed successfully."

    invalid = FakeApi(files=[{"filename": "CHANGELOG.md"}])
    invalid.contents[(HEAD_SHA, ".release-please-manifest.json")] = json.dumps(
        {".": "1.2.3-01"}
    )
    with pytest.raises(GateError, match="Invalid semantic version"):
        _run(invalid)


def test_sync_rejects_removed_historical_changelog_heading() -> None:
    api = FakeApi(files=[{"filename": "CHANGELOG.md"}])
    api.contents[(BASE_SHA, "CHANGELOG.md")] = (
        "# Changelog\n\n" + _heading("1.0.0") + "\n\n" + _heading("0.9.0")
    )
    api.contents[(HEAD_SHA, "CHANGELOG.md")] = (
        "# Changelog\n\n" + _heading("1.2.0") + "\n\n" + _heading("1.0.0")
    )

    with pytest.raises(GateError, match="Changelog history was removed for 0.9.0"):
        _run(api)


def test_promotion_rejects_release_metadata_file_changes() -> None:
    api = FakeApi(
        base_ref="main",
        head_ref="devel",
        base_version="1.2.0",
        head_version="1.2.0",
        files=[{"filename": ".release-please-manifest.json"}],
    )

    with pytest.raises(
        GateError,
        match="is managed",
    ):
        _run(api)


def test_promotion_rejects_integration_version_changes() -> None:
    api = FakeApi(
        base_ref="main",
        head_ref="devel",
        base_version="1.2.0",
        head_version="1.3.0",
        files=[{"filename": "README.md"}],
    )

    with pytest.raises(
        GateError, match="Promotion must preserve the integration version"
    ):
        _run(api)


def test_promotion_with_unchanged_release_metadata_is_allowed() -> None:
    api = FakeApi(
        base_ref="main",
        head_ref="devel",
        base_version="1.2.0",
        head_version="1.2.0",
        files=[{"filename": "README.md"}],
    )
    api.contents[(HEAD_SHA, "CHANGELOG.md")] = api.contents[(BASE_SHA, "CHANGELOG.md")]

    assert _run(api)[-1] == "All required checks completed successfully."


def test_ordinary_feature_cannot_update_release_manifest_version() -> None:
    api = FakeApi(
        head_ref="feature/release-bump",
        head_version="1.1.0",
        files=[{"filename": "custom_components/allpowers_ble/manifest.json"}],
    )

    with pytest.raises(GateError, match="must preserve the integration version"):
        _run(api)


@pytest.mark.parametrize(
    "file_change",
    [
        {
            "filename": "custom_components/allpowers_ble/manifest.json",
            "status": "removed",
        },
        {
            "filename": "docs/manifest.json",
            "previous_filename": "custom_components/allpowers_ble/manifest.json",
            "status": "renamed",
        },
    ],
)
def test_feature_pr_cannot_delete_or_rename_integration_manifest(
    file_change: dict[str, Any],
) -> None:
    api = FakeApi(head_ref="feature/manifest", files=[file_change])

    with pytest.raises(GateError, match="deleted or renamed"):
        _run(api)


def test_fork_version_preserving_manifest_change_is_validated() -> None:
    api = FakeApi(
        head_ref="feature/manifest",
        head_repo="fork/repo",
        head_version="1.0.0",
        files=[{"filename": "custom_components/allpowers_ble/manifest.json"}],
    )

    assert _run(api)[-1] == "All required checks completed successfully."


def test_fork_cannot_use_manifest_change_to_bump_release_version() -> None:
    api = FakeApi(
        head_ref="feature/manifest",
        head_repo="fork/repo",
        files=[{"filename": "custom_components/allpowers_ble/manifest.json"}],
    )
    api.contents[(HEAD_SHA, "custom_components/allpowers_ble/manifest.json")] = (
        json.dumps({"version": "9.9.9"})
    )
    with pytest.raises(GateError, match="must preserve the integration version"):
        _run(api)


def test_fork_named_main_cannot_use_sync_release_exception() -> None:
    api = FakeApi(
        head_ref="main",
        head_repo="fork/repo",
        files=[{"filename": "custom_components/allpowers_ble/manifest.json"}],
    )
    with pytest.raises(GateError, match="must preserve the integration version"):
        _run(api)


@pytest.mark.parametrize(
    "file_change",
    [
        {"filename": "CHANGELOG.md", "status": "removed"},
        {
            "filename": "docs/renamed-changelog.md",
            "previous_filename": "CHANGELOG.md",
            "status": "renamed",
        },
        {"filename": ".release-please-manifest.json", "status": "removed"},
        {
            "filename": "docs/release-state.json",
            "previous_filename": ".release-please-manifest.json",
            "status": "renamed",
        },
    ],
)
def test_promotion_rejects_deleted_or_renamed_release_files(
    file_change: dict[str, Any],
) -> None:
    api = FakeApi(
        base_ref="main",
        head_ref="devel",
        base_version="1.2.0",
        head_version="1.2.0",
        files=[file_change],
    )

    with pytest.raises(
        GateError,
        match="is managed|was deleted or renamed",
    ):
        _run(api)


def test_preflight_and_merge_gate_share_release_metadata_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_allow_newer: list[bool] = []
    original = promotion_metadata.validate_gate_release_metadata

    def shared_policy(**kwargs: Any) -> None:
        observed_allow_newer.append(kwargs["allow_newer"])
        original(**kwargs)

    monkeypatch.setattr(
        promotion_metadata, "validate_gate_release_metadata", shared_policy
    )
    monkeypatch.setattr(merge_gate, "validate_gate_release_metadata", shared_policy)
    git_files = {
        ("base", promotion_metadata.INTEGRATION_MANIFEST): json.dumps(
            {"version": "1.2.0"}
        ),
        ("head", promotion_metadata.INTEGRATION_MANIFEST): json.dumps(
            {"version": "1.2.0"}
        ),
        ("base", promotion_metadata.RELEASE_MANIFEST): json.dumps({".": "1.2.0"}),
        ("head", promotion_metadata.RELEASE_MANIFEST): json.dumps({".": "1.2.0"}),
        ("base", promotion_metadata.CHANGELOG): f"# Changelog\n\n{_heading('1.2.0')}\n",
        ("head", promotion_metadata.CHANGELOG): f"# Changelog\n\n{_heading('1.2.0')}\n",
    }
    monkeypatch.setattr(promotion_metadata, "_changed_paths", lambda *_args: set())
    monkeypatch.setattr(
        promotion_metadata,
        "_git_text",
        lambda revision, path: git_files[(revision, path)],
    )

    promotion_metadata.validate_revisions("base", "head", "main", "devel")
    _run(FakeApi(files=[{"filename": "CHANGELOG.md"}]))

    assert observed_allow_newer == [False, True]


@pytest.mark.parametrize(
    ("base_repo", "head_repo"),
    [("owner/repo", "fork/repo"), (None, "owner/repo"), ("owner/repo", None)],
)
def test_fork_or_missing_repo_identity_cannot_use_sync_exception(
    base_repo: str | None, head_repo: str | None
) -> None:
    api = FakeApi(
        base_repo=base_repo,
        head_repo=head_repo,
        files=[{"filename": "CHANGELOG.md"}],
    )

    with pytest.raises(GateError, match="CHANGELOG.md is managed"):
        _run(api)


def test_feature_pr_keeps_normal_changelog_policy() -> None:
    api = FakeApi(
        head_ref="feature/docs",
        files=[{"filename": "README.md"}],
    )

    assert _run(api)[-1] == "All required checks completed successfully."


@pytest.mark.parametrize("filename", ["CHANGELOG.md", ".release-please-manifest.json"])
def test_feature_pr_cannot_edit_release_please_files(filename: str) -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": filename}])

    with pytest.raises(GateError, match="managed by Release Please"):
        _run(api)


def test_main_branch_policy_restricts_sources_and_release_bot_identity() -> None:
    feature = FakeApi(base_ref="main", head_ref="feature/unrelated")
    with pytest.raises(GateError, match="must come from 'devel'"):
        _run(feature)

    release_pr = FakeApi(base_ref="main", head_ref="release-please--1")
    event = _event(release_pr)
    event["pull_request"]["user"] = {"login": "ordinary-user", "type": "User"}
    with pytest.raises(GateError, match="approved bot"):
        run_gate(
            event,
            release_pr,
            OWNER,
            REPO,
            log=lambda _message: None,
        )


def test_approved_release_please_bot_and_hotfix_ancestry_are_preserved() -> None:
    release_pr = FakeApi(
        base_ref="main",
        head_ref="release-please--1",
        base_version="1.2.0",
        head_version="1.3.0",
        files=[
            {"filename": "CHANGELOG.md"},
            {"filename": ".release-please-manifest.json"},
            {"filename": "custom_components/allpowers_ble/manifest.json"},
        ],
    )
    release_event = _event(release_pr)
    release_event["pull_request"]["user"] = {
        "login": "release-please[bot]",
        "type": "Bot",
    }
    messages: list[str] = []
    run_gate(release_event, release_pr, OWNER, REPO, log=messages.append)
    assert messages[-1] == "All required checks completed successfully."

    hotfix = FakeApi(
        base_ref="main",
        head_ref="hotfix/security-fix",
        files=[{"filename": "README.md"}],
    )
    assert _run(hotfix)[-1] == "All required checks completed successfully."
    hotfix.compare = lambda *_args: {"status": "behind"}  # type: ignore[method-assign]
    with pytest.raises(GateError, match="must be based on 'main'"):
        _run(hotfix)


def test_mutable_associations_cannot_substitute_for_run_title_evidence() -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.runs[0]["pull_requests"] = []
    api.workflow_runs[1010]["pull_requests"] = []
    api.workflow_runs[1010]["display_title"] = "old run with rewritten associations"
    clock = FakeClock()

    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            log=lambda _message: None,
            now=clock.now,
            sleep=clock.sleep_to_missing_grace,
        )


@pytest.mark.parametrize(
    "details_url",
    [
        None,
        "not a url",
        "https://attacker.example/owner/repo/actions/runs/1010",
        "https://github.com/another/repo/actions/runs/1010",
        "https://github.com/owner/repo/actions/runs/",
        "https://github.com/owner/repo/actions/runs/not-a-number",
    ],
)
def test_invalid_or_mismatched_workflow_url_never_satisfies_gate(
    details_url: str | None,
) -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.runs[0]["details_url"] = details_url
    clock = FakeClock()

    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            log=lambda _message: None,
            now=clock.now,
            sleep=clock.sleep_to_missing_grace,
        )


def test_missing_immutable_run_evidence_fails_closed() -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.workflow_runs[1010]["pull_requests"] = []
    api.workflow_runs[1010]["display_title"] = "rewritten associations are not evidence"
    clock = FakeClock()

    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            log=lambda _message: None,
            now=clock.now,
            sleep=clock.sleep_to_missing_grace,
        )


def test_rewritten_associations_do_not_make_old_revision_evidence_current() -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.base_sha = "advanced-base"
    rewritten = [
        {
            "number": PR_NUMBER,
            "base": {"sha": api.base_sha},
            "head": {"sha": api.head_sha},
        }
    ]
    for check in api.runs:
        check["pull_requests"] = rewritten
    for workflow in api.workflow_runs.values():
        workflow["pull_requests"] = rewritten
    clock = FakeClock()
    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            log=lambda _message: None,
            now=clock.now,
            sleep=clock.sleep_to_missing_grace,
        )


def test_fork_pr_with_empty_associations_uses_bound_run_evidence() -> None:
    api = FakeApi(
        head_ref="feature/docs",
        files=[{"filename": "README.md"}],
        head_repo="fork/repo",
    )
    for check in api.runs:
        check["pull_requests"] = []
    for workflow in api.workflow_runs.values():
        workflow["pull_requests"] = []
    assert _run(api)[-1] == "All required checks completed successfully."

    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.workflow_runs[1010]["pull_requests"] = [
        {
            "number": PR_NUMBER,
            "base": {"sha": "older-base"},
            "head": {"sha": HEAD_SHA},
        }
    ]
    api.workflow_runs[1010]["display_title"] = "rewritten associations are not evidence"
    clock = FakeClock()
    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            log=lambda _message: None,
            now=clock.now,
            sleep=clock.sleep_to_missing_grace,
        )


def test_api_failure_and_malformed_check_response_fail_closed() -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.runs = ["not a check run"]  # type: ignore[list-item]
    with pytest.raises(GateError, match="malformed check-run entry"):
        _run(api)

    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.workflow_run = lambda _run_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
        GateError("GitHub API permission denied")
    )
    with pytest.raises(GateError, match="permission denied"):
        _run(api)

    api = FakeApi(head_ref="feature/docs", files=[{"filename": 42}])
    with pytest.raises(GateError, match="malformed pull request file list"):
        _run(api)

    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.runs[0]["id"] = "10"
    with pytest.raises(GateError, match="missing or invalid ID"):
        _run(api)

    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.runs[0]["status"] = "mystery"
    with pytest.raises(GateError, match="malformed status"):
        _run(api)


def test_rest_api_paginates_check_runs_and_uses_read_only_get_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {"check_runs": [{"id": index} for index in range(100)]},
        {"check_runs": [{"id": 100}]},
    ]
    requests: list[Any] = []

    class Response:
        headers: dict[str, str] = {}

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode()

    def fake_urlopen(request: Any, timeout: int) -> Response:
        assert timeout == 30
        requests.append(request)
        return Response(responses.pop(0))

    monkeypatch.setattr(merge_gate, "urlopen", fake_urlopen)
    api = RestGitHubApi(OWNER, REPO, "read-only-test-token", "https://api.github.com")

    runs = api.check_runs(HEAD_SHA)

    assert len(runs) == 101
    assert [
        parse_qs(urlparse(request.full_url).query)["page"] for request in requests
    ] == [
        ["1"],
        ["2"],
    ]
    assert all(request.get_method() == "GET" for request in requests)
    assert all(
        request.get_header("Authorization") == "Bearer read-only-test-token"
        for request in requests
    )
    assert all(
        parse_qs(urlparse(request.full_url).query)["filter"] == ["all"]
        for request in requests
    )


def test_rest_api_paginates_pull_request_files(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RestGitHubApi(OWNER, REPO, "read-only-test-token", "https://api.github.com")
    pages = [
        [{"filename": f"file-{index}.py"} for index in range(100)],
        [{"filename": "tail.py"}],
    ]
    calls: list[dict[str, int]] = []

    def get_page(_path: str, **query: int) -> list[dict[str, str]]:
        calls.append(query)
        return pages.pop(0)

    monkeypatch.setattr(api, "_get", get_page)

    files = api.pull_request_files(PR_NUMBER)

    assert len(files) == 101
    assert [call["page"] for call in calls] == [1, 2]


def test_rest_api_malformed_response_and_permission_failure_are_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        headers: dict[str, str] = {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"not_check_runs": []}'

    monkeypatch.setattr(merge_gate, "urlopen", lambda *_args, **_kwargs: Response())
    api = RestGitHubApi(OWNER, REPO, "secret-token-sentinel", "https://api.github.com")
    with pytest.raises(GateError, match="Unexpected GitHub API response") as error:
        api.check_runs(HEAD_SHA)
    assert "secret-token-sentinel" not in str(error.value)

    def denied(request: Any, timeout: int) -> Any:
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(merge_gate, "urlopen", denied)
    with pytest.raises(GateError, match="GitHub API request failed") as error:
        api.check_runs(HEAD_SHA)
    assert "secret-token-sentinel" not in str(error.value)

    class InvalidJsonResponse(Response):
        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr(
        merge_gate,
        "urlopen",
        lambda *_args, **_kwargs: InvalidJsonResponse(),
    )
    with pytest.raises(GateError, match="GitHub API request failed"):
        api.check_runs(HEAD_SHA)


@pytest.mark.parametrize(
    ("encoding", "content", "message"),
    [
        ("none", "IyBDaGFuZ2Vsb2c=", "unsupported content encoding"),
        ("base64", "%%%", "Could not decode"),
        ("base64", "IyBDaGFuZ2Vsb2c", "Could not decode"),
    ],
)
def test_rest_api_rejects_unsupported_or_malformed_file_content(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    content: str,
    message: str,
) -> None:
    class Response:
        headers: dict[str, str] = {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"type": "file", "encoding": encoding, "content": content}
            ).encode()

    monkeypatch.setattr(merge_gate, "urlopen", lambda *_args, **_kwargs: Response())
    api = RestGitHubApi(OWNER, REPO, "read-only-test-token", "https://api.github.com")

    with pytest.raises(GateError, match=message):
        api.content("CHANGELOG.md", HEAD_SHA)


def test_rest_api_content_decodes_line_wrapped_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "# Changelog\n\n## [1.2.0]\n"
    encoded = base64.b64encode(expected.encode()).decode()
    content = encoded[:12] + "\n" + encoded[12:]

    class Response:
        headers: dict[str, str] = {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"type": "file", "encoding": "base64", "content": content}
            ).encode()

    monkeypatch.setattr(merge_gate, "urlopen", lambda *_args, **_kwargs: Response())
    api = RestGitHubApi(OWNER, REPO, "read-only-test-token", "https://api.github.com")

    assert api.content("CHANGELOG.md", HEAD_SHA) == expected


def test_rest_api_reads_fork_manifest_from_declared_head_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = RestGitHubApi(OWNER, REPO, "read-only-test-token", "https://api.github.com")
    calls: list[tuple[str, dict[str, str | int]]] = []

    def get(path: str, **query: str | int) -> dict[str, str]:
        calls.append((path, query))
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(b'{"version":"1.0.0"}').decode(),
        }

    monkeypatch.setattr(api, "_get", get)

    assert (
        api.content(
            "custom_components/allpowers_ble/manifest.json", HEAD_SHA, "fork/repo"
        )
        == '{"version":"1.0.0"}'
    )
    assert calls == [
        (
            "/repos/fork/repo/contents/custom_components/allpowers_ble/manifest.json",
            {"ref": HEAD_SHA},
        )
    ]


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("in_progress", None),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "timed_out"),
    ],
)
def test_newer_pending_or_failed_current_run_cannot_be_hidden(
    status: str, conclusion: str | None
) -> None:
    api = FakeApi()
    old = api.runs[0]
    newer = {
        **old,
        "id": old["id"] + 500,
        "status": status,
        "conclusion": conclusion,
        "details_url": "https://github.com/owner/repo/actions/runs/9999/job/1",
    }
    api.runs = [old, newer]
    api.workflow_runs[9999] = api.workflow_runs[old["id"] + 1000]

    if conclusion is not None:
        with pytest.raises(GateError, match="Required checks failed"):
            _run(api)
    else:
        sleeps: list[float] = []
        ticks = iter([0.0, 0.0, 1801.0])
        with pytest.raises(GateError, match="Timeout waiting"):
            run_gate(
                _event(api),
                api,
                OWNER,
                REPO,
                log=lambda _message: None,
                now=lambda: next(ticks),
                sleep=sleeps.append,
            )
        assert sleeps == [15]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def sleep_to_missing_grace(self, _seconds: float) -> None:
        self.value = 601.0

    def sleep_to_timeout(self, _seconds: float) -> None:
        self.value = 1801.0


@pytest.mark.parametrize("first_runs", [[], "pending"])
def test_missing_or_pending_checks_can_finish_successfully(first_runs: Any) -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    if first_runs == "pending":
        api.run_pages = [[{**api.runs[0], "status": "in_progress"}], api.runs]
    else:
        api.run_pages = [first_runs, api.runs]
    clock = FakeClock()
    messages: list[str] = []

    run_gate(
        _event(api),
        api,
        OWNER,
        REPO,
        log=messages.append,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert messages[-1] == "All required checks completed successfully."


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_accepted_terminal_conclusions_remain_allowed(conclusion: str) -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    api.runs = [{**run, "conclusion": conclusion} for run in api.runs]

    assert _run(api)[-1] == "All required checks completed successfully."


def test_green_checks_from_push_event_do_not_satisfy_gate() -> None:
    api = FakeApi()
    api.workflow_runs[1010]["event"] = "push"
    ticks = iter([0.0, 0.0, 601.0])

    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            log=lambda _message: None,
            now=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )


def test_gate_entry_point_reads_event_file_and_reports_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event(api)), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", f"{OWNER}/{REPO}")
    monkeypatch.setenv("GITHUB_TOKEN", "entrypoint-token-sentinel")
    monkeypatch.setattr(merge_gate, "RestGitHubApi", lambda *_args: api)

    assert merge_gate.main() == 0

    api.check_runs = lambda _ref: (_ for _ in ()).throw(  # type: ignore[method-assign]
        GateError("GitHub API check request denied")
    )
    assert merge_gate.main() == 1
    captured = capsys.readouterr()
    assert "GitHub API check request denied" in captured.err
    assert "entrypoint-token-sentinel" not in captured.out + captured.err


def test_gate_rejects_boolean_pr_number() -> None:
    api = FakeApi()
    event = _event(api)
    event["pull_request"]["number"] = True

    with pytest.raises(GateError, match="missing its number"):
        run_gate(event, api, OWNER, REPO, log=lambda _message: None)


@pytest.mark.parametrize(
    "changed",
    [
        "title",
        "base_ref",
        "head_ref",
        "base_sha",
        "head_sha",
        "base_repo",
        "head_repo",
    ],
)
def test_pr_metadata_changes_during_polling_prevent_success(changed: str) -> None:
    api = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    original = api.pull_request
    calls = 0

    def mutate_on_final_read(number: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        value = original(number)
        if calls != 2:
            return value
        if changed == "title":
            value["title"] = "docs: updated title"
        elif changed == "base_ref":
            value["base"]["ref"] = "main"
        elif changed == "head_ref":
            value["head"]["ref"] = "renamed-feature"
        elif changed == "base_sha":
            value["base"]["sha"] = "new-base-sha"
        elif changed == "head_sha":
            value["head"]["sha"] = "new-head-sha"
        elif changed == "base_repo":
            value["base"]["repo"]["full_name"] = "other/repo"
        else:
            value["head"]["repo"]["full_name"] = "fork/repo"
        return value

    api.pull_request = mutate_on_final_read  # type: ignore[method-assign]

    with pytest.raises(GateError, match="repository identity|title, refs"):
        _run(api)


def test_draft_still_fails_policy_but_skips_check_polling_when_valid() -> None:
    invalid = FakeApi(
        base_ref="main",
        head_ref="feature/unrelated",
        files=[{"filename": "README.md"}],
    )
    event = _event(invalid)
    event["pull_request"]["draft"] = True
    with pytest.raises(GateError, match="must come from 'devel'"):
        run_gate(event, invalid, OWNER, REPO, log=lambda _message: None)

    valid = FakeApi(head_ref="feature/docs", files=[{"filename": "README.md"}])
    valid.runs = []
    event = _event(valid)
    event["pull_request"]["draft"] = True
    messages: list[str] = []
    run_gate(event, valid, OWNER, REPO, log=messages.append)
    assert messages == [
        "Draft PR detected. Skipping strict gate until ready for review."
    ]


def test_hil_gate_is_conditional_on_main_and_configuration() -> None:
    api = FakeApi(
        base_ref="main",
        head_ref="devel",
        base_version="1.2.0",
        head_version="1.2.0",
        files=[{"filename": "README.md"}],
    )
    api.contents[(HEAD_SHA, "CHANGELOG.md")] = api.contents[(BASE_SHA, "CHANGELOG.md")]
    assert _run(api)[-1] == "All required checks completed successfully."

    clock = FakeClock()
    with pytest.raises(GateError, match="Missing required checks after 10 minutes"):
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            require_hil_stable=True,
            log=lambda _message: None,
            now=clock.now,
            sleep=clock.sleep_to_missing_grace,
        )

    hil_id = 999
    association = {
        "number": PR_NUMBER,
        "base": {"sha": BASE_SHA},
        "head": {"sha": HEAD_SHA},
    }
    api.workflow_runs[hil_id] = {
        "event": "pull_request",
        "name": REQUIRED_WORKFLOWS["HIL qualification (opt-in)"],
        "run_attempt": 1,
        "head_sha": api.head_sha,
        "display_title": (
            f"evidence w=hil j=qualification pr={PR_NUMBER} r={OWNER}/{REPO} "
            f"b={api.base_sha} h={api.head_sha} s={TESTED_SHA} a=1"
        ),
    }
    api.runs.append(
        {
            "id": hil_id,
            "name": "HIL qualification (opt-in)",
            "head_sha": api.head_sha,
            "status": "completed",
            "conclusion": "success",
            "pull_requests": [association],
            "details_url": f"https://github.com/{OWNER}/{REPO}/actions/runs/{hil_id}",
        }
    )
    assert (
        run_gate(
            _event(api),
            api,
            OWNER,
            REPO,
            require_hil_stable=True,
            log=lambda _message: None,
        )
        is None
    )


def test_workflow_wiring_preserves_gate_contract_without_privileged_checkout() -> None:
    path = Path(".github/workflows/merge-gate.yml")
    source = path.read_text(encoding="utf-8")
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    assert workflow["on"]["pull_request"]["types"] == [
        "opened",
        "reopened",
        "synchronize",
        "ready_for_review",
        "edited",
    ]
    for name in (
        "ci",
        "hacs",
        "hassfest",
        "codeql",
        "conventional-commits",
        "dependency-review",
        "hil-qualification",
    ):
        evidence_workflow = yaml.load(
            Path(f".github/workflows/{name}.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        workflow_name = {
            "ci": "CI",
            "hacs": "HACS",
            "hassfest": "Hassfest",
            "codeql": "CodeQL",
            "conventional-commits": "Conventional pull request title",
            "dependency-review": "Dependency review",
            "hil-qualification": "HIL qualification",
        }[name]
        evidence_id = REQUIRED_EVIDENCE_IDS[
            next(
                check
                for check, value in REQUIRED_WORKFLOWS.items()
                if value == workflow_name
            )
        ]
        assert evidence_workflow["run-name"].startswith(
            f"evidence w={evidence_id[0]} j={evidence_id[1]} "
        )
        assert "${{ github.sha }}" in evidence_workflow["run-name"]
        assert "${{ github.run_attempt }}" in evidence_workflow["run-name"]
        if name != "hil-qualification":
            pr_config = evidence_workflow["on"]["pull_request"]
            types = (
                pr_config.get(
                    "types", ["opened", "reopened", "synchronize", "ready_for_review"]
                )
                if isinstance(pr_config, dict)
                else ["opened", "reopened", "synchronize", "ready_for_review"]
            )
            assert "reopened" in types
    job = workflow["jobs"]["gate"]
    assert job["name"] == "Gate all required checks"
    assert job["timeout-minutes"] == "50"
    assert job["permissions"] == {
        "contents": "read",
        "checks": "read",
        "pull-requests": "read",
        "actions": "read",
    }
    steps = job["steps"]
    title_step = next(
        step for step in steps if step.get("name") == "Validate edited PR title"
    )
    checkout_step = next(
        step for step in steps if step.get("name") == "Check out pull request revision"
    )
    setup_python_step = next(
        step for step in steps if step.get("name") == "Set up Python"
    )
    python_step = next(
        step
        for step in steps
        if step.get("name") == "Wait for required checks on PR head"
    )
    assert title_step["uses"].startswith("amannn/action-semantic-pull-request@")
    assert "github.event.changes.title != null" in title_step["if"]
    assert checkout_step["with"]["ref"] == "${{ github.sha }}"
    assert checkout_step["with"]["persist-credentials"] == "false"
    assert setup_python_step["with"]["python-version"] == "3.14"
    assert python_step["run"] == "python -m scripts.merge_gate"
    assert python_step["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert (
        steps.index(title_step)
        < steps.index(checkout_step)
        < steps.index(setup_python_step)
        < steps.index(python_step)
    )
    assert "pull_request_target" not in source
    assert "contents: write" not in source
    assert "pull-requests: write" not in source
    assert "actions/github-script" not in source

    ci = yaml.load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert any(
        step.get("name") == "Merge gate behavior tests"
        and step.get("run") == "pytest tests/test_merge_gate.py"
        for step in ci["jobs"]["quality"]["steps"]
    )


def test_title_edit_does_not_wait_for_old_title_check() -> None:
    api = FakeApi()
    api.runs = [run for run in api.runs if run["name"] != "Validate title"]

    messages = _run(api, action="edited")

    assert messages[-1] == "All required checks completed successfully."


@pytest.mark.parametrize("change", ["base_sha", "head_sha", "base_ref", "head_repo"])
def test_refs_moving_after_metadata_validation_fails_closed(change: str) -> None:
    api = FakeApi(files=[{"filename": "CHANGELOG.md"}])
    original = api.pull_request
    calls = 0

    def moved(number: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        value = original(number)
        if calls == 2:
            if change == "base_sha":
                value["base"]["sha"] = "new-base"
            elif change == "head_sha":
                value["head"]["sha"] = "new-head"
            elif change == "base_ref":
                value["base"]["ref"] = "main"
            else:
                value["head"]["repo"]["full_name"] = "fork/repo"
        return value

    api.pull_request = moved  # type: ignore[method-assign]

    with pytest.raises(
        GateError, match="PR refs moved during release metadata validation"
    ):
        _run(api)
