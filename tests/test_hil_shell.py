"""Shell-safe command rendering for HIL and PR handoff output."""

from __future__ import annotations

import subprocess

import pytest

from scripts.hil import cli


@pytest.fixture(autouse=True)
def configure_event_loop():
    """Pure shell-rendering tests do not need Home Assistant event-loop setup."""
    yield


def test_posix_shell_arguments_are_quoted_without_expansion():
    assert (
        cli._shell_quote("space and 'quote", powershell=False)
        == "'space and '\"'\"'quote'"
    )
    assert cli._shell_join(["git", "add", "file with spaces"], powershell=False) == (
        "git add 'file with spaces'"
    )


def test_powershell_arguments_escape_single_quotes():
    assert cli._shell_quote("C:\\work dir\\it's safe", powershell=True) == (
        "'C:\\work dir\\it''s safe'"
    )
    assert cli._shell_join(["gh", "pr create", "a&b"], powershell=True) == (
        "'gh' 'pr create' 'a&b'"
    )


def test_current_feature_branch_requires_clean_nonprotected_checkout(
    monkeypatch, tmp_path
):
    outputs = iter(("feat/issue-330\n", ""))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=next(outputs)
        ),
    )

    assert cli._current_feature_branch(tmp_path) == "feat/issue-330"


@pytest.mark.parametrize(
    ("branch", "status", "expected"),
    [("devel", "", None), ("main", "", None), ("feature", " M file.py", None)],
)
def test_current_feature_branch_rejects_protected_or_dirty_checkout(
    monkeypatch, tmp_path, branch, status, expected
):
    outputs = iter((branch, status))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=next(outputs)
        ),
    )

    assert cli._current_feature_branch(tmp_path) is expected


def test_current_feature_branch_fails_closed_on_git_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("git", 3)
        ),
    )

    assert cli._current_feature_branch(tmp_path) is None
