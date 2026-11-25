"""Synthetic direct Gold onboard acquisition and qualification tests."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from scripts import validate_hil_qualification
from scripts.hil import cli
from scripts.hil.qualification import migrate_v1_to_v3
from scripts.hil.tiers import evaluate_tiers
from tests.hil.synthetic_hil import SyntheticHILHA


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


def _gold_onboard_answers(
    monkeypatch: pytest.MonkeyPatch,
    fake: SyntheticHILHA,
) -> None:
    class TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", TTY())

    def answer(prompt: str = "") -> str:
        if prompt == "Desired tier: ":
            return "3"
        if prompt.startswith("Action [1]"):
            fake.recovery_requested = True
            return "1"
        if prompt.startswith("Action [1-3]"):
            return "2"  # Reuse Silver reconnect evidence; no second interruption.
        if "Type 1 when the route is interrupted" in prompt:
            return "1"
        if "Type 1 after restoring the same BLE route" in prompt:
            assert any(
                not restored and not connected
                for restored, connected in fake.capability_snapshots
            ), "restore consent must follow an explicit disconnected snapshot"
            fake.disconnected_before_restoration = True
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
            or "Outcome [1-3]" in prompt
        ):
            return "1"
        if "Physically inspect" in prompt:
            return "PRESENT"
        if "[no/yes]" in prompt or "[y/N]" in prompt:
            return "yes"
        if "Type 1" in prompt:
            return "1"
        return "yes"

    monkeypatch.setattr(builtins, "input", answer)


def _run_gold_onboard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: SyntheticHILHA,
) -> tuple[int, Path, Path, dict[str, Any]]:
    repo_root, matrix_path = _temporary_checkout(tmp_path / "gold-checkout")
    monkeypatch.setattr(validate_hil_qualification, "ROOT", repo_root)
    monkeypatch.setattr(cli, "HAClient", lambda *_args, **_kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SYNTHETIC_PRIVATE_TOKEN")
    _gold_onboard_answers(monkeypatch, fake)
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
    output_root = tmp_path / "gold-runs"
    result = cli.main(
        ["onboard", "--ha-url", fake.origin, "--output-root", str(output_root)]
    )
    run_root = next(
        path for path in output_root.iterdir() if path.name.startswith("20")
    )
    execution = json.loads(
        (run_root / "private" / "tier-execution.json").read_text(encoding="utf-8")
    )
    return result, run_root, matrix_path, execution


def test_onboard_gold_runs_full_pipeline_and_reviews_gold_in_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct Gold onboarding creates and reviews all tier evidence in one run."""
    workspace_matrix = (
        Path(__file__).resolve().parent / "hil" / "qualification_matrix.json"
    )
    original_workspace_matrix = workspace_matrix.read_bytes()
    fake = SyntheticHILHA(gold=True)
    result, run_root, matrix_path, execution = _run_gold_onboard(
        monkeypatch, tmp_path, fake
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    output = capsys.readouterr().out
    assert result == 0, f"result={result}; output tail:\n{output[-3000:]}"
    assert "Requested tier: Gold" in output
    assert "POST-REVIEW QUALIFICATION: attained gold" in output
    assert execution["requested_tier"] == execution["execution_tier"] == "gold"
    expected = [
        "read_only_probe",
        "physical_plausibility_observation",
        "ac_output_cycle",
        "dc_output_cycle",
        "light_output_cycle",
        "basic_reconnect",
        "eco_mode_reversal",
        "work_mode_reversal",
        "eco_timeout_reversal",
        "car_charger_reversal",
        "guarded_recovery",
        "minimum_output_requirement",
    ]
    assert [step["step_id"] for step in execution["steps"]] == expected
    by_step = {step["step_id"]: step for step in execution["steps"]}
    assert all(
        by_step[key]["outcome"] == "pass"
        for key in (
            "read_only_probe",
            "physical_plausibility_observation",
            "ac_output_cycle",
            "dc_output_cycle",
            "light_output_cycle",
            "basic_reconnect",
            "eco_mode_reversal",
            "work_mode_reversal",
            "eco_timeout_reversal",
        )
    )
    assert by_step["minimum_output_requirement"]["outcome"] == "pending_review"
    assert by_step["guarded_recovery"]["outcome"] == "shared_evidence"
    exclusion = by_step["car_charger_reversal"]
    assert exclusion["outcome"] == "pending_review"
    assert exclusion["safe_disable_proof"] == {
        "runtime_option_enabled": False,
        "normal_control_available": False,
    }
    assert fake.setting_states == {
        "eco_mode": "off",
        "work_mode": "mute",
        "eco_timeout": "four_hours",
    }
    assert [(key, value) for key, value, _, _ in fake.setting_writes] == [
        ("eco_mode", "on"),
        ("eco_mode", "off"),
        ("work_mode", "standard"),
        ("work_mode", "mute"),
        ("eco_timeout", "two_hours"),
        ("eco_timeout", "four_hours"),
    ]
    assert len({generation for _, _, generation, _ in fake.setting_writes}) == 1
    assert len({capture for _, _, _, capture in fake.setting_writes}) == 6
    assert fake.disconnected_before_restoration is True
    assert fake.output_states == {
        "ac_output": "off",
        "dc_output": "off",
        "light": "off",
    }
    assert fake.state_changing_calls == [
        ("call_control", target, value)
        for target in ("ac_output", "dc_output", "light")
        for value in ("on", "off", "on", "off")
    ] + [
        ("call_control", target, value)
        for target, values in (
            ("eco_mode", ("on", "off")),
            ("work_mode", ("standard", "mute")),
            ("eco_timeout", ("two_hours", "four_hours")),
        )
        for value in values
    ]

    imported = matrix["revisions"][0]["runs"][0]
    errors, _ = validate_hil_qualification.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    assert errors == []
    assert imported["scenarios"]["reconnect_failover"]["status"] == "pass"
    assert imported["feature_presence"]["eco_mode"]["status"] == "present"
    assert imported["feature_presence"]["work_mode"]["status"] == "present"
    assert imported["feature_presence"]["eco_timeout"]["status"] == "present"
    car_charger_contract = matrix["revisions"][0]["feature_contract"]["car_charger"]
    assert car_charger_contract["status"] == "excluded"
    assert car_charger_contract["safely_disabled"] is True
    assert car_charger_contract["evidence"] == [
        f"tests/hil/fixtures/r600-hw03/runs/{imported['run_id']}/feature-contract-car_charger.json"
    ]
    tier = evaluate_tiers(matrix)["tiers"][0]
    assert tier["tier"] == "gold"
    assert tier["assurance"]["bronze"] == "operator_attested"
    assert tier["assurance"]["silver"] == "operator_attested"
    assert tier["assurance"]["gold"] == "operator_attested"
    assert tier["assurance"]["platinum"] == "unknown"
    assert tier["scope"]["observed_route"] == "active_proxy"
    gold_review_path = next(
        (matrix_path.parents[2] / "hil-results" / "tier-reviews").glob("*-gold-*.json")
    )
    gold_review = json.loads(gold_review_path.read_text(encoding="utf-8"))
    assert gold_review["assurance"] == "operator_attested"
    assert gold_review["reviewer_independent_from_operator"] is False
    assert {transition["step_id"] for transition in gold_review["transitions"]} >= {
        "eco_mode_reversal",
        "work_mode_reversal",
        "eco_timeout_reversal",
    }
    archive = run_root / "hil-evidence.zip"
    from scripts.hil.evidence import verify_public_archive

    _manifest, public_members, _digest = verify_public_archive(
        archive, include_contents=True
    )
    public_execution = json.loads(public_members["tier-execution.json"])
    assert public_execution["requested_tier"] == "gold"
    public_artifacts = json.loads(public_members["tier-artifacts.json"])["artifacts"]
    trace_scenarios = {
        item["content"].get("scenario_id")
        for item in public_artifacts
        if item["content"].get("kind") == "gatt_trace_metadata"
    }
    assert trace_scenarios >= {
        "output_ac_cycle",
        "output_dc_cycle",
        "output_light_cycle",
        "recovery_reconnect",
        "settings_eco_mode",
        "settings_work_mode",
        "settings_eco_timeout",
    }
    assert b"SYNTHETIC_PRIVATE_TOKEN" not in b"".join(public_members.values())
    assert workspace_matrix.read_bytes() == original_workspace_matrix
    assert run_root.is_relative_to(tmp_path)


def test_onboard_gold_does_not_qualify_without_setting_restoration_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = SyntheticHILHA(gold=True, omit_setting_restoration="work_mode")
    result, run_root, matrix_path, execution = _run_gold_onboard(
        monkeypatch, tmp_path, fake
    )
    output = capsys.readouterr().out
    assert result == 2
    assert execution["requested_tier"] == "gold"
    work_mode = next(
        step for step in execution["steps"] if step["step_id"] == "work_mode_reversal"
    )
    assert work_mode["outcome"] != "pass"
    checkpoint_path = next(
        path
        for path in (run_root.parent).glob("tier_*/private/checkpoint.json")
        if json.loads(path.read_text(encoding="utf-8"))["scenario_id"]
        == "settings_work_mode"
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    restoration = next(
        step for step in checkpoint["steps"] if step["step_id"] == "restore_original"
    )
    assert restoration["outcome"] == "inconclusive"
    assert restoration.get("transaction") is None
    trace_path = checkpoint_path.parent / restoration["trace"] / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert not any(
        frame.get("command_type") == "settings_control"
        for frame in trace.get("frames", [])
    )
    assert "TEST GATE: FAILED" in output
    assert not (run_root / "hil-evidence.zip").exists()
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert matrix["revisions"][0]["runs"] == []
