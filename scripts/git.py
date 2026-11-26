"""Bounded native Git execution shared by repository automation and HIL."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

GIT_COMMAND_TIMEOUT_SECONDS = 10
GIT_NETWORK_TIMEOUT_SECONDS = 120


class GitExecutionError(subprocess.SubprocessError, OSError):
    """Git could not be invoked, timed out, or returned an unexpected status."""

    def __init__(
        self, args: Sequence[str], detail: str, *, returncode: int | None = None
    ) -> None:
        self.args = tuple(args)
        self.returncode = returncode
        super().__init__(f"git {' '.join(args)} {detail}")


def run_git(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = GIT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    """Run Git without a shell and preserve stdout/stderr as raw bytes."""
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as error:
        raise GitExecutionError(args, "executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise GitExecutionError(args, f"timed out after {timeout:g} seconds") from error
    except OSError as error:
        raise GitExecutionError(
            args, f"could not run ({type(error).__name__})"
        ) from error


def decode_git_output(value: bytes, *, errors: str = "strict") -> str:
    """Decode Git output explicitly; callers needing NUL data should keep bytes."""
    try:
        return value.decode("utf-8", errors=errors)
    except UnicodeDecodeError as error:
        raise GitExecutionError((), "returned non-UTF-8 output") from error


def require_success(
    result: subprocess.CompletedProcess[bytes], args: Sequence[str]
) -> bytes:
    """Return stdout or raise a concise error without losing diagnostics."""
    if result.returncode:
        detail = decode_git_output(
            result.stderr or result.stdout, errors="replace"
        ).strip()
        raise GitExecutionError(
            args,
            f"failed with exit {result.returncode}: {detail}",
            returncode=result.returncode,
        )
    return result.stdout


def run_git_process(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = GIT_COMMAND_TIMEOUT_SECONDS,
    text: bool = True,
    check: bool = True,
    errors: str = "strict",
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Convenience boundary preserving subprocess-style results for callers."""
    result = run_git(args, cwd=cwd, timeout=timeout)
    if check:
        require_success(result, args)
    if not text:
        return result
    return subprocess.CompletedProcess(
        ["git", *args],
        result.returncode,
        decode_git_output(result.stdout, errors=errors),
        decode_git_output(result.stderr, errors=errors),
    )
