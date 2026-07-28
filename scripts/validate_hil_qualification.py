#!/usr/bin/env python3
"""Validate HIL qualification matrix structure and redaction safeguards."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "tests" / "hil" / "qualification_matrix.json"
ALLOWED_STATUS = {"pass", "fail", "pending", "na"}
PASS_STATUS = {"pass", "na"}
REQUIRED_ROUTES = ("local_adapter", "active_proxy")
REQUIRED_SCENARIOS = (
    "golden_vector_replay",
    "reconnect_failover",
    "rapid_consecutive_writes",
    "stale_timeout_recovery",
    "soak_24h",
    "upgrade_reload",
    "vendor_app_contention",
)
REQUIRED_WRITE_CAPABILITIES = (
    "ac_output",
    "dc_output",
    "light_output",
    "eco_mode",
    "work_mode",
    "eco_timeout",
    "car_charger",
)
MAC_ADDRESS = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate tests/hil/qualification_matrix.json and referenced fixture files."
        )
    )
    parser.add_argument(
        "--matrix",
        default=str(DEFAULT_MATRIX),
        help="Path to qualification matrix JSON file.",
    )
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Require all routes/scenarios/write capabilities to be pass/na.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        help="Maximum age in days for route evidence in require-pass mode.",
    )
    return parser.parse_args()


def add_error(errors: list[str], message: str) -> None:
    """Append a normalized error message."""
    errors.append(message)


def read_json(path: Path) -> dict[str, Any]:
    """Read and validate top-level JSON object."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("matrix root must be a JSON object")
    return payload


def iter_strings(value: Any) -> list[str]:
    """Collect all string leaf values from nested JSON data."""
    values: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            values.append(current)
            continue
        if isinstance(current, dict):
            stack.extend(current.values())
            continue
        if isinstance(current, list):
            stack.extend(current)
    return values


def check_redaction(errors: list[str], source: str, content: str) -> None:
    """Reject common non-redacted Bluetooth identifiers."""
    for number, line in enumerate(content.splitlines(), start=1):
        if MAC_ADDRESS.search(line):
            add_error(errors, f"{source}:{number} contains a Bluetooth MAC address")


