"""Deterministic PR template rendering tests."""

from __future__ import annotations

import json

import pytest

from scripts.generate_pr_body import main
from scripts.pr_body import render_pr_body, render_pr_body_file


@pytest.fixture(autouse=True)
def configure_event_loop():
    """Template tests do not need the HA plugin's event-loop fixture."""
    yield


TEMPLATE = """# Before submitting

<!-- Instructions are replaced by generated facts. -->
- [ ] Conventional title
- [ ] Issue linked

## Summary

<!-- Summary placeholder -->

## Protocol and safety impact

<!-- Safety placeholder -->

- [ ] No protocol write behavior changes.
- [ ] Unknown bits remain preserved.

## Validation

<!-- Validation placeholder -->
- [ ] Unit tests added or updated.

## Hardware results

<!-- Hardware placeholder -->
"""

DATA = {
    "title": "test(hil): add reviewed R600 Bronze evidence",
    "issue": "#330 and parent #227",
    "summary": "Add a reviewed Bronze record for R600.",
    "safety_impact": "Evidence only; no protocol code changed.",
    "validation": "Ran schema-v3 validation and scoped tier evaluation.",
    "hardware_results": "R600 HW 0.3, firmware 1.1, active_proxy.",
    "checked": {
        "Conventional title": True,
        "Issue linked": True,
        "No protocol write behavior changes.": True,
    },
}


def test_render_pr_body_preserves_all_headings_and_checklists():
    rendered = render_pr_body(TEMPLATE, DATA)

    for heading in (
        "# Before submitting",
        "## Summary",
        "## Protocol and safety impact",
        "## Validation",
        "## Hardware results",
    ):
        assert heading in rendered
    assert "- [x] Conventional title" in rendered
    assert "- [x] Issue linked" in rendered
    assert "- [x] No protocol write behavior changes." in rendered
    assert "- [ ] Unknown bits remain preserved." in rendered
    assert "- [ ] Unit tests added or updated." in rendered
    assert "placeholder" not in rendered
    assert "R600 HW 0.3" in rendered


def test_render_pr_body_is_deterministic():
    assert render_pr_body(TEMPLATE, DATA) == render_pr_body(TEMPLATE, DATA)


def test_repository_template_headings_and_checklist_are_complete():
    from scripts.pr_body import DEFAULT_TEMPLATE

    template = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    rendered = render_pr_body(template, {**DATA, "checked": {}})

    assert [line for line in template.splitlines() if line.startswith("#")] == [
        line for line in rendered.splitlines() if line.startswith("#")
    ]
    assert sum(line.startswith("- [") for line in template.splitlines()) == sum(
        line.startswith("- [") for line in rendered.splitlines()
    )


@pytest.mark.parametrize(
    "key",
    ["title", "issue", "summary", "safety_impact", "validation", "hardware_results"],
)
def test_render_requires_complete_concrete_facts(key):
    data = dict(DATA)
    data[key] = " "

    with pytest.raises(ValueError, match=key):
        render_pr_body(TEMPLATE, data)


def test_render_rejects_non_boolean_checklist_values():
    data = {**DATA, "checked": {"Conventional title": "yes"}}

    with pytest.raises(ValueError, match="checked"):
        render_pr_body(TEMPLATE, data)


def test_render_rejects_unknown_checklist_label():
    data = {**DATA, "checked": {"not a template item": True}}

    with pytest.raises(ValueError, match="Unknown PR template checkbox"):
        render_pr_body(TEMPLATE, data)


def test_file_generator_writes_utf8_markdown(tmp_path):
    data_path = tmp_path / "facts.json"
    output_path = tmp_path / "nested" / "pr.md"
    data_path.write_text(json.dumps(DATA), encoding="utf-8")

    assert (
        render_pr_body_file(data_path, output_path, template_path=_template(tmp_path))
        == output_path
    )
    assert "Add a reviewed Bronze record" in output_path.read_text(encoding="utf-8")


def _template(tmp_path):
    path = tmp_path / "template.md"
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def test_cli_reports_success_and_failure(tmp_path, capsys):
    data_path = tmp_path / "facts.json"
    output_path = tmp_path / "pr.md"
    template_path = _template(tmp_path)
    data_path.write_text(json.dumps(DATA), encoding="utf-8")

    assert (
        main(
            [
                "--data",
                str(data_path),
                "--output",
                str(output_path),
                "--template",
                str(template_path),
            ]
        )
        == 0
    )
    assert "PR body written to:" in capsys.readouterr().out
    assert (
        main(["--data", str(tmp_path / "missing.json"), "--output", str(output_path)])
        == 2
    )
    assert "Unable to generate PR body" in capsys.readouterr().err


def test_cli_rejects_non_object_data(tmp_path, capsys):
    data_path = tmp_path / "facts.json"
    data_path.write_text("[]", encoding="utf-8")

    assert main(["--data", str(data_path), "--output", str(tmp_path / "pr.md")]) == 2
    assert "JSON object" in capsys.readouterr().err
