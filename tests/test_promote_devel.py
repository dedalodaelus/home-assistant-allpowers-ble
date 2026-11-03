"""Tests for the draft promotion preflight and GitHub orchestration."""

from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Lock
from time import sleep
from typing import Any

import pytest

from scripts import promote_devel
from scripts import promotion_metadata
from scripts.promote_devel import GitHubAPI, PromotionError, prepare_promotion


TEMPLATE = """## Summary

<!-- GENERATED_COMMITS -->

### Tests

<!-- GENERATED_TESTS -->

### Previous release PRs

<!-- GENERATED_PREVIOUS_PROMOTIONS -->
"""
ROOT = Path(__file__).resolve().parents[1]


def _entry(path: str, sha: str) -> dict[str, Any]:
    return {"path": path, "sha": sha, "mode": "100644", "type": "blob"}


class FakeGitHub:
    def __init__(self) -> None:
        self.base_sha = "base-sha"
        self.head_sha = "head-sha"
        self.ref_reads = 0
        self.status = "ahead"
        self.comparison_calls: list[int] = []
        self.comparison_pages: dict[int, list[dict[str, Any]]] = {
            1: [
                {
                    "sha": "abcdef012345",
                    "html_url": "https://example.test/commit/abcdef0",
                    "commit": {"message": "feat: add discovery"},
                }
            ]
        }
        base_manifest = {"version": "1.2.3", "name": "Allpowers BLE"}
        head_manifest = {"version": "1.2.3", "name": "Allpowers BLE", "new_key": True}
        self.blobs = {
            "base-manifest": json.dumps(base_manifest),
            "head-manifest": json.dumps(head_manifest),
        }
        self.base_tree = {
            "tests": {"path": "tests", "sha": "tree-a", "type": "tree"},
            "custom_components/allpowers_ble/manifest.json": _entry(
                "custom_components/allpowers_ble/manifest.json", "base-manifest"
            ),
            "README.md": _entry("README.md", "same-readme"),
        }
        self.head_tree = {
            "tests": {"path": "tests", "sha": "tree-b", "type": "tree"},
            "tests/subdirectory": {
                "path": "tests/subdirectory",
                "sha": "tree-c",
                "type": "tree",
            },
            "custom_components/allpowers_ble/manifest.json": _entry(
                "custom_components/allpowers_ble/manifest.json", "head-manifest"
            ),
            "README.md": _entry("README.md", "changed-readme"),
            "tests/test_feature.py": _entry("tests/test_feature.py", "new-test"),
        }
        self.open: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = [
            {
                "number": 154,
                "title": "chore(release): promote devel to main",
                "html_url": "https://example.test/pull/154",
                "merged_at": "2026-01-01T00:00:00Z",
            },
            {
                "number": 153,
                "title": "chore(release): promotion abandoned",
                "html_url": "https://example.test/pull/153",
                "merged_at": None,
            },
        ]
        self.created: list[dict[str, Any]] = []
        self.association_active = 0
        self.association_max = 0
        self.lock = Lock()
        self.fail_compare: Exception | None = None

    def open_promotions(self) -> list[dict[str, Any]]:
        return self.open

    def refs(self) -> tuple[str, str]:
        self.ref_reads += 1
        return self.base_sha, self.head_sha

    def compare_page(self, base_sha: str, head_sha: str, page: int) -> dict[str, Any]:
        self.comparison_calls.append(page)
        if self.fail_compare:
            raise self.fail_compare
        return {"status": self.status, "commits": self.comparison_pages.get(page, [])}

    def tree(self, commit_sha: str) -> dict[str, dict[str, Any]]:
        return self.base_tree if commit_sha == self.base_sha else self.head_tree

    def blob_text(self, sha: str) -> str:
        return self.blobs[sha]

    def associated_prs(self, commit_sha: str) -> list[dict[str, Any]]:
        with self.lock:
            self.association_active += 1
            self.association_max = max(self.association_max, self.association_active)
        sleep(0.005)
        with self.lock:
            self.association_active -= 1
        return [
            {
                "number": 210,
                "title": "feat: add verified discovery",
                "html_url": "https://example.test/pull/210",
                "merged_at": "2026-01-02T00:00:00Z",
            }
        ]

    def recent_promotions(self) -> list[dict[str, Any]]:
        return [pull for pull in self.closed if pull.get("merged_at")][:4]

    def create_draft(self, body: str) -> dict[str, Any]:
        pull = {
            "html_url": "https://example.test/pull/new",
            "body": body,
            "draft": True,
            "base": {"sha": self.base_sha},
            "head": {"sha": self.head_sha},
        }
        self.created.append(pull)
        self.open = [pull]
        return pull


def test_success_creates_template_based_draft_with_traceable_summaries() -> None:
    api = FakeGitHub()

    url = prepare_promotion(api, TEMPLATE)

    assert url == "https://example.test/pull/new"
    assert len(api.created) == 1
    body = api.created[0]["body"]
    assert api.created[0]["draft"] is True
    assert "add verified discovery" in body
    assert "[#210](https://example.test/pull/210)" in body
    assert "tests/test_feature.py" in body
    assert "* `tests`" not in body
    assert "tests/subdirectory" not in body
    assert "[#154](https://example.test/pull/154)" in body
    assert "#153" not in body
    assert "<!-- GENERATED_" not in body


