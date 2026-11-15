"""Deterministically render a complete PR description from the repository template."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"PR body data requires nonempty {key}")
    return value.strip()


def render_pr_body(template: str, data: dict[str, Any]) -> str:
    """Render facts into template sections while preserving headings and checkboxes."""
    summary = _required_text(data, "summary")
    issue = _required_text(data, "issue")
    safety = _required_text(data, "safety_impact")
    validation = _required_text(data, "validation")
    hardware = _required_text(data, "hardware_results")
    title = _required_text(data, "title")
    checked = data.get("checked", {})
    if not isinstance(checked, dict) or any(
        not isinstance(label, str) or type(value) is not bool
        for label, value in checked.items()
    ):
        raise ValueError("checked must map checkbox labels to booleans")

    sections = {
        "## Summary": f"**Proposed title:** `{title}`\n\n{summary}\n\nRelated issue: {issue}.",
        "## Protocol and safety impact": safety,
        "## Validation": validation,
        "## Hardware results": hardware,
    }
    lines: list[str] = []
    in_comment = False
    current_section: str | None = None
    inserted: set[str] = set()
    seen_checkboxes: set[str] = set()
    for line in template.splitlines():
        if "<!--" in line:
            if "-->" not in line:
                in_comment = True
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if line.startswith("## "):
            current_section = line
            lines.append(line)
            section = sections.get(line)
            if section is not None:
                lines.extend(("", section))
                inserted.add(line)
            continue
        match = re.fullmatch(r"- \[([ xX])\] (.+)", line)
        if match:
            label = match.group(2)
            seen_checkboxes.add(label)
            is_checked = checked.get(label, False)
            lines.append(f"- [{'x' if is_checked else ' '}] {label}")
            continue
        if line.strip() and current_section in sections:
            continue
        lines.append(line)
    missing = set(sections) - inserted
    if missing:
        raise ValueError(
            "PR template is missing required sections: " + ", ".join(sorted(missing))
        )
    unknown_labels = set(checked) - seen_checkboxes
    if unknown_labels:
        raise ValueError(
            "Unknown PR template checkbox labels: " + ", ".join(sorted(unknown_labels))
        )
    return "\n".join(lines).rstrip() + "\n"


def render_pr_body_file(
    data_path: Path, output_path: Path, *, template_path: Path = DEFAULT_TEMPLATE
) -> Path:
    """Render UTF-8 JSON facts to a deterministic Markdown file."""
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Unable to read PR template or structured data") from exc
    if not isinstance(data, dict):
        raise ValueError("PR body data must be a JSON object")
    body = render_pr_body(template, data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(body, encoding="utf-8")
    return output_path
