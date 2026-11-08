"""Result handling tests for the offline transaction-safety runner."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from scripts.hil import transaction_safety


def _junit_report(
    path: Path,
    *,
    omit_case: str | None = None,
    failed_case: str | None = None,
    error_case: str | None = None,
    skipped_case: str | None = None,
) -> None:
    """Write a small JUnit report covering the configured names."""
    suite = ET.Element("testsuite")
    for _, name in transaction_safety._REGRESSION_CASES:
        if name == omit_case:
            continue
        case = ET.SubElement(suite, "testcase", name=name)
        if name == failed_case:
            ET.SubElement(case, "failure", message="assertion failed")
        if name == error_case:
            ET.SubElement(case, "error", message="runner error")
        if name == skipped_case:
            ET.SubElement(case, "skipped", message="skipped in fixture")
    path.write_text(ET.tostring(suite, encoding="unicode"), encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_exit"),
    [
        ("success", "pass", 0),
        ("assertion_failure", "fail", 2),
        ("runner_error", "inconclusive", 2),
        ("skipped_case", "inconclusive", 2),
        ("missing_case", "inconclusive", 2),
        ("no_junit", "inconclusive", 2),
        ("invalid_junit", "inconclusive", 2),
        ("pytest_unavailable", "inconclusive", 2),
    ],
)
def test_simulation_result_status_requires_complete_passing_junit(
    monkeypatch, tmp_path, capsys, case, expected_status, expected_exit
):
    def fake_run(command, **kwargs):
        junit_path = Path(command[command.index("--junitxml") + 1])
        if case == "pytest_unavailable":
            raise FileNotFoundError("pytest")
        if case == "no_junit":
            return SimpleNamespace(returncode=1)
        if case == "invalid_junit":
            junit_path.write_text("not xml", encoding="utf-8")
            return SimpleNamespace(returncode=1)
        if case == "assertion_failure":
            _junit_report(
                junit_path,
                failed_case=transaction_safety._REGRESSION_CASES[0][1],
            )
            return SimpleNamespace(returncode=1)
        if case == "runner_error":
            _junit_report(
                junit_path,
                error_case=transaction_safety._REGRESSION_CASES[0][1],
            )
            return SimpleNamespace(returncode=1)
        if case == "skipped_case":
            _junit_report(
                junit_path,
                skipped_case=transaction_safety._REGRESSION_CASES[0][1],
            )
            return SimpleNamespace(returncode=0)
        if case == "missing_case":
            _junit_report(
                junit_path,
                omit_case=transaction_safety._REGRESSION_CASES[-1][1],
            )
            return SimpleNamespace(returncode=0)
        _junit_report(junit_path)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(transaction_safety.subprocess, "run", fake_run)
    result = transaction_safety.execute_transaction_safety_simulation(
        tmp_path / "hil-results"
    )
    reports = list(
        (tmp_path / "hil-results").rglob("transaction-safety-simulation.json")
    )
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["simulation_status"] == expected_status
    assert result == expected_exit
    if case == "assertion_failure":
        assert report["failures"] == 1
        assert any(step["outcome"] == "fail" for step in report["steps"])
    if case == "pytest_unavailable":
        assert "pip install -r requirements_test.txt" in capsys.readouterr().out
    if case == "skipped_case":
        assert report["skipped"] == 1
    if case == "missing_case":
        assert report["missing_cases"]
