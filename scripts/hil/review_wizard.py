"""Offline interactive front end for the strict imported Bronze reviewer."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
from .api import HAAPIError
from .importer import _reject_symlink_components
from .review import CLAIMS, _claim_support, _verified_members, review_imported_run
from scripts import validate_hil_qualification as validation

_SCOPE_LABELS = {
    "verified_observed_route": "observed BLE route is not verified",
    "complete_capture": "capture is not complete",
    "verified_build": "installed build is not verified",
    "installed_checkout_match": "installed and checkout fingerprints differ",
    "valid_build_fingerprint": "build fingerprint is invalid",
    "session_matches_capture": "session timestamp does not match the capture",
    "session_id_present": "session ID is missing or unknown",
    "read_only_session": "session is not a read-only preflight",
    "runtime_available": "runtime availability is not verified",
    "runtime_connected": "runtime connection is not verified",
    "status_fresh": "runtime status is not fresh",
    "no_pending_transaction": "a runtime transaction is pending",
    "manifest_build_verified": "archive build provenance is not verified",
}
_CLAIM_LABELS = {
    "identity": "model, hardware revision, and firmware identity are not all verified",
    "protocol_probe": "the complete status request and decoded status response are not verified",
    "fresh_telemetry": "fresh, verified, plausible telemetry samples are not established",
}


def _load_run(matrix_path: Path, run_id: str, root: Path) -> tuple[dict, dict]:
    matrix_path = matrix_path.absolute()
    _reject_symlink_components(matrix_path, root)
    try:
        matrix_path.relative_to(root)
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError) as exc:
        raise HAAPIError("Unable to read in-repository qualification matrix") from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != 3:
        raise HAAPIError("Qualification matrix must use schema v3")
    errors, _ = validation.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    if errors:
        raise HAAPIError("Qualification matrix validation failed: " + "; ".join(errors))
    matches = [
        (revision, run)
        for revision in matrix.get("revisions", [])
        if isinstance(revision, dict)
        for run in revision.get("runs", [])
        if isinstance(run, dict) and run.get("run_id") == run_id
    ]
    if not run_id.startswith("archive-") or len(matches) != 1:
        raise HAAPIError("Imported archive run is absent or ambiguous")
    return matches[0]


def _prompt_claim(claim: str, support: bool, failures: list[str]) -> str:
    if not support:
        print(f"  Preflight blockers: {'; '.join(failures)}")
        print("  This claim is unavailable and will remain inconclusive.")
        return "inconclusive"
    answer = (
        input("  Independent decision [inconclusive/pass] (default inconclusive): ")
        .strip()
        .lower()
    )
    if answer in {"", "n", "no", "unknown", "inconclusive"}:
        return "inconclusive"
    if answer != "pass":
        print("  Invalid answer; keeping claim inconclusive.")
        return "inconclusive"
    if (
        input(
            "  Confirm this claim is supported by inspected physical evidence? [no/yes]: "
        )
        .strip()
        .lower()
        != "yes"
    ):
        return "inconclusive"
    return "pass"


def interactive_review(
    matrix_path: Path,
    *,
    run_id: str,
    repo_root: Path | None = None,
) -> int:
    """Collect independent decisions, dry-run them, then optionally apply."""
    root = (repo_root or Path(__file__).resolve().parents[2]).absolute()
    if not sys.stdin.isatty():
        raise HAAPIError("Interactive review requires a TTY")
    revision, run = _load_run(matrix_path, run_id, root)
    members = _verified_members(run, root)
    print(
        "Offline independent physical review (a reviewer response is not cryptographic attestation)."
    )
    print(
        f"Model / hardware / firmware: {revision.get('model')} / {revision.get('hardware_version')} / {run.get('firmware_version')}"
    )
    print(f"Observed BLE route: {run.get('route')}")
    print(f"Run ID: {run_id}")
    print(f"Archive SHA-256: {run.get('archive_sha256')}")
    print(
        f"Installed build: {run.get('build_verification')} ({run.get('installed_fingerprint')})"
    )
    print(f"Capture completeness: {run.get('capture_completeness')}")
    print("Verified public evidence files:")
    for relative in sorted(
        path for path in run.get("evidence", []) if isinstance(path, str)
    ):
        print(f"  {relative}")
    reviewer = input("Reviewer (public identifier): ").strip()
    if re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", reviewer) is None:
        raise HAAPIError(
            "Reviewer must be a nonempty public identifier of at most 80 characters"
        )
    inspected = (
        input("Independently inspected real-device evidence? [no/yes] (default no): ")
        .strip()
        .lower()
    )
    independent = inspected == "yes"
    claims: dict[str, str] = {}
    for claim in CLAIMS:
        supported, evidence, checks = _claim_support(claim, revision, run, members)
        print(
            f"{claim}: objective preflight {'supported' if supported else 'not supported'}"
        )
        print(
            f"  Evidence references checked: {', '.join(evidence) if evidence else 'none'}"
        )
        failures = [
            label
            for key, label in _SCOPE_LABELS.items()
            if checks["scope"].get(key) is False
        ]
        if checks.get(claim) is False:
            failures.append(_CLAIM_LABELS[claim])
        if independent:
            claims[claim] = _prompt_claim(claim, supported, failures)
        else:
            print(
                "  No independent physical inspection confirmed; keeping inconclusive."
            )
            claims[claim] = "inconclusive"
    payload = {
        "schema_version": 1,
        "archive_sha256": run["archive_sha256"],
        "run_id": run["run_id"],
        "reviewed_by": reviewer,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "claims": claims,
    }
    audit_dir = root / "hil-results" / "bronze-reviews"
    _reject_symlink_components(audit_dir, root)
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    audit_path = audit_dir / f"{run_id}-{stamp}.json"
    _reject_symlink_components(audit_path, root)
    try:
        with audit_path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise HAAPIError("Unable to save local review audit copy") from exc
    print(f"Review audit copy: {audit_path.relative_to(root)}")
    review_imported_run(
        matrix_path, run_id=run_id, review_file=audit_path, apply=False, repo_root=root
    )
    if (
        input(
            "Type APPLY REVIEW to update the matrix (anything else cancels): "
        ).strip()
        != "APPLY REVIEW"
    ):
        print("Review cancelled; qualification matrix unchanged.")
        return 0
    return review_imported_run(
        matrix_path, run_id=run_id, review_file=audit_path, apply=True, repo_root=root
    )
