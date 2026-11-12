"""Deterministic, evidence-only hardware support tier evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from scripts import validate_hil_qualification as hil_validation

BASIC_OUTPUTS = ("ac_output", "dc_output", "light_output")
VERIFIED_ROUTES = hil_validation.VERIFIED_ROUTES
ALL_FEATURES = (
    "ac_output",
    "dc_output",
    "light_output",
    "eco_mode",
    "work_mode",
    "eco_timeout",
    "car_charger",
)
GOLD_SCENARIOS = (
    "reconnect_failover",
    "rapid_consecutive_writes",
    "stale_timeout_recovery",
)


def _stamp(run: dict[str, Any]) -> float:
    value = run.get("captured_at")
    if not isinstance(value, str):
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return float("-inf")
        return parsed.astimezone(UTC).timestamp()
    except AttributeError, ValueError:
        return float("-inf")


def _scope_run(run: Any) -> bool:
    if not isinstance(run, dict):
        return False
    authorization = run.get("authorization_context")
    expected_source = {
        "production": "reviewed_production_session",
        "experimental_hil": "experimental_hil_grant",
    }.get(authorization)
    fingerprint = run.get("build_fingerprint")
    if not isinstance(fingerprint, str):
        return False
    try:
        captured = datetime.fromisoformat(
            run.get("captured_at", "").replace("Z", "+00:00")
        )
    except AttributeError, ValueError:
        return False
    return (
        run.get("route") in VERIFIED_ROUTES
        and isinstance(run.get("firmware_version"), str)
        and bool(run["firmware_version"].strip())
        and authorization in {"production", "experimental_hil"}
        and run.get("authorization_context_source") == expected_source
        and run.get("capture_completeness") == "complete"
        and isinstance(run.get("evidence"), list)
        and bool(run["evidence"])
        and hil_validation._evidence_digests(run["evidence"]) is not None
        and run.get("build_verification") == "verified"
        and isinstance(run.get("run_id"), str)
        and bool(run["run_id"].strip())
        and isinstance(run.get("session_id"), str)
        and run["session_id"] not in {"", "unknown"}
        and len(fingerprint) == 64
        and all(char in "0123456789abcdef" for char in fingerprint)
        and run.get("installed_fingerprint") == fingerprint
        and run.get("checkout_fingerprint") == fingerprint
        and captured.tzinfo is not None
        and captured.astimezone(UTC) <= datetime.now(UTC)
        and run.get("qualification_status") != "not_applicable"
        and not (
            authorization == "experimental_hil"
            and run.get("qualification_status") == "pass"
        )
    )


def _result(run: dict[str, Any], section: str, key: str) -> dict[str, Any]:
    values = run.get(section)
    item = values.get(key) if isinstance(values, dict) else None
    return item if isinstance(item, dict) else {}


def _latest_outcome(
    runs: list[dict[str, Any]], section: str, key: str
) -> dict[str, Any]:
    run = _latest_run(runs, section, key)
    return _result(run, section, key) if run else {}


def _latest_run(
    runs: list[dict[str, Any]], section: str, key: str
) -> dict[str, Any] | None:
    candidates = []
    for run in runs:
        item = _result(run, section, key)
        if item.get("status") == "pass" and _pass(item):
            candidates.append((run, item))
        elif item.get("status") == "fail" and _reviewed_fail(item):
            candidates.append((run, item))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda pair: (
            _stamp(pair[0]),
            _result(pair[0], section, key).get("status") == "fail",
        ),
    )[0]


def _presence(runs: list[dict[str, Any]], feature: str) -> str:
    entries = []
    for run in runs:
        presence = run.get("feature_presence")
        item = presence.get(feature) if isinstance(presence, dict) else None
        if isinstance(item, dict) and item.get("status") in {
            "present",
            "absent",
            "unknown",
        }:
            entries.append((_stamp(run), item["status"], item))
    if not entries:
        return "unknown"
    _, status, item = max(
        entries,
        key=lambda entry: (
            entry[0],
            {"present": 0, "absent": 1, "unknown": 2}[entry[1]],
        ),
    )
    if status == "absent":
        try:
            reviewed = datetime.fromisoformat(
                item.get("reviewed_at", "").replace("Z", "+00:00")
            )
        except AttributeError, ValueError:
            return "unknown"
        evidence = item.get("evidence")
        digests = item.get("evidence_sha256")
        actual_digests = hil_validation._evidence_digests(evidence)
        if (
            not item.get("reviewed_by")
            or reviewed.tzinfo is None
            or not isinstance(evidence, list)
            or not evidence
            or not isinstance(digests, dict)
            or actual_digests is None
            or digests != actual_digests
        ):
            return "unknown"
    return status


def _proof(run: dict[str, Any], name: str) -> dict[str, Any]:
    proof = run.get("bronze_evidence")
    item = proof.get(name) if isinstance(proof, dict) else None
    return item if isinstance(item, dict) else {}


def _read_artifact(path: Any) -> dict[str, Any] | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        resolved = (hil_validation.ROOT / path).resolve()
        resolved.relative_to(hil_validation.ROOT)
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError, UnicodeError, ValueError, TypeError:
        return None
    return payload if isinstance(payload, dict) else None


def _scope_matches(
    artifact: dict[str, Any], run: dict[str, Any], revision: dict[str, Any]
) -> bool:
    expected = {
        "revision_id": revision.get("id"),
        "model": revision.get("model"),
        "hardware_revision": revision.get("hardware_version"),
        "firmware_version": run.get("firmware_version"),
        "route": run.get("route"),
        "run_id": run.get("run_id"),
        "session_id": run.get("session_id"),
        "build_fingerprint": run.get("build_fingerprint"),
        "captured_at": run.get("captured_at"),
    }
    return all(artifact.get(key) == value for key, value in expected.items())


def _scope_artifact(
    path: Any,
    run: dict[str, Any],
    revision: dict[str, Any],
    *,
    artifact_type: str,
) -> dict[str, Any] | None:
    artifact = _read_artifact(path)
    if (
        artifact is None
        or artifact.get("artifact_type") != artifact_type
        or not _scope_matches(artifact, run, revision)
    ):
        return None
    return artifact


def _contract_excluded(revision: dict[str, Any], feature: str) -> bool:
    item = revision.get("feature_contract")
    item = item.get(feature) if isinstance(item, dict) else None
    if not isinstance(item, dict) or (
        item.get("status") != "excluded"
        or item.get("safely_disabled") is not True
        or not isinstance(item.get("rationale"), str)
        or not item["rationale"].strip()
    ):
        return False
    if len(item.get("evidence", [])) != 1 or not _review_record(item, "confirmed_pass"):
        return False
    artifact = _read_artifact(item["evidence"][0])
    return bool(
        artifact
        and artifact.get("artifact_type") == "feature_contract"
        and artifact.get("revision_id") == revision.get("id")
        and artifact.get("model") == revision.get("model")
        and artifact.get("hardware_revision") == revision.get("hardware_version")
        and artifact.get("feature") == feature
        and artifact.get("safely_disabled") is True
        and artifact.get("rationale") == item["rationale"]
    )


def normalize_soak_evidence(
    scope: dict[str, Any],
    checkpoint: dict[str, Any],
    sample_rows: list[dict[str, Any]],
    trace_windows: list[dict[str, Any]],
    saved_traces: list[dict[str, Any]],
    *,
    allowed_gap_seconds: int = 600,
) -> list[dict[str, Any]]:
    """Normalize a completed runner checkpoint and its private soak files."""
    steps = checkpoint.get("steps")
    if (
        checkpoint.get("schema_version") != 2
        or checkpoint.get("scenario_id") != "soak_24h_observation"
        or not isinstance(steps, list)
        or len(steps) != 1
    ):
        raise ValueError("soak checkpoint must contain one uninterrupted segment")
    step = steps[0]
    if (
        not isinstance(step, dict)
        or step.get("step_id") != "soak_segment"
        or step.get("outcome") != "pass"
        or step.get("restart_discontinuity") is True
        or step.get("data_limit_hit") is True
        or step.get("dropped_traces") != 0
        or step.get("clock_discontinuities") != 0
    ):
        raise ValueError("soak checkpoint does not prove a clean passing segment")
    started = datetime.fromisoformat(step["segment_started_utc"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(step["ended_utc"].replace("Z", "+00:00"))
    if started.tzinfo is None or ended.tzinfo is None:
        raise ValueError("soak checkpoint timestamps must include a timezone")
    duration = (ended.astimezone(UTC) - started.astimezone(UTC)).total_seconds()
    samples = [row for row in sample_rows if row.get("event") == "sample"]
    windows = [row for row in trace_windows if row.get("event") == "trace_window"]
    elapsed = [row.get("elapsed_seconds") for row in samples]
    if (
        duration < 86400
        or step.get("measured_seconds", 0) < 86400
        or step.get("sample_count") != len(samples)
        or step.get("trace_count") != len(windows)
        or len(samples) < 289
        or len(windows) < 24
        or any(
            row.get("event")
            in {
                "process_restart_gap",
                "session_generation_change",
                "clock_discontinuity",
                "trace_window_dropped",
            }
            for row in [*sample_rows, *trace_windows]
        )
        or any(type(value) not in (int, float) for value in elapsed)
        or any(
            later - earlier > allowed_gap_seconds
            for earlier, later in zip(elapsed, elapsed[1:], strict=False)
        )
        or len(saved_traces) != len(windows)
        or any(
            row.get("saved") is not True
            or row.get("complete") is not True
            or row.get("capture_id_present") is not True
            or row.get("observed_route") != scope["route"]
            for row in windows
        )
        or any(
            trace.get("complete") is not True
            or trace.get("observed_route") != scope["route"]
            or not isinstance(trace.get("capture_id"), str)
            or not trace["capture_id"]
            for trace in saved_traces
        )
    ):
        raise ValueError("runner files do not substantiate the passing soak checkpoint")
    scoped_samples = []
    for row in samples:
        if (
            row.get("status_fresh") is not True
            or not isinstance(row.get("telemetry"), dict)
            or not any(
                isinstance(value, dict) and value.get("verified") is True
                for value in row["telemetry"].values()
            )
        ):
            raise ValueError("soak contains a stale or unverified telemetry sample")
        scoped_samples.append({**scope, **row})
    scoped_windows = [{**scope, **window} for window in windows]
    summary = {
        **scope,
        "event": "segment_summary",
        "segment_started_utc": started.isoformat(),
        "segment_ended_utc": ended.isoformat(),
        "measured_seconds": duration,
        "sample_count": len(scoped_samples),
        "minimum_samples": 289,
        "trace_count": len(scoped_windows),
        "minimum_traces": 24,
        "maximum_gap_seconds": step.get("maximum_gap_seconds"),
        "allowed_gap_seconds": allowed_gap_seconds,
        "dropped_traces": 0,
        "clock_discontinuities": 0,
        "outcome": "pass",
        "resumed": False,
        "interrupted": False,
        "uninterrupted": True,
    }
    return [summary, *scoped_samples, *scoped_windows]


def _review_record(item: dict[str, Any], outcome: str) -> bool:
    review = item.get("physical_review")
    if not isinstance(review, dict) or review.get("outcome") != outcome:
        return False
    try:
        reviewed_at = datetime.fromisoformat(
            review.get("reviewed_at", "").replace("Z", "+00:00")
        )
    except AttributeError, ValueError:
        return False
    evidence = item.get("evidence")
    actual_digests = hil_validation._evidence_digests(evidence)
    return bool(
        isinstance(review.get("reviewed_by"), str)
        and review["reviewed_by"].strip()
        and reviewed_at.tzinfo is not None
        and actual_digests is not None
        and review.get("evidence_sha256") == actual_digests
    )


def _pass(item: dict[str, Any]) -> bool:
    return item.get("status") == "pass" and _review_record(item, "confirmed_pass")


def _reviewed_fail(item: dict[str, Any]) -> bool:
    return item.get("status") == "fail" and _review_record(item, "confirmed_fail")


def _bronze_proof(
    run: dict[str, Any], revision: dict[str, Any], claim: str
) -> dict[str, Any] | None:
    item = _proof(run, claim)
    if not _pass(item) or not item.get("evidence"):
        return None
    artifact = _scope_artifact(
        item.get("claim_artifact"),
        run,
        revision,
        artifact_type="bronze_evidence",
    )
    if (
        artifact is None
        or item.get("claim_artifact") not in item["evidence"]
        or artifact.get("claim") != claim
    ):
        return None
    if claim == "identity":
        return (
            {**artifact, "evidence": item["evidence"]}
            if artifact.get("identity_confirmed") is True
            else None
        )
    if claim == "fresh_telemetry":
        if (
            artifact.get("fresh") is not True
            or artifact.get("plausible") is not True
            or type(artifact.get("sample_age_seconds")) is not int
            or artifact["sample_age_seconds"] > 60
        ):
            return None
        return {**artifact, "evidence": item["evidence"]}
    # An independently reviewed assertion cannot substitute for a real active
    # protocol probe. Require the existing route/session/build-bound GATT row.
    capture = item.get("capture_evidence")
    if capture not in item["evidence"] or not hil_validation._has_bound_capture(
        [capture],
        route=run["route"],
        revision_id=revision["id"],
        firmware_version=run["firmware_version"],
        run_id=run["run_id"],
        captured_at=run["captured_at"],
        session_id=run["session_id"],
        build_fingerprint=run["build_fingerprint"],
        result_key="golden_vector_replay",
        capability=False,
        expected_verdict="ok",
    ):
        return None
    return {**artifact, "evidence": item["evidence"]}


def _bound_capture(
    path: Any,
    run: dict[str, Any],
    revision: dict[str, Any],
    feature: str,
    state: Any,
    *,
    action_stage: str = "transition",
) -> bool:
    if not isinstance(path, str) or path not in run.get("evidence", []):
        return False
    requested_state = state == "on" if isinstance(state, str) else state
    rows = hil_validation._capture_rows([path])
    return bool(
        rows
        and any(
            hil_validation._valid_tier_capture_row(
                row,
                {
                    "route": run["route"],
                    "revision_id": revision["id"],
                    "firmware_version": run["firmware_version"],
                    "run_id": run["run_id"],
                    "session_id": run["session_id"],
                    "build_fingerprint": run["build_fingerprint"],
                    "captured_at": run["captured_at"],
                },
                feature,
                requested_state,
                action_stage,
            )
            for row in rows
        )
    )


def _output_pass(
    item: dict[str, Any], run: dict[str, Any], revision: dict[str, Any], feature: str
) -> bool:
    review = item.get("physical_review")
    if not _pass(item) or not isinstance(review, dict):
        return False
    transitions = review.get("output_transitions")
    if not isinstance(transitions, list) or len(transitions) != 2:
        return False
    states: set[str] = set()
    for entry in transitions:
        if not isinstance(entry, dict) or entry.get("state") not in {"on", "off"}:
            return False
        state = entry["state"]
        artifact = _scope_artifact(
            entry.get("evidence"),
            run,
            revision,
            artifact_type="output_transition",
        )
        if (
            state in states
            or artifact is None
            or entry.get("evidence") not in item.get("evidence", [])
            or entry.get("capture_evidence") not in item.get("evidence", [])
            or artifact.get("feature") != feature
            or artifact.get("requested_state") != state
            or artifact.get("observed_state") != state
            or artifact.get("restored") is not True
            or artifact.get("initial_state") not in {"on", "off"}
            or artifact.get("restored_state") != artifact.get("initial_state")
            or not _bound_capture(
                entry.get("capture_evidence"), run, revision, feature, state
            )
            or not _bound_restoration(
                entry.get("restoration_capture"),
                item,
                run,
                revision,
                feature,
                artifact.get("initial_state") == "on" if artifact else None,
            )
        ):
            return False
        states.add(state)
    return states == {"on", "off"}


def _restoration_pass(
    item: dict[str, Any],
    run: dict[str, Any],
    revision: dict[str, Any],
    feature: str,
) -> bool:
    review = item.get("physical_review")
    artifact = _scope_artifact(
        review.get("restoration_evidence") if isinstance(review, dict) else None,
        run,
        revision,
        artifact_type="feature_restoration",
    )
    return bool(
        _pass(item)
        and hil_validation._has_bound_capture(
            item.get("evidence"),
            route=run.get("route"),
            revision_id=revision.get("id"),
            firmware_version=run.get("firmware_version"),
            run_id=run.get("run_id"),
            captured_at=run.get("captured_at"),
            session_id=run.get("session_id"),
            build_fingerprint=run.get("build_fingerprint"),
            result_key=feature,
            capability=True,
            expected_verdict="ok",
        )
        and artifact
        and review.get("restoration_evidence") in item.get("evidence", [])
        and artifact.get("feature") == feature
        and artifact.get("restored") is True
        and artifact.get("pre_state") is not None
        and artifact.get("restored_state") == artifact.get("pre_state")
        and _bound_restoration(
            review.get("restoration_capture"),
            item,
            run,
            revision,
            feature,
            artifact.get("pre_state"),
        )
    )


def _bound_restoration(
    capture: Any,
    item: dict[str, Any],
    run: dict[str, Any],
    revision: dict[str, Any],
    feature: str,
    restored_state: Any,
) -> bool:
    if not isinstance(capture, str) or capture not in item.get("evidence", []):
        return False
    rows = hil_validation._capture_rows([capture])
    return bool(
        rows
        and _bound_capture(
            capture,
            run,
            revision,
            feature,
            restored_state,
            action_stage="restoration",
        )
    )


def _feature_pass(
    item: dict[str, Any], run: dict[str, Any], revision: dict[str, Any], feature: str
) -> bool:
    if feature in BASIC_OUTPUTS:
        return _output_pass(item, run, revision, feature)
    return _restoration_pass(item, run, revision, feature)


def _scenario_pass(
    item: dict[str, Any],
    run: dict[str, Any],
    revision: dict[str, Any],
    scenario: str,
) -> bool:
    return _pass(item) and hil_validation._has_bound_capture(
        item.get("evidence"),
        route=run.get("route"),
        revision_id=revision.get("id"),
        firmware_version=run.get("firmware_version"),
        run_id=run.get("run_id"),
        captured_at=run.get("captured_at"),
        session_id=run.get("session_id"),
        build_fingerprint=run.get("build_fingerprint"),
        result_key=scenario,
        capability=False,
        expected_verdict="ok",
    )


def _soak_pass(
    item: dict[str, Any], run: dict[str, Any], revision: dict[str, Any]
) -> bool:
    scope = {
        "route": run.get("route"),
        "revision_id": revision.get("id"),
        "firmware_version": run.get("firmware_version"),
        "run_id": run.get("run_id"),
        "captured_at": run.get("captured_at"),
        "session_id": run.get("session_id"),
        "build_fingerprint": run.get("build_fingerprint"),
    }
    return _pass(item) and hil_validation._valid_tier_soak(item, scope)


def _tier_for_scope(
    revision: dict[str, Any],
    firmware: str,
    route: str,
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    links: set[str] = set()
    qualified: list[str] = []
    exclusions: list[str] = []

    bronze = True
    for requirement in ("identity", "protocol_probe", "fresh_telemetry"):
        proof = next(
            (
                _bronze_proof(run, revision, requirement)
                for run in sorted(runs, key=_stamp, reverse=True)
                if _bronze_proof(run, revision, requirement)
            ),
            {},
        )
        if not proof:
            bronze = False
            reasons.append(f"Bronze requires reviewed {requirement} evidence")
        else:
            links.update(
                item for item in proof.get("evidence", []) if isinstance(item, str)
            )

    latest_features: dict[str, dict[str, Any]] = {}
    latest_feature_runs: dict[str, dict[str, Any]] = {}
    feature_presence: dict[str, str] = {}
    for scenario in ("reconnect_failover", *GOLD_SCENARIOS, "soak_24h"):
        scenario_result = _latest_outcome(runs, "scenarios", scenario)
        links.update(
            item
            for item in scenario_result.get("evidence", [])
            if isinstance(item, str)
        )
    for feature in ALL_FEATURES:
        feature_presence[feature] = _presence(runs, feature)
        latest_features[feature] = _latest_outcome(runs, "write_capabilities", feature)
        latest_feature_runs[feature] = (
            _latest_run(runs, "write_capabilities", feature) or {}
        )
        result = latest_features[feature]
        links.update(
            item for item in result.get("evidence", []) if isinstance(item, str)
        )
        if _feature_pass(result, latest_feature_runs[feature], revision, feature):
            qualified.append(feature)
        if feature_presence[feature] == "absent":
            exclusions.append(feature)
        elif (
            feature in ALL_FEATURES[3:]
            and feature_presence[feature] == "unknown"
            and _contract_excluded(revision, feature)
        ):
            exclusions.append(f"{feature} (documented unavailable and safely disabled)")

    if not bronze:
        return _report(
            revision,
            firmware,
            route,
            "unqualified",
            reasons,
            links,
            qualified,
            exclusions,
            feature_presence,
        )

    reconnect_run = _latest_run(runs, "scenarios", "reconnect_failover") or {}
    reconnect = _result(reconnect_run, "scenarios", "reconnect_failover")
    silver = _scenario_pass(reconnect, reconnect_run, revision, "reconnect_failover")
    applicable_basic = [f for f in BASIC_OUTPUTS if feature_presence[f] != "absent"]
    passed_basic = [
        feature
        for feature in applicable_basic
        if _feature_pass(
            latest_features[feature],
            latest_feature_runs[feature],
            revision,
            feature,
        )
    ]
    for feature in BASIC_OUTPUTS:
        state = feature_presence[feature]
        outcome = latest_features[feature].get("status")
        if state == "unknown":
            silver = False
            reasons.append(
                f"Silver requires reviewed present/absent inventory for {feature}"
            )
        elif state == "present" and not (
            _feature_pass(
                latest_features[feature],
                latest_feature_runs.get(feature, {}),
                revision,
                feature,
            )
        ):
            silver = False
            reasons.append(
                f"Silver requires supervised on/off PASS for {feature}; "
                f"latest result is {outcome or 'missing'}"
            )
        elif state == "absent" and outcome == "pass":
            silver = False
            reasons.append(f"Silver has conflicting PASS for reviewed-absent {feature}")
    if not passed_basic:
        silver = False
        reasons.append(
            "Silver requires at least one physically present basic output PASS"
        )
    if not silver:
        reasons.append("Silver requires a reviewed basic reconnect PASS")
    if not silver:
        return _report(
            revision,
            firmware,
            route,
            "bronze",
            reasons,
            links,
            qualified,
            exclusions,
            feature_presence,
        )

    gold = True
    for feature in ALL_FEATURES:
        state = feature_presence[feature]
        result = latest_features[feature].get("status")
        if (
            feature in ALL_FEATURES[3:]
            and state == "unknown"
            and _contract_excluded(revision, feature)
        ):
            continue
        if state == "unknown":
            gold = False
            reasons.append(
                f"Gold requires declared present/absent inventory for {feature}"
            )
        elif state == "present" and not _feature_pass(
            latest_features[feature],
            latest_feature_runs[feature],
            revision,
            feature,
        ):
            gold = False
            reasons.append(
                f"Gold requires a reviewed PASS for declared {feature}; latest result is {result or 'missing'}"
            )
        elif state == "absent" and result == "pass":
            gold = False
            reasons.append(f"Gold has conflicting PASS for reviewed-absent {feature}")
    for scenario in GOLD_SCENARIOS:
        scenario_run = _latest_run(runs, "scenarios", scenario) or {}
        if not _scenario_pass(
            _result(scenario_run, "scenarios", scenario),
            scenario_run,
            revision,
            scenario,
        ):
            gold = False
            reasons.append(f"Gold requires reviewed {scenario} PASS")
    if not gold:
        return _report(
            revision,
            firmware,
            route,
            "silver",
            reasons,
            links,
            qualified,
            exclusions,
            feature_presence,
        )

    soak = _latest_outcome(runs, "scenarios", "soak_24h")
    soak_run = _latest_run(runs, "scenarios", "soak_24h") or {}
    platinum = _soak_pass(soak, soak_run, revision)
    if platinum:
        links.update(item for item in soak.get("evidence", []) if isinstance(item, str))
        tier = "platinum"
    else:
        tier = "gold"
        reasons.append(
            "Platinum requires a reviewed, uninterrupted 86,400-second soak with no resume"
        )
    return _report(
        revision,
        firmware,
        route,
        tier,
        reasons,
        links,
        qualified,
        exclusions,
        feature_presence,
    )


def _report(
    revision: dict[str, Any],
    firmware: str,
    route: str,
    tier: str,
    reasons: list[str],
    links: set[str],
    qualified: list[str],
    exclusions: list[str],
    presence: dict[str, str],
) -> dict[str, Any]:
    return {
        "tier": tier,
        "scope": {
            "model": revision.get("model"),
            "revision_id": revision.get("id"),
            "hardware_revision": revision.get("hardware_version"),
            "firmware": firmware,
            "observed_route": route,
        },
        "evidence_links": sorted(links),
        "qualified_features": sorted(set(qualified)),
        "exclusions": sorted(set(exclusions)),
        "feature_presence": presence,
        "unmet_requirements": sorted(set(reasons)) if tier != "platinum" else [],
    }


def evaluate_tiers(matrix: dict[str, Any]) -> dict[str, Any]:
    """Calculate the highest evidence-supported tier independently per exact scope."""
    results: list[dict[str, Any]] = []
    for revision in matrix.get("revisions", []):
        if not isinstance(revision, dict):
            continue
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for run in revision.get("runs", []):
            if _scope_run(run):
                grouped.setdefault((run["firmware_version"], run["route"]), []).append(
                    run
                )
        for (firmware, route), runs in sorted(grouped.items()):
            results.append(_tier_for_scope(revision, firmware, route, runs))
        if not grouped:
            results.append(
                {
                    "tier": "unqualified",
                    "scope": {
                        "model": revision.get("model"),
                        "revision_id": revision.get("id"),
                        "hardware_revision": revision.get("hardware_version"),
                        "firmware": None,
                        "observed_route": None,
                    },
                    "evidence_links": [],
                    "qualified_features": [],
                    "exclusions": [],
                    "feature_presence": {
                        feature: "unknown" for feature in ALL_FEATURES
                    },
                    "unmet_requirements": [
                        "No production run has an exact firmware and verified observed route"
                    ],
                }
            )
    results.sort(
        key=lambda item: (
            str(item["scope"]["revision_id"]),
            item["scope"]["firmware"],
            item["scope"]["observed_route"],
        )
    )
    return {"schema_version": 1, "tiers": results}
