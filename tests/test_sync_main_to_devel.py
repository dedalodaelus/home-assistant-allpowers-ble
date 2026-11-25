"""Tests for reviewed main-to-devel synchronization."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts import sync_main_to_devel
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
    return {
        "url": url,
        "headRefName": "main",
        "baseRefName": "devel",
        "isCrossRepository": False,
        "headRefOid": main_sha,
        "baseRefOid": devel_sha,
        "mergeable": mergeable,
        "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else "DIRTY",
    }


def test_sync_pr_creation_has_correct_direction_and_traceable_refs(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/42"
    real_run = sync_main_to_devel.run
    calls: list[list[str]] = []

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        if command[0] != "gh":
            return real_run(command, check=check)
        calls.append(command)
        if command[1:3] == ["pr", "list"]:
            return "[]"
        if command[1:3] == ["pr", "create"]:
            return url
        if command[1:3] == ["pr", "view"]:
            return json.dumps(
                _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
            )
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    result = sync_main_to_devel.prepare_sync()
    assert result.url == url
    assert result.main_sha == divergent_repo["main_sha"]
    assert result.devel_sha == divergent_repo["devel_sha"]

    create = next(call for call in calls if call[1:3] == ["pr", "create"])
    assert create[create.index("--base") + 1] == "devel"
    assert create[create.index("--head") + 1] == "main"
    assert (
        create[create.index("--title") + 1] == "chore(ci): synchronize main into devel"
    )
    body = create[create.index("--body") + 1]
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
    fork = {
        "url": "https://github.com/fork/repo/pull/8",
        "headRefName": "main",
        "baseRefName": "devel",
        "isCrossRepository": True,
    }
    own = {**fork, "url": url, "isCrossRepository": False}
    real_run = sync_main_to_devel.run
    commands: list[list[str]] = []

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        if command[0] != "gh":
            return real_run(command, check=check)
        commands.append(command)
        if command[1:3] == ["pr", "list"]:
            return json.dumps([fork, own])
        if command[1:3] == ["pr", "view"]:
            return json.dumps(
                _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
            )
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    assert sync_main_to_devel.prepare_sync().url == url
    assert not any(call[1:3] == ["pr", "create"] for call in commands)


def test_duplicate_creation_race_reuses_winning_sync_pr(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/10"
    real_run = sync_main_to_devel.run
    list_calls = 0
    create_calls = 0

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        nonlocal list_calls, create_calls
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            list_calls += 1
            if list_calls == 1:
                return "[]"
            return json.dumps(
                [
                    {
                        "url": url,
                        "headRefName": "main",
                        "baseRefName": "devel",
                        "isCrossRepository": False,
                    }
                ]
            )
        if command[1:3] == ["pr", "create"]:
            create_calls += 1
            raise SyncError("a concurrent workflow created the PR")
        if command[1:3] == ["pr", "view"]:
            return json.dumps(
                _pr(url, divergent_repo["main_sha"], divergent_repo["devel_sha"])
            )
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    assert sync_main_to_devel.prepare_sync().url == url
    assert create_calls == 1
    assert list_calls == 2


def test_pr_creation_permission_error_leaves_both_branch_heads_unchanged(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = sync_main_to_devel.run
    remote_before = _remote_shas(divergent_repo["repo"])
    list_calls = 0

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        nonlocal list_calls
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            list_calls += 1
            return "[]"
        if command[1:3] == ["pr", "create"]:
            raise SyncError("permission denied: pull requests are disabled")
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    with pytest.raises(SyncError, match="permission denied"):
        sync_main_to_devel.prepare_sync()
    assert list_calls == 2
    assert _remote_shas(divergent_repo["repo"]) == remote_before


def test_unrelated_or_wrong_direction_pr_is_ignored() -> None:
    wrong = {
        "url": "https://github.com/owner/repo/pull/3",
        "headRefName": "devel",
        "baseRefName": "main",
        "isCrossRepository": False,
    }
    with patch.object(sync_main_to_devel, "run", return_value=json.dumps([wrong])):
        assert sync_main_to_devel.open_sync_pr() is None


def test_conflicting_existing_pr_is_reported_without_resolution(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/repo/pull/11"
    real_run = sync_main_to_devel.run

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            return json.dumps(
                [
                    {
                        "url": url,
                        "headRefName": "main",
                        "baseRefName": "devel",
                        "isCrossRepository": False,
                    }
                ]
            )
        if command[1:3] == ["pr", "view"]:
            return json.dumps(
                _pr(
                    url,
                    divergent_repo["main_sha"],
                    divergent_repo["devel_sha"],
                    mergeable="CONFLICTING",
                )
            )
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    with pytest.raises(SyncError, match="merge conflicts"):
        sync_main_to_devel.prepare_sync()


def test_pr_ref_movement_fails_closed() -> None:
    with pytest.raises(SyncError, match="refs moved"):
        sync_main_to_devel.validate_current_pr(
            _pr("https://github.com/owner/repo/pull/12", "new-main", "devel"),
            "old-main",
            "devel",
            url="https://github.com/owner/repo/pull/12",
        )


def test_pr_inspection_rejects_cross_repository_or_wrong_direction() -> None:
    wrong = {
        "url": "https://github.com/fork/repo/pull/2",
        "headRefName": "main",
        "baseRefName": "devel",
        "isCrossRepository": True,
    }
    with (
        patch.object(sync_main_to_devel, "run", return_value=json.dumps(wrong)),
        pytest.raises(SyncError, match="unrelated or cross-repository"),
    ):
        sync_main_to_devel.inspect_pr(str(wrong["url"]))


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
    real_run = sync_main_to_devel.run
    create_seen = False

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        nonlocal create_seen
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            return "[]"
        if command[1:3] == ["pr", "create"]:
            create_seen = True
            assert "main ancestry marker" in command[command.index("--body") + 1]
            return url
        if command[1:3] == ["pr", "view"]:
            return json.dumps(_pr(url, main_sha, devel_sha))
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    assert sync_main_to_devel.prepare_sync().url == url
    assert create_seen


def test_ancestry_only_pr_creation_failure_gives_safe_recovery(
    ancestry_only_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = sync_main_to_devel.run
    list_calls = 0

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        nonlocal list_calls
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            list_calls += 1
            return "[]"
        if command[1:3] == ["pr", "create"]:
            raise SyncError("GitHub cannot create an empty diff PR")
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
    with pytest.raises(SyncError, match="identical.*No branch was changed"):
        sync_main_to_devel.prepare_sync()
    assert list_calls == 2


def test_github_api_errors_are_reported_and_no_protected_branch_is_written(
    divergent_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = sync_main_to_devel.run

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            raise SyncError("permission denied")
        raise AssertionError(f"Unexpected gh command: {command}")

    before = (
        _git(divergent_repo["repo"], "ls-remote", "origin", "refs/heads/main"),
        _git(divergent_repo["repo"], "ls-remote", "origin", "refs/heads/devel"),
    )
    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
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
    real_run = sync_main_to_devel.run
    created: list[str] = []
    viewed: list[tuple[str, str]] = []
    pr_exists = False

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        nonlocal pr_exists
        if command[0] != "gh":
            return real_run(command, check=check)
        if command[1:3] == ["pr", "list"]:
            return (
                json.dumps(
                    [
                        {
                            "url": url,
                            "headRefName": "main",
                            "baseRefName": "devel",
                            "isCrossRepository": False,
                        }
                    ]
                )
                if pr_exists
                else "[]"
            )
        if command[1:3] == ["pr", "create"]:
            created.append(command[command.index("--head") + 1])
            pr_exists = True
            return url
        if command[1:3] == ["pr", "view"]:
            main_sha, devel_sha = sync_main_to_devel.fetch_refs()
            viewed.append((main_sha, devel_sha))
            return json.dumps(_pr(url, main_sha, devel_sha))
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(sync_main_to_devel, "run", gh_boundary)
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
    assert created == ["main"]
    assert viewed == [
        (first.main_sha, first.devel_sha),
        (second.main_sha, second.devel_sha),
    ]
    assert remote_after == remote_before
    assert (repo / "devel-only-follow-up.txt").read_text() == "new development work\n"
