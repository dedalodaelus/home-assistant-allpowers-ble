from __future__ import annotations

import unittest
from typing import Any

from scripts.track_fixed_in_devel import (
    LABEL,
    explicit_issue_numbers,
    is_eligible_merge,
    mark_issue,
)


class FakeGitHubAPI:
    def __init__(
        self,
        issue: dict[str, Any] | None,
        fail_on: str | None = None,
    ):
        self.issue = issue
        self.labels = {label["name"] for label in (issue or {}).get("labels", [])}
        self.comments: list[dict[str, str]] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_on = fail_on

    def request(self, method: str, path: str, data: Any = None) -> Any:
        self.calls.append((method, path, data))
        if self.fail_on == path and method == "POST":
            raise RuntimeError(f"simulated API failure for {path}")
        if method == "GET" and path.startswith("/issues/") and path.count("/") == 2:
            return self.issue
        if method == "GET" and path == "/labels?per_page=100&page=1":
            return [{"name": name} for name in self.labels]
        if method == "POST" and path == "/labels":
            self.labels.add(data["name"])
        if (
            method == "POST"
            and path.startswith("/issues/")
            and path.endswith("/labels")
        ):
            self.issue["labels"].extend({"name": name} for name in data["labels"])
        if method == "POST" and path.endswith("/comments"):
            self.comments.append(data)
        return {}

    def pages(self, path: str):
        if path.startswith("/issues/") and path.endswith("/comments"):
            yield from self.comments
        elif path == "/labels":
            yield from ({"name": name} for name in self.labels)


class TrackFixedInDevelTests(unittest.TestCase):
    def test_only_explicit_closing_references_are_selected(self) -> None:
        body = (
            "Fixes #12, closes #13; casual mention #14. Resolves #12. "
            "Fixes owner/repo#15."
        )
        self.assertEqual(explicit_issue_numbers(body), [12, 13])

    def test_empty_body_has_no_targets(self) -> None:
        self.assertEqual(explicit_issue_numbers(None), [])

    def test_only_merged_devel_prs_are_eligible(self) -> None:
        self.assertTrue(is_eligible_merge("true", "devel"))
        self.assertFalse(is_eligible_merge("false", "devel"))
        self.assertFalse(is_eligible_merge("true", "main"))

    def test_marks_issue_and_comments_once_with_pr_link(self) -> None:
        api = FakeGitHubAPI({"labels": []})
        mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")
        self.assertIn(LABEL, {label["name"] for label in api.issue["labels"]})
        self.assertEqual(len(api.comments), 1)
        self.assertIn("not yet included in a stable release", api.comments[0]["body"])
        self.assertIn("<!-- fixed-in-devel:pr-45 -->", api.comments[0]["body"])

    def test_retry_does_not_duplicate_label_or_comment(self) -> None:
        api = FakeGitHubAPI({"labels": []})
        mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")
        mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")
        self.assertEqual(
            sum(label["name"] == LABEL for label in api.issue["labels"]), 1
        )
        self.assertEqual(len(api.comments), 1)

    def test_failed_label_or_comment_write_raises(self) -> None:
        for failed_path in ("/issues/12/labels", "/issues/12/comments"):
            with self.subTest(path=failed_path):
                api = FakeGitHubAPI({"labels": []}, fail_on=failed_path)
                with self.assertRaises(RuntimeError):
                    mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")

    def test_skips_pull_requests(self) -> None:
        api = FakeGitHubAPI({"labels": [], "pull_request": {"url": "..."}})
        mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")
        self.assertEqual(len(api.calls), 1)


if __name__ == "__main__":
    unittest.main()
