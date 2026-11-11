"""Shared fail-closed production capability checks for HIL preflights."""

from __future__ import annotations

from typing import Any


def feature_write_authorized(
    snapshot: dict[str, Any], feature: str, legacy_group: str
) -> bool:
    """Check one feature in schema v2; retain the schema-v1 group fallback."""
    if snapshot.get("schema_version") != 2:
        capabilities = snapshot.get("capabilities", {})
        return (
            isinstance(capabilities, dict)
            and capabilities.get(legacy_group) is True
            and snapshot.get("schema_version", 1) == 1
        )

    feature_key = "light_output" if feature == "light" else feature
    features = snapshot.get("features")
    facts = features.get(feature_key) if isinstance(features, dict) else None
    return (
        isinstance(facts, dict)
        and facts.get("production_authorized") is True
        and facts.get("physical_presence") != "absent"
        and facts.get("runtime_ready") is True
    )
