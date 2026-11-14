"""Independently review privacy-safe imported Bronze evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any

from .api import HAAPIError
from .evidence import _bronze_samples_fresh
from .importer import (
    _atomic_write,
    _canonical_content_sha256,
    _reject_symlink_components,
)
from scripts import validate_hil_qualification as validation

ROOT = Path(__file__).resolve().parents[2]
CLAIMS = ("identity", "protocol_probe", "fresh_telemetry")
STATUS_REQUEST_HEX = validation.STATUS_REQUEST.hex()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HAAPIError("Unable to read imported review evidence") from exc
    if not isinstance(value, dict):
        raise HAAPIError("Invalid imported review evidence")
    return value


def _evidence(run: dict[str, Any], root: Path, name: str) -> tuple[str, Path]:
    refs = run.get("evidence")
    matches = [
        item for item in refs or [] if isinstance(item, str) and Path(item).name == name
    ]
    if len(matches) != 1:
        raise HAAPIError(f"Imported run must contain one {name}")
    relative = matches[0]
    target = root / relative
    _reject_symlink_components(target, root)
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise HAAPIError("Imported evidence path escapes repository") from exc
    if not stat.S_ISREG(resolved.lstat().st_mode):
        raise HAAPIError("Imported evidence is not a regular file")
    return relative, resolved


def _verified_members(run: dict[str, Any], root: Path) -> dict[str, bytes]:
    _, manifest_path = _evidence(run, root, "manifest.json")
    manifest = _json(manifest_path)
    records = manifest.get("files")
    if not isinstance(records, list):
        raise HAAPIError("Imported manifest has no file list")
    members: dict[str, bytes] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "size", "sha256"}
            or not isinstance(record.get("name"), str)
            or Path(record["name"]).name != record["name"]
            or type(record.get("size")) is not int
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", record["sha256"]) is None
        ):
            raise HAAPIError("Invalid imported manifest entry")
        _, path = _evidence(run, root, record["name"])
        content = path.read_bytes()
        if (
            len(content) != record["size"]
            or hashlib.sha256(content).hexdigest() != record["sha256"]
        ):
            raise HAAPIError("Imported evidence digest mismatch")
        members[record["name"]] = content
    members["manifest.json"] = manifest_path.read_bytes()
    if _canonical_content_sha256(members) != run.get("content_sha256"):
        raise HAAPIError("Imported archive content binding mismatch")
    return members


def _review_file(path: Path, run: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    value = _json(path)
    if (
        set(value)
        != {
            "schema_version",
            "archive_sha256",
            "run_id",
            "reviewed_by",
            "reviewed_at",
            "claims",
        }
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("archive_sha256") != run.get("archive_sha256")
        or not isinstance(value.get("archive_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", value["archive_sha256"]) is None
        or value.get("run_id") != run.get("run_id")
        or not isinstance(value.get("reviewed_by"), str)
        or re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", value["reviewed_by"].strip()) is None
        or not isinstance(value.get("reviewed_at"), str)
        or not isinstance(value.get("claims"), dict)
        or set(value["claims"]) != set(CLAIMS)
        or any(
            not isinstance(value["claims"].get(key), str)
            or value["claims"][key] not in {"pass", "inconclusive"}
            for key in CLAIMS
        )
    ):
        raise HAAPIError("Review file does not match this imported run")
    try:
        timestamp = datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00"))
        captured = datetime.fromisoformat(run["captured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise HAAPIError("Review timestamp is invalid") from exc
    if (
        timestamp.tzinfo is None
        or captured.tzinfo is None
        or timestamp.astimezone(UTC) < captured.astimezone(UTC)
        or timestamp.astimezone(UTC) > datetime.now(UTC)
    ):
        raise HAAPIError("Review timestamp must be timezone-aware and current")
    return value["reviewed_by"].strip(), timestamp.isoformat(), value["claims"]


def _rows(content: bytes) -> list[dict[str, Any]]:
    result = []
    for line in content.splitlines():
        if not line:
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, ValueError) as exc:
            raise HAAPIError("Imported GATT evidence is invalid") from exc
        if not isinstance(value, dict):
            raise HAAPIError("Imported GATT evidence is invalid")
        result.append(value)
    return result


def _claim_support(
    claim: str,
    revision: dict[str, Any],
    run: dict[str, Any],
    members: dict[str, bytes],
) -> tuple[bool, list[str], dict[str, Any]]:
    refs = run.get("evidence", [])
    bronze = json.loads(members.get("tier-bronze-read-only.json", b"{}"))
    session = json.loads(members.get("session.json", b"{}"))
    session = session.get("data", {})
    device = session.get("device", {})
    runtime = session.get("runtime_capabilities", {})
    manifest = json.loads(members["manifest.json"])
    build = manifest.get("build_provenance", {})
    supported_scope = bool(
        run.get("route") in validation.VERIFIED_ROUTES
        and run.get("capture_completeness") == "complete"
        and run.get("build_verification") == "verified"
        and run.get("installed_fingerprint") == run.get("checkout_fingerprint")
        and isinstance(run.get("build_fingerprint"), str)
        and re.fullmatch(r"[a-f0-9]{64}", run["build_fingerprint"])
        and run.get("captured_at") == session.get("started_at")
        and run.get("session_id") not in {None, "", "unknown"}
        and session.get("mode") == "read_only_preflight"
        and isinstance(runtime, dict)
        and runtime.get("available") is True
        and runtime.get("connected") is True
        and runtime.get("status_fresh") is True
        and runtime.get("pending_transaction") is False
        and build.get("verification") == "verified"
    )
    identity = bool(
        isinstance(device, dict)
        and device.get("model", {}).get("verified") is True
        and device.get("model", {}).get("value")
        == revision.get("model", "").removeprefix("ALLPOWERS ")
        and device.get("hardware_version", {}).get("verified") is True
        and device.get("hardware_version", {}).get("value")
        == revision.get("hardware_version")
        and device.get("firmware_version", {}).get("verified") is True
        and device.get("firmware_version", {}).get("value")
        == run.get("firmware_version")
    )
    samples = bronze.get("samples", [])
    telemetry = bool(
        bronze.get("fresh_verified_samples") is True
        and bronze.get("physical_observation") == "plausible"
        and _bronze_samples_fresh(samples)
    )
    protocol = False
    protocol_evidence: list[str] = []
    capture_id = bronze.get("protocol_capture_id")
    generation = bronze.get("protocol_session_generation")
    status_seq = bronze.get("protocol_status_request_seq")
    write_ok_seq = bronze.get("protocol_status_write_ok_seq")
    decoded_seq = bronze.get("protocol_decoded_response_seq")
    if (
        bronze.get("protocol_probe") == "verified"
        and bronze.get("protocol_status_request_hex") == STATUS_REQUEST_HEX
        and bronze.get("protocol_route") == run.get("route")
        and bronze.get("qualification_status") == "inconclusive"
        and isinstance(capture_id, str)
        and type(generation) is int
        and generation > 0
        and type(status_seq) is int
        and type(write_ok_seq) is int
        and type(decoded_seq) is int
    ):
        captures = manifest.get("captures", {}).get("by_capture", [])
        capture = next(
            (item for item in captures if item.get("capture_id") == capture_id), None
        )
        events = _rows(members.get("events.jsonl", b""))
        frames = _rows(members.get("frames.jsonl", b""))
        status = next(
            (
                row
                for row in events
                if row.get("capture_id") == capture_id
                and row.get("seq") == status_seq
                and row.get("event") == "write_attempt"
            ),
            None,
        )
        write_ok = next(
            (
                row
                for row in events
                if row.get("capture_id") == capture_id
                and row.get("seq") == write_ok_seq
                and row.get("event") == "write_ok"
            ),
            None,
        )
        decoded = next(
            (
                row
                for row in events
                if row.get("capture_id") == capture_id
                and row.get("seq") == decoded_seq
                and row.get("event") == "status_data_decoded"
            ),
            None,
        )
        frame = next(
            (
                row
                for row in frames
                if row.get("capture_id") == capture_id and row.get("seq") == status_seq
            ),
            None,
        )
        protocol = bool(
            supported_scope
            and capture
            and capture.get("complete") is True
            and capture.get("route") == run.get("route")
            and status
            and write_ok
            and decoded
            and status_seq < write_ok_seq
            and status_seq < decoded_seq
            and frame
            and frame.get("direction") == "tx"
            and frame.get("command_type") == "status_request"
            and frame.get("frame_validation") == "validated_tx"
            and status.get("session_generation") == decoded.get("session_generation")
            and status.get("session_generation") == write_ok.get("session_generation")
            and status.get("observed_route") == run.get("route")
            and write_ok.get("observed_route") == run.get("route")
            and decoded.get("observed_route") == run.get("route")
        )
        if protocol:
            protocol_evidence = [
                item
                for item in refs
                if isinstance(item, str)
                and Path(item).name in {"events.jsonl", "frames.jsonl"}
            ]
    support = supported_scope and (
        identity
        if claim == "identity"
        else protocol
        if claim == "protocol_probe"
        else telemetry
    )
    evidence = [
        item
        for item in refs
        if isinstance(item, str) and Path(item).name == "tier-bronze-read-only.json"
    ]
    if claim == "protocol_probe":
        evidence.extend(protocol_evidence)
    return (
        support,
        evidence,
        {
            "identity": identity,
            "protocol_probe": protocol,
            "fresh_telemetry": telemetry,
        },
    )


def _evidence_digests(
    paths: list[str], files: dict[Path, bytes], root: Path
) -> dict[str, str]:
    result = {}
    for relative in paths:
        path = root / relative
        content = files.get(path)
        if content is None:
            content = path.read_bytes()
        result[relative] = hashlib.sha256(content).hexdigest()
    return result


def review_imported_run(
    matrix_path: Path,
    *,
    run_id: str,
    review_file: Path,
    apply: bool = False,
    repo_root: Path = ROOT,
) -> int:
    """Preview/apply digest-bound Bronze claims for a previously imported archive."""
    root = repo_root.absolute()
    matrix_path = matrix_path.absolute()
    _reject_symlink_components(matrix_path, root)
    try:
        matrix_path.relative_to(root)
        before = matrix_path.read_bytes()
        matrix = json.loads(before)
    except (ValueError, OSError, UnicodeError) as exc:
        raise HAAPIError("Unable to read in-repository qualification matrix") from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != 3:
        raise HAAPIError("Qualification matrix must use schema v3")
    errors, _ = validation.validate_matrix(
        matrix, matrix_path=matrix_path, require_pass=False, max_age_days=None
    )
    if errors:
        raise HAAPIError("Qualification matrix validation failed: " + "; ".join(errors))
    found = next(
        (
            (revision, run)
            for revision in matrix.get("revisions", [])
            if isinstance(revision, dict)
            for run in revision.get("runs", [])
            if isinstance(run, dict) and run.get("run_id") == run_id
        ),
        None,
    )
    if found is None or not run_id.startswith("archive-"):
        raise HAAPIError("Imported archive run was not found")
    revision, run = found
    reviewer, reviewed_at, decisions = _review_file(review_file, run)
    members = _verified_members(run, root)
    files: dict[Path, bytes] = {}
    updated = json.loads(json.dumps(run))
    updated["authorization_context"] = "read_only"
    updated["authorization_context_source"] = "verified_read_only_session"
    updated["qualification_status"] = "pending"
    updated["bronze_evidence"] = {}
    run_rel = Path("tests/hil/fixtures") / revision["id"] / "runs" / run_id
    for claim in CLAIMS:
        supported, evidence, _checks = _claim_support(claim, revision, run, members)
        passed = decisions[claim] == "pass" and supported
        item: dict[str, Any] = {"status": "inconclusive", "evidence": []}
        if passed:
            if claim == "protocol_probe":
                capture_rel = (run_rel / "bronze-protocol-capture.jsonl").as_posix()
                bronze = json.loads(members["tier-bronze-read-only.json"])
                capture_id = bronze["protocol_capture_id"]
                status_seq = bronze["protocol_status_request_seq"]
                write_ok_seq = bronze["protocol_status_write_ok_seq"]
                decoded_seq = bronze["protocol_decoded_response_seq"]
                generation = bronze["protocol_session_generation"]
                scope = {
                    "revision_id": revision["id"],
                    "model": revision["model"],
                    "hardware_revision": revision["hardware_version"],
                    "firmware_version": updated["firmware_version"],
                    "route": updated["route"],
                    "run_id": updated["run_id"],
                    "captured_at": updated["captured_at"],
                    "session_id": updated["session_id"],
                    "build_fingerprint": updated["build_fingerprint"],
                }
                capture_rows = [
                    {
                        **scope,
                        "capture_id": capture_id,
                        "session_generation": generation,
                        "seq": status_seq,
                        "scenario": "read_only_snapshot",
                        "operation_id": "read_only_snapshot",
                        "event": "write_attempt",
                        "direction": "write",
                        "frame_hex": STATUS_REQUEST_HEX,
                        "verdict": "ok",
                    },
                    {
                        **scope,
                        "capture_id": capture_id,
                        "session_generation": generation,
                        "seq": write_ok_seq,
                        "scenario": "read_only_snapshot",
                        "operation_id": "read_only_snapshot",
                        "event": "write_ok",
                        "direction": "write",
                        "verdict": "ok",
                    },
                    {
                        **scope,
                        "capture_id": capture_id,
                        "session_generation": generation,
                        "seq": decoded_seq,
                        "scenario": "read_only_snapshot",
                        "operation_id": "read_only_snapshot",
                        "event": "status_data_decoded",
                        "direction": "notify",
                        "verdict": "ok",
                    },
                ]
                capture_rows.sort(key=lambda row: row["seq"])
                files[root / capture_rel] = (
                    "".join(
                        json.dumps(row, sort_keys=True) + "\n" for row in capture_rows
                    )
                ).encode()
                evidence.append(capture_rel)
            artifact_rel = (run_rel / f"bronze-{claim}-claim.json").as_posix()
            artifact: dict[str, Any] = {
                "artifact_type": "bronze_evidence",
                "claim": claim,
                "revision_id": revision["id"],
                "model": revision["model"],
                "hardware_revision": revision["hardware_version"],
                "firmware_version": updated["firmware_version"],
                "route": updated["route"],
                "run_id": updated["run_id"],
                "session_id": updated["session_id"],
                "build_fingerprint": updated["build_fingerprint"],
                "captured_at": updated["captured_at"],
            }
            if claim == "identity":
                artifact["identity_confirmed"] = True
            elif claim == "protocol_probe":
                artifact["capture_evidence"] = capture_rel
            else:
                samples = json.loads(members["tier-bronze-read-only.json"])["samples"]
                ages = [
                    record["sample_age_seconds"]
                    for sample in samples
                    for key, record in sample["telemetry"].items()
                    if key
                    in {"battery", "input_power", "output_power", "remaining_time"}
                    and record.get("verified") is True
                    and type(record.get("sample_age_seconds")) is int
                ]
                artifact.update(
                    fresh=True, plausible=True, sample_age_seconds=max(ages)
                )
            files[root / artifact_rel] = (
                json.dumps(artifact, sort_keys=True, indent=2) + "\n"
            ).encode()
            evidence.append(artifact_rel)
            item = {
                "status": "pass",
                "evidence": evidence,
                "claim_artifact": artifact_rel,
                "physical_review": {
                    "outcome": "confirmed_pass",
                    "reviewed_by": reviewer,
                    "reviewed_at": reviewed_at,
                },
            }
            if claim == "protocol_probe":
                item["capture_evidence"] = capture_rel
        else:
            item["rationale"] = (
                "Review declined or imported machine evidence did not satisfy the Bronze claim contract."
            )
        updated["bronze_evidence"][claim] = item
        if passed:
            for relative in evidence:
                if relative not in updated["evidence"]:
                    updated["evidence"].append(relative)
    for item in updated["bronze_evidence"].values():
        if item.get("status") == "pass":
            item["physical_review"]["evidence_sha256"] = _evidence_digests(
                item["evidence"], files, root
            )
    new_revision = json.loads(json.dumps(revision))
    new_revision["runs"] = [
        updated if item.get("run_id") == run_id else item for item in revision["runs"]
    ]
    proposed = dict(matrix)
    proposed["generated_at"] = datetime.now(UTC).date().isoformat()
    proposed["revisions"] = [
        new_revision if item is revision else item for item in matrix["revisions"]
    ]
    statuses = {
        claim: item["status"] for claim, item in updated["bronze_evidence"].items()
    }
    summary = {
        "run_id": run_id,
        "reviewed_by": reviewer,
        "bronze_claims": statuses,
        "applied": apply,
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    if not apply:
        return 0
    from .tiers import evaluate_tiers

    created: list[Path] = []
    try:
        for path, content in files.items():
            _reject_symlink_components(path, root)
            if path.exists():
                if path.is_symlink() or path.read_bytes() != content:
                    raise HAAPIError("Review evidence path conflicts")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content)
            created.append(path)
        errors, _ = validation.validate_matrix(
            proposed, matrix_path=matrix_path, require_pass=False, max_age_days=None
        )
        if errors:
            raise HAAPIError("Reviewed matrix failed validation: " + "; ".join(errors))
        tier_result = evaluate_tiers(proposed)
        if matrix_path.is_symlink() or matrix_path.read_bytes() != before:
            raise HAAPIError("Qualification matrix changed during review")
        _atomic_write(
            matrix_path,
            (json.dumps(proposed, indent=2, ensure_ascii=False) + "\n").encode(),
        )
        print(json.dumps(tier_result, sort_keys=True, indent=2))
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return 0
