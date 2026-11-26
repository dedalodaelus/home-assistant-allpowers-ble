"""Tests for Git and REST-based promotion preparation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import github_api, promote_devel
from scripts import promotion_metadata
from scripts.promote_devel import PromotionError


TEMPLATE = """## Changes
<!-- GENERATED_COMMITS -->
## Tests
<!-- GENERATED_TESTS -->
"""


def test_render_body_links_actual_commits_and_lists_changed_tests() -> None:
    with (
        patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}),
        patch.object(
            promote_devel,
            "run",
            return_value="0123456789abcdef\tfeat: add support\n",
        ),
        patch.object(
            promote_devel,
            "run_git",
            return_value=subprocess.CompletedProcess(
                [], 0, b"tests/test_feature.py\0README.md\0", b""
            ),
        ),
    ):
        body = promote_devel.render_body(TEMPLATE, "base", "head")

    assert "[`0123456`](https://github.com/owner/repo/commit/0123456789abcdef)" in body
    assert "tests/test_feature.py" in body
    assert "GENERATED_" not in body


def test_render_body_fails_when_no_commits_exist() -> None:
    with (
        patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}),
        patch.object(promote_devel, "run", side_effect=[""]),
        pytest.raises(PromotionError, match="no promotable commits"),
    ):
        promote_devel.render_body(TEMPLATE, "base", "head")


class FakeGitHubApi:
    owner = "owner"
    repo = "repo"

    def __init__(self, pulls: list[dict[str, object]] | None = None) -> None:
        self.pulls = pulls or []
        self.created: dict[str, object] | None = None
        self.create_args: dict[str, object] | None = None
        self.detail: dict[str, object] | None = None
        self.create_error: Exception | None = None

    def list_pull_requests(self, **_kwargs: object) -> list[dict[str, object]]:
        return self.pulls

    def create_pull_request(self, **kwargs: object) -> dict[str, object]:
        self.create_args = kwargs
        if self.create_error is not None:
            raise self.create_error
        if self.created is None:
            self.created = _rest_pr(42, kwargs.get("draft", False), "head", "base")
        return self.created

    def pull_request(self, _number: int) -> dict[str, object]:
        return self.detail or self.created or {}


def _rest_pr(
    number: int, draft: object, head_sha: str, base_sha: str
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "draft": draft,
        "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": "owner/repo"}},
        "head": {"ref": "devel", "sha": head_sha, "repo": {"full_name": "owner/repo"}},
    }


def test_open_promotion_selects_same_repository_pr_after_fork_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fork = _rest_pr(4, True, "head", "base")
    fork["html_url"] = "https://github.com/fork/repo/pull/4"
    fork["head"]["repo"] = {"full_name": "fork/repo"}  # type: ignore[index]
    own = _rest_pr(5, True, "head", "base")
    api = FakeGitHubApi([fork, own])
    monkeypatch.setattr(promote_devel, "client_from_env", lambda **_kwargs: api)
    assert promote_devel.open_promotion() == {
        "url": own["html_url"],
        "isDraft": True,
        "baseSha": "base",
        "headSha": "head",
    }


def test_open_promotion_ignores_fork_only_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    fork = _rest_pr(4, True, "head", "base")
    fork["head"]["repo"] = {"full_name": "fork/repo"}  # type: ignore[index]
    monkeypatch.setattr(
        promote_devel, "client_from_env", lambda **_kwargs: FakeGitHubApi([fork])
    )
    assert promote_devel.open_promotion() is None


def test_open_promotion_rejects_incomplete_repository_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _rest_pr(6, True, "head", "base")
    candidate["head"]["repo"] = None  # type: ignore[index]
    monkeypatch.setattr(
        promote_devel,
        "client_from_env",
        lambda **_kwargs: FakeGitHubApi([candidate]),
    )

    with pytest.raises(PromotionError, match="incomplete repository identity"):
        promote_devel.open_promotion()


def test_open_promotion_returns_none_when_no_pr_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promote_devel, "client_from_env", lambda **_kwargs: FakeGitHubApi()
    )
    assert promote_devel.open_promotion() is None


def test_open_promotion_rejects_invalid_rest_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeGitHubApi([{"base": None, "head": None}])
    monkeypatch.setattr(promote_devel, "client_from_env", lambda **_kwargs: api)
    with pytest.raises(PromotionError, match="without base/head"):
        promote_devel.open_promotion()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_prepare_promotion_creates_draft_from_real_local_git_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "origin.git"
    repo = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Promotion test")
    _git(repo, "config", "user.email", "promotion@example.test")
    _git(repo, "switch", "-c", "main")

    manifest_path = repo / "custom_components/allpowers_ble/manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"version": "1.2.3"}\n', encoding="utf-8")
    (repo / ".release-please-manifest.json").write_text(
        '{".": "1.2.3"}\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "chore: initialize release metadata")
    _git(repo, "push", "--set-upstream", "origin", "main")
    base_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "devel")
    (repo / "tests").mkdir()
    (repo / "tests/test_promotion_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("Promotion change\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: add promotion smoke change")
    _git(repo, "push", "--set-upstream", "origin", "devel")
    head_sha = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    url = "https://github.com/owner/repo/pull/42"
    api = FakeGitHubApi()
    api.created = _rest_pr(42, True, head_sha, base_sha)
    api.detail = api.created
    monkeypatch.setattr(promote_devel, "client_from_env", lambda **_kwargs: api)

    assert promote_devel.prepare_promotion(TEMPLATE) == url
    assert api.create_args is not None
    assert api.create_args["draft"] is True
    assert api.create_args["base"] == "main"
    assert api.create_args["head"] == "devel"
    body = str(api.create_args["body"])
    assert f"https://github.com/owner/repo/commit/{head_sha}" in body
    assert "tests/test_promotion_smoke.py" in body


def test_prepare_promotion_reuses_open_draft_without_fetching() -> None:
    existing = {
        "url": "https://github.com/owner/repo/pull/9",
        "isDraft": True,
    }
    with patch.object(promote_devel, "open_promotion", return_value=existing):
        assert promote_devel.prepare_promotion(TEMPLATE) == existing["url"]


def test_prepare_promotion_fails_closed_for_diverged_refs() -> None:
    with (
        patch.object(promote_devel, "open_promotion", return_value=None),
        patch.object(promote_devel, "run", return_value="") as command,
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel,
            "run_git",
            return_value=subprocess.CompletedProcess([], 1, b"", b""),
        ),
        pytest.raises(PromotionError, match="diverged"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)
    assert command.call_count == 3  # fetch and both commit-object checks


def test_prepare_promotion_identical_trees_are_a_noop() -> None:
    with (
        patch.object(promote_devel, "open_promotion", return_value=None),
        patch.object(promote_devel, "run", side_effect=["", "", "", "tree", "tree"]),
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel,
            "run_git",
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        ),
    ):
        assert promote_devel.prepare_promotion(TEMPLATE) is None


def test_prepare_promotion_rejects_refs_moving_before_pr_creation() -> None:
    with (
        patch.object(promote_devel, "open_promotion", return_value=None),
        patch.object(
            promote_devel, "run", side_effect=["", "", "", "main-tree", "devel-tree"]
        ),
        patch.object(
            promote_devel,
            "remote_refs",
            side_effect=[("main", "devel"), ("new-main", "devel")],
        ),
        patch.object(
            promote_devel,
            "run_git",
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        ),
        patch.object(promote_devel, "validate_revisions"),
        patch.object(promote_devel, "render_body", return_value="body"),
        pytest.raises(PromotionError, match="moved during preflight"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)


def test_prepare_promotion_rejects_draft_for_moved_revisions() -> None:
    opened = iter([None, None])
    api = FakeGitHubApi()
    api.created = _rest_pr(3, True, "head", "base")
    api.detail = _rest_pr(3, True, "new-head", "base")
    with (
        patch.object(promote_devel, "open_promotion", side_effect=lambda: next(opened)),
        patch.object(
            promote_devel,
            "run",
            side_effect=["", "", "", "main-tree", "devel-tree"],
        ),
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel,
            "run_git",
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        ),
        patch.object(promote_devel, "client_from_env", return_value=api),
        patch.object(promote_devel, "validate_revisions"),
        patch.object(promote_devel, "render_body", return_value="body"),
        pytest.raises(PromotionError, match="validated revisions"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)


def test_prepare_promotion_surfaces_git_errors() -> None:
    with (
        patch.object(promote_devel, "open_promotion", return_value=None),
        patch.object(
            promote_devel,
            "run",
            side_effect=PromotionError("git fetch permission denied"),
        ),
        pytest.raises(PromotionError, match="permission denied"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)


def test_prepare_promotion_reuses_pr_won_by_concurrent_invocation() -> None:
    existing = {
        "url": "https://github.com/owner/repo/pull/12",
        "isDraft": True,
        "baseSha": "main",
        "headSha": "devel",
    }
    opened = iter([None, None, existing])
    api = FakeGitHubApi()
    api.create_error = github_api.GitHubApiError("already exists")

    with (
        patch.object(promote_devel, "open_promotion", side_effect=lambda: next(opened)),
        patch.object(
            promote_devel, "run", side_effect=["", "", "", "main-tree", "devel-tree"]
        ),
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel,
            "run_git",
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        ),
        patch.object(promote_devel, "client_from_env", return_value=api),
        patch.object(promote_devel, "validate_revisions"),
        patch.object(promote_devel, "render_body", return_value="body"),
    ):
        assert promote_devel.prepare_promotion(TEMPLATE) == existing["url"]


@pytest.mark.parametrize(
    ("base", "head", "regresses"),
    [
        ("1.2.3", "1.2.3", False),
        ("1.2.3", "1.2.4", False),
        ("1.2.3", "1.2.2", True),
        ("1.2.3", "1.2.3-rc.1", True),
        ("1.2.3-rc.1", "1.2.3", False),
    ],
)
def test_semver_preflight_rejects_version_regression(
    base: str, head: str, regresses: bool
) -> None:
    if regresses:
        with pytest.raises(promotion_metadata.MetadataError, match="regressed"):
            promotion_metadata.validate_version_change(base, head)
    else:
        promotion_metadata.validate_version_change(base, head)


def test_promotion_requires_unchanged_integration_version() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="must remain unchanged"):
        promotion_metadata.validate_version_policy("1.2.3", "1.2.4", "main", "devel")


def test_main_to_devel_sync_can_import_newer_release_metadata() -> None:
    promotion_metadata.validate_version_policy("0.3.0", "1.2.0", "devel", "main")
    promotion_metadata.validate_release_file_changes(
        {"CHANGELOG.md", ".release-please-manifest.json"},
        "devel",
        "main",
        same_repository_sync=True,
    )
    promotion_metadata.validate_release_please_versions({".": "0.3.0"}, {".": "1.2.0"})


def test_sync_file_exception_requires_same_repository_evidence() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="CHANGELOG.md"):
        promotion_metadata.validate_release_file_changes(
            {"CHANGELOG.md"}, "devel", "main"
        )


def test_main_to_devel_sync_rejects_release_metadata_regression() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="regressed"):
        promotion_metadata.validate_version_policy("1.2.0", "0.3.0", "devel", "main")
    with pytest.raises(promotion_metadata.MetadataError, match="regressed"):
        promotion_metadata.validate_release_please_versions(
            {".": "1.2.0"}, {".": "0.3.0"}
        )


def test_release_please_manifest_cannot_drop_history_or_regress() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="regressed"):
        promotion_metadata.validate_release_please_versions(
            {".": "1.2.0"}, {".": "1.1.9"}
        )
    with pytest.raises(
        promotion_metadata.MetadataError, match="removed package history"
    ):
        promotion_metadata.validate_release_please_versions({".": "1.2.0"}, {})


def test_release_files_are_protected_in_promotion() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="CHANGELOG.md"):
        promotion_metadata.validate_release_file_changes(
            {"CHANGELOG.md"}, "main", "devel"
        )
    with pytest.raises(
        promotion_metadata.MetadataError, match=".release-please-manifest.json"
    ):
        promotion_metadata.validate_release_file_changes(
            {".release-please-manifest.json"}, "main", "devel"
        )


def test_renamed_manifest_source_is_treated_as_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        returncode = 0
        stdout = (
            b"R100\0custom_components/allpowers_ble/manifest.json\0docs/manifest.json\0"
        )
        stderr = b""

    monkeypatch.setattr(
        promotion_metadata, "run_git", lambda *args, **kwargs: Completed()
    )
    paths = promotion_metadata._changed_paths("base", "head")
    with pytest.raises(promotion_metadata.MetadataError, match="deleted or renamed"):
        promotion_metadata.validate_release_file_changes(paths, "main", "devel")