def test_existing_open_draft_is_reused_without_modification() -> None:
    api = FakeGitHub()
    existing = {"html_url": "https://example.test/pull/existing", "draft": True}
    api.open = [existing]

    assert prepare_promotion(api, TEMPLATE) == existing["html_url"]
    assert api.created == []


def test_identical_branches_do_not_create_empty_pr() -> None:
    api = FakeGitHub()
    api.status = "identical"

    assert prepare_promotion(api, TEMPLATE) is None
    assert api.created == []


def test_ahead_branches_with_identical_trees_are_a_successful_noop() -> None:
    api = FakeGitHub()
    api.head_tree = dict(api.base_tree)

    assert prepare_promotion(api, TEMPLATE) is None
    assert api.comparison_calls == [1]
    assert api.created == []


def test_diverged_branches_fail_before_pr_creation() -> None:
    api = FakeGitHub()
    api.status = "diverged"

    with pytest.raises(PromotionError, match="merge commit"):
        prepare_promotion(api, TEMPLATE)
    assert api.created == []


@pytest.mark.parametrize(
    "protected_path", ["CHANGELOG.md", ".release-please-manifest.json"]
)
def test_release_please_files_are_protected_even_when_deleted(
    protected_path: str,
) -> None:
    api = FakeGitHub()
    api.base_tree[protected_path] = _entry(protected_path, "old-content")

    with pytest.raises(PromotionError, match="Release Please managed files changed"):
        prepare_promotion(api, TEMPLATE)
    assert api.created == []


def test_manifest_metadata_change_is_allowed_when_semver_does_not_regress() -> None:
    api = FakeGitHub()

    assert prepare_promotion(api, TEMPLATE) == "https://example.test/pull/new"


def test_manifest_version_regression_fails_closed() -> None:
    api = FakeGitHub()
    api.blobs["head-manifest"] = json.dumps({"version": "1.2.2"})

    with pytest.raises(PromotionError, match="regressed"):
        prepare_promotion(api, TEMPLATE)
    assert api.created == []


def test_manifest_version_increase_is_not_allowed_in_devel_to_main_promotion() -> None:
    api = FakeGitHub()
    api.blobs["head-manifest"] = json.dumps({"version": "1.2.4"})

    with pytest.raises(PromotionError, match="must remain unchanged"):
        prepare_promotion(api, TEMPLATE)
    assert api.created == []


def test_incomplete_api_comparison_fails_closed() -> None:
    api = FakeGitHub()
    api.fail_compare = PromotionError("permission denied")

    with pytest.raises(PromotionError, match="permission denied"):
        prepare_promotion(api, TEMPLATE)
    assert api.created == []


def test_authentication_failure_is_reported_without_pr_creation() -> None:
    with pytest.raises(PromotionError, match="token is missing"):
        GitHubAPI("owner", "repo", "")


def test_github_permission_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import HTTPError

    headers = Message()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise HTTPError("https://api.github.test", 403, "forbidden", headers, None)

    monkeypatch.setattr(promote_devel, "urlopen", forbidden)
    api = GitHubAPI("owner", "repo", "token")

    with pytest.raises(PromotionError, match="authentication/permission failure"):
        api.request("GET", "/repos/owner/repo")


def test_rate_limit_is_retried_with_bounded_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import HTTPError

    headers = Message()
    headers["Retry-After"] = "0"
    attempts = 0

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"ok": true}'

    def limited_once(*args: Any, **kwargs: Any) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(
                "https://api.github.test",
                429,
                "rate limited",
                headers,
                BytesIO(b"retry"),
            )
        return Response()

    monkeypatch.setattr(promote_devel, "urlopen", limited_once)
    monkeypatch.setattr(promote_devel.time, "sleep", lambda _: None)
    api = GitHubAPI("owner", "repo", "token")

    assert api.request("GET", "/repos/owner/repo") == {"ok": True}
    assert attempts == 2


def test_repeated_invocations_reuse_the_first_created_pr() -> None:
    api = FakeGitHub()

    first = prepare_promotion(api, TEMPLATE)
    second = prepare_promotion(api, TEMPLATE)

    assert first == second
    assert len(api.created) == 1


def test_all_commit_pages_are_included_and_association_requests_are_bounded() -> None:
    api = FakeGitHub()
    api.comparison_pages[1] = [
        {
            "sha": f"{index:040x}",
            "html_url": f"https://example.test/commit/{index}",
            "commit": {"message": f"fix: change {index}"},
        }
        for index in range(100)
    ]
    api.comparison_pages[2] = [
        {
            "sha": f"{101:040x}",
            "html_url": "https://example.test/commit/101",
            "commit": {"message": "fix: final change"},
        }
    ]
    assert prepare_promotion(api, TEMPLATE) == "https://example.test/pull/new"
    assert len(api.created) == 1
    assert api.comparison_calls == [1, 2]
    assert 1 < api.association_max <= 3


