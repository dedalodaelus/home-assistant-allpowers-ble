"""Tests for the Git and GitHub CLI promotion preflight."""

from __future__ import annotations

import json
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


def test_open_promotion_reuses_existing_draft_including_draft_flag() -> None:
    draft = {"url": "https://github.com/owner/repo/pull/4", "draft": True}
    with patch.object(promote_devel, "run", return_value=json.dumps([draft])):
        assert promote_devel.open_promotion() == draft


def test_open_promotion_returns_none_when_no_pr_exists() -> None:
    with patch.object(promote_devel, "run", return_value="[]"):
        assert promote_devel.open_promotion() is None


def test_open_promotion_rejects_invalid_cli_json() -> None:
    with patch.object(promote_devel, "run", return_value="not json"):
        with pytest.raises(PromotionError, match="invalid PR data"):
            promote_devel.open_promotion()


def test_prepare_promotion_reuses_open_draft_without_fetching() -> None:
    existing = {"url": "https://github.com/owner/repo/pull/9", "draft": True}
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
        pytest.raises(PromotionError, match="moved refs"),
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
    existing = {"url": "https://github.com/owner/repo/pull/12", "draft": True}
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
