"""Tests for reviewed main-to-devel synchronization."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts import github_api, sync_main_to_devel
from scripts.git import GitExecutionError
from scripts.sync_main_to_devel import SyncError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _remote_shas(repo: Path) -> tuple[str, str]:
    output = _git(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/devel")
    refs = {line.split()[1]: line.split()[0] for line in output.splitlines()}
    return refs["refs/heads/main"], refs["refs/heads/devel"]


@pytest.fixture
def divergent_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Create real, divergent main/devel refs with independently changed files."""
    remote = tmp_path / "origin.git"
    repo = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Sync test")
    _git(repo, "config", "user.email", "sync@example.test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(remote))

    integration = repo / "custom_components/allpowers_ble/manifest.json"
    integration.parent.mkdir(parents=True)

    def manifest(version: str, *, development_category: bool = False) -> str:
        fields = ['  "domain": "allpowers_ble",']
        if development_category:
            fields.append('  "categories": ["battery"],')
        fields.extend(f'  "test_field_{index}": "value",' for index in range(12))
        fields.append(f'  "version": "{version}"')
        return "{\n" + "\n".join(fields) + "\n}\n"

    integration.write_text(manifest("1.1.0"), encoding="utf-8")
    (repo / ".release-please-manifest.json").write_text(
        '{".": "1.1.0"}\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.0.0](https://example.test/compare/0.9.0...1.0.0)\n",
        encoding="utf-8",
    )
    base_sha = _commit(repo, "chore: initialize release files")
    _git(repo, "push", "origin", "main")

    _git(repo, "switch", "-c", "devel")
    integration.write_text(
        manifest("1.1.0", development_category=True), encoding="utf-8"
    )
    (repo / "devel-only.txt").write_text("development work\n", encoding="utf-8")
    devel_sha = _commit(repo, "feat: add devel-only integration metadata")
    _git(repo, "push", "origin", "devel")

    _git(repo, "switch", "main")
    integration.write_text(manifest("1.2.0"), encoding="utf-8")
    (repo / ".release-please-manifest.json").write_text(
        '{".": "1.2.0"}\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [1.2.0](https://example.test/compare/1.1.0...1.2.0)\n\n"
        "## [1.0.0](https://example.test/compare/0.9.0...1.0.0)\n",
        encoding="utf-8",
    )
    (repo / "hotfix.txt").write_text("production fix\n", encoding="utf-8")
    main_sha = _commit(repo, "fix: preserve production hotfix")
    _git(repo, "push", "origin", "main")
    _git(repo, "switch", "devel")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    return {
        "repo": repo,
        "remote": remote,
        "base_sha": base_sha,
        "main_sha": main_sha,
        "devel_sha": devel_sha,
    }


@pytest.fixture
def ancestry_only_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Create independent commits with identical trees on main and devel."""
    remote = tmp_path / "ancestry-only.git"
    repo = tmp_path / "ancestry-only-checkout"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Sync test")
    _git(repo, "config", "user.email", "sync@example.test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "README.md").write_text("same tree\n", encoding="utf-8")
    base_sha = _commit(repo, "chore: common base")
    _git(repo, "push", "origin", "main")
    _git(repo, "commit", "--allow-empty", "-m", "chore: main ancestry marker")
    _git(repo, "push", "origin", "main")
    _git(repo, "switch", "-c", "devel", base_sha)
    _git(repo, "commit", "--allow-empty", "-m", "chore: devel ancestry marker")
    _git(repo, "push", "origin", "devel")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    main_sha = _git(repo, "rev-parse", "origin/main")
    devel_sha = _git(repo, "rev-parse", "origin/devel")
    return {"repo": repo, "main_sha": main_sha, "devel_sha": devel_sha}


def _pr(
    url: str, main_sha: str, devel_sha: str, *, mergeable: str = "MERGEABLE"
) -> dict[str, object]:
    is_conflict = mergeable == "CONFLICTING"
    return {
        "number": int(url.rstrip("/").split("/")[-1]),
        "html_url": url,
        "state": "open",
        "draft": False,
        "base": {"ref": "devel", "sha": devel_sha, "repo": {"full_name": "owner/repo"}},
        "head": {"ref": "main", "sha": main_sha, "repo": {"full_name": "owner/repo"}},
        "mergeable": False if is_conflict else True,
        "mergeable_state": "dirty" if is_conflict else "clean",
    }


class FakeRestApi:
    owner = "owner"
    repo = "repo"
    api_url = "https://api.github.com"

    def __init__(self) -> None:
        self.pulls: list[dict[str, object]] = []
        self.details: dict[int, dict[str, object]] = {}
        self.create_error: Exception | None = None
        self.list_error: Exception | None = None
        self.create_calls: list[dict[str, object]] = []
        self.list_calls = 0
        self.on_list: Any = None
        self.on_pull_request: Any = None
        self.create_response: dict[str, object] | None = None
        self.create_result: dict[str, object] | None = None
        self.create_url = "https://github.com/owner/repo/pull/42"
        self.viewed: list[tuple[str, str]] = []

    def list_pull_requests(self, **_kwargs: object) -> list[dict[str, object]]:
        self.list_calls += 1
        if self.list_error:
            raise self.list_error
        if self.on_list:
            self.on_list()
        return self.pulls

    def create_pull_request(self, **kwargs: object) -> dict[str, object]:
        self.create_calls.append(kwargs)
        if self.create_error:
            raise self.create_error
        number = int(self.create_url.rstrip("/").split("/")[-1])
        self.details[number] = self.create_response or _pr(
            self.create_url, "head", "base"
        )
        self.details[number].setdefault("draft", kwargs.get("draft", False))
        return self.create_result or {"number": number, "html_url": self.create_url}

    def pull_request(self, number: int) -> dict[str, object]:
        if self.on_pull_request:
            self.on_pull_request()
        pull = self.details[number]
        base, head = pull["base"], pull["head"]
        self.viewed.append((str(head["sha"]), str(base["sha"])))  # type: ignore[index]
        return pull


def _install_api(monkeypatch: pytest.MonkeyPatch, api: FakeRestApi) -> None:
    monkeypatch.setattr(sync_main_to_devel, "client_from_env", lambda **_kwargs: api)


def test_sync_pr_creation_has_correct_direction_and_traceable_refs(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/42"
    api = FakeRestApi()
    api.create_url = url
    api.create_response = _pr(
        url, divergent_repo["main_sha"], divergent_repo["devel_sha"]
    )
    _install_api(monkeypatch, api)
    result = sync_main_to_devel.prepare_sync()
    assert result.url == url
    assert result.main_sha == divergent_repo["main_sha"]
    assert result.devel_sha == divergent_repo["devel_sha"]

    assert len(api.create_calls) == 1
    create = api.create_calls[0]
    assert create["base"] == "devel"
    assert create["head"] == "main"
    assert create["title"] == "chore(ci): synchronize main into devel"
    body = str(create["body"])
    assert divergent_repo["main_sha"] in body
    assert divergent_repo["devel_sha"] in body
    assert "preserving both branch histories" in body
    assert "not CI or merge-validation results" in body


def test_real_merge_preserves_main_ancestry_release_data_and_devel_fields(
    divergent_repo: dict[str, Any],
) -> None:
    repo = divergent_repo["repo"]
    _git(repo, "merge", "--no-ff", "--no-edit", "origin/main")
    _git(repo, "push", "origin", "devel")

    assert sync_main_to_devel.is_ancestor(
        divergent_repo["main_sha"], _git(repo, "rev-parse", "HEAD")
    )
    manifest = json.loads(
        (repo / "custom_components/allpowers_ble/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["version"] == "1.2.0"
    assert manifest["categories"] == ["battery"]
    assert (
        json.loads((repo / ".release-please-manifest.json").read_text())["."] == "1.2.0"
    )
    changelog = (repo / "CHANGELOG.md").read_text()
    assert "## [1.2.0]" in changelog
    assert "## [1.0.0]" in changelog
    assert (repo / "hotfix.txt").read_text() == "production fix\n"
    assert (repo / "devel-only.txt").read_text() == "development work\n"


def test_main_ancestor_is_a_successful_noop(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = divergent_repo["repo"]
    _git(repo, "merge", "--no-ff", "--no-edit", "origin/main")
    _git(repo, "push", "origin", "devel")
    with patch.object(sync_main_to_devel, "open_sync_pr") as open_pr:
        assert sync_main_to_devel.prepare_sync().url is None
    open_pr.assert_not_called()


def test_reuses_only_same_repository_main_to_devel_pr(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/9"
    api = FakeRestApi()
    fork = _pr("https://github.com/owner/repo/pull/8", "fork-main", "devel")
    fork["head"]["repo"] = {"full_name": "fork/repo"}  # type: ignore[index]
    own = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    api.pulls = [fork, own]
    api.details[9] = own
    _install_api(monkeypatch, api)
    assert sync_main_to_devel.prepare_sync().url == url
    assert not api.create_calls


def test_open_sync_pr_rejects_incomplete_repository_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeRestApi()
    candidate = _pr("https://github.com/owner/repo/pull/16", "main-sha", "devel-sha")
    candidate["head"]["repo"] = None  # type: ignore[index]
    api.pulls = [candidate]
    _install_api(monkeypatch, api)

    with pytest.raises(SyncError, match="incomplete repository identity"):
        sync_main_to_devel.open_sync_pr()


def test_duplicate_creation_race_reuses_winning_sync_pr(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/10"
    api = FakeRestApi()
    api.create_url = url
    api.create_error = github_api.GitHubApiError("request timed out")
    pr = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    api.details[10] = pr
    api.on_list = lambda: setattr(api, "pulls", [pr]) if api.list_calls == 2 else None
    _install_api(monkeypatch, api)
    assert sync_main_to_devel.prepare_sync().url == url
    assert len(api.create_calls) == 1
    assert api.list_calls == 2


def test_pr_creation_permission_error_leaves_both_branch_heads_unchanged(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeRestApi()
    api.create_error = github_api.GitHubApiError("HTTP 403 permission denied")
    _install_api(monkeypatch, api)
    remote_before = _remote_shas(divergent_repo["repo"])
    with pytest.raises(SyncError, match="permission denied"):
        sync_main_to_devel.prepare_sync()
    assert api.list_calls == 2
    assert _remote_shas(divergent_repo["repo"]) == remote_before


def test_unrelated_or_wrong_direction_pr_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeRestApi()
    api.pulls = [
        {
            "html_url": "https://github.com/owner/repo/pull/3",
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
            "head": {"ref": "devel", "repo": {"full_name": "owner/repo"}},
        }
    ]
    _install_api(monkeypatch, api)
    assert sync_main_to_devel.open_sync_pr() is None


def test_conflicting_existing_pr_is_reported_without_resolution(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/11"
    api = FakeRestApi()
    pr = _pr(
        url,
        divergent_repo["main_sha"],
        divergent_repo["devel_sha"],
        mergeable="CONFLICTING",
    )
    api.pulls = [pr]
    api.details[11] = pr
    _install_api(monkeypatch, api)
    with pytest.raises(SyncError, match="merge conflicts"):
        sync_main_to_devel.prepare_sync()


def test_pr_ref_movement_fails_closed() -> None:
    with pytest.raises(SyncError, match="refs moved"):
        sync_main_to_devel.validate_current_pr(
            {"headRefOid": "new-main", "baseRefOid": "devel"},
            "old-main",
            "devel",
            url="https://github.com/owner/repo/pull/12",
        )


@pytest.mark.parametrize("changed_ref", ["head", "base"])
def test_prepare_sync_rejects_stale_pr_sha_from_detail(
    divergent_repo: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    changed_ref: str,
) -> None:
    repo = divergent_repo["repo"]
    url = "https://github.com/owner/repo/pull/17"
    api = FakeRestApi()
    listed = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    stale_detail = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    stale_detail[changed_ref]["sha"] = "stale-sha"  # type: ignore[index]
    api.pulls = [listed]
    api.details[17] = stale_detail
    _install_api(monkeypatch, api)
    remote_before = _remote_shas(repo)

    with pytest.raises(SyncError, match="refs moved"):
        sync_main_to_devel.prepare_sync()

    assert api.create_calls == []
    assert _remote_shas(repo) == remote_before


def test_prepare_sync_rejects_remote_ref_movement_during_inspection(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = divergent_repo["repo"]
    url = "https://github.com/owner/repo/pull/18"
    api = FakeRestApi()
    pr = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    api.pulls = [pr]
    api.details[18] = pr
    _install_api(monkeypatch, api)
    original_remote = _remote_shas(repo)
    advanced_main: list[str] = []

    def advance_main_once() -> None:
        if advanced_main:
            return
        _git(repo, "switch", "main")
        (repo / "hotfix-race.txt").write_text("intentional race\n", encoding="utf-8")
        advanced_main.append(_commit(repo, "fix: advance main during PR inspection"))
        _git(repo, "push", "origin", "main")
        _git(repo, "switch", "devel")

    api.on_pull_request = advance_main_once
    with pytest.raises(SyncError, match="moved during preflight"):
        sync_main_to_devel.prepare_sync()

    after = _remote_shas(repo)
    assert after == (advanced_main[0], original_remote[1])
    assert api.create_calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 128, b"", b"fatal: invalid object"),
        GitExecutionError(["merge-base"], "timed out after 10 seconds"),
    ],
    ids=["unexpected-exit", "execution-error"],
)
def test_prepare_sync_fails_closed_when_ancestry_is_unknown(
    divergent_repo: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    outcome: subprocess.CompletedProcess[bytes] | GitExecutionError,
) -> None:
    original_run_git = sync_main_to_devel.run_git

    def intercept_ancestry(args: list[str], **kwargs: object) -> Any:
        if args and args[0] == "merge-base":
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return original_run_git(args, **kwargs)

    monkeypatch.setattr(sync_main_to_devel, "run_git", intercept_ancestry)
    api = FakeRestApi()
    _install_api(monkeypatch, api)
    before = _remote_shas(divergent_repo["repo"])

    with pytest.raises(SyncError, match="ancestry"):
        sync_main_to_devel.prepare_sync()

    assert api.list_calls == 0
    assert api.create_calls == []
    assert _remote_shas(divergent_repo["repo"]) == before


def test_unknown_mergeability_remains_an_incomplete_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://github.com/owner/repo/pull/15"
    pr = _pr(url, "main-sha", "devel-sha")
    pr["mergeable"] = None
    pr["mergeable_state"] = "unknown"
    api = FakeRestApi()
    api.details[15] = pr
    _install_api(monkeypatch, api)

    inspected = sync_main_to_devel.inspect_pr(url)

    assert inspected["mergeable"] == "UNKNOWN"
    assert inspected["mergeStateStatus"] == "UNKNOWN"


def test_pr_inspection_rejects_cross_repository_or_wrong_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://github.com/owner/repo/pull/2"
    wrong = _pr(url, "main", "devel")
    wrong["head"]["repo"] = {"full_name": "fork/repo"}  # type: ignore[index]
    api = FakeRestApi()
    api.details[2] = wrong
    _install_api(monkeypatch, api)
    with pytest.raises(SyncError, match="unrelated or cross-repository"):
        sync_main_to_devel.inspect_pr(url)


def test_pr_inspection_rejects_url_for_another_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeRestApi()
    _install_api(monkeypatch, api)

    with pytest.raises(SyncError, match="mismatched identity"):
        sync_main_to_devel.inspect_pr("https://github.com/fork/repo/pull/2")


@pytest.mark.parametrize(
    ("state", "draft"),
    [("closed", False), ("open", True)],
    ids=["closed-detail", "unexpected-draft"],
)
def test_prepare_sync_rejects_closed_or_draft_created_pr(
    divergent_repo: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    draft: bool,
) -> None:
    url = "https://github.com/owner/repo/pull/19"
    api = FakeRestApi()
    api.create_url = url
    api.create_response = _pr(
        url, divergent_repo["main_sha"], divergent_repo["devel_sha"]
    )
    api.create_response["state"] = state
    api.create_response["draft"] = draft
    _install_api(monkeypatch, api)

    with pytest.raises(SyncError, match="closed|non-draft"):
        sync_main_to_devel.prepare_sync()

    assert api.create_calls[0]["draft"] is False
    assert len(api.create_calls) == 1


def test_prepare_sync_rejects_post_number_url_mismatch_without_retry(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeRestApi()
    api.create_url = "https://github.com/owner/repo/pull/43"
    api.create_result = {"number": 42, "html_url": api.create_url}
    _install_api(monkeypatch, api)

    with pytest.raises(SyncError, match="mismatched identity"):
        sync_main_to_devel.prepare_sync()

    assert len(api.create_calls) == 1
    assert api.list_calls == 2


def test_prepare_sync_reconciles_ambiguous_create_with_fresh_open_non_draft_detail(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/20"
    api = FakeRestApi()
    api.create_url = url
    api.create_error = github_api.GitHubApiError("request timed out")
    pr = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    api.details[20] = pr
    api.on_list = lambda: setattr(api, "pulls", [pr]) if api.list_calls == 2 else None
    _install_api(monkeypatch, api)

    assert sync_main_to_devel.prepare_sync().url == url
    assert api.create_calls[0]["draft"] is False
    assert len(api.create_calls) == 1


def test_prepare_sync_rejects_draft_candidate_after_ambiguous_create(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/21"
    api = FakeRestApi()
    api.create_error = github_api.GitHubApiError("request timed out")
    pr = _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
    pr["draft"] = True
    api.details[21] = pr
    api.on_list = lambda: setattr(api, "pulls", [pr]) if api.list_calls == 2 else None
    _install_api(monkeypatch, api)

    with pytest.raises(SyncError, match="non-draft"):
        sync_main_to_devel.prepare_sync()

    assert len(api.create_calls) == 1


def test_identical_tree_divergence_still_attempts_review_pr(
    ancestry_only_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tree equality must not be mistaken for ancestry synchronization."""
    repo = ancestry_only_repo["repo"]
    main_sha = ancestry_only_repo["main_sha"]
    devel_sha = ancestry_only_repo["devel_sha"]
    assert _git(repo, "rev-parse", f"{main_sha}^{{tree}}") == _git(
        repo, "rev-parse", f"{devel_sha}^{{tree}}"
    )
    assert not sync_main_to_devel.is_ancestor(main_sha, devel_sha)

    url = "https://github.com/owner/repo/pull/13"
    api = FakeRestApi()
    api.create_url = url
    api.create_response = _pr(url, main_sha, devel_sha)
    _install_api(monkeypatch, api)
    assert sync_main_to_devel.prepare_sync().url == url
    assert "main ancestry marker" in str(api.create_calls[0]["body"])


def test_ancestry_only_pr_creation_failure_gives_safe_recovery(
    ancestry_only_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeRestApi()
    api.create_error = github_api.GitHubApiError(
        "GitHub cannot create an empty diff PR"
    )
    _install_api(monkeypatch, api)
    with pytest.raises(SyncError, match="identical.*No branch was changed"):
        sync_main_to_devel.prepare_sync()
    assert api.list_calls == 2


def test_github_api_errors_are_reported_and_no_protected_branch_is_written(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeRestApi()
    api.list_error = github_api.GitHubApiError("HTTP 403 permission denied")
    before = (
        _git(divergent_repo["repo"], "ls-remote", "origin", "refs/heads/main"),
        _git(divergent_repo["repo"], "ls-remote", "origin", "refs/heads/devel"),
    )
    _install_api(monkeypatch, api)
    with pytest.raises(SyncError, match="permission denied"):
        sync_main_to_devel.prepare_sync()
    after = (
        _git(divergent_repo["repo"], "ls-remote", "origin", "refs/heads/main"),
        _git(divergent_repo["repo"], "ls-remote", "origin", "refs/heads/devel"),
    )
    assert after == before


def test_repeated_prepare_reuses_pr_after_remote_refs_advance(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second real preflight observes new tips without editing branches or PR."""
    repo = divergent_repo["repo"]
    url = "https://github.com/owner/repo/pull/14"
    api = FakeRestApi()
    api.create_url = url
    api.create_response = _pr(
        url, divergent_repo["main_sha"], divergent_repo["devel_sha"]
    )

    def refresh_existing_pr() -> None:
        if api.list_calls > 1:
            main_sha, devel_sha = sync_main_to_devel.fetch_refs()
            current = _pr(url, main_sha, devel_sha)
            api.pulls = [current]
            api.details[14] = current

    api.on_list = refresh_existing_pr
    _install_api(monkeypatch, api)
    initial_remote = _remote_shas(repo)
    first = sync_main_to_devel.prepare_sync()
    assert first.url == url
    assert _remote_shas(repo) == initial_remote

    _git(repo, "switch", "main")
    (repo / "hotfix.txt").write_text("follow-up production fix\n", encoding="utf-8")
    _commit(repo, "fix: follow up production change")
    _git(repo, "push", "origin", "main")
    _git(repo, "switch", "devel")
    (repo / "devel-only-follow-up.txt").write_text(
        "new development work\n", encoding="utf-8"
    )
    _commit(repo, "feat: add follow-up development work")
    _git(repo, "push", "origin", "devel")
    remote_before = _remote_shas(repo)

    second = sync_main_to_devel.prepare_sync()
    remote_after = _remote_shas(repo)

    assert second.url == url
    assert second.main_sha != first.main_sha
    assert second.devel_sha != first.devel_sha
    assert [call["head"] for call in api.create_calls] == ["main"]
    assert api.viewed == [
        (first.main_sha, first.devel_sha),
        (second.main_sha, second.devel_sha),
    ]
    assert remote_after == remote_before
    assert (repo / "devel-only-follow-up.txt").read_text() == "new development work\n"