def test_branch_movement_during_preflight_prevents_creation() -> None:
    class MovingBranch(FakeGitHub):
        def refs(self) -> tuple[str, str]:
            self.ref_reads += 1
            if self.ref_reads > 1:
                return self.base_sha, "new-head-sha"
            return self.base_sha, self.head_sha

    api = MovingBranch()

    with pytest.raises(PromotionError, match="moved during preflight"):
        prepare_promotion(api, TEMPLATE)
    assert api.created == []


@pytest.mark.parametrize("moving_ref", ["base", "head"])
def test_branch_movement_during_creation_rejects_outdated_draft(
    moving_ref: str,
) -> None:
    class MovingDuringCreation(FakeGitHub):
        def create_draft(self, body: str) -> dict[str, Any]:
            setattr(self, f"{moving_ref}_sha", "new-revision")
            return super().create_draft(body)

    api = MovingDuringCreation()

    with pytest.raises(PromotionError, match="summary is outdated") as caught:
        prepare_promotion(api, TEMPLATE)

    assert len(api.created) == 1
    assert api.created[0]["draft"] is True
    assert api.created[0]["html_url"] in str(caught.value)
    assert "close the draft and rerun" in str(caught.value)


@pytest.mark.parametrize("missing_ref", ["base", "head"])
def test_missing_created_pr_revision_is_not_accepted(missing_ref: str) -> None:
    class IncompleteCreation(FakeGitHub):
        def create_draft(self, body: str) -> dict[str, Any]:
            pull = super().create_draft(body)
            del pull[missing_ref]
            return pull

    api = IncompleteCreation()

    with pytest.raises(PromotionError, match="unverified"):
        prepare_promotion(api, TEMPLATE)
    assert len(api.created) == 1
    assert api.created[0]["draft"] is True


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
def test_semantic_version_comparison(base: str, head: str, regresses: bool) -> None:
    if regresses:
        with pytest.raises(promotion_metadata.MetadataError, match="regressed"):
            promotion_metadata.validate_version_change(base, head)
    else:
        promotion_metadata.validate_version_change(base, head)


def test_version_increase_requires_release_please_or_main_to_devel_sync() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="must remain unchanged"):
        promotion_metadata.validate_version_policy("1.2.3", "1.2.4", "devel", "feature")
    with pytest.raises(promotion_metadata.MetadataError, match="must remain unchanged"):
        promotion_metadata.validate_version_policy("1.2.3", "1.2.4", "main", "devel")

    promotion_metadata.validate_version_policy(
        "1.2.3", "1.2.4", "main", "release-please--123"
    )
    promotion_metadata.validate_version_policy("1.2.3", "1.2.4", "devel", "main")


@pytest.mark.parametrize("version", ["1.2.3-", "1.2.3-01", "1.2.3+build."])
def test_invalid_semver_is_rejected(version: str) -> None:
    with pytest.raises(
        promotion_metadata.MetadataError, match="Invalid semantic version"
    ):
        promotion_metadata.semver_key(version)


def test_release_please_files_only_flow_back_in_main_to_devel_sync() -> None:
    changed = {".release-please-manifest.json", "CHANGELOG.md"}

    promotion_metadata.validate_release_file_changes(changed, "devel", "main")
    with pytest.raises(promotion_metadata.MetadataError, match="CHANGELOG.md"):
        promotion_metadata.validate_release_file_changes(changed, "main", "devel")


def test_release_please_manifest_rejects_version_downgrade_and_history_loss() -> None:
    with pytest.raises(promotion_metadata.MetadataError, match="regressed"):
        promotion_metadata.validate_release_please_versions(
            {".": "1.2.0"}, {".": "1.1.9"}
        )
    with pytest.raises(
        promotion_metadata.MetadataError, match="removed package history"
    ):
        promotion_metadata.validate_release_please_versions({".": "1.2.0"}, {})


def test_renamed_protected_file_source_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = "R100\0CHANGELOG.md\0docs/history.md\0"

    monkeypatch.setattr(
        promotion_metadata.subprocess, "run", lambda *args, **kwargs: Completed()
    )

    changed = promotion_metadata._changed_paths("base", "head")

    assert "CHANGELOG.md" in changed
    with pytest.raises(promotion_metadata.MetadataError, match="CHANGELOG.md"):
        promotion_metadata.validate_release_file_changes(changed, "devel", "feature")


def test_trusted_metadata_workflow_never_checks_out_or_runs_pr_head_code() -> None:
    workflow = (ROOT / ".github/workflows/release-metadata.yml").read_text(
        encoding="utf-8"
    )
    gate = (ROOT / ".github/workflows/merge-gate.yml").read_text(encoding="utf-8")

    assert "pull_request_target:" in workflow
    assert "ref: main" in workflow
    assert "refs/pull/$PR_NUMBER/head" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in workflow
    assert "python3 -m scripts.promotion_metadata" in workflow
    assert "REQUIRE_RELEASE_METADATA_GUARD" not in gate
    assert "environment: release-metadata-guard" in workflow
    assert "permission-checks: write" in workflow
    assert "scripts.release_guard_check start" in workflow
