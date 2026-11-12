"""Pure Python policy helpers shared by the merge gate and regression tests."""

from __future__ import annotations

import json
import sys
from typing import Any

ACCEPTED_CONCLUSIONS = {"success", "neutral", "skipped"}


def latest_runs_by_name(check_runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Choose the newest run by monotonically increasing ID; check runs lack created_at."""
    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        previous = latest.get(run["name"])
        if previous is None or int(run["id"]) > int(previous["id"]):
            latest[run["name"]] = run
    return latest


def title_check_state(
    *,
    title_was_edited: bool,
    validator_passed: bool,
    run: dict[str, Any] | None,
) -> str:
    """Classify the title requirement, relying on the pinned action for edits."""
    if title_was_edited and not validator_passed:
        return "failed"
    if run is None:
        return "missing"
    if run.get("status") != "completed":
        return "pending"
    if title_was_edited:
        return "accepted"
    return "accepted" if run.get("conclusion") in ACCEPTED_CONCLUSIONS else "failed"


def main() -> None:
    payload = json.load(sys.stdin)
    runs = latest_runs_by_name(payload["check_runs"])
    state = title_check_state(
        title_was_edited=payload["title_was_edited"],
        validator_passed=True,
        run=runs.get("Validate title"),
    )
    json.dump({"latest_by_name": runs, "title_state": state}, sys.stdout)


if __name__ == "__main__":
    main()
