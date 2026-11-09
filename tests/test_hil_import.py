"""Import public HIL archives as scoped, inconclusive review evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.hil.api import HAAPIError
from scripts.hil.evidence import export_archive
from scripts.hil.importer import import_archive
from scripts.hil.qualification import migrate_v1_to_v2


def _fact(value: Any, source: str, verified: bool = True) -> dict[str, Any]:
    return {"value": value, "source": source, "verified": verified}


def _public_archive(path: Path) -> Path:
    run = path / "run"
    run.mkdir(parents=True)
    environment = {
        "schema_version": 1,
        "home_assistant": {},
        "runner_host": {},
        "integration": {
            "installed_manifest_version": _fact("1.2.3", "ha_ws_manifest_get"),
            "build_match": _fact("mismatch", "runner_repository_manifest"),
            "runner_checkout_commit": _fact(None, "unknown", False),
        },
    }
    session = {
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "qualification_status": "inconclusive",
        "mode": "read_only_preflight",
        "scenario_id": "read_only",
        "started_at": "2026-11-09T12:00:00+00:00",
        "device": {
            "model": _fact("R600", "ha_device_registry"),
            "hardware_version": _fact("0.3", "ha_device_registry"),
            "firmware_version": _fact("1.1", "ha_device_registry"),
        },
    }
    (run / "environment.json").write_text(json.dumps(environment), encoding="utf-8")
    (run / "session.json").write_text(json.dumps(session), encoding="utf-8")
    return export_archive(run, confirmed=True)


def _matrix(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    matrix = migrate_v1_to_v2(
        {
            "schema_version": 1,
            "generated_at": "2026-11-09",
            "retention": "Sanitized public evidence only.",
            "verified_revisions": [
                {
                    "id": "r600-hw03",
                    "model": "ALLPOWERS R600",
                    "hardware_version": "0.3",
                    "raw_hardware_version": "0x03",
                    "privacy_review": "pass",
                    "routes": {},
                    "scenarios": {},
                    "write_capabilities": {},
                }
            ],
        }
    )
    result = path / "qualification_matrix.json"
    result.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    return result


def test_import_defaults_to_preview_and_apply_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import validate_hil_qualification

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)

    assert import_archive(archive, matrix_path=matrix_path, repo_root=tmp_path) == 0
    preview = capsys.readouterr().out
    assert "no PASS results will be created" in preview
    assert "Observed route: unverified" in preview
    assert len(json.loads(matrix_path.read_text())["revisions"][0]["runs"]) == 0
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()

    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    revision = payload["revisions"][0]
    assert revision["id"] == "r600-hw03"
    imported = revision["runs"][0]
    assert imported["qualification_status"] == "inconclusive"
    assert imported["firmware_version"] == "1.1"
    assert imported["route"] == "unverified"
    assert imported["build_verification"] == "mismatch"
    assert all(
        result["status"] == "inconclusive" for result in imported["scenarios"].values()
    )
    assert all(
        result["status"] == "inconclusive"
        for result in imported["write_capabilities"].values()
    )
    evidence = [tmp_path / item for item in imported["evidence"]]
    assert evidence and all(item.is_file() for item in evidence)

    assert (
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
        == 0
    )
    assert len(json.loads(matrix_path.read_text())["revisions"][0]["runs"]) == 1
    matrix_before_conflict = matrix_path.read_bytes()
    evidence[0].write_text("conflicting contents", encoding="utf-8")
    with pytest.raises(HAAPIError, match="Existing import evidence conflicts"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert evidence[0].read_text(encoding="utf-8") == "conflicting contents"
    assert matrix_path.read_bytes() == matrix_before_conflict


def test_conflicting_run_identity_fails_before_writing_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import importer

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    proposal = importer._build_proposal(archive, matrix_path, repo_root=tmp_path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    conflicting_run = dict(proposal["run"])
    conflicting_run["evidence"] = []
    conflicting_run["reported_scenario_id"] = "different-run-content"
    matrix["revisions"][0]["runs"] = [conflicting_run]
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    matrix_before = matrix_path.read_bytes()

    with pytest.raises(HAAPIError, match="Archive identity conflicts"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert matrix_path.read_bytes() == matrix_before
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()


def test_fixture_collision_does_not_overwrite_or_leave_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import validate_hil_qualification
    from scripts.hil import importer

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    proposal = importer._build_proposal(archive, matrix_path, repo_root=tmp_path)
    destinations = list(proposal["destination_files"].items())
    first_path = destinations[0][0]
    collision_path = destinations[1][0]
    collision_path.parent.mkdir(parents=True)
    collision_path.write_bytes(b"pre-existing unrelated evidence")
    matrix_before = matrix_path.read_bytes()

    with pytest.raises(HAAPIError, match="Evidence file collision"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert collision_path.read_bytes() == b"pre-existing unrelated evidence"
    assert not first_path.exists()
    assert matrix_path.read_bytes() == matrix_before


def test_failed_matrix_validation_rolls_back_imported_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hil import importer
    from scripts import validate_hil_qualification

    archive = _public_archive(tmp_path)
    matrix_path = _matrix(tmp_path)
    before = matrix_path.read_bytes()
    monkeypatch.setattr(validate_hil_qualification, "ROOT", tmp_path)
    validate_original = importer.validate_matrix
    validation_calls = 0

    def fail_after_file_writes(
        *args: Any, **kwargs: Any
    ) -> tuple[list[str], list[str]]:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            return validate_original(*args, **kwargs)
        return ["forced validation failure"], []

    monkeypatch.setattr(importer, "validate_matrix", fail_after_file_writes)

    with pytest.raises(HAAPIError, match="Proposed matrix is invalid"):
        import_archive(archive, matrix_path=matrix_path, apply=True, repo_root=tmp_path)
    assert matrix_path.read_bytes() == before
    assert not (tmp_path / "tests" / "hil" / "fixtures").exists()
