"""Tests for the advisory-only Home Assistant version proposal guard."""

from scripts.validate_ha_candidate import (
    validate_candidate,
    validate_proposal_update,
    validate_upgrade_paths,
    validate_upgrade_workflow,
)

ACTIVE = """# BEGIN HA TEST STACK (managed by scripts/sync_ha_test_deps.py)
homeassistant==2026.10.4
pytest-homeassistant-custom-component==0.13.375
# END HA TEST STACK
"""

VALID_DEPENDABOT = """version: 2
updates:
  - package-ecosystem: pip
    directory: /requirements/ha-candidate
    target-branch: devel
    allow:
      - dependency-name: homeassistant
    schedule:
      interval: monthly
    open-pull-requests-limit: 1
    labels:
      - ha-version-proposal
"""


def test_candidate_must_match_active_ha_baseline() -> None:
    assert validate_candidate(ACTIVE, "homeassistant==2026.10.4\n") == []
    errors = validate_candidate(ACTIVE, "homeassistant==2026.11.0\n")
    assert "must contain only the selected baseline" in errors[0]


def test_candidate_must_contain_only_one_homeassistant_pin() -> None:
    errors = validate_candidate(ACTIVE, "homeassistant==2026.10.4\nbleak==3.0.2\n")
    assert "must contain only the selected baseline" in errors[0]


def test_proposal_dependabot_entry_is_narrow_and_low_volume() -> None:
    assert validate_proposal_update(VALID_DEPENDABOT) == []


def test_proposal_dependabot_entry_rejects_other_packages() -> None:
    invalid = VALID_DEPENDABOT.replace(
        "dependency-name: homeassistant", "dependency-name: bleak"
    )
    assert "allow only homeassistant" in validate_proposal_update(invalid)[0]


def test_upgrade_workflow_uses_trusted_code_and_waits_for_pr_ci() -> None:
    workflow = open(".github/workflows/prepare-ha-upgrade.yml", encoding="utf-8").read()
    ci = open(".github/workflows/ci.yml", encoding="utf-8").read()
    assert validate_upgrade_workflow(workflow, ci) == []


def test_upgrade_path_guard_accepts_both_manifests() -> None:
    assert (
        validate_upgrade_paths(
            [
                "requirements/ha-candidate/requirements.txt",
                "requirements_test.txt",
            ]
        )
        == []
    )


def test_upgrade_path_guard_accepts_fixture_only_change() -> None:
    assert validate_upgrade_paths(["requirements_test.txt"]) == []


def test_upgrade_path_guard_accepts_clean_noop() -> None:
    assert validate_upgrade_paths([]) == []


def test_upgrade_path_guard_rejects_unexpected_file() -> None:
    errors = validate_upgrade_paths(["requirements_test.txt", "scripts/unexpected.py"])
    assert "scripts/unexpected.py" in errors[0]
