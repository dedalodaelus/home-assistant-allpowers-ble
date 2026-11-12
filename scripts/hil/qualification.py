"""Qualification matrix schema and conservative v1 migration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MATRIX_SCHEMA_VERSION = 3


def migrate_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve v2 records verbatim as unqualified history in the v3 envelope."""
    if (
        type(payload.get("schema_version")) is int
        and payload.get("schema_version") == 3
    ):
        return deepcopy(payload)
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 2
    ):
        raise ValueError("input matrix schema_version must be 2")
    revisions = payload.get("revisions")
    if not isinstance(revisions, list):
        raise ValueError("v2 revisions must be a list")
    migrated = deepcopy(payload)
    migrated["schema_version"] = 3
    migrated["migration"] = {
        "from_schema_version": 2,
        "policy": "v2 runs retained verbatim as unqualified legacy history",
    }
    for revision in migrated["revisions"]:
        if not isinstance(revision, dict):
            raise ValueError("each v2 revision must be an object")
        revision["legacy_v2_runs"] = revision.pop("runs", [])
        revision.setdefault("runs", [])
        revision.setdefault("legacy_results", [])
    return migrated


def migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap v1 results as unscoped history; never manufacture qualified runs."""
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        raise ValueError("input matrix schema_version must be 1")
    old_revisions = payload.get("verified_revisions")
    if not isinstance(old_revisions, list):
        raise ValueError("v1 verified_revisions must be a list")

    revisions: list[dict[str, Any]] = []

    def normalized_outcomes(item: dict[str, Any]) -> dict[str, Any]:
        def status(value: Any) -> Any:
            return "not_applicable" if value == "na" else value

        routes = item.get("routes")
        scenarios = item.get("scenarios")
        capabilities = item.get("write_capabilities")
        return {
            "routes": {
                key: status(value.get("status"))
                for key, value in routes.items()
                if isinstance(routes, dict) and isinstance(value, dict)
            }
            if isinstance(routes, dict)
            else {},
            "scenarios": {key: status(value) for key, value in scenarios.items()}
            if isinstance(scenarios, dict)
            else {},
            "write_capabilities": {
                key: status(value) for key, value in capabilities.items()
            }
            if isinstance(capabilities, dict)
            else {},
        }

    for item in old_revisions:
        if not isinstance(item, dict):
            raise ValueError("each v1 revision must be an object")
        revisions.append(
            {
                "id": item.get("id"),
                "model": item.get("model"),
                "hardware_version": item.get("hardware_version"),
                "raw_hardware_version": item.get("raw_hardware_version"),
                "privacy_review": item.get("privacy_review"),
                "runs": [],
                "legacy_results": [
                    {
                        "scope": "unscoped_legacy",
                        "source_schema_version": 1,
                        "source": deepcopy(item),
                        "normalized_outcomes": normalized_outcomes(item),
                    }
                ],
            }
        )

    return {
        "schema_version": 2,
        "generated_at": payload.get("generated_at"),
        "retention": payload.get("retention"),
        "migration": {
            "from_schema_version": 1,
            "policy": "historical outcomes preserved without route or firmware attribution",
        },
        "revisions": revisions,
    }


def migrate_v1_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the deterministic v1 → v2 → v3 migration without qualifying history."""
    migrated = migrate_v2_to_v3(migrate_v1_to_v2(payload))
    migrated["migration"] = {
        "from_schema_version": 1,
        "policy": "v1 outcomes and v2 history preserved without qualification",
    }
    return migrated


def migrate_matrix_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate v1/v2 matrices to v3; v3 input is returned unchanged."""
    version = payload.get("schema_version")
    if version == 1 and type(version) is int:
        return migrate_v1_to_v3(payload)
    if version == 2 and type(version) is int:
        return migrate_v2_to_v3(payload)
    if version == 3 and type(version) is int:
        return deepcopy(payload)
    raise ValueError("matrix schema_version must be 1, 2, or 3")
