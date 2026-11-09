"""Validation gates for firmware and route-scoped HIL matrix v2."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from scripts import validate_hil_qualification as validator


def _result(evidence: str) -> dict[str, Any]:
    return {"status": "pass", "evidence": [evidence]}


def test_max_age_days_rejects_negative_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_hil_qualification.py", "--max-age-days", "-1"],
    )
    with pytest.raises(SystemExit) as error:
        validator.parse_args()
    assert error.value.code == 2


def test_require_pass_rejects_future_and_missing_firmware_route_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    evidence = "tests/hil/fixtures/public.json"
    evidence_path = tmp_path / evidence
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"sanitized": true}\n', encoding="utf-8")
    matrix_path = tmp_path / "matrix.json"
    run = {
        "run_id": "future-run",
        "firmware_version": "1.0",
        "route": "local_adapter",
        "captured_at": "2099-01-01T00:00:00+00:00",
        "tool_version": "1.0",
        "integration_version": "1.0",
        "build_verification": "verified",
        "capture_completeness": "complete",
        "qualification_status": "pass",
        "evidence": [evidence],
        "scenarios": {key: _result(evidence) for key in validator.REQUIRED_SCENARIOS},
        "write_capabilities": {
            key: _result(evidence) for key in validator.REQUIRED_WRITE_CAPABILITIES
        },
    }
    matrix = {
        "schema_version": 2,
        "generated_at": date.today().isoformat(),
        "migration": {"from_schema_version": 1},
        "revisions": [
            {
                "id": "r600-hw03",
                "model": "R600",
                "hardware_version": "0.3",
                "raw_hardware_version": "0x03",
                "privacy_review": "pass",
                "runs": [run],
                "legacy_results": [],
            }
        ],
    }
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
    )
    assert any("captured_at is in the future" in item for item in errors)
    assert any("missing strict route" in item for item in errors)
