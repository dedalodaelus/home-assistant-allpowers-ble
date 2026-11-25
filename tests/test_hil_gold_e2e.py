"""Synthetic direct Gold onboard acquisition and qualification tests."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shlex
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
    assert imported["authorization_context"] == "production"
    assert imported["authorization_context_source"] == "reviewed_production_session"
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
    scenario_by_id = {
        scenario["scenario_id"]: scenario
        for path in run_root.parent.glob("tier_*/private/scenarios.json")
        if (scenario := json.loads(path.read_text(encoding="utf-8")))
    }
    recovered_session = next(
        step
        for step in scenario_by_id["recovery_reconnect"]["steps"]
        if step["step_id"] == "recovered_session"
    )
    assert recovered_session["disconnect_observed"] is True
    assert recovered_session["session_generation_before"] == 7
    assert recovered_session["session_generation_after"] == 8
    assert recovered_session["fresh_status_telemetry"] is True
    assert recovered_session["route"] == "active_proxy"
    recovery_scenario_path = next(
        path
        for path in run_root.parent.glob("tier_*/private/scenarios.json")
        if json.loads(path.read_text(encoding="utf-8"))["scenario_id"]
        == "recovery_reconnect"
    )
    for scenario_id in (
        "settings_eco_mode",
        "settings_work_mode",
        "settings_eco_timeout",
    ):
        actions = scenario_by_id[scenario_id]["steps"]
        assert len(actions) == 3
        for action in actions[1:]:
            assert action["outcome"] == "pass"
            assert action["session_generation"] == 8
            assert action["transaction"]["kind"] == "settings"
            assert action["transaction"]["session_generation"] == 8
    assert {generation for _, _, generation, _ in fake.setting_writes} == {
        recovered_session["session_generation_after"]
    }
    archive = run_root / "hil-evidence.zip"
    from scripts.hil.evidence import verify_public_archive

    manifest, public_members, archive_digest = verify_public_archive(
        archive, include_contents=True
    )
    assert imported["source_run_id"] == manifest["run_id"]
    assert imported["archive_sha256"] == archive_digest
    contract_path = car_charger_contract["evidence"][0]
    contract_artifact = json.loads(
        (matrix_path.parents[2] / contract_path).read_text(encoding="utf-8")
    )
    assert contract_artifact["source_run_id"] == imported["run_id"]
    assert contract_artifact["source_archive_sha256"] == archive_digest
    repo_root = matrix_path.parents[2]
    stage_paths = cli._verified_review_stage_paths(
        matrix,
        matrix_path,
        repo_root,
        revision_id=matrix["revisions"][0]["id"],
        run_id=imported["run_id"],
        tier="gold",
    )
    assert stage_paths is not None
    assert contract_path in stage_paths
    contract_file = repo_root / contract_path
    contract_bytes = contract_file.read_bytes()
    contract_file.write_bytes(contract_bytes + b" ")
    assert (
        cli._verified_review_stage_paths(
            matrix,
            matrix_path,
            repo_root,
            revision_id=matrix["revisions"][0]["id"],
            run_id=imported["run_id"],
            tier="gold",
        )
        is None
    )
    contract_file.write_bytes(contract_bytes)
    external_contract = tmp_path / "external-contract.json"
    external_contract.write_bytes(contract_bytes)
    contract_file.unlink()
    contract_file.symlink_to(external_contract)
    assert (
        cli._verified_review_stage_paths(
            matrix,
            matrix_path,
            repo_root,
            revision_id=matrix["revisions"][0]["id"],
            run_id=imported["run_id"],
            tier="gold",
        )
        is None
    )
    contract_file.unlink()
    contract_file.write_bytes(contract_bytes)
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
    public_steps = {item["step_id"]: item for item in public_execution["steps"]}
    for step_id in (
        "eco_mode_reversal",
        "work_mode_reversal",
        "eco_timeout_reversal",
    ):
        assert public_steps[step_id]["authorization_context"] == "production"
        assert (
            public_steps[step_id]["authorization_context_source"]
            == "runtime_production_authorized"
        )
        guided = next(
            item["content"]
            for item in public_artifacts
            if item["content"].get("kind") == "guided_step_evidence"
            and item["content"].get("step_id") == step_id
        )
        assert len(guided["actions"]) == 2
        assert all(
            action["session_generation"] == 8
            and action["transaction"]["session_generation"] == 8
            and action["transaction"]["kind"] == "settings"
            for action in guided["actions"]
        )
        transactions = {
            action["transaction"]["transaction_sha256"] for action in guided["actions"]
        }
        setting_traces = [
            item["content"]
            for item in public_artifacts
            if item["content"].get("kind") == "gatt_trace_metadata"
            and item["content"].get("scenario_id")
            == {
                "eco_mode_reversal": "settings_eco_mode",
                "work_mode_reversal": "settings_work_mode",
                "eco_timeout_reversal": "settings_eco_timeout",
            }[step_id]
        ]
        assert len(setting_traces) == 2
        assert all(
            trace["observed_route"] == "active_proxy"
            and {event["session_generation"] for event in trace["events"]} == {8}
            and {frame["session_generation"] for frame in trace["frames"]} == {8}
            and all(
                frame["command_type"] == "settings_control"
                and frame["direction"] == "tx"
                for frame in trace["frames"]
            )
            for trace in setting_traces
        )
        assert {
            next(
                event["transaction_sha256"]
                for event in trace["events"]
                if event["event"] == "transaction_confirmed"
            )
            for trace in setting_traces
        } == transactions
    reconnect_trace = next(
        item["content"]
        for item in public_artifacts
        if item["content"].get("kind") == "gatt_trace_metadata"
        and item["content"].get("scenario_id") == "recovery_reconnect"
    )
    assert reconnect_trace["observed_route"] == "active_proxy"
    assert {event["session_generation"] for event in reconnect_trace["events"]} == {8}
    assert {frame["session_generation"] for frame in reconnect_trace["frames"]} == {8}
    assert public_steps["basic_reconnect"]["outcome"] == "pass"
    assert (
        hashlib.sha256(recovery_scenario_path.read_bytes()).hexdigest()
        in (public_steps["basic_reconnect"]["machine_evidence_sha256"])
    )
    assert b"SYNTHETIC_PRIVATE_TOKEN" not in b"".join(public_members.values())
    git_add_line = next(
        line for line in output.splitlines() if line.startswith("  git add ")
    )
    git_add_args = shlex.split(git_add_line.strip())[2:]
    repo_root = matrix_path.parents[2]
    subprocess.run(["git", "add", *git_add_args], cwd=repo_root, check=True)
    staged_paths = set(
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
    )
    expected_staged = {matrix_path.relative_to(repo_root).as_posix()}
    for section in (
        "bronze_evidence",
        "write_capabilities",
        "scenarios",
        "feature_presence",
    ):
        records = imported.get(section, {})
        if isinstance(records, dict):
            expected_staged.update(
                reference
                for record in records.values()
                if isinstance(record, dict)
                for reference in record.get("evidence", [])
            )
    expected_staged.update(imported["evidence"])
    expected_staged.update(car_charger_contract["evidence"])
    assert expected_staged <= staged_paths
    run_prefix = f"tests/hil/fixtures/r600-hw03/runs/{imported['run_id']}/"
    assert all(
        path.startswith(run_prefix)
        for path in staged_paths
        if path != matrix_path.relative_to(repo_root).as_posix()
    )
    assert not any(
        "hil-results" in path or path.endswith(".zip") for path in staged_paths
    )
    pr_body_line = next(
        line for line in output.splitlines() if line.startswith("Local PR body: ")
    )
    pr_body = Path(pr_body_line.removeprefix("Local PR body: ")).read_text(
        encoding="utf-8"
    )
    assert "car_charger: excluded/unverified (safely disabled)" in pr_body
    assert "tested controls:" in pr_body
    assert "physical absences:" in pr_body
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
    assert fake.setting_states["work_mode"] == "mute"
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
