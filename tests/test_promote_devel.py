"""Tests for the draft promotion preflight and GitHub orchestration."""

from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
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
            "custom_components/allpowers_ble/manifest.json": _entry(
                "custom_components/allpowers_ble/manifest.json", "base-manifest"
            ),
            "README.md": _entry("README.md", "same-readme"),
        }
        self.head_tree = {
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

    with pytest.raises(PromotionError, match="would regress"):
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
