#!/usr/bin/env python3
"""Validate schema-v2 HIL qualification records and privacy safeguards."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX_SCHEMA_VERSION = 2
DEFAULT_MATRIX = ROOT / "tests" / "hil" / "qualification_matrix.json"
STATUSES = {"pass", "fail", "pending", "not_applicable", "inconclusive"}
STRICT_SCENARIO_STATUSES = {"pass"}
STRICT_CAPABILITY_STATUSES = {"pass", "not_applicable"}
REQUIRED_ROUTES = {"local_adapter", "active_proxy"}
RUN_ROUTES = REQUIRED_ROUTES | {"unverified", "mixed"}
CAPTURE_COMPLETENESS = {"complete", "incomplete", "unknown"}
BUILD_VERIFICATION = {"verified", "unverified", "mismatch"}
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--max-age-days", type=int, default=30)
    args = parser.parse_args()
    if args.max_age_days < 0:
        parser.error("--max-age-days must be a non-negative integer")
    return args


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("matrix root must be a JSON object")
    return payload


def _error(errors: list[str], message: str) -> None:
    errors.append(message)


def _strings(value: Any):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _check_date(value: Any, context: str, errors: list[str]) -> date | None:
    if not isinstance(value, str):
        _error(errors, f"{context} must be a YYYY-MM-DD date")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _error(errors, f"{context} must be a YYYY-MM-DD date")
        return None


def _check_evidence(
    values: Any, context: str, errors: list[str], *, required: bool = False
) -> None:
    if not isinstance(values, list) or (required and not values):
        _error(
            errors,
            f"{context} evidence must be {'a non-empty' if required else 'a'} list",
        )
        return
    for index, value in enumerate(values, 1):
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            _error(
                errors,
                f"{context} evidence[{index}] must be a safe repository-relative path",
            )
            continue
        path = (ROOT / value).resolve()
        try:
            path.relative_to(ROOT)
        except ValueError:
            _error(errors, f"{context} evidence[{index}] escapes the repository")
            continue
        if not path.is_file():
            _error(errors, f"{context} evidence file not found: {value}")
            continue
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            _error(errors, f"{context} evidence file unreadable {value}: {exc}")
            continue
        if MAC_ADDRESS.search(contents):
            _error(errors, f"{value} contains a Bluetooth MAC address")


def _capture_rows(values: Any) -> list[dict[str, Any]]:
    """Load explicitly scoped JSONL capture rows, excluding legacy examples."""
    if not isinstance(values, list):
        return []
    rows: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, str) or ".sample." in Path(value).name:
            continue
        path = (ROOT / value).resolve()
        try:
            path.relative_to(ROOT)
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeError, ValueError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError, TypeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _has_bound_capture(
    values: Any,
    *,
    route: Any,
    revision_id: str,
    firmware_version: Any,
    run_id: Any,
    captured_at: Any,
    result_key: str,
    capability: bool,
    expected_verdict: str,
) -> bool:
    for row in _capture_rows(values):
        frame_hex = row.get("frame_hex")
        row_captured_at = row.get("captured_at")
        try:
            capture_time = datetime.fromisoformat(
                row_captured_at.replace("Z", "+00:00")
            )
            run_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except AttributeError, ValueError:
            continue
        if (
            row.get("route") != route
            or row.get("revision_id") != revision_id
            or row.get("firmware_version") != firmware_version
            or row.get("run_id") != run_id
            or capture_time.tzinfo is None
            or run_time.tzinfo is None
            or capture_time.astimezone(UTC) != run_time.astimezone(UTC)
            or row.get("verdict") != expected_verdict
            or row.get("physical_outcome")
            != ("not_applicable" if expected_verdict == "not_applicable" else "pass")
            or not isinstance(frame_hex, str)
            or re.fullmatch(r"(?:[0-9A-Fa-f]{2}){1,256}", frame_hex) is None
        ):
            continue
        if capability:
            if row.get("capability") == result_key and row.get("direction") == (
                "write" if expected_verdict == "ok" else "observe"
            ):
                return True
        elif row.get("scenario") == result_key and row.get("direction") in {
            "write",
            "notify",
        }:
            return True
    return False


def _check_result_map(
    results: Any,
    expected: tuple[str, ...],
    context: str,
    errors: list[str],
    *,
    require_pass: bool,
    route: Any,
    revision_id: str,
    firmware_version: Any,
    run_id: Any,
    captured_at: Any,
    capability: bool = False,
) -> None:
    if not isinstance(results, dict):
        _error(errors, f"{context} must be an object")
        return
    for key in expected:
        item = results.get(key)
        item_context = f"{context} {key}"
        if not isinstance(item, dict):
            _error(errors, f"{item_context} must be an object")
            continue
        status = item.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            _error(errors, f"{item_context} status must be one of {sorted(STATUSES)}")
        _check_evidence(
            item.get("evidence"),
            item_context,
            errors,
            required=status == "pass"
            or (require_pass and capability and status == "not_applicable"),
        )
        if status == "not_applicable" and not (
            isinstance(item.get("rationale"), str) and item["rationale"].strip()
        ):
            _error(errors, f"{item_context} not_applicable status requires a rationale")
        if require_pass:
            acceptable = (
                STRICT_CAPABILITY_STATUSES if capability else STRICT_SCENARIO_STATUSES
            )
            if status not in acceptable:
                expected = "pass or justified not_applicable" if capability else "pass"
                _error(
                    errors, f"{item_context} must be {expected} in require-pass mode"
                )
            elif not _has_bound_capture(
                item.get("evidence"),
                route=route,
                revision_id=revision_id,
                firmware_version=firmware_version,
                run_id=run_id,
                captured_at=captured_at,
                result_key=key,
                capability=capability,
                expected_verdict="not_applicable"
                if status == "not_applicable"
                else "ok",
            ):
                _error(
                    errors,
                    f"{item_context} evidence must contain a route- and operation-bound capture row",
                )


def _validate_run(
    run: Any,
    context: str,
    errors: list[str],
    *,
    require_pass: bool,
    max_age_days: int,
    revision_id: str,
) -> str | None:
    if not isinstance(run, dict):
        _error(errors, f"{context} must be an object")
        return None
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id
    ):
        _error(errors, f"{context} run_id must be a stable opaque identifier")
    route = run.get("route")
    if not isinstance(route, str) or route not in RUN_ROUTES:
        _error(errors, f"{context} route must be one of {sorted(RUN_ROUTES)}")
    firmware = run.get("firmware_version")
    if firmware is not None and (not isinstance(firmware, str) or not firmware.strip()):
        _error(errors, f"{context} firmware_version must be a non-empty string or null")
    if (
        isinstance(route, str)
        and route in REQUIRED_ROUTES
        and firmware is None
        and require_pass
    ):
        _error(errors, f"{context} cannot qualify without a firmware_version")
    captured_at = run.get("captured_at")
    captured_date: date | None = None
    if captured_at is None:
        pass
    elif not isinstance(captured_at, str):
        _error(errors, f"{context} captured_at must be an ISO-8601 timestamp")
    else:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            captured_date = parsed.date()
        except ValueError:
            _error(errors, f"{context} captured_at must include a timezone")
    for field in ("tool_version", "integration_version"):
        value = run.get(field)
        if not isinstance(value, str) or not value.strip():
            _error(
                errors,
                f"{context} {field} must be a non-empty string (use 'unknown' if unavailable)",
            )
    build_verification = run.get("build_verification")
    if (
        not isinstance(build_verification, str)
        or build_verification not in BUILD_VERIFICATION
    ):
        _error(
            errors,
            f"{context} build_verification must be one of {sorted(BUILD_VERIFICATION)}",
        )
    completeness = run.get("capture_completeness")
    if not isinstance(completeness, str) or completeness not in CAPTURE_COMPLETENESS:
        _error(
            errors,
            f"{context} capture_completeness must be one of {sorted(CAPTURE_COMPLETENESS)}",
        )
    run_evidence = run.get("evidence")
    _check_evidence(run_evidence, context, errors)
    qualification = run.get("qualification_status")
    if not isinstance(qualification, str) or qualification not in STATUSES:
        _error(
            errors, f"{context} qualification_status must be one of {sorted(STATUSES)}"
        )
    if qualification == "not_applicable" and not (
        isinstance(run.get("rationale"), str) and run["rationale"].strip()
    ):
        _error(errors, f"{context} not_applicable status requires a rationale")
    if require_pass:
        if (
            qualification != "pass"
            or route not in REQUIRED_ROUTES
            or firmware is None
            or completeness != "complete"
            or build_verification != "verified"
            or not run_evidence
        ):
            _error(
                errors,
                f"{context} lacks complete, attributed evidence for require-pass mode",
            )
        if captured_date is not None:
            age = (datetime.now(UTC).date() - captured_date).days
            if age < 0:
                _error(errors, f"{context} captured_at is in the future")
            if age > max_age_days:
                _error(
                    errors, f"{context} evidence is stale ({age}d > {max_age_days}d)"
                )
    _check_result_map(
        run.get("scenarios"),
        REQUIRED_SCENARIOS,
        f"{context} scenario",
        errors,
        require_pass=require_pass,
        route=route,
        revision_id=revision_id,
        firmware_version=firmware,
        run_id=run_id,
        captured_at=captured_at,
    )
    _check_result_map(
        run.get("write_capabilities"),
        REQUIRED_WRITE_CAPABILITIES,
        f"{context} write capability",
        errors,
        require_pass=require_pass,
        route=route,
        revision_id=revision_id,
        firmware_version=firmware,
        run_id=run_id,
        captured_at=captured_at,
        capability=True,
    )
    if qualification == "pass" and (
        route not in REQUIRED_ROUTES
        or firmware is None
        or completeness != "complete"
        or build_verification != "verified"
        or not run_evidence
        or captured_date is None
    ):
        _error(
            errors,
            f"{context} cannot claim pass without complete, dated, firmware- and route-attributed evidence",
        )
    return run_id if isinstance(run_id, str) else None


def _strict_candidate(run: Any) -> tuple[str, str, datetime] | None:
    """Return only complete, build-verified physical outcomes eligible for strict gating."""
    if not isinstance(run, dict):
        return None
    firmware = run.get("firmware_version")
    route = run.get("route")
    captured_at = run.get("captured_at")
    if (
        not isinstance(firmware, str)
        or not firmware.strip()
        or route not in REQUIRED_ROUTES
        or run.get("capture_completeness") != "complete"
        or run.get("build_verification") != "verified"
        or run.get("qualification_status") not in {"pass", "fail"}
        or not isinstance(run.get("evidence"), list)
        or not run["evidence"]
        or not isinstance(captured_at, str)
    ):
        return None
    try:
        parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return firmware, route, parsed.astimezone(UTC)


def validate_matrix(
    payload: dict[str, Any], *, matrix_path: Path, require_pass: bool, max_age_days: int
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    summaries: list[str] = []
    if payload.get("schema_version") != MATRIX_SCHEMA_VERSION:
        _error(errors, f"schema_version must be {MATRIX_SCHEMA_VERSION}")
    _check_date(payload.get("generated_at"), "generated_at", errors)
    if MAC_ADDRESS.search(matrix_path.read_text(encoding="utf-8")):
        _error(errors, "matrix contains a Bluetooth MAC address")
    revisions = payload.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        _error(errors, "revisions must be a non-empty list")
        return errors, summaries
    seen_revisions: set[str] = set()
    seen_runs: set[str] = set()
    for index, revision in enumerate(revisions, 1):
        if not isinstance(revision, dict):
            _error(errors, f"revisions[{index}] must be an object")
            continue
        revision_id = revision.get("id")
        if not isinstance(revision_id, str) or not revision_id:
            _error(errors, f"revisions[{index}] id must be a non-empty string")
            continue
        if revision_id in seen_revisions:
            _error(errors, f"duplicate revision id: {revision_id}")
        seen_revisions.add(revision_id)
        for field in ("model", "hardware_version", "raw_hardware_version"):
            if not isinstance(revision.get(field), str) or not revision[field].strip():
                _error(
                    errors, f"revision {revision_id} {field} must be a non-empty string"
                )
        if revision.get("privacy_review") != "pass":
            _error(errors, f"revision {revision_id} privacy_review must be pass")
        runs = revision.get("runs")
        if not isinstance(runs, list):
            _error(errors, f"revision {revision_id} runs must be a list")
            continue
        strict_candidates: dict[tuple[str, str], list[tuple[datetime, Any]]] = {}
        firmware_scope: set[str] = set()
        for run_index, run in enumerate(runs, 1):
            run_id = _validate_run(
                run,
                f"revision {revision_id} run[{run_index}]",
                errors,
                require_pass=False,
                max_age_days=max_age_days,
                revision_id=revision_id,
            )
            if run_id is not None:
                if run_id in seen_runs:
                    _error(errors, f"duplicate run_id: {run_id}")
                seen_runs.add(run_id)
            # Any explicitly reported firmware is in the qualification scope,
            # even when its run is incomplete or inconclusive. Eligibility for
            # a route remains a separate decision below.
            if isinstance(run, dict):
                firmware = run.get("firmware_version")
                if isinstance(firmware, str) and firmware.strip():
                    firmware_scope.add(firmware)
            candidate = _strict_candidate(run)
            if candidate is not None:
                firmware, route, captured_at = candidate
                strict_candidates.setdefault((firmware, route), []).append(
                    (captured_at, run)
                )
        legacy = revision.get("legacy_results")
        if not isinstance(legacy, list):
            _error(errors, f"revision {revision_id} legacy_results must be a list")
            continue
        for legacy_index, item in enumerate(legacy, 1):
            context = f"revision {revision_id} legacy_results[{legacy_index}]"
            if (
                not isinstance(item, dict)
                or item.get("scope") != "unscoped_legacy"
                or type(item.get("source_schema_version")) is not int
                or item.get("source_schema_version") != 1
                or not isinstance(item.get("source"), dict)
            ):
                _error(
                    errors, f"{context} must preserve a v1 source as unscoped_legacy"
                )
                continue
            source = item["source"]
            if source.get("id") != revision_id:
                _error(errors, f"{context} source revision id does not match")
            normalized = item.get("normalized_outcomes")
            if not isinstance(normalized, dict) or set(normalized) != {
                "routes",
                "scenarios",
                "write_capabilities",
            }:
                _error(
                    errors,
                    f"{context} normalized_outcomes must preserve v1 result groups",
                )
            else:
                for result_group in normalized.values():
                    if not isinstance(result_group, dict):
                        _error(
                            errors,
                            f"{context} normalized result groups must be objects",
                        )
                        continue
                    for status in result_group.values():
                        if not isinstance(status, str) or status not in STATUSES:
                            _error(
                                errors,
                                f"{context} has an invalid normalized legacy status",
                            )
            for value in _strings(source):
                if MAC_ADDRESS.search(value):
                    _error(errors, f"{context} contains a Bluetooth MAC address")
        if require_pass and not strict_candidates:
            _error(
                errors,
                f"revision {revision_id} has no eligible scoped runs for require-pass mode",
            )
        if require_pass:
            for firmware in sorted(firmware_scope):
                for route in sorted(REQUIRED_ROUTES):
                    candidates = strict_candidates.get((firmware, route), [])
                    if not candidates:
                        _error(
                            errors,
                            f"revision {revision_id} firmware {firmware} missing strict route: {route}",
                        )
                        continue
                    newest_at = max(captured_at for captured_at, _ in candidates)
                    newest = [
                        run
                        for captured_at, run in candidates
                        if captured_at == newest_at
                    ]
                    if len(newest) != 1:
                        _error(
                            errors,
                            f"revision {revision_id} firmware {firmware} route {route} has ambiguous latest runs at {newest_at.isoformat()}",
                        )
                        continue
                    run = newest[0]
                    run_id = run.get("run_id", "unknown")
                    _validate_run(
                        run,
                        f"revision {revision_id} current run {run_id}",
                        errors,
                        require_pass=True,
                        max_age_days=max_age_days,
                        revision_id=revision_id,
                    )
        summaries.append(
            f"{revision_id}: {len(runs)} scoped run(s), "
            f"{len(legacy)} unscoped legacy record(s)"
        )
    migration = payload.get("migration")
    if migration is not None and (
        not isinstance(migration, dict) or migration.get("from_schema_version") != 1
    ):
        _error(
            errors, "migration metadata, when present, must identify v1 source history"
        )
    return errors, summaries


def main() -> int:
    args = parse_args()
    matrix_path = Path(args.matrix).resolve()
    if not matrix_path.is_file():
        print(f"Matrix file not found: {matrix_path}", file=sys.stderr)
        return 1
    try:
        payload = read_json(matrix_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load matrix file: {exc}", file=sys.stderr)
        return 1
    errors, summaries = validate_matrix(
        payload,
        matrix_path=matrix_path,
        require_pass=args.require_pass,
        max_age_days=args.max_age_days,
    )
    if errors:
        print("HIL qualification validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("HIL qualification validation passed")
    for summary in summaries:
        print(f"- {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
