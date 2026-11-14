#!/usr/bin/env python3
"""Check that the Dependabot-only HA proposal matches the selected test baseline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sync_ha_test_deps import (  # noqa: E402
    HA_DISTRIBUTION,
    selected_versions_from_requirements,
)

ACTIVE_REQUIREMENTS = ROOT / "requirements_test.txt"
CANDIDATE_REQUIREMENTS = ROOT / "requirements/ha-candidate/requirements.txt"


def validate_candidate(active_text: str, candidate_text: str) -> list[str]:
    """Reject proposals that are not exactly the selected HA version pin."""
    try:
        selected_ha, _ = selected_versions_from_requirements(active_text)
    except ValueError as err:
        return [f"cannot read selected HA baseline: {err}"]

    candidate_lines = [
        line.strip()
        for line in candidate_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    expected = f"{HA_DISTRIBUTION}=={selected_ha}"
    if candidate_lines != [expected]:
        return [
            "HA candidate manifest must contain only the selected baseline "
            f"({expected}); found {candidate_lines!r}"
        ]
    return []


def validate_proposal_update(config_text: str) -> list[str]:
    """Require a narrowly scoped, monthly advisory-only Dependabot entry."""
    try:
        config = yaml.safe_load(config_text)
    except yaml.YAMLError as err:
        return [f"invalid Dependabot YAML: {err}"]
    if not isinstance(config, dict) or not isinstance(config.get("updates"), list):
        return ["Dependabot configuration must define an updates list"]
    matches = [
        entry
        for entry in config["updates"]
        if isinstance(entry, dict)
        and entry.get("package-ecosystem") == "pip"
        and entry.get("directory") == "/requirements/ha-candidate"
    ]
    if len(matches) != 1:
        return ["Dependabot must define exactly one HA candidate update entry"]
    entry = matches[0]
    errors: list[str] = []
    if entry.get("target-branch") != "devel":
        errors.append("HA candidate Dependabot updates must target devel")
    if entry.get("allow") != [{"dependency-name": HA_DISTRIBUTION}]:
        errors.append("HA candidate updates must allow only homeassistant")
    schedule = entry.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("interval") != "monthly":
        errors.append("HA candidate Dependabot updates must run monthly")
    if entry.get("open-pull-requests-limit") != 1:
        errors.append("HA candidate Dependabot open-PR limit must be one")
    if "ha-version-proposal" not in entry.get("labels", []):
        errors.append("HA candidate Dependabot PRs must use ha-version-proposal label")
    return errors


def validate_upgrade_workflow(workflow_text: str, ci_text: str) -> list[str]:
    """Check the manual workflow's trusted source, inputs, permissions, and PR gate."""
    try:
        workflow = yaml.safe_load(workflow_text)
        ci = yaml.safe_load(ci_text)
    except yaml.YAMLError as err:
        return [f"invalid workflow YAML: {err}"]
    if not isinstance(workflow, dict) or not isinstance(ci, dict):
        return ["HA upgrade and CI workflows must be YAML mappings"]
    workflow_events = workflow.get("on", workflow.get(True, {}))
    ci_events = ci.get("on", ci.get(True, {}))
    errors: list[str] = []
    dispatch = (
        workflow_events.get("workflow_dispatch")
        if isinstance(workflow_events, dict)
        else None
    )
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    if not isinstance(inputs, dict) or set(inputs) != {"ha-version", "fixture-version"}:
        errors.append(
            "HA upgrade workflow must dispatch with explicit HA and fixture inputs"
        )
    if "pull_request_target" in workflow_events:
        errors.append("HA upgrade workflow must not use pull_request_target")

    jobs = workflow.get("jobs")
    job = jobs.get("prepare") if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        errors.append("HA upgrade workflow must define the prepare job")
        return errors
    if job.get("if") != "github.ref == 'refs/heads/main'":
        errors.append("HA upgrade workflow must dispatch from main")
    if job.get("permissions") != {"contents": "write", "pull-requests": "write"}:
        errors.append(
            "HA upgrade job must request only contents and pull-request writes"
        )
    steps = job.get("steps")
    steps = steps if isinstance(steps, list) else []
    checkout_refs = [
        step.get("with", {}).get("ref")
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/checkout@")
    ]
    if checkout_refs != ["devel"]:
        errors.append("HA upgrade workflow must check out trusted devel code")
    run_commands = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    for required in ("git push", "gh pr create", "--base devel", "uv pip check"):
        if required not in run_commands:
            errors.append(
                f"HA upgrade workflow is missing required command: {required}"
            )
    if not isinstance(ci_events, dict) or "pull_request" not in ci_events:
        errors.append("CI must run on pull_request events for generated upgrade PRs")
    return errors


def main() -> int:
    """Validate the committed active and advisory manifests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active", type=Path, default=ACTIVE_REQUIREMENTS)
    parser.add_argument("--candidate", type=Path, default=CANDIDATE_REQUIREMENTS)
    args = parser.parse_args()
    try:
        errors = validate_candidate(
            args.active.read_text(encoding="utf-8"),
            args.candidate.read_text(encoding="utf-8"),
        )
        errors.extend(
            validate_proposal_update(
                (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
            )
        )
        errors.extend(
            validate_upgrade_workflow(
                (ROOT / ".github/workflows/prepare-ha-upgrade.yml").read_text(
                    encoding="utf-8"
                ),
                (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
            )
        )
    except OSError as err:
        errors = [str(err)]
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("HA candidate manifest matches the selected baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
