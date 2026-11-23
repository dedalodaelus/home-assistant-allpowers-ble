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


def _temporary_checkout(root: Path) -> tuple[Path, Path]:
    root.mkdir()
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
        assert all(
            call[1]
            in {
                "allpowers_ble/hil/start",
                "allpowers_ble/hil/fetch",
                "allpowers_ble/hil/stop",
            }
            for call in fake.calls
            if call[0] == "websocket"
        )
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
