"""Tests for the bounded native Git execution boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import git
from scripts.git import GitExecutionError


def _run(repo: Path, *args: str) -> bytes:
    result = git.run_git(args, cwd=repo)
    return git.require_success(result, args)


def _repo(path: Path) -> Path:
    path.mkdir()
    _run(path, "init", "-b", "main")
    _run(path, "config", "user.name", "Git helper test")
    _run(path, "config", "user.email", "git-helper@example.test")
    _run(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _run(path, "add", ".")
    _run(path, "commit", "-m", "base")
    return path


def test_helper_runs_in_requested_directory_and_preserves_binary_nul_paths(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    base = git.decode_git_output(_run(repo, "rev-parse", "HEAD")).strip()
    odd_path = " leading space/café name.txt"
    (repo / odd_path).parent.mkdir()
    (repo / odd_path).write_text("content\n", encoding="utf-8")
    _run(repo, "add", "--", odd_path)
    _run(repo, "commit", "-m", "add unusual path")
    head = git.decode_git_output(_run(repo, "rev-parse", "HEAD")).strip()

    raw = git.require_success(
        git.run_git(["diff", "--name-only", "-z", base, head], cwd=repo), ["diff"]
    )
    assert raw == odd_path.encode() + b"\0"
    assert (
        git.run_git_process(["status", "--porcelain"], cwd=repo, text=True).stdout == ""
    )
    (repo / odd_path).write_text("modified\n", encoding="utf-8")
    assert git.run_git_process(
        ["status", "--porcelain"], cwd=repo, text=True
    ).stdout.startswith(" M ")


def test_merge_base_exit_codes_keep_non_ancestor_distinct_from_execution_error(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    base = git.decode_git_output(_run(repo, "rev-parse", "HEAD")).strip()
    _run(repo, "switch", "-c", "other")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "other line")
    other = git.decode_git_output(_run(repo, "rev-parse", "HEAD")).strip()

    ancestor = git.run_git(["merge-base", "--is-ancestor", base, other], cwd=repo)
    not_ancestor = git.run_git(["merge-base", "--is-ancestor", other, base], cwd=repo)
    assert ancestor.returncode == 0
    assert not_ancestor.returncode == 1

    with pytest.raises(GitExecutionError) as error:
        git.require_success(
            git.run_git(["merge-base", "--is-ancestor", "missing", base], cwd=repo),
            ["merge-base"],
        )
    assert error.value.returncode != 1


def test_missing_executable_timeout_and_decode_errors_are_consistent() -> None:
    with patch.object(git.subprocess, "run", side_effect=FileNotFoundError("git")):
        with pytest.raises(GitExecutionError, match="executable was not found"):
            git.run_git(["status"])

    with patch.object(
        git.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["git", "fetch"], 2),
    ):
        with pytest.raises(GitExecutionError, match="timed out after 2 seconds"):
            git.run_git(["fetch"], timeout=2)

    with pytest.raises(GitExecutionError, match="non-UTF-8"):
        git.decode_git_output(b"\xff")
