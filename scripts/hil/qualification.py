"""Qualification matrix schema and conservative v1 migration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MATRIX_SCHEMA_VERSION = 2


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
        "schema_version": MATRIX_SCHEMA_VERSION,
        "generated_at": payload.get("generated_at"),
        "retention": payload.get("retention"),
        "migration": {
            "from_schema_version": 1,
            "policy": "historical outcomes preserved without route or firmware attribution",
        },
        "revisions": revisions,
    }
