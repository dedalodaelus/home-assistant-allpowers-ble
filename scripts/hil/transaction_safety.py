"""Offline fake-GATT transaction-safety regression runner for the HIL CLI."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any
import xml.etree.ElementTree as ET

from .api import HAAPIError
from .storage import write_run, write_scenario_progress

_ROOT = Path(__file__).resolve().parents[2]
_REGRESSION_CASES = (
    (
        "tests/test_client_logic.py",
        "test_transaction_rejects_unconfirmed_dependent_write",
    ),
    (
        "tests/test_client_logic.py",
        "test_sequential_output_commands_use_confirmed_snapshot",
    ),
    (
        "tests/test_client_logic.py",
        "test_transaction_keepalive_rejects_pending_settings",
    ),
    (
        "tests/test_client_logic.py",
        "test_automatic_keepalive_does_not_replay_pending_settings",
    ),
    (
        "tests/test_client_logic.py",
        "test_transaction_failed_write_requires_new_telemetry",
    ),
    (
        "tests/test_client_logic.py",
        "test_transaction_pre_send_notification_is_not_confirmation",
    ),
    (
        "tests/test_client_logic.py",
        "test_stale_status_notification_does_not_clear_pending_output_transaction",
    ),
    (
        "tests/test_client_logic.py",
        "test_consecutive_output_commands_handle_delayed_and_duplicate_confirmations",
    ),
    (
        "tests/test_client_logic.py",
        "test_transaction_expiration_during_write_keeps_group_ambiguous",
    ),
    (
        "tests/test_hil_capture.py",
        "test_transaction_evidence_distinguishes_submit_and_confirm",
    ),
    (
        "tests/test_hil_capture.py",
        "test_real_disconnect_records_one_terminal_pending_transaction",
    ),
    (
        "tests/test_hil_capture.py",
        "test_new_run_inherits_pending_transaction_without_replaying_tx",
    ),
)


def _test_results(report_path: Path) -> tuple[list[dict[str, Any]], int, int, int]:
    """Extract only test names and counts; never persist runner output/tracebacks."""
    root = ET.parse(report_path).getroot()
    cases = root.findall(".//testcase")
    rows = []
    failures = 0
    errors = 0
    for case in cases:
        failed = case.find("failure") is not None
        errored = case.find("error") is not None
        failures += int(failed)
        errors += int(errored)
        name = case.get("name", "unnamed")
        safe_name = "".join(
            char if char.isalnum() or char in "_-" else "_" for char in name
        )
        rows.append(
            {
                "step_id": f"case_{safe_name[:54]}",
                "outcome": "fail" if failed or errored else "pass",
                "test": name[:120],
            }
        )
    return rows, len(cases), failures, errors


def execute_transaction_safety_simulation(output_root: Path) -> int:
    """Run selected deterministic fake-GATT tests without contacting hardware."""
    run_id = secrets.token_hex(8)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_dir = write_run(
        output_root,
        f"{timestamp}_{run_id}",
        {"evidence_mode": "simulation", "hardware_qualification": False},
        {"run_id": run_id, "route": "synthetic", "device": "synthetic"},
    )
    steps: list[dict[str, Any]] = []
    process_returncode: int | None = None
    total = failures = errors = 0
    try:
        with tempfile.TemporaryDirectory(prefix="allpowers-hil-transaction-") as temp:
            report_path = Path(temp) / "junit.xml"
            expression = " or ".join(name for _, name in _REGRESSION_CASES)
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--junitxml",
                str(report_path),
                "-k",
                expression,
                *(str(_ROOT / file) for file, _ in _REGRESSION_CASES),
            ]
            result = subprocess.run(
                command,
                cwd=_ROOT,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            process_returncode = result.returncode
            if report_path.is_file():
                steps, total, failures, errors = _test_results(report_path)
    except OSError, subprocess.SubprocessError, ET.ParseError:
        steps = []

    passed = (
        process_returncode == 0
        and total > 0
        and failures == 0
        and errors == 0
        and len(steps) == total
    )
    if not steps:
        steps = [
            {
                "step_id": "runner_unavailable",
                "outcome": "inconclusive",
                "reason": "fake-GATT regression runner did not produce a report",
            }
        ]
    fingerprint = "0" * 64
    write_scenario_progress(
        output_dir,
        run_id,
        "transaction_safety_simulation",
        fingerprint,
        steps,
    )
    report = {
        "schema_version": 1,
        "evidence_mode": "simulation",
        "hardware_qualification": False,
        "tests_run": total,
        "failures": failures,
        "errors": errors,
        "simulation_status": "pass" if passed else "inconclusive",
        "qualification_status": "not_applicable",
        "steps": steps,
    }
    try:
        path = output_dir / "transaction-safety-simulation.json"
        path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        path.chmod(0o600)
    except OSError as ex:
        raise HAAPIError("Unable to persist private transaction-safety report") from ex
    print(f"Transaction-safety fake-GATT simulation: {report['simulation_status']}")
    print(
        f"Synthetic regression cases: {total}; failures: {failures}; errors: {errors}"
    )
    print("This run provides no hardware or integration qualification")
    print("Private report:", path)
    return 0 if passed else 2
