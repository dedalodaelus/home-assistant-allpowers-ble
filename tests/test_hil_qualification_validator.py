"""Validation gates for firmware and route-scoped HIL matrix v2."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from scripts import validate_hil_qualification as validator


def _result(evidence: str) -> dict[str, Any]:
    return {"status": "pass", "evidence": [evidence]}


def _capture_fixture(
    tmp_path: Path,
    route: str,
    name: str,
    *,
    firmware_version: str,
    captured_at: str,
    row_run_id: str | None = None,
    verdict: str = "ok",
    not_applicable_capability: str | None = None,
) -> str:
    relative = f"tests/hil/fixtures/{name}.jsonl"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "route": route,
            "revision_id": "r600-hw03",
            "firmware_version": firmware_version,
            "run_id": row_run_id or name,
            "captured_at": captured_at,
            "scenario": scenario,
            "direction": "notify" if scenario == "golden_vector_replay" else "write",
            "frame_hex": "AA550E00107A0102005A6BC1000000",
            "verdict": verdict,
            "physical_outcome": "fail" if verdict == "fail" else "pass",
        }
        for scenario in validator.REQUIRED_SCENARIOS
    ]
    for capability in validator.REQUIRED_WRITE_CAPABILITIES:
        is_na = capability == not_applicable_capability
        rows.append(
            {
                "route": route,
                "revision_id": "r600-hw03",
                "firmware_version": firmware_version,
                "run_id": row_run_id or name,
                "captured_at": captured_at,
                "capability": capability,
                "direction": "observe" if is_na else "write",
                "frame_hex": "AA550E00107A0102005A6BC1000000",
                "verdict": "not_applicable" if is_na else verdict,
                "physical_outcome": "not_applicable"
                if is_na
                else "fail"
                if verdict == "fail"
                else "pass",
            }
        )
    target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return relative


def _qualified_run(
    tmp_path: Path,
    route: str,
    run_id: str,
    captured_at: datetime,
    *,
    qualification: str = "pass",
    firmware_version: str = "1.0",
    not_applicable_capability: str | None = None,
) -> dict[str, Any]:
    evidence = _capture_fixture(
        tmp_path,
        route,
        run_id,
        firmware_version=firmware_version,
        captured_at=captured_at.isoformat(),
        verdict="fail" if qualification == "fail" else "ok",
        not_applicable_capability=not_applicable_capability,
    )
    capabilities = {
        key: _result(evidence) for key in validator.REQUIRED_WRITE_CAPABILITIES
    }
    if not_applicable_capability:
        capabilities[not_applicable_capability] = {
            "status": "not_applicable",
            "rationale": "This hardware revision does not expose this control.",
            "evidence": [evidence],
        }
    if qualification == "fail":
        scenarios = {
            key: {"status": "fail", "evidence": [evidence]}
            for key in validator.REQUIRED_SCENARIOS
        }
        capabilities = {
            key: {"status": "fail", "evidence": [evidence]}
            for key in validator.REQUIRED_WRITE_CAPABILITIES
        }
    else:
        scenarios = {key: _result(evidence) for key in validator.REQUIRED_SCENARIOS}
    return {
        "run_id": run_id,
        "firmware_version": firmware_version,
        "route": route,
        "captured_at": captured_at.isoformat(),
        "tool_version": "1.0",
        "integration_version": "1.0",
        "build_verification": "verified",
        "capture_completeness": "complete",
        "qualification_status": qualification,
        "feature_presence": {
            key: {
                "status": "absent" if key == not_applicable_capability else "present",
                "evidence": [evidence],
                **(
                    {"reviewed_by": "physical-review"}
                    if key == not_applicable_capability
                    else {}
                ),
            }
            for key in validator.REQUIRED_WRITE_CAPABILITIES
        },
        "evidence": [evidence],
        "scenarios": scenarios,
        "write_capabilities": capabilities,
    }


def _matrix(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "generated_at": date.today().isoformat(),
        "migration": {"from_schema_version": 1},
        "revisions": [
            {
                "id": "r600-hw03",
                "model": "R600",
                "hardware_version": "0.3",
                "raw_hardware_version": "0x03",
                "privacy_review": "pass",
                "runs": runs,
                "legacy_results": [],
            }
        ],
    }


def test_max_age_days_rejects_negative_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_hil_qualification.py", "--max-age-days", "-1"],
    )
    with pytest.raises(SystemExit) as error:
        validator.parse_args()
    assert error.value.code == 2


def test_require_pass_rejects_future_run_without_requiring_second_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC)
    matrix_path = tmp_path / "matrix.json"
    run = _qualified_run(
        tmp_path,
        "local_adapter",
        "future-run",
        now.replace(year=2099),
    )
    matrix = _matrix([run])
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
    )
    assert any("captured_at is in the future" in item for item in errors)
    assert not any("missing strict route" in item for item in errors)


def test_strict_gate_uses_latest_eligible_run_and_ignores_imported_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC)
    runs = [
        _qualified_run(
            tmp_path,
            "local_adapter",
            "old-fail",
            now - timedelta(days=3),
            qualification="fail",
        ),
        _qualified_run(
            tmp_path,
            "local_adapter",
            "local-pass",
            now - timedelta(days=2),
            not_applicable_capability="car_charger",
        ),
        _qualified_run(
            tmp_path,
            "active_proxy",
            "proxy-pass",
            now - timedelta(days=2),
        ),
        {
            **_qualified_run(
                tmp_path,
                "local_adapter",
                "archive-import",
                now,
            ),
            "qualification_status": "inconclusive",
            "capture_completeness": "unknown",
            "build_verification": "unverified",
            "scenarios": {
                key: {"status": "inconclusive", "evidence": []}
                for key in validator.REQUIRED_SCENARIOS
            },
            "write_capabilities": {
                key: {"status": "inconclusive", "evidence": []}
                for key in validator.REQUIRED_WRITE_CAPABILITIES
            },
        },
    ]
    matrix = _matrix(runs)
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert errors == []

    runs.append(
        _qualified_run(
            tmp_path,
            "local_adapter",
            "newer-fail",
            now - timedelta(days=1),
            qualification="fail",
        )
    )
    matrix = _matrix(runs)
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("current run newer-fail" in item for item in errors)
    assert any("must be pass in require-pass mode" in item for item in errors)


def test_one_verified_route_qualifies_without_the_other_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "active_proxy", "proxy-only", now)
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
        route_scope="active_proxy",
    )

    assert errors == []


def test_strict_gate_requires_routes_for_every_reported_firmware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    runs = [
        _qualified_run(
            tmp_path,
            route,
            f"{route}-fw-1.1",
            now,
            firmware_version="1.1",
        )
        for route in sorted(validator.REQUIRED_ROUTES)
    ]
    # A newer inconclusive import on an already qualified firmware must not
    # supersede its latest eligible route result.
    inconclusive_1_1 = _qualified_run(
        tmp_path,
        "local_adapter",
        "import-fw-1.1",
        now + timedelta(hours=1),
        firmware_version="1.1",
    )
    inconclusive_1_1.update(
        qualification_status="inconclusive",
        capture_completeness="unknown",
        build_verification="unverified",
        scenarios={
            key: {"status": "inconclusive", "evidence": []}
            for key in validator.REQUIRED_SCENARIOS
        },
        write_capabilities={
            key: {"status": "inconclusive", "evidence": []}
            for key in validator.REQUIRED_WRITE_CAPABILITIES
        },
    )
    runs.append(inconclusive_1_1)
    matrix_path = tmp_path / "matrix.json"

    def errors_for(current_runs: list[dict[str, Any]]) -> list[str]:
        matrix = _matrix(current_runs)
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        errors, _ = validator.validate_matrix(
            matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
        )
        return errors

    assert errors_for(runs) == []

    inconclusive_1_2 = _qualified_run(
        tmp_path,
        "local_adapter",
        "import-fw-1.2",
        now + timedelta(hours=2),
        firmware_version="1.2",
    )
    inconclusive_1_2.update(
        qualification_status="inconclusive",
        capture_completeness="unknown",
        build_verification="unverified",
        scenarios={
            key: {"status": "inconclusive", "evidence": []}
            for key in validator.REQUIRED_SCENARIOS
        },
        write_capabilities={
            key: {"status": "inconclusive", "evidence": []}
            for key in validator.REQUIRED_WRITE_CAPABILITIES
        },
    )
    runs.append(inconclusive_1_2)
    errors = errors_for(runs)
    assert any("firmware 1.2 has no eligible observed route" in item for item in errors)

    runs.extend(
        _qualified_run(
            tmp_path,
            route,
            f"{route}-fw-1.2",
            now + timedelta(hours=3),
            firmware_version="1.2",
        )
        for route in sorted(validator.REQUIRED_ROUTES)
    )
    assert errors_for(runs) == []


def test_strict_gate_rejects_note_only_or_wrong_route_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    runs = [
        _qualified_run(tmp_path, route, f"{route}-run", now)
        for route in sorted(validator.REQUIRED_ROUTES)
    ]
    note = "tests/hil/fixtures/note.md"
    note_path = tmp_path / note
    note_path.write_text("All tests passed.\n", encoding="utf-8")
    runs[0]["scenarios"]["golden_vector_replay"]["evidence"] = [note]
    matrix = _matrix(runs)
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("route- and operation-bound capture row" in item for item in errors)


def test_capture_evidence_is_bound_to_revision_firmware_run_and_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    runs = [
        _qualified_run(
            tmp_path,
            route,
            f"{route}-fw-{firmware}",
            now,
            firmware_version=firmware,
        )
        for firmware in ("1.1", "1.2")
        for route in sorted(validator.REQUIRED_ROUTES)
    ]
    matrix_path = tmp_path / "matrix.json"
    matrix = _matrix(runs)
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert errors == []

    source = next(run for run in runs if run["run_id"] == "local_adapter-fw-1.1")
    target = next(run for run in runs if run["run_id"] == "local_adapter-fw-1.2")
    target["scenarios"]["golden_vector_replay"]["evidence"] = source["evidence"]
    matrix = _matrix(runs)
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("route- and operation-bound capture row" in item for item in errors)

    target["scenarios"]["golden_vector_replay"]["evidence"] = [
        f"tests/hil/fixtures/{target['run_id']}.jsonl"
    ]
    target["captured_at"] = (now + timedelta(days=1)).isoformat()
    matrix = _matrix(runs)
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("route- and operation-bound capture row" in item for item in errors)

    target["captured_at"] = now.isoformat()
    matrix = _matrix(runs)
    matrix["revisions"][0]["id"] = "other-revision"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("route- and operation-bound capture row" in item for item in errors)

    wrong_route_name = (
        "local_adapter" if runs[0]["route"] == "active_proxy" else "active_proxy"
    )
    wrong_route = _capture_fixture(
        tmp_path,
        wrong_route_name,
        f"{runs[0]['run_id']}-route-mismatch",
        firmware_version=runs[0]["firmware_version"],
        captured_at=runs[0]["captured_at"],
        row_run_id=runs[0]["run_id"],
        verdict="ok",
    )
    runs[0]["scenarios"]["golden_vector_replay"]["evidence"] = [wrong_route]
    matrix = _matrix(runs)
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("route- and operation-bound capture row" in item for item in errors)
