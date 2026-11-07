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
    parameters: dict[str, Any]


def _validate(raw: Any) -> Scenario:
    required_fields = {
        "id",
        "schema_version",
        "suite",
        "title",
        "mode",
        "steps",
        "required_evidence",
    }
    if not isinstance(raw, dict) or set(raw) not in (
        required_fields,
        required_fields | {"parameters"},
    ):
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
    parameters = raw.get("parameters", {})
    if raw["suite"] == "soak-24h":
        expected_parameters = {
            "duration_seconds",
            "sample_interval_seconds",
            "max_gap_seconds",
            "trace_interval_seconds",
            "trace_window_seconds",
            "max_bytes",
            "minimum_samples",
        }
        if (
            raw["mode"] != "soak"
            or not isinstance(parameters, dict)
            or set(parameters) != expected_parameters
            or parameters.get("duration_seconds") != 86400
            or type(parameters.get("sample_interval_seconds")) is not int
            or not 60 <= parameters["sample_interval_seconds"] <= 1800
            or type(parameters.get("max_gap_seconds")) is not int
            or not parameters["sample_interval_seconds"]
            < parameters["max_gap_seconds"]
            <= 3600
            or type(parameters.get("trace_interval_seconds")) is not int
            or not 300 <= parameters["trace_interval_seconds"] <= 7200
            or type(parameters.get("trace_window_seconds")) is not int
            or not 1 <= parameters["trace_window_seconds"] <= 30
            or type(parameters.get("max_bytes")) is not int
            or not 1_048_576 <= parameters["max_bytes"] <= 104_857_600
            or type(parameters.get("minimum_samples")) is not int
            or parameters["minimum_samples"]
            != parameters["duration_seconds"] // parameters["sample_interval_seconds"]
            + 1
        ):
            raise ValueError("Invalid bounded soak parameters")
    elif "parameters" in raw:
        raise ValueError("Only the soak scenario may declare runner parameters")
    return Scenario(
        raw["id"],
        raw["schema_version"],
        raw["suite"],
        raw["title"],
        raw["mode"],
        tuple(dict(step) for step in raw["steps"]),
        tuple(evidence),
        dict(parameters),
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
