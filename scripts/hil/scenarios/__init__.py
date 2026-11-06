"""Versioned, reviewed HIL scenario declarations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

SCHEMA_VERSION = 1
SUITES = (
    "read-only",
    "outputs",
    "settings",
    "transaction-safety",
    "recovery",
    "soak-24h",
)
OUTCOMES = {"pass", "fail", "inconclusive", "skipped", "not_applicable"}
ACTION_TARGETS = {
    "observe": {None},
    "manual": {None},
    "switch": {"ac_output", "dc_output", "light", "eco_mode", "car_charger"},
    "select": {"work_mode", "eco_timeout"},
}


@dataclass(frozen=True)
class Scenario:
    """Validated immutable scenario declaration."""

    id: str
    schema_version: int
    suite: str
    title: str
    mode: str
    steps: tuple[dict[str, Any], ...]
    required_evidence: tuple[str, ...]


def _validate(raw: Any) -> Scenario:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "schema_version",
        "suite",
        "title",
        "mode",
        "steps",
        "required_evidence",
    }:
        raise ValueError("Invalid scenario fields")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError("Unsupported scenario schema version")
    if (
        not isinstance(raw["id"], str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", raw["id"]) is None
    ):
        raise ValueError("Invalid scenario ID")
    if (
        not isinstance(raw["suite"], str)
        or raw["suite"] not in SUITES
        or not isinstance(raw["mode"], str)
        or raw["mode"]
        not in {
            "read_only",
            "interactive_write",
            "manual",
            "soak",
        }
    ):
        raise ValueError("Invalid scenario suite or mode")
    if raw["mode"] == "interactive_write" and raw["suite"] not in {
        "outputs",
        "settings",
        "transaction-safety",
    }:
        raise ValueError("Write actions are not allowed in this suite")
    if not isinstance(raw["title"], str) or not raw["title"] or len(raw["title"]) > 120:
        raise ValueError("Invalid scenario title")
    if not isinstance(raw["steps"], list) or not raw["steps"]:
        raise ValueError("Scenario must declare at least one step")
    for step in raw["steps"]:
        if not isinstance(step, dict) or set(step) != {
            "id",
            "instruction",
            "timeout_seconds",
            "action",
            "target",
            "expected",
        }:
            raise ValueError("Invalid scenario step fields")
        if (
            not isinstance(step["id"], str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", step["id"]) is None
        ):
            raise ValueError("Invalid scenario step ID")
        if not isinstance(step["instruction"], str) or not step["instruction"]:
            raise ValueError("Invalid scenario instruction")
        timeout = step["timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError("Scenario timeout must be bounded to 1-86400 seconds")
        if not isinstance(step["action"], str) or step["action"] not in {
            "observe",
            "manual",
            "switch",
            "select",
        }:
            raise ValueError("Unsupported scenario action")
        target = step["target"]
        if target is not None and not isinstance(target, str):
            raise ValueError("Scenario action target is not allowlisted")
        if target not in ACTION_TARGETS[step["action"]]:
            raise ValueError("Scenario action target is not allowlisted")
        if raw["mode"] != "interactive_write" and step["action"] in {
            "switch",
            "select",
        }:
            raise ValueError("HA writes require an interactive-write scenario")
        if not isinstance(step["expected"], str):
            raise ValueError("Invalid expected observation")
    evidence = raw["required_evidence"]
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) for item in evidence
    ):
        raise ValueError("Invalid required evidence")
    return Scenario(
        raw["id"],
        raw["schema_version"],
        raw["suite"],
        raw["title"],
        raw["mode"],
        tuple(dict(step) for step in raw["steps"]),
        tuple(evidence),
    )


def load_scenarios() -> tuple[Scenario, ...]:
    """Load every bundled declaration and reject duplicate IDs eagerly."""
    root = files(__package__)
    scenarios = tuple(
        _validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.name.endswith(".json")
    )
    ids = [item.id for item in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scenario ID")
    return scenarios
