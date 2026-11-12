"""Regression checks for merge-gate title edits and GitHub check-run responses."""

from pathlib import Path
import re
import unittest


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/merge-gate.yml"
TITLE_TYPES = {
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "test",
}
TITLE_PATTERN = re.compile(r"^(\w+)(?:\(([^\r\n()]+)\))?(!)?: (.+\S)$")


def latest_check_by_id(check_runs: list[dict]) -> dict[str, dict]:
    """Model the workflow's selection using the fields present in check-run API responses."""
    latest: dict[str, dict] = {}
    for run in check_runs:
        previous = latest.get(run["name"])
        if previous is None or int(run["id"]) > int(previous["id"]):
            latest[run["name"]] = run
    return latest


def valid_title(title: str) -> bool:
    match = TITLE_PATTERN.fullmatch(title)
    return bool(match and match.group(1) in TITLE_TYPES)


class MergeGateWorkflowContractTests(unittest.TestCase):
    def test_uses_real_check_run_fields_and_handles_queued_newer_run(self) -> None:
        source = WORKFLOW.read_text()
        self.assertIn("Number(run.id) > Number(previous.id)", source)
        self.assertNotIn("run.created_at", source)
        self.assertNotIn("run.started_at", source)

        old_success = {
            "id": 100,
            "name": "Validate title",
            "status": "completed",
            "conclusion": "success",
        }
        new_queued = {
            "id": 101,
            "name": "Validate title",
            "status": "queued",
            "conclusion": None,
            "started_at": None,
        }
        chosen = latest_check_by_id([old_success, new_queued])["Validate title"]
        self.assertEqual(chosen["id"], 101)
        self.assertEqual(chosen["status"], "queued")

    def test_title_edit_validates_current_title_not_old_same_sha_result(self) -> None:
        source = WORKFLOW.read_text()
        self.assertIn("if (titleWasEdited && !currentTitleIsValid)", source)
        self.assertIn('checkName === "Validate title" && titleWasEdited', source)
        self.assertTrue(valid_title("fix: correct the gate"))
        self.assertTrue(valid_title("feat(ble)!: change a public behavior"))
        self.assertFalse(valid_title("invalid title"))
        self.assertFalse(valid_title("unknown: title type"))

    def test_base_edit_does_not_mark_title_as_edited(self) -> None:
        source = WORKFLOW.read_text()
        self.assertIn('Object.hasOwn(context.payload.changes || {}, "title")', source)
        self.assertIn("      - edited", source)


if __name__ == "__main__":
    unittest.main()