def parse_iso_date(value: str, *, context: str, errors: list[str]) -> date | None:
    """Parse YYYY-MM-DD and report a contextual error on failure."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        add_error(errors, f"{context} must use YYYY-MM-DD date format")
        return None


def _validate_route(
    route_name: str,
    route: dict[str, Any],
    *,
    revision_id: str,
    matrix_path: Path,
    require_pass: bool,
    max_age_days: int,
    errors: list[str],
) -> str | None:
    """Validate one route lane under a revision entry."""
    context = f"revision {revision_id} route {route_name}"

    status = route.get("status")
    if not isinstance(status, str) or status not in ALLOWED_STATUS:
        add_error(errors, f"{context} status must be one of {sorted(ALLOWED_STATUS)}")
        return None

    evidence = route.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        add_error(errors, f"{context} evidence must be a non-empty list")
    else:
        for index, value in enumerate(evidence, start=1):
            if not isinstance(value, str) or not value:
                add_error(
                    errors, f"{context} evidence[{index}] must be a non-empty string"
                )
                continue
            file_path = (ROOT / value).resolve()
            if not file_path.is_file():
                add_error(errors, f"{context} evidence file not found: {value}")
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except OSError as ex:
                add_error(errors, f"{context} evidence file unreadable {value}: {ex}")
                continue
            check_redaction(errors, str(file_path.relative_to(ROOT)), text)

    last_tested = route.get("last_tested")
    if not isinstance(last_tested, str):
        add_error(errors, f"{context} last_tested must be a string date")
        last_tested_date = None
    else:
        last_tested_date = parse_iso_date(
            last_tested,
            context=f"{context} last_tested",
            errors=errors,
        )

    if require_pass:
        if status not in PASS_STATUS:
            add_error(errors, f"{context} must be pass or na in require-pass mode")
        if last_tested_date is not None:
            age_days = (datetime.now(UTC).date() - last_tested_date).days
            if age_days > max_age_days:
                add_error(
                    errors,
                    f"{context} evidence is stale ({age_days}d > {max_age_days}d)",
                )

    return status if isinstance(status, str) else None


def validate_matrix(
    payload: dict[str, Any],
    *,
    matrix_path: Path,
    require_pass: bool,
    max_age_days: int,
) -> tuple[list[str], list[str]]:
    """Validate matrix schema, route evidence, and redaction expectations."""
    errors: list[str] = []
    summaries: list[str] = []

    schema_version = payload.get("schema_version")
    if schema_version != 1:
        add_error(errors, "schema_version must be 1")

    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str):
        add_error(errors, "generated_at must be a YYYY-MM-DD string")
    else:
        parse_iso_date(generated_at, context="generated_at", errors=errors)

    matrix_text = matrix_path.read_text(encoding="utf-8")
    check_redaction(errors, str(matrix_path.relative_to(ROOT)), matrix_text)

    revisions = payload.get("verified_revisions")
    if not isinstance(revisions, list) or not revisions:
        add_error(errors, "verified_revisions must be a non-empty list")
        return errors, summaries

    seen_revision_ids: set[str] = set()

    for index, entry in enumerate(revisions, start=1):
        if not isinstance(entry, dict):
            add_error(errors, f"verified_revisions[{index}] must be an object")
            continue

        revision_id = entry.get("id")
        if not isinstance(revision_id, str) or not revision_id:
            add_error(
                errors, f"verified_revisions[{index}] id must be a non-empty string"
            )
            continue
        if revision_id in seen_revision_ids:
            add_error(errors, f"duplicate revision id: {revision_id}")
            continue
        seen_revision_ids.add(revision_id)

        for key in ("model", "hardware_version", "raw_hardware_version"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                add_error(
                    errors, f"revision {revision_id} {key} must be a non-empty string"
                )

        privacy_review = entry.get("privacy_review")
        if privacy_review != "pass":
            add_error(errors, f"revision {revision_id} privacy_review must be pass")

        routes = entry.get("routes")
        if not isinstance(routes, dict):
            add_error(errors, f"revision {revision_id} routes must be an object")
            continue

        route_summaries: list[str] = []
        for route_name in REQUIRED_ROUTES:
            route = routes.get(route_name)
            if not isinstance(route, dict):
                add_error(errors, f"revision {revision_id} missing route {route_name}")
                continue
            route_status = _validate_route(
                route_name,
                route,
                revision_id=revision_id,
                matrix_path=matrix_path,
                require_pass=require_pass,
                max_age_days=max_age_days,
                errors=errors,
            )
            if route_status is not None:
                route_summaries.append(f"{route_name}={route_status}")

        scenarios = entry.get("scenarios")
        if not isinstance(scenarios, dict):
            add_error(errors, f"revision {revision_id} scenarios must be an object")
        else:
            for scenario in REQUIRED_SCENARIOS:
                status = scenarios.get(scenario)
                if not isinstance(status, str) or status not in ALLOWED_STATUS:
                    add_error(
                        errors,
                        f"revision {revision_id} scenario {scenario} must be one of {sorted(ALLOWED_STATUS)}",
                    )
                    continue
                if require_pass and status not in PASS_STATUS:
                    add_error(
                        errors,
                        f"revision {revision_id} scenario {scenario} must be pass or na in require-pass mode",
                    )

        write_capabilities = entry.get("write_capabilities")
        if not isinstance(write_capabilities, dict):
            add_error(
                errors, f"revision {revision_id} write_capabilities must be an object"
            )
        else:
            for capability in REQUIRED_WRITE_CAPABILITIES:
                status = write_capabilities.get(capability)
                if not isinstance(status, str) or status not in ALLOWED_STATUS:
                    add_error(
                        errors,
                        "revision "
                        f"{revision_id} write capability {capability} "
                        f"must be one of {sorted(ALLOWED_STATUS)}",
                    )
                    continue
                if require_pass and status not in PASS_STATUS:
                    add_error(
                        errors,
                        "revision "
                        f"{revision_id} write capability {capability} "
                        "must be pass or na in require-pass mode",
                    )

        for text_value in iter_strings(entry):
            if MAC_ADDRESS.search(text_value):
                add_error(
                    errors,
                    f"revision {revision_id} contains non-redacted Bluetooth MAC text",
                )

        if route_summaries:
            summaries.append(f"{revision_id}: " + ", ".join(route_summaries))

    return errors, summaries


def main() -> int:
    """Run validation and print a concise summary."""
    args = parse_args()
    matrix_path = Path(args.matrix).resolve()

    if not matrix_path.is_file():
        print(f"Matrix file not found: {matrix_path}", file=sys.stderr)
        return 1

    try:
        payload = read_json(matrix_path)
    except (OSError, ValueError, json.JSONDecodeError) as ex:
        print(f"Failed to load matrix file: {ex}", file=sys.stderr)
        return 1

    errors, summaries = validate_matrix(
        payload,
        matrix_path=matrix_path,
        require_pass=bool(args.require_pass),
        max_age_days=args.max_age_days,
    )

    if errors:
        print("HIL qualification validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("HIL qualification validation passed")
    if summaries:
        for summary in summaries:
            print(f"- {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
