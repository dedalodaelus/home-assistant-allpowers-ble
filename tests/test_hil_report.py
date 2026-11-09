"""Privacy regression tests for local HIL public reports."""

import json
from pathlib import Path

import pytest

from scripts.hil.api import HAAPIError
from scripts.hil.evidence import write_report


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
