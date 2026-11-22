"""Privacy regression tests for local HIL public reports."""

import json
from pathlib import Path

import pytest

from scripts.hil.api import HAAPIError
from scripts.hil.evidence import export_archive, verify_public_archive, write_report


def test_report_projects_only_safe_environment_and_inconclusive_session(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "environment.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "home_assistant": {
                    "version": {
                        "value": "2026.10.4",
                        "source": "ha_rest_config",
                        "verified": True,
                    },
                    "secret": "192.168.1.4",
                },
                "runner_host": {},
                "integration": {},
                "access_token": "super-secret-token",
            }
        ),
        encoding="utf-8",
    )
    (run / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "0123456789abcdef",
                "qualification_status": "pass",
                "reason": "AA:BB:CC:DD:EE:FF",
            }
        ),
        encoding="utf-8",
    )

    report = write_report(run)
    public = run / "public"
    assert report.is_file()
    joined = b"".join(path.read_bytes() for path in public.iterdir())
    assert b"super-secret-token" not in joined
    assert b"192.168.1.4" not in joined
    assert b"AA:BB:CC:DD:EE:FF" not in joined
    session = json.loads((public / "session.json").read_text())
    assert session["data"]["qualification_status"] == "inconclusive"


def test_report_rejects_symlinked_source_files(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    (run / "environment.json").symlink_to(secret)
    (run / "session.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )

    with pytest.raises(HAAPIError):
        write_report(run)
    assert not (run / "public").exists()


def test_inconclusive_guided_bronze_report_has_no_false_pass(
    tmp_path: Path,
) -> None:
    import hashlib

    run = tmp_path / "guided-run"
    private = run / "private"
    private.mkdir(parents=True)
    run_id = "0123456789abcdef"
    (private / "environment.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    (private / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "requested_route": "auto",
            }
        ),
        encoding="utf-8",
    )
    bronze = {
        "schema_version": 1,
        "step_id": "read_only_probe",
        "sample_sha256": ["a" * 64, "b" * 64],
        "samples": [],
        "fresh_verified_samples": False,
        "physical_observation": "plausible",
        "protocol_probe": {"outcome": "inconclusive"},
    }
    bronze_bytes = (json.dumps(bronze) + "\n").encode()
    (private / "tier-bronze-read-only.json").write_bytes(bronze_bytes)
    evidence = {
        "path": "tier-bronze-read-only.json",
        "sha256": hashlib.sha256(bronze_bytes).hexdigest(),
    }
    execution = {
        "schema_version": 1,
        "run_id": run_id,
        "requested_tier": "bronze",
        "execution_tier": "bronze",
        "review_state": "pending",
        "attained_tier": None,
        "steps": [
            {
                "step_id": step_id,
                "outcome": outcome,
                "machine_evidence": [evidence],
            }
            for step_id, outcome in (
                ("read_only_probe", "inconclusive"),
                ("physical_plausibility_observation", "inconclusive"),
            )
        ],
    }
    (private / "tier-execution.json").write_text(
        json.dumps(execution), encoding="utf-8"
    )

    report = write_report(run).read_text(encoding="utf-8")

    assert "Acquisition status: pre-review evidence snapshot" in report
    assert "Qualification: **inconclusive**" not in report
    assert "Requested tier: `bronze`; executed tier: `bronze`" in report
    assert "`read_only_probe`: **inconclusive**" in report
    assert "`physical_plausibility_observation`: **inconclusive**" in report
    assert "`read_only_probe`: **pass**" not in report
    assert "`physical_plausibility_observation`: **pass**" not in report
    assert "operator-reported physical plausibility `plausible`" in report
    assert "fresh verified telemetry samples missing" in report
    assert (
        "No correlated scenario steps were recorded; guided tier steps above are a separate acquisition record."
        in report
    )
    archive = export_archive(run, confirmed=True)
    _manifest, members, _digest = verify_public_archive(archive, include_contents=True)
    assert members["report.md"].decode("utf-8") == report
    assert "qualification pending" in members["report.md"].decode("utf-8")


def test_guided_output_report_exposes_each_cycle_physical_outcome() -> None:
    from scripts.hil.evidence import _render_report

    report = _render_report(
        "0123456789abcdef",
        {"data": {}},
        {"data": {}},
        {"steps": []},
        0,
        0,
        "local_adapter",
        tier_execution={
            "requested_tier": "silver",
            "execution_tier": "silver",
            "steps": [
                {
                    "step_id": "ac_output_cycle",
                    "outcome": "inconclusive",
                    "machine_evidence": [{"artifact_id": "output-cycle"}],
                }
            ],
        },
        tier_artifacts={
            "artifacts": [
                {
                    "artifact_id": "output-cycle",
                    "content": {
                        "output_cycles": [
                            {
                                "initial_state": "off",
                                "target_state": "on",
                                "physical_change_outcome": "correct",
                                "physical_restore_outcome": "correct",
                                "private_note": "must not appear",
                            },
                            {
                                "initial_state": "on",
                                "target_state": "off",
                                "physical_change_outcome": "unable_to_observe",
                                "physical_restore_outcome": "incorrect",
                            },
                            {
                                "initial_state": "off",
                                "target_state": "on",
                                "physical_change_outcome": "correct",
                                "physical_restore_outcome": "correct",
                            },
                        ],
                        "final_restore_outcome": "incorrect",
                    },
                }
            ]
        },
    ).decode("utf-8")

    assert (
        "output cycle 1 (off to on): physical change `correct`; "
        "physical restoration `correct`"
    ) in report
    assert (
        "output cycle 2 (on to off): physical change `unable_to_observe`; "
        "physical restoration `incorrect`"
    ) in report
    assert "final restore outcome `incorrect`" in report
    assert "private_note" not in report
    assert "must not appear" not in report
    assert "output cycle 3" not in report
