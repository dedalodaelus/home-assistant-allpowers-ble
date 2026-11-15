"""Startup and timeout-contract checks for the independently installable HIL CLI."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from custom_components.allpowers_ble.const import MAX_STALE_TIMEOUT, WRITE_TIMEOUT
from scripts.hil.api import (
    API_TIMEOUT,
    HIL_EXECUTE_TIMEOUT,
    HIL_MAX_STALE_TIMEOUT,
    HIL_WRITE_TIMEOUT,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_cli_help_starts_without_site_packages() -> None:
    """A real interpreter subprocess must not depend on HA test stubs."""
    for args in (("--help",), ("doctor", "--help"), ("onboard", "--help")):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "scripts.hil", *args],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout.lower()


def test_doctor_reaches_preflight_without_site_packages() -> None:
    """Doctor handles missing connection settings after isolated startup."""
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "scripts.hil",
            "doctor",
            "--device",
            "1",
            "--route",
            "local_adapter",
            "--non-interactive",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "HIL preflight failed: Set --ha-url or HA_HIL_URL" in result.stderr


def test_standalone_timeout_tracks_integration_bounds() -> None:
    """The duplicated standalone bounds stay no shorter than integration limits."""
    assert HIL_MAX_STALE_TIMEOUT >= MAX_STALE_TIMEOUT
    assert HIL_WRITE_TIMEOUT >= WRITE_TIMEOUT
    assert HIL_EXECUTE_TIMEOUT >= API_TIMEOUT + MAX_STALE_TIMEOUT + WRITE_TIMEOUT + 5
    assert HIL_EXECUTE_TIMEOUT >= 327
