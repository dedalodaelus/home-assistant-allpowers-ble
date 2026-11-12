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
        comments: list[dict[str, str]] | None = None,
    ):
        self.issue = issue
        self.comments = comments or []
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, data: Any = None) -> Any:
        self.calls.append((method, path, data))
        if method == "GET" and path.startswith("/issues/") and path.count("/") == 2:
            return self.issue
        if method == "GET" and path == "/labels?per_page=100&page=1":
            return [{"name": LABEL}]
        return {}

    def pages(self, path: str):
        if path.startswith("/issues/") and path.endswith("/comments"):
            yield from self.comments


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
        self.assertIn(("POST", "/issues/12/labels", {"labels": [LABEL]}), api.calls)
        comment = next(call for call in api.calls if call[1] == "/issues/12/comments")
        self.assertIn("not yet included in a stable release", comment[2]["body"])
        self.assertIn("<!-- fixed-in-devel:pr-45 -->", comment[2]["body"])

    def test_retry_does_not_duplicate_label_or_comment(self) -> None:
        api = FakeGitHubAPI(
            {"labels": [{"name": LABEL}]},
            [{"body": "<!-- fixed-in-devel:pr-45 -->"}],
        )
        mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")
        self.assertFalse(any(path.endswith("/labels") for _, path, _ in api.calls))
        self.assertFalse(any(path.endswith("/comments") for _, path, _ in api.calls))

    def test_skips_pull_requests(self) -> None:
        api = FakeGitHubAPI({"labels": [], "pull_request": {"url": "..."}})
        mark_issue(api, 12, 45, "https://github.com/example/repo/pull/45")
        self.assertEqual(len(api.calls), 1)


if __name__ == "__main__":
    unittest.main()
