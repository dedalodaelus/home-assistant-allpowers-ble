"""Offline tests for synchronizing test requirements with HA manifests."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import sync_ha_test_deps as sync

FIXTURES = Path(__file__).parent / "fixtures" / "ha_test_deps"
UNMANAGED_REQUIREMENTS = "pytest>=9,<10\nruff==0.17.4\n"


def _expected(release: str, *, ha_version: str, fixture_version: str) -> dict[str, str]:
    return sync.expected_stack(
        FIXTURES / release / "components",
        ha_version=ha_version,
        fixture_version=fixture_version,
    )


def _requirements_text(expected: dict[str, str]) -> str:
    return sync.render_stack_block(expected) + "\n" + UNMANAGED_REQUIREMENTS


def test_manifest_pins_are_read_from_selected_components() -> None:
    expected = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")

    assert expected["homeassistant"] == "homeassistant==2026.10.4"
    assert expected["pytest-homeassistant-custom-component"] == (
        "pytest-homeassistant-custom-component==0.13.375"
    )
    assert expected["bleak"] == "bleak==3.0.2"
    assert expected["bleak-esphome"] == "bleak-esphome==4.0.0"
    assert expected["aioesphomeapi"] == "aioesphomeapi==46.6.0"
    assert expected["aiousbwatcher"] == "aiousbwatcher==1.1.2"
    assert expected["serialx"] == "serialx==1.11.0"
    assert len(expected) == 14


def test_selected_manifest_version_changes_generated_pins() -> None:
    old = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    new = _expected("new", ha_version="2026.11.0", fixture_version="0.13.400")

    assert old["bleak-esphome"] == "bleak-esphome==4.0.0"
    assert new["bleak-esphome"] == "bleak-esphome==4.1.0"
    assert old["serialx"] == "serialx==1.11.0"
    assert new["serialx"] == "serialx==1.15.1"
    assert new["homeassistant"] == "homeassistant==2026.11.0"
    assert new["pytest-homeassistant-custom-component"] == (
        "pytest-homeassistant-custom-component==0.13.400"
    )


def test_sync_is_deterministic_idempotent_and_check_is_read_only() -> None:
    expected = _expected("new", ha_version="2026.11.0", fixture_version="0.13.400")
    initial = (
        "# unrelated requirements\n"
        + sync.render_stack_block(
            _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
        )
        + "\n"
        + UNMANAGED_REQUIREMENTS
    )

    updated = sync.synchronize_text(initial, expected)

    assert updated != initial
    assert "pytest>=9,<10" in updated
    assert "ruff==0.17.4" in updated
    assert sync.synchronize_text(updated, expected) == updated
    before_check = updated
    assert sync.validate_requirements_text(updated, expected) == []
    assert updated == before_check


def test_sync_cli_updates_selected_stack_then_check_accepts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requirements = tmp_path / "requirements_test.txt"
    old = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    requirements.write_text(_requirements_text(old), encoding="utf-8")
    versions = {
        sync.HA_DISTRIBUTION: "2026.11.0",
        sync.FIXTURE_DISTRIBUTION: "0.13.400",
    }
    monkeypatch.setattr(sync, "installed_version", lambda name: versions[name])
    monkeypatch.setattr(
        sync,
        "components_root_for_ha",
        lambda: FIXTURES / "new" / "components",
    )

    assert (
        sync.main(
            [
                "--sync",
                "--ha-version",
                "2026.11.0",
                "--fixture-version",
                "0.13.400",
                "--requirements",
                str(requirements),
            ]
        )
        == 0
    )
    updated = requirements.read_text(encoding="utf-8")
    assert "homeassistant==2026.11.0" in updated
    assert "pytest-homeassistant-custom-component==0.13.400" in updated
    assert "bleak-esphome==4.1.0" in updated
    assert "serialx==1.15.1" in updated
    assert sync.main(["--check", "--requirements", str(requirements)]) == 0
    assert requirements.read_text(encoding="utf-8") == updated
    assert "Synchronized HA 2026.11.0" in capsys.readouterr().out


def test_check_rejects_stale_manifest_pin_without_modifying_input() -> None:
    expected = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    stale = _requirements_text(expected).replace(
        "bleak-esphome==4.0.0", "bleak-esphome==4.1.0"
    )

    errors = sync.validate_requirements_text(stale, expected)

    assert any("bleak-esphome" in error for error in errors)
    assert "bleak-esphome==4.1.0" in stale


def test_check_rejects_unreviewed_duplicate_override_outside_managed_block() -> None:
    expected = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    text = _requirements_text(expected) + "bleak-esphome==4.1.0\n"

    errors = sync.validate_requirements_text(text, expected)

    assert any("outside the managed block" in error for error in errors)


def test_check_rejects_a_fixture_not_selected_for_this_upgrade() -> None:
    old_fixture = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    reviewed_fixture = _expected(
        "old", ha_version="2026.10.4", fixture_version="0.13.400"
    )

    errors = sync.validate_requirements_text(
        _requirements_text(old_fixture), reviewed_fixture
    )

    assert any("pytest-homeassistant-custom-component" in error for error in errors)


def test_check_cli_uses_requirement_versions_without_duplicated_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requirements = tmp_path / "requirements_test.txt"
    original = _requirements_text(
        _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    )
    requirements.write_text(original, encoding="utf-8")
    versions = {
        sync.HA_DISTRIBUTION: "2026.10.4",
        sync.FIXTURE_DISTRIBUTION: "0.13.375",
    }
    monkeypatch.setattr(sync, "installed_version", lambda name: versions[name])
    monkeypatch.setattr(
        sync,
        "components_root_for_ha",
        lambda: FIXTURES / "old" / "components",
    )

    assert sync.main(["--check", "--requirements", str(requirements)]) == 0
    assert requirements.read_text(encoding="utf-8") == original
    assert "2026.10.4" in capsys.readouterr().out


def test_check_rejects_installed_versions_that_differ_from_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requirements = tmp_path / "requirements_test.txt"
    requirements.write_text(
        _requirements_text(
            _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
        ),
        encoding="utf-8",
    )
    versions = {
        sync.HA_DISTRIBUTION: "2026.10.4",
        sync.FIXTURE_DISTRIBUTION: "0.13.400",
    }
    monkeypatch.setattr(sync, "installed_version", lambda name: versions[name])

    assert sync.main(["--check", "--requirements", str(requirements)]) == 1
    assert "do not match" in capsys.readouterr().err


def test_dependabot_ignores_match_all_managed_pins_and_leave_tools_eligible() -> None:
    expected = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    requirements = _requirements_text(expected)
    ignored = "\n".join(
        f'      - dependency-name: "{name}"' for name in sorted(expected)
    )
    dependabot = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: pip\n"
        "    directory: /\n"
        "    versioning-strategy: increase-if-necessary\n"
        "    ignore:\n"
        f"{ignored}\n"
    )

    assert sync.validate_dependabot_ignores(requirements, dependabot) == []
    assert "pytest" not in expected
    assert "ruff" not in expected


def test_dependabot_ignore_validation_detects_new_generated_stack_pin() -> None:
    expected = _expected("old", ha_version="2026.10.4", fixture_version="0.13.375")
    requirements = _requirements_text(expected).replace(
        "# END HA TEST STACK", "future-ha-library==2.0.0\n# END HA TEST STACK"
    )
    ignored = "\n".join(
        f'      - dependency-name: "{name}"' for name in sorted(expected)
    )
    dependabot = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: pip\n"
        "    directory: /\n"
        "    versioning-strategy: increase-if-necessary\n"
        "    ignore:\n"
        f"{ignored}\n"
    )

    errors = sync.validate_dependabot_ignores(requirements, dependabot)

    assert any("future-ha-library" in error for error in errors)


def test_sync_requires_explicit_ha_and_fixture_selections() -> None:
    with pytest.raises(SystemExit):
        sync._parse_args(["--sync"])


def test_manifest_conflicts_across_components_are_rejected(tmp_path: Path) -> None:
    for component in sync.COMPONENTS:
        path = tmp_path / component / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            f'{{"domain": "{component}", "requirements": []}}\n', encoding="utf-8"
        )
    (tmp_path / "bluetooth" / "manifest.json").write_text(
        '{"domain": "bluetooth", "requirements": ["bleak==3.0.2"]}\n',
        encoding="utf-8",
    )
    (tmp_path / "esphome" / "manifest.json").write_text(
        '{"domain": "esphome", "requirements": ["bleak==3.1.0"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(sync.SyncError, match="manifests disagree on bleak"):
        sync.read_manifest_requirements(tmp_path)


def test_invalid_selected_version_is_rejected() -> None:
    with pytest.raises(sync.SyncError, match="invalid Home Assistant version"):
        _expected("old", ha_version="latest", fixture_version="0.13.375")
