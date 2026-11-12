"""Behavior tests for merge-gate decisions using GitHub check-run response fields."""

from scripts.merge_gate_policy import latest_runs_by_name, title_check_state


def test_new_queued_check_wins_over_old_success_without_created_at() -> None:
    runs = latest_runs_by_name(
        [
            {
                "id": 100,
                "name": "Validate title",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 101,
                "name": "Validate title",
                "status": "queued",
                "started_at": None,
            },
        ]
    )
    chosen = runs["Validate title"]
    assert chosen["id"] == 101
    assert (
        title_check_state(title_was_edited=True, validator_passed=True, run=chosen)
        == "pending"
    )


def test_valid_to_invalid_title_fails_even_with_old_success() -> None:
    old_success = {
        "id": 100,
        "name": "Validate title",
        "status": "completed",
        "conclusion": "success",
    }
    assert (
        title_check_state(
            title_was_edited=True, validator_passed=False, run=old_success
        )
        == "failed"
    )


def test_invalid_to_valid_title_uses_authoritative_validator_result() -> None:
    old_failure = {
        "id": 100,
        "name": "Validate title",
        "status": "completed",
        "conclusion": "failure",
    }
    assert (
        title_check_state(title_was_edited=True, validator_passed=True, run=old_failure)
        == "accepted"
    )


def test_base_edit_still_enforces_the_existing_title_check_conclusion() -> None:
    success = {
        "id": 100,
        "name": "Validate title",
        "status": "completed",
        "conclusion": "success",
    }
    failure = {**success, "conclusion": "failure"}
    assert (
        title_check_state(title_was_edited=False, validator_passed=True, run=success)
        == "accepted"
    )
    assert (
        title_check_state(title_was_edited=False, validator_passed=True, run=failure)
        == "failed"
    )
