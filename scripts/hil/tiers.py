"""Deterministic, evidence-only hardware support tier evaluation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scripts.validate_hil_qualification import VERIFIED_ROUTES

BASIC_OUTPUTS = ("ac_output", "dc_output", "light_output")
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
    return (
        isinstance(run, dict)
        and run.get("route") in VERIFIED_ROUTES
        and isinstance(run.get("firmware_version"), str)
        and bool(run["firmware_version"].strip())
        and run.get("authorization_context") == "production"
        and run.get("qualification_status") != "not_applicable"
    )


def _result(run: dict[str, Any], section: str, key: str) -> dict[str, Any]:
    values = run.get(section)
    item = values.get(key) if isinstance(values, dict) else None
    return item if isinstance(item, dict) else {}


def _latest_outcome(
    runs: list[dict[str, Any]], section: str, key: str
) -> dict[str, Any]:
    candidates = [(_stamp(run), _result(run, section, key)) for run in runs]
    candidates = [
        (stamp, item)
        for stamp, item in candidates
        if (
            item.get("status") == "pass"
            and _pass(item)
            or item.get("status") == "fail"
            and _reviewed_fail(item)
        )
    ]
    return (
        max(
            candidates,
            key=lambda pair: (pair[0], pair[1].get("status") == "fail"),
        )[1]
        if candidates
        else {}
    )


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
        if (
            not item.get("reviewed_by")
            or reviewed.tzinfo is None
            or not isinstance(evidence, list)
            or not evidence
            or not isinstance(digests, dict)
            or not set(evidence).issubset(digests)
        ):
            return "unknown"
    return status


def _proof(run: dict[str, Any], name: str) -> dict[str, Any]:
    proof = run.get("bronze_evidence")
    item = proof.get(name) if isinstance(proof, dict) else None
    return item if isinstance(item, dict) else {}


def _contract_excluded(runs: list[dict[str, Any]], feature: str) -> bool:
    for run in sorted(runs, key=_stamp, reverse=True):
        exclusions = run.get("contract_exclusions")
        item = exclusions.get(feature) if isinstance(exclusions, dict) else None
        if not isinstance(item, dict):
            continue
        if (
            item.get("documented_unavailable") is True
            and item.get("safely_disabled") is True
            and isinstance(item.get("rationale"), str)
            and item["rationale"].strip()
            and _pass({"status": "pass", **item})
        ):
            return True
    return False


def _pass(item: dict[str, Any]) -> bool:
    review = item.get("physical_review")
    return (
        item.get("status") == "pass"
        and isinstance(item.get("evidence"), list)
        and bool(item["evidence"])
        and isinstance(review, dict)
        and review.get("outcome") == "confirmed_pass"
        and isinstance(review.get("reviewed_by"), str)
        and bool(review["reviewed_by"].strip())
        and isinstance(review.get("evidence_sha256"), dict)
        and set(item["evidence"]).issubset(review["evidence_sha256"])
    )


def _reviewed_fail(item: dict[str, Any]) -> bool:
    review = item.get("physical_review")
    return (
        item.get("status") == "fail"
        and isinstance(item.get("evidence"), list)
        and bool(item["evidence"])
        and isinstance(review, dict)
        and review.get("outcome") == "confirmed_fail"
        and isinstance(review.get("reviewed_by"), str)
        and bool(review["reviewed_by"].strip())
        and isinstance(review.get("evidence_sha256"), dict)
        and set(item["evidence"]).issubset(review["evidence_sha256"])
    )


def _output_pass(item: dict[str, Any]) -> bool:
    review = item.get("physical_review")
    return (
        _pass(item)
        and isinstance(review.get("verified_states"), list)
        and {"on", "off"}.issubset(review["verified_states"])
    )


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
                _proof(run, requirement)
                for run in sorted(runs, key=_stamp, reverse=True)
                if _pass(_proof(run, requirement))
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
        result = latest_features[feature]
        links.update(
            item for item in result.get("evidence", []) if isinstance(item, str)
        )
        if _output_pass(result) if feature in BASIC_OUTPUTS else _pass(result):
            qualified.append(feature)
        if feature_presence[feature] == "absent":
            exclusions.append(feature)
        elif (
            feature in ALL_FEATURES[3:]
            and feature_presence[feature] == "unknown"
            and _contract_excluded(runs, feature)
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

    silver = _pass(_latest_outcome(runs, "scenarios", "reconnect_failover"))
    applicable_basic = [f for f in BASIC_OUTPUTS if feature_presence[f] != "absent"]
    passed_basic = [f for f in applicable_basic if _output_pass(latest_features[f])]
    for feature in BASIC_OUTPUTS:
        state = feature_presence[feature]
        outcome = latest_features[feature].get("status")
        if state == "unknown":
            silver = False
            reasons.append(
                f"Silver requires reviewed present/absent inventory for {feature}"
            )
        elif state == "present" and not (
            _output_pass(latest_features[feature])
            if feature in BASIC_OUTPUTS
            else _pass(latest_features[feature])
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
    if not _pass(_latest_outcome(runs, "scenarios", "reconnect_failover")):
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
            and _contract_excluded(runs, feature)
        ):
            continue
        if state == "unknown":
            gold = False
            reasons.append(
                f"Gold requires declared present/absent inventory for {feature}"
            )
        elif state == "present" and not (
            _output_pass(latest_features[feature])
            if feature in BASIC_OUTPUTS
            else _pass(latest_features[feature])
        ):
            gold = False
            reasons.append(
                f"Gold requires a reviewed PASS for declared {feature}; latest result is {result or 'missing'}"
            )
        elif state == "absent" and result == "pass":
            gold = False
            reasons.append(f"Gold has conflicting PASS for reviewed-absent {feature}")
    for scenario in GOLD_SCENARIOS:
        if not _pass(_latest_outcome(runs, "scenarios", scenario)):
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
    details = soak.get("details") if isinstance(soak.get("details"), dict) else {}
    duration = details.get("continuous_seconds", details.get("measured_seconds"))
    platinum = (
        _pass(soak)
        and isinstance(duration, (int, float))
        and duration >= 86400
        and details.get("resumed") is False
        and details.get("interrupted") is False
        and details.get("uninterrupted") is True
    )
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
