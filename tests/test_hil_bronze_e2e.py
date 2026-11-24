"""Synthetic onboard-to-reviewed-Bronze pipeline regression coverage."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from scripts.hil import cli
from scripts.hil.qualification import migrate_v1_to_v3
from scripts.hil.tiers import evaluate_tiers
from scripts import validate_hil_qualification
from tests.hil.synthetic_bronze import SyntheticBronzeHA


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("entry_id", "other-entry", "Mismatched entry identifier"),
        ("capture_id", "other-capture", "Mismatched capture identifier"),
    ],
)
def test_synthetic_bronze_rejects_mismatched_capture_identifiers(
    field: str, value: str, message: str
) -> None:
    fake = SyntheticBronzeHA()
    fake.websocket(
        [
            {
                "type": "allpowers_ble/hil/start",
                "entry_id": "synthetic-entry",
                "run_id": "synthetic-run",
                "scenario_id": "read_only_snapshot",
                "requested_route": "auto",
            }
        ]
    )
    command = {
        "type": "allpowers_ble/hil/fetch",
        "entry_id": "synthetic-entry",
        "capture_id": "synthetic-capture",
    }
    command[field] = value
    with pytest.raises(AssertionError, match=message):
        fake.websocket([command])


def _temporary_checkout(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    gitignore = root / ".gitignore"
    shutil.copyfile(Path(__file__).resolve().parents[1] / ".gitignore", gitignore)
    template = root / ".github/PULL_REQUEST_TEMPLATE.md"
    template.parent.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / ".github" / "PULL_REQUEST_TEMPLATE.md",
        template,
    )
    matrix_path = root / "tests/hil/qualification_matrix.json"
    matrix_path.parent.mkdir(parents=True)
    matrix = migrate_v1_to_v3(
        {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).date().isoformat(),
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
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "test/bronze-e2e", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Synthetic test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "synthetic@example.invalid"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "add",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".gitignore",
            "tests/hil/qualification_matrix.json",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "test: initialize isolated matrix"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return root, matrix_path


def _onboard_answers(monkeypatch: pytest.MonkeyPatch, *, status_ok: bool) -> None:
    answers = iter(
        [
            "1",  # Select Bronze through the interactive onboard entrypoint.
            "RUN",
            "1",  # Test-only physical plausibility attestation.
            "yes",  # Create the local privacy-safe archive.
            "EXPORT",
            "yes",  # Apply the importer proposal to the isolated matrix.
            "APPLY IMPORT",
            "1",  # Operator attestation route.
            "Synthetic test operator",
            "yes",
            *(["pass", "yes", "pass", "yes", "pass", "yes"] if status_ok else []),
            "APPLY REVIEW",
        ]
    )

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", TTY())

    def answer(_prompt: str = "") -> str:
        return next(answers)

    monkeypatch.setattr(builtins, "input", answer)


@pytest.mark.parametrize("correlated_status", [True, False])
def test_onboard_bronze_runs_the_real_pipeline_in_an_empty_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    correlated_status: bool,
) -> None:
    """Prove Bronze qualification traverses acquisition, gate, ZIP, review and evaluation."""
    repo_root, matrix_path = _temporary_checkout(tmp_path / "checkout")
    workspace_matrix = (
        Path(__file__).resolve().parent / "hil" / "qualification_matrix.json"
    )
    original_workspace_matrix = workspace_matrix.read_bytes()
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    fake = SyntheticBronzeHA(correlated_status=correlated_status)
    monkeypatch.setattr(cli, "HAClient", lambda *_args, **_kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SYNTHETIC_PRIVATE_TOKEN")
    _onboard_answers(monkeypatch, status_ok=correlated_status)

    real_import_review = cli._onboard_import_review

    def import_review(archive: Path, output_dir: Path, requested_tier: str) -> bool:
        return real_import_review(
            archive,
            output_dir,
            requested_tier,
            repo_root=repo_root,
            matrix_path=matrix_path,
        )

    monkeypatch.setattr(cli, "_onboard_import_review", import_review)
    output_root = tmp_path / "runs"
    result = cli.main(
        [
            "onboard",
            "--ha-url",
            fake.origin,
            "--output-root",
            str(output_root),
        ]
    )

    (run_root,) = tuple(output_root.iterdir())
    private = run_root / "private"
    execution = json.loads((private / "tier-execution.json").read_text())
    matrix = json.loads(matrix_path.read_text())
    output = capsys.readouterr().out
    assert "Requested tier: Bronze" in output
    assert (
        "TEST GATE: PASS" in output
        if correlated_status
        else "TEST GATE: FAILED" in output
    )
    assert execution["requested_tier"] == "bronze"
    assert execution["execution_tier"] == "bronze"
    assert [row["step_id"] for row in execution["steps"]] == [
        "read_only_probe",
        "physical_plausibility_observation",
    ]
    assert all(
        row["outcome"] == ("pass" if correlated_status else "inconclusive")
        for row in execution["steps"]
    )
    lifecycle = [
        call[1] if call[0] == "websocket" else "refresh"
        for call in fake.calls
        if call[0] in {"websocket", "read_only_refresh"}
    ]
    assert lifecycle[0] == "allpowers_ble/hil/start"
    assert lifecycle[1] == "refresh"
    assert lifecycle[-1] == "allpowers_ble/hil/stop"
    assert all(command == "allpowers_ble/hil/fetch" for command in lifecycle[2:-1])
    assert fake.fetch_count >= 1
    assert fake.fetch_snapshots
    assert all(not snapshot["complete"] for snapshot in fake.fetch_snapshots)
    assert fake.stop_count == 1
    assert fake.stop_snapshot is not None
    assert fake.stop_snapshot["complete"] is True
    assert fake.stop_snapshot["termination_reason"] == "requested_stop"
    assert fake.stop_snapshot["events"][-1]["event"] == "capture_stopped"
    assert not fake.capture_active

    if correlated_status:
        assert result == 0, output
        assert len(matrix["revisions"][0]["runs"]) == 1
        imported = matrix["revisions"][0]["runs"][0]
        assert imported["route"] == "active_proxy"
        assert imported["build_verification"] == "verified"
        assert imported["installed_commit"]
        assert imported["bronze_evidence"]["protocol_probe"]["status"] == "pass"
        assert imported["bronze_evidence"]["fresh_telemetry"]["status"] == "pass"
        assert imported["bronze_evidence"]["identity"]["status"] == "pass"
        errors, _ = validate_hil_qualification.validate_matrix(
            matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
        )
        assert errors == []
        tier = evaluate_tiers(matrix)["tiers"][0]
        assert tier["tier"] == "bronze"
        assert tier["scope"] == {
            "revision_id": "r600-hw03",
            "model": "ALLPOWERS R600",
            "hardware_revision": "0.3",
            "firmware": "0.2",
            "observed_route": "active_proxy",
        }
        assert tier["assurance"]["bronze"] == "operator_attested"
        assert not any(
            item["tier"] in {"silver", "gold"}
            for item in evaluate_tiers(matrix)["tiers"]
        )
        assert "POST-REVIEW QUALIFICATION: attained bronze" in output
        archive = run_root / "hil-evidence.zip"
        with zipfile.ZipFile(archive) as exported:
            public_bytes = b"".join(exported.read(name) for name in exported.namelist())
        assert b"SYNTHETIC_PRIVATE_TOKEN" not in public_bytes
        assert b"synthetic-identifier" not in public_bytes
        assert b"synthetic-proxy" not in public_bytes
        assert not list(repo_root.glob("**/*.pcap"))
        assert fake.state_changing_calls == []
        refresh_calls = [call for call in fake.calls if call[0] == "read_only_refresh"]
        assert len(refresh_calls) == 1
        assert refresh_calls[0][1].entity_id == "button.synthetic_refresh"
    else:
        assert result == 2
        assert matrix["revisions"][0]["runs"] == []
        assert not (run_root / "hil-evidence.zip").exists()
        assert "TEST GATE: FAILED" in output
        assert "No ZIP export prompt" in output
        assert fake.state_changing_calls == []
    assert workspace_matrix.read_bytes() == original_workspace_matrix
    assert repo_root.is_relative_to(tmp_path)
    assert run_root.is_relative_to(tmp_path)


def test_onboard_silver_runs_acquisition_export_review_and_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise actual Silver acquisition, public handoff, review and tier evaluation."""
    repo_root, matrix_path = _temporary_checkout(tmp_path / "silver-checkout")
    workspace_matrix = (
        Path(__file__).resolve().parent / "hil" / "qualification_matrix.json"
    )
    original_workspace_matrix = workspace_matrix.read_bytes()
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    fake = SyntheticBronzeHA(silver=True)
    monkeypatch.setattr(cli, "HAClient", lambda *_args, **_kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SYNTHETIC_PRIVATE_TOKEN")

    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", TTY())

    def answer(prompt: str = "") -> str:
        if prompt == "Desired tier: ":
            return "2"
        if prompt.startswith("Action [1]"):
            fake.recovery_requested = True
            return "1"
        if "Type 1 after restoring the same BLE route" in prompt:
            fake.restore_requested = True
            return "1"
        if "Restore the selected route now" in prompt:
            fake.restore_requested = True
            return "1"
        if "APPLY IMPORT" in prompt:
            return "APPLY IMPORT"
        if "APPLY REVIEW" in prompt:
            return "APPLY REVIEW"
        if "EXPORT" in prompt:
            return "EXPORT"
        if prompt.startswith("Choose assurance") or prompt.startswith("Review route"):
            return "1"
        if "public identifier" in prompt:
            return "Synthetic test operator"
        if "[inconclusive/pass]" in prompt:
            return "pass"
        if prompt.startswith("Type RUN"):
            return "RUN"
        if (
            "Physical plausibility" in prompt
            or "Physical outcome" in prompt
            or "Physical result" in prompt
            or "Outcome [1-3]" in prompt
        ):
            return "1"
        if "[no/yes]" in prompt:
            return "yes"
        if "[y/N]" in prompt:
            return "yes"
        if "Type 1" in prompt:
            return "1"
        return "yes"

    monkeypatch.setattr(builtins, "input", answer)
    real_import_review = cli._onboard_import_review

    def import_review(archive: Path, output_dir: Path, requested_tier: str) -> bool:
        return real_import_review(
            archive,
            output_dir,
            requested_tier,
            repo_root=repo_root,
            matrix_path=matrix_path,
        )

    monkeypatch.setattr(cli, "_onboard_import_review", import_review)
    output_root = tmp_path / "silver-runs"
    result = cli.main(
        ["onboard", "--ha-url", fake.origin, "--output-root", str(output_root)]
    )

    run_root = next(
        path for path in output_root.iterdir() if path.name.startswith("20")
    )
    execution = json.loads((run_root / "private" / "tier-execution.json").read_text())
    matrix = json.loads(matrix_path.read_text())
    output = capsys.readouterr().out
    assert result == 0, output
    archive = run_root / "hil-evidence.zip"
    assert archive.is_file()
    from scripts.hil.evidence import verify_public_archive

    _manifest, public_members, _archive_digest = verify_public_archive(
        archive, include_contents=True
    )
    public_artifacts = json.loads(public_members["tier-artifacts.json"])["artifacts"]
    assert {
        item["content"]["step_id"]
        for item in public_artifacts
        if item["content"].get("kind") == "guided_step_evidence"
    } >= {
        "ac_output_cycle",
        "dc_output_cycle",
        "light_output_cycle",
    }
    public_execution = json.loads(public_members["tier-execution.json"])
    assert public_execution["requested_tier"] == "silver"
    reconnect_step = next(
        row for row in public_execution["steps"] if row["step_id"] == "basic_reconnect"
    )
    assert reconnect_step["outcome"] == "pass"
    assert reconnect_step["machine_evidence"]
    assert any(
        item["content"].get("kind") == "gatt_trace_metadata"
        and item["content"].get("scenario_id") == "recovery_reconnect"
        for item in public_artifacts
    )
    assert "Requested tier: Silver" in output
    assert execution["requested_tier"] == "silver"
    assert execution["execution_tier"] == "silver"
    assert [row["step_id"] for row in execution["steps"]] == [
        "read_only_probe",
        "physical_plausibility_observation",
        "ac_output_cycle",
        "dc_output_cycle",
        "light_output_cycle",
        "basic_reconnect",
        "minimum_output_requirement",
    ]
    assert all(
        row["outcome"] == "pass"
        for row in execution["steps"]
        if row["step_id"] != "minimum_output_requirement"
    )
    child_scenarios = {}
    for child_run in output_root.glob("tier_*"):
        scenario_path = child_run / "private" / "scenarios.json"
        if scenario_path.is_file():
            scenario = json.loads(scenario_path.read_text())
            child_scenarios[scenario["scenario_id"]] = scenario
    for scenario_id, target in (
        ("output_ac_cycle", "ac_output"),
        ("output_dc_cycle", "dc_output"),
        ("output_light_cycle", "light"),
    ):
        rows = child_scenarios[scenario_id]["steps"]
        assert [row["target"] for row in rows] == [target] * 4
        assert [row["expected"] for row in rows] == ["on", "off", "on", "off"]
        assert all(isinstance(row.get("transaction"), dict) for row in rows)
    recovery = child_scenarios["recovery_reconnect"]["steps"]
    recovered_session = next(
        row for row in recovery if row["step_id"] == "recovered_session"
    )
    assert recovered_session["disconnect_observed"] is True
    assert recovered_session["session_generation_before"] == 7
    assert recovered_session["session_generation_after"] == 8
    assert recovered_session["fresh_status_telemetry"] is True
    assert recovered_session["route"] == "active_proxy"
    assert all(
        row["authorization_context"] == "production"
        and row["authorization_context_source"] == "runtime_production_authorized"
        for row in execution["steps"][2:5]
    )
    assert "experimental_hil_grant" not in json.dumps(execution)
    assert fake.output_states == {
        "ac_output": "off",
        "dc_output": "off",
        "light": "off",
    }
    assert not any(
        call[0] == "experimental_output" for call in fake.state_changing_calls
    )

    imported = matrix["revisions"][0]["runs"][0]
    errors, _ = validate_hil_qualification.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    assert imported["scenarios"]["reconnect_failover"]["status"] == "pass"
    tier = evaluate_tiers(matrix)["tiers"][0]
    assert tier["tier"] == "silver"
    assert tier["assurance"]["silver"] == "operator_attested"
    assert "POST-REVIEW QUALIFICATION: attained silver" in output
    assert fake.state_changing_calls
    assert workspace_matrix.read_bytes() == original_workspace_matrix
    assert repo_root.is_relative_to(tmp_path)
    assert run_root.is_relative_to(tmp_path)
