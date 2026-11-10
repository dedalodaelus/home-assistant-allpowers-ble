"""Review and import validated public HIL archives as non-qualifying evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .api import HAAPIError
from .discovery import safe_model, safe_version
from .evidence import PUBLIC_NAMES, verify_public_archive
from scripts.validate_hil_qualification import validate_matrix

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "tests" / "hil" / "qualification_matrix.json"
SCENARIOS = (
    "golden_vector_replay",
    "reconnect_failover",
    "rapid_consecutive_writes",
    "stale_timeout_recovery",
    "soak_24h",
    "upgrade_reload",
    "vendor_app_contention",
)
CAPABILITIES = (
    "ac_output",
    "dc_output",
    "light_output",
    "eco_mode",
    "work_mode",
    "eco_timeout",
    "car_charger",
)


def _json_file(content: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeError, ValueError) as exc:
        raise HAAPIError(f"Invalid {name} in verified archive") from exc
    if not isinstance(value, dict):
        raise HAAPIError(f"Invalid {name} in verified archive")
    return value


def _verified_fact(device: Any, key: str) -> str | None:
    record = device.get(key) if isinstance(device, dict) else None
    if not isinstance(record, dict) or record.get("verified") is not True:
        return None
    value = record.get("value")
    return value if isinstance(value, str) and value else None


def _canonical_model(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.removeprefix("ALLPOWERS ")
    return candidate if safe_model(candidate) == candidate else None


def _route_and_completeness(manifest: dict[str, Any]) -> tuple[str, str]:
    captures = manifest["captures"]
    by_capture = captures["by_capture"]
    routes = {item["route"] for item in by_capture}
    route = (
        next(iter(routes)) if len(routes) == 1 else "mixed" if routes else "unverified"
    )
    if not by_capture:
        completeness = "unknown"
    else:
        completeness = "complete" if captures["complete"] else "incomplete"
    return route, completeness


def _revision_id(model: str | None, hardware: str | None, fallback_digest: str) -> str:
    if model is None or hardware is None:
        return f"unattributed-{fallback_digest[:12]}"
    model_part = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:40]
    hardware_part = re.sub(r"[^a-z0-9]+", "-", hardware.lower()).strip("-")[:24]
    return f"{model_part or 'model'}-hw-{hardware_part or 'unknown'}"


def _canonical_content_sha256(members: dict[str, bytes]) -> str:
    """Hash verified member names and contents, excluding ZIP container metadata."""
    canonical_members = [
        {
            "name": name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(members.items())
    ]
    encoded = json.dumps(
        canonical_members, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _logical_run_id(
    *,
    source_run_id: str | None,
    content_sha256: str,
    scope: dict[str, Any],
) -> str:
    identity = (
        {"source_run_id": source_run_id, "scope": scope}
        if source_run_id is not None
        else {"content_sha256": content_sha256, "scope": scope}
    )
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"archive-{hashlib.sha256(encoded).hexdigest()}"


def _load_archive(archive_path: Path) -> tuple[dict[str, Any], dict[str, bytes], str]:
    result = verify_public_archive(archive_path, include_contents=True)
    if not isinstance(result, tuple) or len(result) != 3:
        raise HAAPIError("Verified archive contents are unavailable")
    manifest, members, digest = result
    return manifest, members, digest


def _build_proposal(
    archive_path: Path,
    matrix_path: Path,
    *,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    matrix_path = matrix_path.absolute()
    try:
        matrix_path.relative_to(repo_root.absolute())
    except ValueError as exc:
        raise HAAPIError("Qualification matrix must be inside the repository") from exc
    _reject_symlink_components(matrix_path, repo_root)
    if not matrix_path.is_file():
        raise HAAPIError("Qualification matrix not found")
    try:
        matrix_bytes = matrix_path.read_bytes()
        matrix = json.loads(matrix_bytes)
    except (OSError, ValueError) as exc:
        raise HAAPIError("Unable to read qualification matrix") from exc
    if (
        not isinstance(matrix, dict)
        or matrix.get("schema_version") != 2
        or not isinstance(matrix.get("revisions"), list)
    ):
        raise HAAPIError("Qualification matrix must use schema v2")
    matrix_errors, _ = validate_matrix(
        matrix,
        matrix_path=matrix_path,
        require_pass=False,
        max_age_days=30,
    )
    if matrix_errors:
        raise HAAPIError(
            "Existing qualification matrix is invalid: " + "; ".join(matrix_errors)
        )

    manifest, members, archive_digest = _load_archive(archive_path)
    session = _json_file(members["session.json"], "session.json")["data"]
    device = session.get("device", {})
    model = _canonical_model(_verified_fact(device, "model"))
    hardware = _verified_fact(device, "hardware_version")
    if hardware is not None:
        hardware = safe_version(hardware)
    firmware = _verified_fact(device, "firmware_version")
    if firmware is not None:
        firmware = safe_version(firmware)

    route, completeness = _route_and_completeness(manifest)
    content_sha256 = _canonical_content_sha256(members)
    source_run_id = manifest.get("run_id")
    if not isinstance(source_run_id, str) or source_run_id == "unavailable":
        source_run_id = None
    revision_fallback = (
        hashlib.sha256(source_run_id.encode("utf-8")).hexdigest()
        if source_run_id is not None
        else content_sha256
    )
    revision_id = _revision_id(model, hardware, revision_fallback)
    revisions = matrix["revisions"]
    existing = next(
        (
            revision
            for revision in revisions
            if isinstance(revision, dict)
            and model is not None
            and hardware is not None
            and _canonical_model(revision.get("model")) == model
            and revision.get("hardware_version") == hardware
        ),
        None,
    )
    if existing is None:
        existing = next(
            (
                revision
                for revision in revisions
                if isinstance(revision, dict) and revision.get("id") == revision_id
            ),
            None,
        )
    if existing is not None:
        revision_id = existing["id"]
    elif any(
        isinstance(revision, dict) and revision.get("id") == revision_id
        for revision in revisions
    ):
        revision_id = f"{revision_id}-{content_sha256[:12]}"
        if any(
            isinstance(revision, dict) and revision.get("id") == revision_id
            for revision in revisions
        ):
            revision_id = f"{revision_id}-{content_sha256}"

    scope = {
        "revision_id": revision_id,
        "model": model,
        "hardware_version": hardware,
        "firmware_version": firmware,
        "route": route,
    }
    if source_run_id is not None:
        known_source = next(
            (
                (revision, item)
                for revision in revisions
                if isinstance(revision, dict)
                for item in revision.get("runs", [])
                if isinstance(item, dict) and item.get("source_run_id") == source_run_id
            ),
            None,
        )
        if known_source is not None:
            prior_revision, prior_run = known_source
            if (
                prior_revision.get("id") != revision_id
                or prior_run.get("firmware_version") != firmware
                or prior_run.get("route") != route
            ):
                raise HAAPIError(
                    "Source run identity conflicts with existing firmware or route scope"
                )
            if prior_run.get("content_sha256") != content_sha256:
                raise HAAPIError("Source run identity conflicts with existing contents")
    run_id = _logical_run_id(
        source_run_id=source_run_id,
        content_sha256=content_sha256,
        scope=scope,
    )
    run_rel_dir = Path("tests/hil/fixtures") / revision_id / "runs" / run_id
    imported_members = {
        name: content for name, content in members.items() if name in PUBLIC_NAMES
    }
    evidence_paths = [
        (run_rel_dir / name).as_posix() for name in sorted(imported_members)
    ]
    scenario_path = (run_rel_dir / "scenarios.json").as_posix()
    scenarios_data = (
        _json_file(members["scenarios.json"], "scenarios.json")
        if "scenarios.json" in members
        else None
    )
    reported_scenario = session.get("scenario_id")
    reported_steps = scenarios_data.get("steps", []) if scenarios_data else []
    reported_step_ids = sorted(
        {
            item.get("step_id")
            for item in reported_steps
            if isinstance(item, dict) and isinstance(item.get("step_id"), str)
        }
    )
    env = _json_file(members["environment.json"], "environment.json")["data"]
    integration = env.get("integration", {})
    installed_version_record = (
        integration.get("installed_manifest_version")
        if isinstance(integration, dict)
        else None
    )
    installed_version = (
        installed_version_record.get("value")
        if isinstance(installed_version_record, dict)
        else None
    )
    integration_version = (
        installed_version if isinstance(installed_version, str) else "unknown"
    )
    captured_at = session.get("started_at")
    if not isinstance(captured_at, str):
        captured_at = None

    run = {
        "run_id": run_id,
        "firmware_version": firmware,
        "route": route,
        "captured_at": captured_at,
        "tool_version": manifest["toolkit_version"],
        "integration_version": integration_version,
        "build_verification": manifest["build_provenance"]["verification"],
        "installed_commit": manifest["build_provenance"].get("installed_commit"),
        "capture_completeness": completeness,
        "qualification_status": "inconclusive",
        "evidence": evidence_paths,
        "scenarios": {
            name: {
                "status": "inconclusive",
                "evidence": [scenario_path] if scenarios_data else [],
                "rationale": "Public archive does not establish physical qualification.",
            }
            for name in SCENARIOS
        },
        "write_capabilities": {
            name: {
                "status": "inconclusive",
                "evidence": [],
                "rationale": "Public archive does not establish physical qualification.",
            }
            for name in CAPABILITIES
        },
        "archive_sha256": archive_digest,
        "content_sha256": content_sha256,
        "source_run_id": source_run_id,
        "reported_scenario_id": reported_scenario,
        "reported_step_ids": reported_step_ids,
        "capture_summary": manifest["captures"],
    }
    prior = None
    if existing is None:
        new_revision = {
            "id": revision_id,
            "model": model or "unknown",
            "hardware_version": hardware or "unknown",
            "raw_hardware_version": "unavailable",
            "privacy_review": "pass",
            "runs": [run],
            "legacy_results": [],
        }
    else:
        new_revision = json.loads(json.dumps(existing))
        if not isinstance(new_revision.get("runs"), list):
            raise HAAPIError("Target revision has invalid runs list")
        existing_runs = new_revision["runs"]
        prior = next(
            (
                item
                for item in existing_runs
                if isinstance(item, dict) and item.get("run_id") == run_id
            ),
            None,
        )
        if prior is not None:
            if prior.get("content_sha256") != content_sha256:
                raise HAAPIError("Source run identity conflicts with existing contents")
            prior_without_artifact_hash = dict(prior)
            run_without_artifact_hash = dict(run)
            prior_without_artifact_hash.pop("archive_sha256", None)
            run_without_artifact_hash.pop("archive_sha256", None)
            if prior_without_artifact_hash != run_without_artifact_hash:
                raise HAAPIError("Source run identity conflicts with existing contents")
            run = prior
        if prior is None:
            new_revision["runs"].append(run)

    if existing is None:
        proposed_revisions = [*revisions, new_revision]
    else:
        proposed_revisions = [
            new_revision if revision is existing else revision for revision in revisions
        ]
    proposal = dict(matrix)
    proposal["generated_at"] = datetime.now(UTC).date().isoformat()
    proposal["revisions"] = proposed_revisions
    destination_files = {
        (repo_root / run_rel_dir / name): content
        for name, content in imported_members.items()
    }
    missing_reasons = [
        "the verified ZIP contract records qualification_status=inconclusive",
        "none of the required physical qualification scenario verdicts is established",
        "none of the write-capability results is qualified",
    ]
    if firmware is None:
        missing_reasons.append("verified firmware version is unavailable")
    if route not in {"local_adapter", "active_proxy"}:
        missing_reasons.append("a single supported route is not verified")
    if completeness != "complete":
        missing_reasons.append(f"capture completeness is {completeness}")
    if run["build_verification"] != "verified":
        missing_reasons.append(f"installed build is {run['build_verification']}")
    if not manifest["captures"]["frames"]:
        missing_reasons.append("public archive contains no frame payloads")

    return {
        "matrix_path": matrix_path,
        "matrix_bytes": matrix_bytes,
        "matrix": proposal,
        "revision_id": revision_id,
        "model": model or "unknown",
        "hardware_version": hardware or "unknown",
        "firmware_version": firmware or "unknown",
        "route": route,
        "completeness": completeness,
        "run_id": run_id,
        "reported_scenario_id": reported_scenario,
        "reported_step_ids": reported_step_ids,
        "missing_reasons": missing_reasons,
        "run": run,
        "destination_files": destination_files,
        "existing_run": prior is not None,
        "archive_digest": archive_digest,
        "repo_root": repo_root,
    }


def _reject_symlink_components(path: Path, root: Path) -> None:
    absolute = path.absolute()
    root = root.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise HAAPIError("Import destination must stay inside the repository") from exc
    current = root
    if current.is_symlink():
        raise HAAPIError("Unsafe import destination")
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise HAAPIError("Unsafe import destination")
        except FileNotFoundError:
            continue


def _print_preview(proposal: dict[str, Any]) -> None:
    print("HIL evidence import preview")
    print(f"Profile: {proposal['model']} hardware {proposal['hardware_version']}")
    print(f"Firmware: {proposal['firmware_version']}")
    print(f"Observed route: {proposal['route']}")
    print(f"Capture completeness: {proposal['completeness']}")
    print(f"Reported guided scenario: {proposal['reported_scenario_id'] or 'none'}")
    print(
        "Reported step records: " + (", ".join(proposal["reported_step_ids"]) or "none")
    )
    print("Qualification: inconclusive; no PASS results will be created")
    print(f"Revision record: {proposal['revision_id']}")
    print(f"Run record: {proposal['run_id']}")
    print("Matrix scenario verdicts: all inconclusive; no physical result is inferred")
    print("Write-capability verdicts: all inconclusive")
    print(
        "Coverage mapping: guided scenario/step records are informational and do not "
        "satisfy the required physical scenario set"
    )
    print("Missing strict qualification evidence:")
    for reason in proposal["missing_reasons"] or [
        "Physical qualification requires independent reviewed evidence."
    ]:
        print(f"  - {reason}")
    print("Evidence files proposed:")
    for path in sorted(proposal["destination_files"]):
        print(f"  - {path.relative_to(proposal['repo_root'])}")
    print(f"Matrix: {proposal['matrix_path']}")


def _atomic_write(path: Path, content: bytes) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(
        temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o644
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _apply_proposal(proposal: dict[str, Any]) -> bool:
    if proposal["existing_run"]:
        for path, content in proposal["destination_files"].items():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise HAAPIError(f"Existing import evidence conflicts: {path}")
        print("This archive is already imported; no changes needed.")
        return False
    repo_root: Path = proposal["repo_root"]
    matrix_path: Path = proposal["matrix_path"]
    matrix_before: bytes = proposal["matrix_bytes"]
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for path, content in proposal["destination_files"].items():
            _reject_symlink_components(path, repo_root)
            missing = []
            current = path.parent
            while not current.exists():
                missing.append(current)
                current = current.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            if path.exists():
                if path.is_symlink() or path.read_bytes() != content:
                    raise HAAPIError(f"Evidence file collision: {path}")
                continue
            stream = path.open("xb")
            created_files.append(path)
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

        errors, _ = validate_matrix(
            proposal["matrix"],
            matrix_path=matrix_path,
            require_pass=False,
            max_age_days=30,
        )
        if errors:
            raise HAAPIError("Proposed matrix is invalid: " + "; ".join(errors))
        if matrix_path.is_symlink() or matrix_path.read_bytes() != matrix_before:
            raise HAAPIError("Qualification matrix changed during import")
        rendered = (
            json.dumps(proposal["matrix"], indent=2, ensure_ascii=False) + "\n"
        ).encode()
        _atomic_write(matrix_path, rendered)
    except BaseException:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    print(f"Imported inconclusive evidence to revision {proposal['revision_id']}.")
    print("No route, scenario, or write capability was marked PASS.")
    return True


def import_archive(
    archive_path: Path,
    *,
    matrix_path: Path = DEFAULT_MATRIX,
    apply: bool = False,
    repo_root: Path = ROOT,
) -> int:
    proposal = _build_proposal(archive_path, matrix_path, repo_root=repo_root)
    _print_preview(proposal)
    if not apply:
        print("Dry run only. Rerun with --apply after reviewing the proposed changes.")
        return 0
    _apply_proposal(proposal)
    return 0
