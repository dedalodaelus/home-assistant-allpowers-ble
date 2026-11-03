"""Regression tests for the trusted release guard and its required check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.release_guard_check import (
    CHECK_NAME,
    GuardContext,
    GuardError,
    current_revision_matches,
    finish_checks,
    start_checks,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MERGE_SHA = "c" * 40


def context() -> GuardContext:
    return GuardContext(
        repository="owner/repo",
        number=211,
        base_ref="main",
        base_sha=BASE_SHA,
        head_ref="devel",
        head_sha=HEAD_SHA,
        run_url="https://github.com/owner/repo/actions/runs/1234",
    )


def live_pr() -> dict[str, Any]:
    return {
        "state": "open",
        "number": 211,
        "base": {"ref": "main", "sha": BASE_SHA},
        "head": {"ref": "devel", "sha": HEAD_SHA},
        "merge_commit_sha": MERGE_SHA,
    }


class FakeGitHub:
    def __init__(self, pull: dict[str, Any] | None = None) -> None:
        self.pull = live_pr() if pull is None else pull
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail_get = False
        self.next_id = 987

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, payload))
        if method == "POST":
            value = self.next_id
            self.next_id += 1
            return {"id": value}
        if method == "GET":
            if self.fail_get:
                raise GuardError("unavailable")
            return self.pull
        if method == "PATCH":
            return {"id": 987}
        raise AssertionError(f"Unexpected API method: {method}")


def test_start_creates_pending_checks_for_head_and_test_merge() -> None:
    api = FakeGitHub()
    assert start_checks(context(), api) == (987, 988, MERGE_SHA)
    assert [call[0] for call in api.calls] == ["GET", "POST", "POST"]
    for sha, (_, path, payload) in zip((HEAD_SHA, MERGE_SHA), api.calls[1:]):
        assert path == "/repos/owner/repo/check-runs"
        assert payload is not None
        assert payload["name"] == CHECK_NAME
        assert payload["head_sha"] == sha
        assert payload["status"] == "in_progress"
        assert "conclusion" not in payload


def test_success_completes_both_checks_only_after_live_validation() -> None:
    api = FakeGitHub()
    finish_checks(context(), api, 987, 988, MERGE_SHA, "success")
    assert [call[0] for call in api.calls] == ["GET", "PATCH", "PATCH"]
    assert all(call[2]["conclusion"] == "success" for call in api.calls[1:])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_sha", "d" * 40),
        ("head_sha", "d" * 40),
        ("base_ref", "devel"),
        ("head_ref", "feature"),
        ("state", "closed"),
        ("number", 999),
        ("merge_commit_sha", "e" * 40),
    ],
)
def test_changed_or_closed_pr_fails_closed(field: str, value: Any) -> None:
    pull = live_pr()
    if field in {"base_sha", "head_sha"}:
        pull[field.split("_")[0]]["sha"] = value
    elif field in {"base_ref", "head_ref"}:
        pull[field.split("_")[0]]["ref"] = value
    else:
        pull[field] = value
    api = FakeGitHub(pull)
    with pytest.raises(GuardError, match="changed during validation"):
        finish_checks(context(), api, 987, 988, MERGE_SHA, "success")
    assert all(call[2]["conclusion"] == "failure" for call in api.calls[1:])


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", ""])
def test_non_success_validator_cannot_pass(outcome: str) -> None:
    api = FakeGitHub()
    with pytest.raises(GuardError, match="did not succeed"):
        finish_checks(context(), api, 987, 988, MERGE_SHA, outcome)
    assert all(call[2]["conclusion"] == "failure" for call in api.calls[1:])


def test_pr_lookup_failure_reports_failure_on_both_checks() -> None:
    api = FakeGitHub()
    api.fail_get = True
    with pytest.raises(GuardError, match="Could not re-read"):
        finish_checks(context(), api, 987, 988, MERGE_SHA, "success")
    assert [call[0] for call in api.calls] == ["GET", "PATCH", "PATCH"]
    assert all(call[2]["conclusion"] == "failure" for call in api.calls[1:])


def test_no_test_merge_sha_still_creates_head_check_only() -> None:
    pull = live_pr()
    pull["merge_commit_sha"] = None
    api = FakeGitHub(pull)
    assert start_checks(context(), api) == (987, None, None)
    finish_checks(context(), api, 987, None, None, "success")
    assert [call[0] for call in api.calls] == ["GET", "POST", "GET", "PATCH"]


def test_merge_sha_appearing_later_fails_closed() -> None:
    pull = live_pr()
    api = FakeGitHub(pull)
    with pytest.raises(GuardError, match="changed during validation"):
        finish_checks(context(), api, 987, None, None, "success")
    assert api.calls[-1][2]["conclusion"] == "failure"


def test_malformed_live_pr_is_not_current() -> None:
    assert not current_revision_matches(context(), {"number": 211})
    assert not current_revision_matches(context(), {})


def test_context_rejects_invalid_repository_sha_and_event_fields() -> None:
    good = {
        "GITHUB_REPOSITORY": "owner/repo",
        "PR_NUMBER": "211",
        "PR_BASE_REF": "main",
        "PR_HEAD_REF": "devel",
        "PR_BASE_SHA": BASE_SHA,
        "PR_HEAD_SHA": HEAD_SHA,
        "GITHUB_RUN_ID": "1234",
    }
    assert GuardContext.from_env(good) == context()
    for key, invalid in (
        ("GITHUB_REPOSITORY", "../oops"),
        ("PR_NUMBER", "0"),
        ("PR_BASE_SHA", "bad"),
        ("PR_HEAD_REF", ""),
        ("GITHUB_RUN_ID", "bad"),
    ):
        with pytest.raises(GuardError):
            GuardContext.from_env({**good, key: invalid})


def test_trusted_workflow_and_gate_contract() -> None:
    guard = (ROOT / ".github/workflows/release-metadata.yml").read_text(
        encoding="utf-8"
    )
    gate = (ROOT / ".github/workflows/merge-gate.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" in guard
    assert "environment: release-metadata-guard" in guard
    assert "ref: main" in guard
    assert "permission-checks: write" in guard
    assert "permission-pull-requests: read" in guard
    assert "refs/pull/" in guard
    assert "refs/pull/$PR_NUMBER/merge" in guard
    assert "scripts.release_guard_check start" in guard
    assert "scripts.release_guard_check finish" in guard
    assert "scripts.promotion_metadata" in guard
    assert "- edited" in gate
    assert "context.payload.changes?.base" in gate
    assert "Validate title (current PR edit)" in gate
    assert "latestPull.data.title !== pr.title" in gate
    assert "REQUIRE_RELEASE_METADATA_GUARD" not in gate
    assert "actions.listWorkflowRuns" not in gate
