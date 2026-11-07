"""Tests for the Git and GitHub CLI promotion preflight."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import promote_devel
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
            side_effect=[
                "0123456789abcdef\tfeat: add support\n",
                "tests/test_feature.py\0README.md",
            ],
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


def test_open_promotion_selects_same_repository_pr_after_fork_pr() -> None:
    fork = {
        "url": "https://github.com/fork/repo/pull/4",
        "baseRefName": "main",
        "headRefName": "devel",
        "isDraft": True,
        "isCrossRepository": True,
    }
    own = {
        "url": "https://github.com/owner/repo/pull/5",
        "baseRefName": "main",
        "headRefName": "devel",
        "isDraft": True,
        "isCrossRepository": False,
    }
    expected_fields = "url,headRefName,baseRefName,isDraft,isCrossRepository"
    with patch.object(
        promote_devel, "run", return_value=json.dumps([fork, own])
    ) as run:
        assert promote_devel.open_promotion() == own
    command = run.call_args.args[0]
    assert command[command.index("--json") + 1] == expected_fields


def test_open_promotion_ignores_fork_only_pr() -> None:
    fork = {
        "url": "https://github.com/fork/repo/pull/4",
        "baseRefName": "main",
        "headRefName": "devel",
        "isDraft": True,
        "isCrossRepository": True,
    }
    with patch.object(promote_devel, "run", return_value=json.dumps([fork])) as run:
        assert promote_devel.open_promotion() is None
    command = run.call_args.args[0]
    assert command[command.index("--json") + 1] == (
        "url,headRefName,baseRefName,isDraft,isCrossRepository"
    )


def test_open_promotion_returns_none_when_no_pr_exists() -> None:
    with patch.object(promote_devel, "run", return_value="[]"):
        assert promote_devel.open_promotion() is None


def test_open_promotion_rejects_invalid_cli_json() -> None:
    with patch.object(promote_devel, "run", return_value="not json"):
        with pytest.raises(PromotionError, match="invalid PR data"):
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
    gh_calls: list[list[str]] = []
    real_run = promote_devel.run

    def gh_boundary(command: list[str], *, check: bool = True) -> str:
        if command[0] != "gh":
            return real_run(command, check=check)
        gh_calls.append(command)
        if command[1:3] == ["pr", "list"]:
            return "[]"
        if command[1:3] == ["pr", "create"]:
            return url
        if command[1:3] == ["pr", "view"]:
            return json.dumps(
                {"isDraft": True, "headRefOid": head_sha, "baseRefOid": base_sha}
            )
        raise AssertionError(f"Unexpected GitHub CLI command: {command}")

    monkeypatch.setattr(promote_devel, "run", gh_boundary)

    assert promote_devel.prepare_promotion(TEMPLATE) == url
    create = next(command for command in gh_calls if command[1:3] == ["pr", "create"])
    assert "--draft" in create
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == "devel"
    body = create[create.index("--body") + 1]
    assert f"https://github.com/owner/repo/commit/{head_sha}" in body
    assert "tests/test_promotion_smoke.py" in body
    view = next(command for command in gh_calls if command[1:3] == ["pr", "view"])
    assert view[view.index("--json") + 1] == "url,isDraft,headRefOid,baseRefOid"


def test_prepare_promotion_reuses_open_draft_without_fetching() -> None:
    existing = {
        "url": "https://github.com/owner/repo/pull/9",
        "baseRefName": "main",
        "headRefName": "devel",
        "isDraft": True,
        "isCrossRepository": False,
    }
    with patch.object(promote_devel, "open_promotion", return_value=existing):
        assert promote_devel.prepare_promotion(TEMPLATE) == existing["url"]


def test_prepare_promotion_fails_closed_for_diverged_refs() -> None:
    with (
        patch.object(promote_devel, "open_promotion", return_value=None),
        patch.object(promote_devel, "run", return_value="") as command,
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 1})(),
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
            promote_devel.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0})(),
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
            promote_devel.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0})(),
        ),
        patch.object(promote_devel, "validate_revisions"),
        patch.object(promote_devel, "render_body", return_value="body"),
        pytest.raises(PromotionError, match="moved during preflight"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)


def test_prepare_promotion_rejects_draft_for_moved_revisions() -> None:
    opened = iter([None, None])
    with (
        patch.object(promote_devel, "open_promotion", side_effect=lambda: next(opened)),
        patch.object(
            promote_devel,
            "run",
            side_effect=[
                "",
                "",
                "",
                "main-tree",
                "devel-tree",
                "https://github.com/owner/repo/pull/3",
                json.dumps({"headRefOid": "new-head", "baseRefOid": "main"}),
            ],
        ),
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0})(),
        ),
        patch.object(promote_devel, "validate_revisions"),
        patch.object(promote_devel, "render_body", return_value="body"),
        pytest.raises(PromotionError, match="validated revisions"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)


def test_prepare_promotion_surfaces_git_or_github_cli_errors() -> None:
    with (
        patch.object(promote_devel, "open_promotion", return_value=None),
        patch.object(
            promote_devel,
            "run",
            side_effect=PromotionError("gh: permission denied"),
        ),
        pytest.raises(PromotionError, match="permission denied"),
    ):
        promote_devel.prepare_promotion(TEMPLATE)


def test_prepare_promotion_reuses_pr_won_by_concurrent_invocation() -> None:
    existing = {
        "url": "https://github.com/owner/repo/pull/12",
        "baseRefName": "main",
        "headRefName": "devel",
        "isDraft": True,
        "isCrossRepository": False,
    }
    opened = iter([None, None, existing])
    commands = ["", "", "", "main-tree", "devel-tree", PromotionError("already exists")]

    def fake_run(_command: list[str]) -> str:
        result = commands.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with (
        patch.object(promote_devel, "open_promotion", side_effect=lambda: next(opened)),
        patch.object(promote_devel, "run", side_effect=fake_run),
        patch.object(promote_devel, "remote_refs", return_value=("main", "devel")),
        patch.object(
            promote_devel.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0})(),
        ),
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
        stdout = (
            "R100\0custom_components/allpowers_ble/manifest.json\0docs/manifest.json\0"
        )

    monkeypatch.setattr(
        promotion_metadata.subprocess, "run", lambda *args, **kwargs: Completed()
    )
    paths = promotion_metadata._changed_paths("base", "head")
    with pytest.raises(promotion_metadata.MetadataError, match="deleted or renamed"):
        promotion_metadata.validate_release_file_changes(paths, "main", "devel")
