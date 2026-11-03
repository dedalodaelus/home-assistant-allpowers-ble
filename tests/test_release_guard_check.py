"""Regression tests for the trusted release guard and its required check."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.release_guard_check import (
    CHECK_NAME,
    authorized_release_source,
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
        "base": {
            "ref": "main",
            "sha": BASE_SHA,
            "repo": {"full_name": "owner/repo"},
        },
        "head": {
            "ref": "devel",
            "sha": HEAD_SHA,
            "repo": {"full_name": "owner/repo"},
        },
        "mergeable": True,
        "user": {"type": "User", "login": "contributor"},
        "merge_commit_sha": MERGE_SHA,
    }


class FakeGitHub:
    def __init__(
        self,
        pull: dict[str, Any] | None = None,
        pull_responses: list[dict[str, Any]] | None = None,
        commits: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.pull = live_pr() if pull is None else pull
        self.pull_responses = pull_responses or [self.pull]
        self.pull_index = 0
        self.commits = commits or {
            MERGE_SHA: {
                "sha": MERGE_SHA,
                "parents": [{"sha": BASE_SHA}, {"sha": HEAD_SHA}],
            }
        }
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
            if "/pulls/" in path:
                pull = self.pull_responses[
                    min(self.pull_index, len(self.pull_responses) - 1)
                ]
                self.pull_index += 1
                return pull
            if "/commits/" in path:
                return self.commits[path.rsplit("/", 1)[-1]]
        if method == "PATCH":
            return {"id": 987}
        raise AssertionError(f"Unexpected API method: {method}")


def test_start_creates_pending_checks_for_head_and_test_merge() -> None:
    api = FakeGitHub()
    assert start_checks(context(), api) == (987, 988, MERGE_SHA)
    assert [call[0] for call in api.calls] == ["POST", "GET", "GET", "POST"]
    for sha, (_, path, payload) in zip(
        (HEAD_SHA, MERGE_SHA), (api.calls[0], api.calls[-1]), strict=True
    ):
        assert path == "/repos/owner/repo/check-runs"
        assert payload is not None
        assert payload["name"] == CHECK_NAME
        assert payload["head_sha"] == sha
        assert payload["status"] == "in_progress"
        assert "conclusion" not in payload


def test_start_polls_until_mergeability_and_test_merge_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = live_pr()
    pending["mergeable"] = None
    pending["merge_commit_sha"] = None
    api = FakeGitHub(pull_responses=[pending, live_pr()])
    sleeps: list[int] = []
    monkeypatch.setattr("scripts.release_guard_check.time.sleep", sleeps.append)

    assert start_checks(context(), api) == (987, 988, MERGE_SHA)
    assert sleeps == [3]
    assert sum("/pulls/" in call[1] for call in api.calls) == 2


def test_start_fails_closed_when_merge_sha_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = live_pr()
    pending["mergeable"] = None
    pending["merge_commit_sha"] = None
    api = FakeGitHub(pull_responses=[pending])
    monkeypatch.setattr("scripts.release_guard_check.time.sleep", lambda _: None)

    with pytest.raises(GuardError, match="stable test-merge commit"):
        start_checks(context(), api)
    assert sum("/pulls/" in call[1] for call in api.calls) == 10
    assert [call[2]["head_sha"] for call in api.calls if call[0] == "POST"] == [
        HEAD_SHA
    ]
    assert api.calls[-1][2]["conclusion"] == "failure"


@pytest.mark.parametrize(
    ("pull", "commit", "message"),
    [
        ({"mergeable": False}, None, "merge conflicts"),
        (
            {},
            {"sha": MERGE_SHA, "parents": [{"sha": BASE_SHA}, {"sha": "d" * 40}]},
            "expected current base/head parents",
        ),
    ],
)
def test_start_rejects_conflict_or_wrong_test_merge(
    pull: dict[str, Any],
    commit: dict[str, Any] | None,
    message: str,
) -> None:
    live = live_pr()
    live.update(pull)
    commits = {MERGE_SHA: commit} if commit is not None else None
    api = FakeGitHub(pull=live, commits=commits)

    with pytest.raises(GuardError, match=message):
        start_checks(context(), api)
    assert [call[2]["head_sha"] for call in api.calls if call[0] == "POST"] == [
        HEAD_SHA
    ]
    assert api.calls[-1][2]["conclusion"] == "failure"


@pytest.mark.parametrize("moved_ref", ["base", "head"])
def test_start_fails_if_base_or_head_moves_while_mergeability_is_pending(
    moved_ref: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = live_pr()
    pending["mergeable"] = None
    moved = live_pr()
    moved[moved_ref]["sha"] = "d" * 40
    api = FakeGitHub(pull_responses=[pending, moved])
    monkeypatch.setattr("scripts.release_guard_check.time.sleep", lambda _: None)

    with pytest.raises(GuardError, match="changed before starting"):
        start_checks(context(), api)
    assert [call[2]["head_sha"] for call in api.calls if call[0] == "POST"] == [
        HEAD_SHA
    ]
    assert api.calls[-1][2]["conclusion"] == "failure"


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


def test_merge_sha_changing_during_validation_fails_closed() -> None:
    pull = live_pr()
    api = FakeGitHub(pull)
    api.pull_responses = [pull, {**pull, "merge_commit_sha": "d" * 40}]
    start_checks(context(), api)
    with pytest.raises(GuardError, match="changed during validation"):
        finish_checks(context(), api, 987, 988, MERGE_SHA, "success")
    assert api.calls[-1][2]["conclusion"] == "failure"


def test_fork_branch_named_devel_cannot_impersonate_promotion() -> None:
    pull = live_pr()
    pull["head"]["repo"]["full_name"] = "attacker/fork"
    assert not authorized_release_source(context(), pull)
    api = FakeGitHub(pull)
    with pytest.raises(GuardError, match="changed before starting"):
        start_checks(context(), api)
    assert api.calls[-1][2]["conclusion"] == "failure"


def test_fork_branch_named_main_cannot_impersonate_release_sync() -> None:
    ctx = replace(context(), base_ref="devel", head_ref="main")
    pull = live_pr()
    pull["base"]["ref"] = "devel"
    pull["head"]["ref"] = "main"
    pull["head"]["repo"]["full_name"] = "attacker/fork"
    assert not current_revision_matches(ctx, pull)


@pytest.mark.parametrize(
    ("login", "account_type", "allowed"),
    [
        ("contributor", "User", False),
        ("allpowers-release-please[bot]", "Bot", True),
        ("github-actions[bot]", "Bot", True),
        ("release-please[bot]", "Bot", True),
        ("other-bot[bot]", "Bot", False),
        ("github-actions[bot]", "User", False),
    ],
)
def test_release_please_branch_requires_authorized_bot_and_repo(
    login: str, account_type: str, allowed: bool
) -> None:
    ctx = replace(context(), head_ref="release-please--branches--main")
    pull = live_pr()
    pull["head"]["ref"] = ctx.head_ref
    pull["user"] = {"login": login, "type": account_type}
    assert authorized_release_source(ctx, pull) is allowed
    assert current_revision_matches(ctx, pull) is allowed
    pull["head"]["repo"]["full_name"] = "attacker/fork"
    assert not current_revision_matches(ctx, pull)


def test_privileged_source_changing_during_validation_fails_both_checks() -> None:
    api = FakeGitHub()
    api.pull["head"]["repo"]["full_name"] = "attacker/fork"
    with pytest.raises(GuardError, match="changed during validation"):
        finish_checks(context(), api, 987, 988, MERGE_SHA, "success")
    assert all(call[2]["conclusion"] == "failure" for call in api.calls[1:])


def test_nonprivileged_feature_pr_can_come_from_fork() -> None:
    ctx = replace(context(), base_ref="devel", head_ref="feature/capability")
    pull = live_pr()
    pull["base"]["ref"] = "devel"
    pull["head"]["ref"] = "feature/capability"
    pull["head"]["repo"]["full_name"] = "contributor/fork"
    assert current_revision_matches(ctx, pull)


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
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
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
    assert "branches:\n      - main" in guard
    assert "      - devel" not in guard
    assert "pull_request_target:" in gate
    assert "\n  pull_request:\n" not in gate
    assert "actions/checkout@" in gate
    assert "persist-credentials: false" in gate
    assert "refs/pull/$PR_NUMBER/head" in gate
    assert "ref: main" in gate
    assert "python3 -m scripts.promotion_metadata" in gate
    assert "- edited" in gate
    assert 'context.payload.action === "edited"' in gate
    assert "PR metadata changed. Push a new commit" in gate
    assert "latestPull.data.title !== pr.title" in gate
    assert '"allpowers-release-please[bot]"' in gate
    assert "REQUIRE_RELEASE_METADATA_GUARD" not in gate
    assert "actions.listWorkflowRuns" not in gate
    assert "environment: release-please" in release
    assert "RELEASE_PLEASE_APP_ID" in release
    assert "RELEASE_PLEASE_APP_PRIVATE_KEY" in release
    assert "token: ${{ steps.app-token.outputs.token }}" in release


def test_merge_gate_blocks_retarget_and_later_title_edit_until_synchronize() -> None:
    gate = (ROOT / ".github/workflows/merge-gate.yml").read_text(encoding="utf-8")
    edited_guard_start = gate.index('if (context.payload.action === "edited")')
    edited_guard_end = gate.index("// Policy: PRs to main", edited_guard_start)
    edited_guard = gate[edited_guard_start:edited_guard_end]

    assert "context.payload.changes" not in edited_guard
    assert "core.setFailed(" in edited_guard
    assert "return;" in edited_guard
    assert "PR metadata changed. Push a new commit" in edited_guard
    assert gate.index("PR metadata changed. Push a new commit") < gate.index(
        "const requiredChecks ="
    )
