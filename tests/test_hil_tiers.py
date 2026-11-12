"""Schema-validated tier evaluation against reviewed HIL evidence artifacts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import validate_hil_qualification as validator
from scripts.hil.tiers import evaluate_tiers

REVISION = {
    "id": "r600-hw03",
    "model": "ALLPOWERS R600",
    "hardware_version": "0.3",
    "raw_hardware_version": "0x03",
    "privacy_review": "pass",
    "legacy_results": [],
}
OUTPUTS = {"ac_output", "dc_output", "light_output"}


def _digest(root: Path, paths: list[str]) -> dict[str, str]:
    return {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def _write_json(root: Path, name: str, data: dict[str, Any]) -> str:
    path = f"tests/hil/fixtures/{name}.json"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return path


def _scope(run: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": revision["id"],
        "model": revision["model"],
        "hardware_revision": revision["hardware_version"],
        "firmware_version": run["firmware_version"],
        "route": run["route"],
        "run_id": run["run_id"],
        "session_id": run["session_id"],
        "build_fingerprint": run["build_fingerprint"],
        "captured_at": run["captured_at"],
    }


def _reviewed(
    root: Path,
    status: str,
    evidence: list[str],
    **review_fields: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "evidence": evidence,
        "physical_review": {
            "outcome": "confirmed_pass" if status == "pass" else "confirmed_fail",
            "reviewed_by": "independent-reviewer",
            "reviewed_at": datetime.now(UTC).isoformat(),
            "evidence_sha256": _digest(root, evidence),
            **review_fields,
        },
    }


def _frame(operation: str, requested_state: Any = None) -> str:
    if operation in {
        "golden_vector_replay",
        "soak_24h",
        "upgrade_reload",
        "vendor_app_contention",
    }:
        return "a565b10001080100640000000000001d"
    if operation in {"stale_timeout_recovery", "reconnect_failover"}:
        return validator.STATUS_REQUEST.hex()
    if operation in OUTPUTS:
        mask = {"ac_output": 0x02, "dc_output": 0x01, "light_output": 0x20}[operation]
        data = bytes(
            (0xA5, 0x65, 0x00, 0xB1, 0x01, 0x01, 0x00, mask if requested_state else 0)
        )
    elif operation in {"eco_mode", "work_mode", "eco_timeout", "car_charger"}:
        requested_state = requested_state if requested_state is not None else 0
        flags = {
            "eco_mode": int(bool(requested_state)),
            "work_mode": int(requested_state) << 1,
            "eco_timeout": 0,
            "car_charger": 0x10 if requested_state else 0,
        }[operation]
        timeout = int(requested_state) if operation == "eco_timeout" else 1
        data = bytes((0xA5, 0x65, 0x00, 0xB1, 0x01, 0x02, 0x02, flags, timeout))
    else:
        data = bytes((0xA5, 0x65, 0x00, 0xB1, 0x01, 0x01, 0x00, 0x03))
    checksum = 0
    for byte in data:
        checksum ^= byte
    return (data + bytes((checksum,))).hex()


def _capture(run: dict[str, Any], revision: dict[str, Any], root: Path) -> str:
    rows: list[dict[str, Any]] = []
    common = _scope(run, revision)
    for scenario in validator.REQUIRED_SCENARIOS:
        direction = (
            "write"
            if scenario
            in {
                "rapid_consecutive_writes",
                "stale_timeout_recovery",
                "reconnect_failover",
            }
            else "notify"
        )
        rows.append(
            {
                **common,
                "scenario": scenario,
                "operation_id": scenario,
                "direction": direction,
                "frame_hex": _frame(scenario),
                "verdict": "ok",
                "physical_outcome": "pass",
            }
        )
    for feature in validator.REQUIRED_WRITE_CAPABILITIES:
        if feature == "light_output":
            continue
        states = (
            (True, False)
            if feature in OUTPUTS
            else (False,)
            if feature == "car_charger"
            else (True,)
        )
        for requested in states:
            rows.append(
                {
                    **common,
                    "capability": feature,
                    "operation_id": feature,
                    "requested_state": requested
                    if feature in OUTPUTS | {"eco_mode", "car_charger"}
                    else (0 if feature == "work_mode" else 1),
                    "direction": "write",
                    "action_stage": "transition",
                    "frame_hex": _frame(feature, requested),
                    "verdict": "ok",
                    "physical_outcome": "pass",
                }
            )
            restored = {
                "eco_mode": True,
                "work_mode": 0,
                "eco_timeout": 1,
                "car_charger": False,
            }.get(feature, not requested if feature in OUTPUTS else None)
            if restored is not None:
                rows.append(
                    {
                        **common,
                        "capability": feature,
                        "operation_id": feature,
                        "requested_state": restored,
                        "direction": "write",
                        "action_stage": "restoration",
                        "frame_hex": _frame(feature, restored),
                        "verdict": "ok",
                        "physical_outcome": "pass",
                    }
                )
    path = f"tests/hil/fixtures/{run['run_id']}-capture.jsonl"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _bronze_records(
    run: dict[str, Any], revision: dict[str, Any], root: Path, capture: str
) -> dict[str, Any]:
    records = {}
    claims = {
        "identity": {"identity_confirmed": True},
        "protocol_probe": {},
        "fresh_telemetry": {
            "fresh": True,
            "plausible": True,
            "sample_age_seconds": 10,
        },
    }
    for claim, values in claims.items():
        artifact = _write_json(
            root,
            f"{run['run_id']}-{claim}",
            {
                "artifact_type": "bronze_evidence",
                "claim": claim,
                **_scope(run, revision),
                **values,
            },
        )
        evidence = [artifact]
        record = {"claim_artifact": artifact}
        if claim == "protocol_probe":
            evidence.append(capture)
            record["capture_evidence"] = capture
        proof = _reviewed(root, "pass", evidence)
        proof.update(record)
        records[claim] = proof
    return records


def _output_result(
    run: dict[str, Any],
    revision: dict[str, Any],
    root: Path,
    capture: str,
    feature: str,
    *,
    states: tuple[str, ...] = ("on", "off"),
) -> dict[str, Any]:
    evidence = [capture]
    transitions = []
    initial = {"on": "off", "off": "on"}
    for state in states:
        path = _write_json(
            root,
            f"{run['run_id']}-{feature}-{state}",
            {
                "artifact_type": "output_transition",
                **_scope(run, revision),
                "feature": feature,
                "requested_state": state,
                "observed_state": state,
                "initial_state": initial[state],
                "restored": True,
                "restored_state": initial[state],
            },
        )
        evidence.append(path)
        transitions.append(
            {
                "state": state,
                "evidence": path,
                "capture_evidence": capture,
                "restoration_capture": capture,
            }
        )
    return _reviewed(root, "pass", evidence, output_transitions=transitions)


def _setting_result(
    run: dict[str, Any],
    revision: dict[str, Any],
    root: Path,
    capture: str,
    feature: str,
) -> dict[str, Any]:
    path = _write_json(
        root,
        f"{run['run_id']}-{feature}-restore",
        {
            "artifact_type": "feature_restoration",
            **_scope(run, revision),
            "feature": feature,
            "restored": True,
            "pre_state": {
                "eco_mode": True,
                "work_mode": 0,
                "eco_timeout": 1,
                "car_charger": False,
            }[feature],
            "restored_state": {
                "eco_mode": True,
                "work_mode": 0,
                "eco_timeout": 1,
                "car_charger": False,
            }[feature],
        },
    )
    return _reviewed(
        root,
        "pass",
        [capture, path],
        restoration_evidence=path,
        restoration_capture=capture,
    )


def _soak_artifact(
    run: dict[str, Any],
    revision: dict[str, Any],
    root: Path,
    mode: str,
) -> str:
    captured = datetime.fromisoformat(run["captured_at"])
    duration = 86400 if mode != "short" else 1800
    started = captured - timedelta(seconds=duration)
    samples = []
    interval = 300
    count = duration // interval + 1
    for index in range(count):
        samples.append(
            {
                "event": "sample",
                "elapsed_seconds": index * interval,
                "status_fresh": True,
                "telemetry": {"battery": {"value": 50, "verified": True}},
            }
        )
    traces = [
        {
            "event": "trace_window",
            "utc": (started + timedelta(seconds=3600 * index)).isoformat(),
            "saved": True,
            "complete": True,
            "capture_id_present": True,
            "observed_route": run["route"],
        }
        for index in range(1, 25 if mode != "short" else 2)
    ]
    summary = {
        "event": "segment_summary",
        **_scope(run, revision),
        "route": run["route"],
        "segment_started_utc": started.isoformat(),
        "segment_ended_utc": captured.isoformat(),
        "measured_seconds": duration,
        "sample_count": len(samples),
        "minimum_samples": 289,
        "trace_count": len(traces),
        "minimum_traces": 24,
        "maximum_gap_seconds": 300,
        "allowed_gap_seconds": 600,
        "dropped_traces": 0,
        "clock_discontinuities": 0,
        "outcome": "pass",
        "resumed": mode == "resumed",
        "interrupted": mode == "interrupted",
        "uninterrupted": mode == "normal",
    }
    path = f"tests/hil/fixtures/{run['run_id']}-soak-evidence.jsonl"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n"
            for row in [summary, *samples, *traces]
        ),
        encoding="utf-8",
    )
    return path


def _matrix_fixture(
    root: Path,
    tier: str,
    *,
    soak_mode: str = "normal",
    run_id: str = "tier-run",
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    revision = dict(REVISION)
    run_time = captured_at or datetime.now(UTC).replace(microsecond=0)
    stamp = run_time.isoformat()
    run: dict[str, Any] = {
        "run_id": run_id,
        "firmware_version": "1.0",
        "route": "active_proxy",
        "captured_at": stamp,
        "tool_version": "1.0",
        "integration_version": "1.0",
        "build_verification": "verified",
        "capture_completeness": "complete",
        "qualification_status": "pending" if tier == "bronze" else "pass",
        "session_id": f"session-{run_id}",
        "build_fingerprint": "b" * 64,
        "installed_fingerprint": "b" * 64,
        "checkout_fingerprint": "b" * 64,
        "authorization_context": "production",
        "authorization_context_source": "reviewed_production_session",
    }
    capture = _capture(run, revision, root)
    run["evidence"] = [capture]
    run["bronze_evidence"] = _bronze_records(run, revision, root, capture)
    absent = "tests/hil/fixtures/light-absence.md"
    (root / absent).parent.mkdir(parents=True, exist_ok=True)
    (root / absent).write_text(
        "Reviewed physical absence of light.\n", encoding="utf-8"
    )
    run["feature_presence"] = {
        feature: (
            {
                "status": "absent",
                "reviewed_by": "independent-reviewer",
                "reviewed_at": stamp,
                "evidence": [absent],
                "evidence_sha256": _digest(root, [absent]),
            }
            if feature == "light_output"
            else {"status": "present"}
        )
        for feature in validator.REQUIRED_WRITE_CAPABILITIES
    }
    scenarios = {
        key: {"status": "pending", "evidence": []}
        for key in validator.REQUIRED_SCENARIOS
    }
    capabilities = {
        key: {"status": "pending", "evidence": []}
        for key in validator.REQUIRED_WRITE_CAPABILITIES
    }
    if tier in {"silver", "gold", "platinum"}:
        scenarios["reconnect_failover"] = _reviewed(root, "pass", [capture])
        for feature in ("ac_output", "dc_output"):
            capabilities[feature] = _output_result(
                run, revision, root, capture, feature
            )
    if tier in {"gold", "platinum"}:
        for feature in validator.REQUIRED_WRITE_CAPABILITIES:
            if feature == "light_output":
                capabilities[feature] = {
                    "status": "not_applicable",
                    "rationale": "Reviewed physical absence.",
                    "evidence": [absent],
                }
            elif feature not in {"ac_output", "dc_output"}:
                capabilities[feature] = _setting_result(
                    run, revision, root, capture, feature
                )
        for scenario in (
            "rapid_consecutive_writes",
            "stale_timeout_recovery",
        ):
            scenarios[scenario] = _reviewed(root, "pass", [capture])
    if tier == "platinum":
        soak = _soak_artifact(run, revision, root, soak_mode)
        scenarios["soak_24h"] = _reviewed(
            root, "pass", [capture, soak], soak_artifact=soak
        )
    run["scenarios"] = scenarios
    run["write_capabilities"] = capabilities
    revision["runs"] = [run]
    return {
        "schema_version": 3,
        "generated_at": datetime.now(UTC).date().isoformat(),
        "revisions": [revision],
    }


def _validate_and_evaluate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    matrix: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    errors, _ = validator.validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=None,
    )
    return errors, evaluate_tiers(matrix)


def test_validator_and_evaluator_allow_bronze_while_write_gates_are_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "bronze")
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "bronze"


def test_schema_valid_reviewed_outputs_and_absent_light_earn_silver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "silver")
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "silver"
    assert "light_output" in result["tiers"][0]["exclusions"]


def test_off_only_capture_does_not_satisfy_output_transition_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "silver")
    run = matrix["revisions"][0]["runs"][0]
    output = _output_result(
        run,
        matrix["revisions"][0],
        tmp_path,
        run["evidence"][0],
        "ac_output",
    )
    run["write_capabilities"]["ac_output"] = output
    capture_path = tmp_path / run["evidence"][0]
    rows = [
        json.loads(line)
        for line in capture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n"
            for row in rows
            if not (
                row.get("capability") == "ac_output"
                and row.get("requested_state") is True
            )
        ),
        encoding="utf-8",
    )
    for records in (
        run["bronze_evidence"],
        run["scenarios"],
        run["write_capabilities"],
    ):
        for record in records.values():
            review = record.get("physical_review")
            if isinstance(review, dict) and run["evidence"][0] in record.get(
                "evidence", []
            ):
                review["evidence_sha256"] = _digest(tmp_path, record["evidence"])
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert any("on transition lacks bound observation" in error for error in errors)
    assert result["tiers"][0]["tier"] == "bronze"


def test_gold_requires_reviewed_feature_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "gold")
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "gold"


def test_gold_rejects_restoration_without_bound_physical_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "gold")
    run = matrix["revisions"][0]["runs"][0]
    run["write_capabilities"]["eco_mode"]["physical_review"]["restoration_capture"] = (
        "tests/hil/fixtures/missing-restoration.jsonl"
    )
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert any("safe-restoration evidence" in error for error in errors)
    assert result["tiers"][0]["tier"] == "silver"


def _exclude_feature_contract(
    matrix: dict[str, Any], root: Path, feature: str, *, safe: bool = True
) -> None:
    revision = matrix["revisions"][0]
    rationale = (
        "This optional control is documented as unavailable and safely disabled."
    )
    evidence = _write_json(
        root,
        f"contract-{feature}",
        {
            "artifact_type": "feature_contract",
            "revision_id": revision["id"],
            "model": revision["model"],
            "hardware_revision": revision["hardware_version"],
            "feature": feature,
            "safely_disabled": safe,
            "rationale": rationale,
        },
    )
    contract = _reviewed(root, "pass", [evidence])
    contract.update(
        status="excluded",
        safely_disabled=safe,
        rationale=rationale,
    )
    revision["feature_contract"] = {feature: contract}
    revision["runs"][0]["feature_presence"][feature] = {"status": "unknown"}
    revision["runs"][0]["write_capabilities"][feature] = {
        "status": "pending",
        "evidence": [],
    }


def test_gold_accepts_only_reviewed_revision_contract_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "gold")
    _exclude_feature_contract(matrix, tmp_path, "car_charger")
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "gold"
    assert any("car_charger" in item for item in result["tiers"][0]["exclusions"])


def test_free_form_exclusion_boolean_cannot_omit_unknown_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "gold")
    run = matrix["revisions"][0]["runs"][0]
    run["feature_presence"]["car_charger"] = {"status": "unknown"}
    run["write_capabilities"]["car_charger"] = {"status": "pending", "evidence": []}
    run["contract_exclusions"] = {
        "car_charger": {
            "documented_unavailable": True,
            "safely_disabled": True,
            "rationale": "unchecked free-form assertion",
        }
    }
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "silver"


@pytest.mark.parametrize("mode", ["short", "resumed", "interrupted"])
def test_short_resumed_and_interrupted_soaks_cannot_earn_platinum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    matrix = _matrix_fixture(tmp_path, "platinum", soak_mode=mode)
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert any("24-hour soak artifact" in error for error in errors)
    assert result["tiers"][0]["tier"] == "gold"


def test_actual_complete_soak_artifact_can_earn_platinum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "platinum")
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "platinum"


def test_incomplete_provenance_stays_unqualified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "bronze")
    run = matrix["revisions"][0]["runs"][0]
    run["capture_completeness"] = "incomplete"
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "unqualified"


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_missing_or_tampered_bronze_evidence_cannot_earn_bronze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
):
    matrix = _matrix_fixture(tmp_path, "bronze")
    proof = matrix["revisions"][0]["runs"][0]["bronze_evidence"]["identity"]
    if mutation == "missing":
        missing = "tests/hil/fixtures/nonexistent-identity.json"
        proof["claim_artifact"] = missing
        proof["evidence"] = [missing]
    else:
        artifact_path = tmp_path / proof["claim_artifact"]
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["identity_confirmed"] = False
        artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors
    assert result["tiers"][0]["tier"] == "unqualified"


def test_later_reviewed_failure_supersedes_prior_feature_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    now = datetime.now(UTC).replace(microsecond=0)
    older = _matrix_fixture(
        tmp_path, "gold", run_id="older", captured_at=now - timedelta(seconds=2)
    )
    newer = _matrix_fixture(
        tmp_path,
        "bronze",
        run_id="newer",
        captured_at=now - timedelta(seconds=1),
    )
    older_run = older["revisions"][0]["runs"][0]
    newer_run = newer["revisions"][0]["runs"][0]
    # A failed result has its own reviewed, scope-bound capture and cannot inherit
    # the older PASS even though the two runs share model/firmware/route.
    capture = newer_run["evidence"][0]
    newer_run["write_capabilities"]["eco_mode"] = _reviewed(tmp_path, "fail", [capture])
    newer_run["write_capabilities"]["eco_mode"]["physical_review"]["outcome"] = (
        "confirmed_fail"
    )
    older["revisions"][0]["runs"] = [older_run, newer_run]
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    result = evaluate_tiers(older)["tiers"][0]
    assert "eco_mode" not in result["qualified_features"]


def test_reviewed_experimental_hil_result_can_contribute_after_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    matrix = _matrix_fixture(tmp_path, "bronze")
    run = matrix["revisions"][0]["runs"][0]
    run["authorization_context"] = "experimental_hil"
    run["authorization_context_source"] = "experimental_hil_grant"
    run["qualification_status"] = "inconclusive"
    run["write_capabilities"]["ac_output"] = _output_result(
        run,
        matrix["revisions"][0],
        tmp_path,
        run["evidence"][0],
        "ac_output",
    )
    errors, result = _validate_and_evaluate(monkeypatch, tmp_path, matrix)
    assert errors == []
    assert result["tiers"][0]["tier"] == "bronze"
    assert "ac_output" in result["tiers"][0]["qualified_features"]
