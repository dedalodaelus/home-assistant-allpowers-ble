"""Validation gates for firmware and route-scoped HIL matrix v2."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from scripts import validate_hil_qualification as validator


def _digest_manifest(root: Path, evidence: list[str]) -> dict[str, str]:
    return {
        item: hashlib.sha256((root / item).read_bytes()).hexdigest()
        for item in sorted(evidence)
    }


def _result(
    evidence: str, tmp_path: Path, captured_at: datetime, *, status: str = "pass"
) -> dict[str, Any]:
    evidence_paths = [evidence]
    return {
        "status": status,
        "evidence": [evidence],
        "physical_review": {
            "outcome": "confirmed_pass" if status == "pass" else "confirmed_fail",
            "reviewed_by": "independent-reviewer",
            "reviewed_at": captured_at.isoformat(),
            "evidence_sha256": _digest_manifest(tmp_path, evidence_paths),
        },
    }


def _capture_fixture(
    tmp_path: Path,
    route: str,
    name: str,
    *,
    firmware_version: str,
    captured_at: str,
    row_run_id: str | None = None,
    session_id: str | None = None,
    build_fingerprint: str = "b" * 64,
    verdict: str = "ok",
    not_applicable_capability: str | None = None,
) -> str:
    relative = f"tests/hil/fixtures/{name}.jsonl"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    def frame_for(operation: str, direction: str) -> str:
        if direction == "notify":
            data = bytes.fromhex("A5 65 B1 00 01 08 01 00 64 00 00 00 00 00 00 00")
        elif operation in {"stale_timeout_recovery", "reconnect_failover"}:
            return validator.STATUS_REQUEST.hex()
        elif operation in {"eco_mode", "work_mode", "eco_timeout", "car_charger"}:
            setting_flags = {
                "eco_mode": 0x01,
                "work_mode": 0x00,
                "eco_timeout": 0x00,
                "car_charger": 0x00,
            }[operation]
            data = bytes(
                (0xA5, 0x65, 0x00, 0xB1, 0x01, 0x02, 0x02, setting_flags, 0x01, 0x00)
            )
        elif operation in {"ac_output", "dc_output", "light_output"}:
            output_flag = {"ac_output": 0x02, "dc_output": 0x01, "light_output": 0x20}[
                operation
            ]
            data = bytes((0xA5, 0x65, 0x00, 0xB1, 0x01, 0x01, 0x00, output_flag, 0x00))
        else:
            data = bytes.fromhex("A5 65 00 B1 01 01 00 03 00")
        checksum = 0
        for byte in data[:-1]:
            checksum ^= byte
        return (data[:-1] + bytes((checksum,))).hex()

    rows = [
        {
            "route": route,
            "revision_id": "r600-hw03",
            "firmware_version": firmware_version,
            "run_id": row_run_id or name,
            "session_id": session_id or f"session-{row_run_id or name}",
            "build_fingerprint": build_fingerprint,
            "operation_id": scenario,
            "captured_at": captured_at,
            "scenario": scenario,
            "direction": "write"
            if scenario
            in {
                "rapid_consecutive_writes",
                "stale_timeout_recovery",
                "reconnect_failover",
            }
            else "notify",
            "frame_hex": frame_for(
                scenario,
                "write"
                if scenario
                in {
                    "rapid_consecutive_writes",
                    "stale_timeout_recovery",
                    "reconnect_failover",
                }
                else "notify",
            ),
            "verdict": verdict,
            "physical_outcome": "fail" if verdict == "fail" else "pass",
        }
        for scenario in validator.REQUIRED_SCENARIOS
    ]
    for capability in validator.REQUIRED_WRITE_CAPABILITIES:
        if capability == not_applicable_capability:
            continue
        is_na = capability == not_applicable_capability
        rows.append(
            {
                "route": route,
                "revision_id": "r600-hw03",
                "firmware_version": firmware_version,
                "run_id": row_run_id or name,
                "session_id": session_id or f"session-{row_run_id or name}",
                "build_fingerprint": build_fingerprint,
                "operation_id": capability,
                "captured_at": captured_at,
                "capability": capability,
                "requested_state": {
                    "ac_output": True,
                    "dc_output": True,
                    "light_output": True,
                    "eco_mode": True,
                    "work_mode": 0,
                    "eco_timeout": 1,
                    "car_charger": False,
                }[capability],
                "direction": "observe" if is_na else "write",
                "frame_hex": frame_for(capability, "write"),
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


def test_protocol_capture_validation_checks_documented_frames() -> None:
    assert validator._valid_protocol_frame(
        "a56500b10101000372", "write", "rapid_consecutive_writes"
    )
    assert validator._valid_protocol_frame(
        "a565b10001080100640000000000001d", "notify", "golden_vector_replay"
    )
    assert validator._valid_protocol_frame(
        "a56500b1010202010170", "write", "eco_mode", requested_state=True
    )
    preserved_unknown_bit = _with_xor_checksum(
        bytes.fromhex("A5 65 00 B1 01 02 02 81 01")
    ).hex()
    assert validator._valid_protocol_frame(
        preserved_unknown_bit, "write", "eco_mode", requested_state=True
    )
    reserved_mode = _with_xor_checksum(
        bytes.fromhex("A5 65 00 B1 01 02 02 06 01")
    ).hex()
    assert not validator._valid_protocol_frame(
        reserved_mode, "write", "work_mode", requested_state=3
    )
    assert not validator._valid_protocol_frame(
        "AA550E00107A0102005A6BC1000000", "notify", "golden_vector_replay"
    )
    assert not validator._valid_protocol_frame(
        "a565b1000108010064000000000000", "notify", "golden_vector_replay"
    )  # declared length does not match
    assert not validator._valid_protocol_frame(
        "a565b100010801006400000000000001", "notify", "golden_vector_replay"
    )  # invalid checksum
    assert not validator._valid_protocol_frame(
        "a565b100010804006400000000000018", "notify", "golden_vector_replay"
    )  # unsupported command
    assert not validator._valid_protocol_frame(
        "a565b10001080100640000000000001d", "write", "golden_vector_replay"
    )  # notification cannot prove a write
    assert not validator._valid_protocol_frame(
        "a56500b10101000372", "write", "soak_24h"
    )  # unrelated operation


@pytest.mark.parametrize(
    "capability,mutation",
    [
        ("eco_mode", "frame_targets_other_field"),
        ("eco_timeout", "unsupported_timeout"),
        ("ac_output", "unsupported_transition"),
    ],
)
def test_strict_gate_rejects_capability_frame_false_positives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    mutation: str,
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    captured_at = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "local_adapter", f"invalid-{mutation}", captured_at)
    capture_path = tmp_path / run["evidence"][0]
    rows = [json.loads(line) for line in capture_path.read_text().splitlines()]
    row = next(item for item in rows if item.get("capability") == capability)
    if mutation == "frame_targets_other_field":
        data = bytes.fromhex("A5 65 00 B1 01 02 02 02 01")
        row["frame_hex"] = _with_xor_checksum(data).hex()
    elif mutation == "unsupported_timeout":
        data = bytes.fromhex("A5 65 00 B1 01 02 02 00 03")
        row["frame_hex"] = _with_xor_checksum(data).hex()
    else:
        data = bytes.fromhex("A5 65 00 B1 01 01 00 01")
        row["frame_hex"] = _with_xor_checksum(data).hex()
    capture_path.write_text(
        "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8"
    )
    digest = _digest_manifest(tmp_path, run["evidence"])
    for result in [*run["scenarios"].values(), *run["write_capabilities"].values()]:
        result["physical_review"]["evidence_sha256"] = digest

    matrix_path = tmp_path / "matrix.json"
    matrix = _matrix([run])
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )

    assert any(capability in error and "capture row" in error for error in errors)


def _with_xor_checksum(data: bytes) -> bytes:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return data + bytes((checksum,))


def test_strict_gate_rejects_undocumented_protocol_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    captured_at = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "local_adapter", "unsupported-model", captured_at)
    matrix = _matrix([run])
    revision = matrix["revisions"][0]
    revision.update({"id": "ap-r1500-hw03", "model": "AP R1500"})

    capture_path = tmp_path / run["evidence"][0]
    rows = [json.loads(line) for line in capture_path.read_text().splitlines()]
    for row in rows:
        row["revision_id"] = revision["id"]
    capture_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    digest = _digest_manifest(tmp_path, run["evidence"])
    for result in [*run["scenarios"].values(), *run["write_capabilities"].values()]:
        result["physical_review"]["evidence_sha256"] = digest

    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    strict_errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )
    assert any("unsupported protocol profile" in error for error in strict_errors)

    non_strict_errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=30
    )
    assert not any(
        "unsupported protocol profile" in error for error in non_strict_errors
    )


def test_strict_pass_rejects_scoped_reviewed_aa55_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    captured_at = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "local_adapter", "aa55-run", captured_at)
    capture_path = tmp_path / run["evidence"][0]
    rows = [json.loads(line) for line in capture_path.read_text().splitlines()]
    rows[0]["frame_hex"] = "AA550E00107A0102005A6BC1000000"
    capture_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    digest = _digest_manifest(tmp_path, run["evidence"])
    for result in [*run["scenarios"].values(), *run["write_capabilities"].values()]:
        result["physical_review"]["evidence_sha256"] = digest

    matrix_path = tmp_path / "matrix.json"
    matrix = _matrix([run])
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=True, max_age_days=30
    )

    assert any(
        "golden_vector_replay" in error and "capture row" in error for error in errors
    )


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
        session_id=f"session-{run_id}",
        verdict="fail" if qualification == "fail" else "ok",
        not_applicable_capability=not_applicable_capability,
    )
    capabilities = {
        key: _result(evidence, tmp_path, captured_at)
        for key in validator.REQUIRED_WRITE_CAPABILITIES
    }
    if not_applicable_capability:
        absence_evidence = f"tests/hil/fixtures/{run_id}-absence.md"
        absence_path = tmp_path / absence_evidence
        absence_path.parent.mkdir(parents=True, exist_ok=True)
        absence_path.write_text(
            "Independent inspection confirms the control is physically absent.\n",
            encoding="utf-8",
        )
        capabilities[not_applicable_capability] = {
            "status": "not_applicable",
            "rationale": "This hardware revision does not expose this control.",
            "evidence": [absence_evidence],
        }
    if qualification == "fail":
        scenarios = {
            key: _result(evidence, tmp_path, captured_at, status="fail")
            for key in validator.REQUIRED_SCENARIOS
        }
        capabilities = {
            key: _result(evidence, tmp_path, captured_at, status="fail")
            for key in validator.REQUIRED_WRITE_CAPABILITIES
        }
    else:
        scenarios = {
            key: _result(evidence, tmp_path, captured_at)
            for key in validator.REQUIRED_SCENARIOS
        }
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
        "session_id": f"session-{run_id}",
        "build_fingerprint": "b" * 64,
        "installed_fingerprint": "b" * 64,
        "checkout_fingerprint": "b" * 64,
        "authorization_context": "production",
        "authorization_context_source": "reviewed_production_session",
        "feature_presence": {
            key: {
                "status": "absent" if key == not_applicable_capability else "present",
                "evidence": [
                    f"tests/hil/fixtures/{run_id}-absence.md"
                    if key == not_applicable_capability
                    else evidence
                ],
                **(
                    {
                        "reviewed_by": "physical-review",
                        "reviewed_at": captured_at.isoformat(),
                        "evidence_sha256": _digest_manifest(
                            tmp_path,
                            [f"tests/hil/fixtures/{run_id}-absence.md"],
                        ),
                    }
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
                "model": "ALLPOWERS R600",
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


def test_feature_scope_ignores_other_controls_and_unselected_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(
        tmp_path,
        "active_proxy",
        "ac-only",
        now,
        not_applicable_capability="light_output",
    )
    run["qualification_status"] = "inconclusive"
    for feature in validator.REQUIRED_WRITE_CAPABILITIES:
        if feature in {"ac_output", "light_output"}:
            continue
        run["write_capabilities"][feature] = {
            "status": "inconclusive",
            "evidence": [],
        }
        run["feature_presence"][feature] = {"status": "unknown", "evidence": []}
    matrix = _matrix([run])
    matrix["revisions"].append(
        {
            "id": "volix-p1800-hw03",
            "model": "VOLIX P1800",
            "hardware_version": "0.3",
            "raw_hardware_version": "0x03",
            "privacy_review": "pass",
            "runs": [],
            "legacy_results": [],
        }
    )
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
        route_scope="active_proxy",
        revision_scope="r600-hw03",
        firmware_scope="1.0",
        feature_scope="ac_output",
    )

    assert errors == []


def test_reviewed_physical_absence_can_be_not_applicable_without_gatt_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(
        tmp_path,
        "active_proxy",
        "absent-light",
        now,
        not_applicable_capability="light_output",
    )
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
        route_scope="active_proxy",
        revision_scope="r600-hw03",
        firmware_scope="1.0",
        feature_scope="light_output",
    )

    assert errors == []


def test_not_applicable_rejects_unknown_or_unreviewed_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)

    def errors_for(run: dict[str, Any]) -> list[str]:
        matrix = _matrix([run])
        matrix_path = tmp_path / "matrix.json"
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        errors, _ = validator.validate_matrix(
            matrix,
            matrix_path=matrix_path,
            require_pass=True,
            max_age_days=30,
            route_scope="active_proxy",
            revision_scope="r600-hw03",
            firmware_scope="1.0",
            feature_scope="light_output",
        )
        return errors

    unknown = _qualified_run(
        tmp_path,
        "active_proxy",
        "unknown-light",
        now,
        not_applicable_capability="light_output",
    )
    unknown["feature_presence"]["light_output"]["status"] = "unknown"
    assert any("cannot be not_applicable" in error for error in errors_for(unknown))

    unreviewed = _qualified_run(
        tmp_path,
        "active_proxy",
        "unreviewed-light",
        now,
        not_applicable_capability="light_output",
    )
    unreviewed["feature_presence"]["light_output"].pop("reviewed_by")
    assert any(
        "requires reviewed evidence and reviewer" in error
        for error in errors_for(unreviewed)
    )

    no_evidence = _qualified_run(
        tmp_path,
        "active_proxy",
        "no-absence-evidence",
        now,
        not_applicable_capability="light_output",
    )
    no_evidence["feature_presence"]["light_output"]["evidence"] = []
    assert any(
        "evidence must be a non-empty list" in error
        for error in errors_for(no_evidence)
    )


def test_feature_scope_rejects_unverified_provenance_and_crossed_capture_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)

    def errors_for(run: dict[str, Any]) -> list[str]:
        matrix = _matrix([run])
        matrix_path = tmp_path / "matrix.json"
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        errors, _ = validator.validate_matrix(
            matrix,
            matrix_path=matrix_path,
            require_pass=True,
            max_age_days=30,
            route_scope="active_proxy",
            revision_scope="r600-hw03",
            firmware_scope="1.0",
            feature_scope="ac_output",
        )
        return errors

    experimental = _qualified_run(tmp_path, "active_proxy", "experimental", now)
    experimental["authorization_context"] = "experimental_hil"
    experimental["authorization_context_source"] = "experimental_hil_grant"
    assert any(
        "experimental_hil evidence cannot claim" in error
        for error in errors_for(experimental)
    )

    missing_provenance = _qualified_run(
        tmp_path, "active_proxy", "missing-provenance", now
    )
    missing_provenance.pop("authorization_context_source")
    assert any(
        "authorization_context_source" in error
        for error in errors_for(missing_provenance)
    )

    crossed_session = _qualified_run(tmp_path, "active_proxy", "cross-session", now)
    crossed_session["session_id"] = "different-session"
    assert any(
        "route- and operation-bound capture row" in error
        for error in errors_for(crossed_session)
    )

    crossed_build = _qualified_run(tmp_path, "active_proxy", "cross-build", now)
    crossed_build["build_fingerprint"] = "c" * 64
    crossed_build["installed_fingerprint"] = "c" * 64
    crossed_build["checkout_fingerprint"] = "c" * 64
    assert any(
        "route- and operation-bound capture row" in error
        for error in errors_for(crossed_build)
    )

    unreviewed = _qualified_run(tmp_path, "active_proxy", "unreviewed-pass", now)
    unreviewed["write_capabilities"]["ac_output"].pop("physical_review")
    assert any(
        "requires an independent physical_review" in error
        for error in errors_for(unreviewed)
    )


def test_default_validation_rejects_unreviewed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "active_proxy", "unreviewed-default", now)
    run["write_capabilities"]["ac_output"].pop("physical_review")
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(_matrix([run])), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        _matrix([run]), matrix_path=matrix_path, require_pass=False, max_age_days=30
    )

    assert any(
        "ac_output requires an independent physical_review" in error for error in errors
    )


def test_default_validation_rejects_unreviewed_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(
        tmp_path, "active_proxy", "unreviewed-fail", now, qualification="inconclusive"
    )
    run["write_capabilities"]["ac_output"]["status"] = "fail"
    run["write_capabilities"]["ac_output"].pop("physical_review")
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=30
    )

    assert any(
        "ac_output requires an independent physical_review" in error for error in errors
    )


def test_default_validation_rejects_reviewed_fail_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(
        tmp_path,
        "active_proxy",
        "empty-evidence-fail",
        now,
        qualification="inconclusive",
    )
    result = run["write_capabilities"]["ac_output"]
    result["status"] = "fail"
    result["evidence"] = []
    result["physical_review"] = {
        "outcome": "confirmed_fail",
        "reviewed_by": "independent-reviewer",
        "reviewed_at": now.isoformat(),
    }
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=30
    )

    assert any(
        "write capability ac_output evidence must be a non-empty list" in error
        for error in errors
    )
    assert any(
        "write capability ac_output physical_review must independently confirm" in error
        for error in errors
    )


def test_default_validation_rejects_overall_pass_without_result_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(
        tmp_path,
        "active_proxy",
        "empty-overall-pass",
        now,
        qualification="inconclusive",
    )
    run["qualification_status"] = "pass"
    for result_map in (run["scenarios"], run["write_capabilities"]):
        for result in result_map.values():
            result["status"] = "inconclusive"
            result.pop("physical_review", None)
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=30
    )

    assert any(
        "qualification_status pass requires at least one reviewed" in error
        for error in errors
    )


def test_physical_review_binds_every_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "active_proxy", "multi-evidence", now)
    result = run["write_capabilities"]["ac_output"]
    second_evidence = "tests/hil/fixtures/second-evidence.md"
    second_path = tmp_path / second_evidence
    second_path.write_text("reviewed physical observation\n", encoding="utf-8")
    result["evidence"].append(second_evidence)
    result["physical_review"]["evidence_sha256"] = _digest_manifest(
        tmp_path, result["evidence"]
    )
    second_path.write_text("changed after review\n", encoding="utf-8")
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=30
    )

    assert any(
        "physical_review must independently confirm" in error for error in errors
    )


def test_strict_qualification_rejects_malformed_capture_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "active_proxy", "truncated-capture", now)
    capture = tmp_path / run["evidence"][0]
    with capture.open("a", encoding="utf-8") as stream:
        stream.write('{"truncated":\n')
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
        route_scope="active_proxy",
        revision_scope="r600-hw03",
        firmware_scope="1.0",
        feature_scope="ac_output",
    )

    assert any("route- and operation-bound capture row" in error for error in errors)


def test_scoped_route_mismatch_does_not_qualify_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    run = _qualified_run(tmp_path, "local_adapter", "local-only", now)
    matrix = _matrix([run])
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
        route_scope="active_proxy",
        revision_scope="r600-hw03",
        firmware_scope="1.0",
        feature_scope="ac_output",
    )

    assert any("no reviewed ac_output outcome" in error for error in errors)


def test_later_reviewed_feature_failure_supersedes_older_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    now = datetime.now(UTC) - timedelta(days=2)
    runs = [
        _qualified_run(tmp_path, "active_proxy", "ac-pass", now),
        _qualified_run(
            tmp_path,
            "active_proxy",
            "ac-fail",
            now + timedelta(days=1),
            qualification="fail",
        ),
    ]
    matrix = _matrix(runs)
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=True,
        max_age_days=30,
        route_scope="active_proxy",
        revision_scope="r600-hw03",
        firmware_scope="1.0",
        feature_scope="ac_output",
    )

    assert any("must be pass or justified not_applicable" in error for error in errors)


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
