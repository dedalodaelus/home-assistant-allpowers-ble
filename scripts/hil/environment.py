"""Public, allowlisted environment and read-only sample collection."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import hashlib
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
from typing import Any

from . import SCHEMA_VERSION
from .api import HAAPIError, HAClient
from .discovery import BINARY_KEYS, SENSOR_KEYS, Device, safe_version

ROOT = Path(__file__).resolve().parents[2]
VALUE_LIMIT = 200
MAX_INTEGRATION_FILES = 256
MAX_INTEGRATION_ENTRIES = 1024
MAX_INTEGRATION_FILE_BYTES = 2 * 1024 * 1024
MAX_INTEGRATION_TOTAL_BYTES = 8 * 1024 * 1024


def observed(value: str | int | float | bool, source: str) -> dict[str, Any]:
    """Record data and its acquisition source, not an ungrounded claim."""
    return {"value": value, "source": source, "verified": True}


def unknown(reason: str) -> dict[str, Any]:
    """Mark an inaccessible value explicitly, preserving its reason."""
    return {"value": None, "source": "unavailable", "verified": False, "reason": reason}


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_label(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.+ -]{1,80}", value):
        return value
    return None


def local_commit() -> dict[str, Any]:
    """An optional *runner checkout* SHA is never an installed-build proof."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return unknown("checkout Git commit unavailable")
    sha = result.stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        return unknown("checkout Git commit unavailable")
    return observed(sha, "runner_git_checkout")


def local_manifest_version() -> str | None:
    try:
        value = json.loads(
            (ROOT / "custom_components/allpowers_ble/manifest.json").read_text(
                encoding="utf-8"
            )
        ).get("version")
    except OSError, ValueError, AttributeError:
        return None
    return _safe_label(value)


def _scan_local_tree(
    root: Path,
) -> tuple[
    dict[Path, tuple[int, ...]], dict[Path, tuple[tuple[str, tuple[int, ...]], ...]]
]:
    """Build a bounded subtree snapshot and reject symlinks or special files."""
    if root.is_symlink() or not stat.S_ISDIR(root.lstat().st_mode):
        raise OSError("invalid integration directory")
    root = root.resolve(strict=True)
    pending = [root]
    files: dict[Path, tuple[int, ...]] = {}
    directories: dict[Path, tuple[tuple[str, tuple[int, ...]], ...]] = {}
    entries_seen = 0
    total_bytes = 0
    while pending:
        current = pending.pop()
        if current.is_symlink() or not current.resolve(strict=True).is_relative_to(
            root
        ):
            raise OSError("integration path escaped root")
        snapshot: list[tuple[str, tuple[int, ...]]] = []
        child_directories: list[Path] = []
        with os.scandir(current) as iterator:
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_INTEGRATION_ENTRIES:
                    raise OSError("too many integration entries")
                metadata = entry.stat(follow_symlinks=False)
                identity = _file_identity(metadata)
                if stat.S_ISLNK(metadata.st_mode):
                    raise OSError("symlink in integration")
                if stat.S_ISDIR(metadata.st_mode):
                    if entry.name == "__pycache__":
                        continue
                    snapshot.append((entry.name, identity))
                    child_directories.append(current / entry.name)
                elif entry.name.endswith(".pyc"):
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError("invalid generated integration file")
                else:
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError("invalid integration file")
                    if metadata.st_size > MAX_INTEGRATION_FILE_BYTES:
                        raise OSError("integration file exceeds size limit")
                    total_bytes += metadata.st_size
                    if total_bytes > MAX_INTEGRATION_TOTAL_BYTES:
                        raise OSError("integration exceeds total size limit")
                    files[current / entry.name] = identity
                    if len(files) > MAX_INTEGRATION_FILES:
                        raise OSError("too many integration files")
                    snapshot.append((entry.name, identity))
        directories[current] = tuple(sorted(snapshot))
        pending.extend(reversed(sorted(child_directories)))
    if not files:
        raise OSError("empty integration")
    return files, directories


def _local_integration_snapshot() -> tuple[str, list[Path]] | None:
    """Return one stable bounded fingerprint and its exact included file set."""
    root = ROOT / "custom_components/allpowers_ble"
    try:
        files, directories = _scan_local_tree(root)
        digest = hashlib.sha256()
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            expected = files[path]
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                before = _file_identity(os.fstat(stream.fileno()))
                if before != expected:
                    return None
                content = stream.read(MAX_INTEGRATION_FILE_BYTES + 1)
                after = _file_identity(os.fstat(stream.fileno()))
            if (
                len(content) != expected[3]
                or after != before
                or _file_identity(path.lstat()) != expected
            ):
                return None
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        current_files, current_directories = _scan_local_tree(root)
        if current_files != files or current_directories != directories:
            return None
        return digest.hexdigest(), sorted(
            files, key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError, ValueError:
        return None


def local_integration_fingerprint() -> str | None:
    """Hash packaged files using the same path/content stream as HA."""
    snapshot = _local_integration_snapshot()
    return snapshot[0] if snapshot else None


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _checkout_clean(paths: list[Path]) -> bool:
    try:
        tracked = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "ls-files",
                "--",
                "custom_components/allpowers_ble",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=True,
        )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "status",
                "--porcelain",
                "--",
                "custom_components/allpowers_ble",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return False
    expected = {path.relative_to(ROOT).as_posix() for path in paths}
    return not result.stdout.strip() and set(tracked.stdout.splitlines()) == expected


def integration_build(
    manifest: dict[str, Any] | None, remote_fingerprint: str | None = None
) -> dict[str, Any]:
    """Establish installed identity from file bytes and a clean Git checkout."""
    version = _safe_label(manifest.get("version")) if manifest else None
    checkout_version = local_manifest_version()
    local_snapshot = _local_integration_snapshot()
    local_fingerprint = local_snapshot[0] if local_snapshot else None
    local_paths = local_snapshot[1] if local_snapshot else []
    clean = _checkout_clean(local_paths)
    checkout_commit = local_commit()
    commit_sha = checkout_commit.get("value")
    commit_valid = (
        checkout_commit.get("verified") is True
        and isinstance(commit_sha, str)
        and re.fullmatch(r"[a-f0-9]{40}", commit_sha) is not None
    )
    remote_valid = (
        isinstance(remote_fingerprint, str)
        and re.fullmatch(r"[a-f0-9]{64}", remote_fingerprint) is not None
    )
    same = bool(
        local_fingerprint and remote_valid and remote_fingerprint == local_fingerprint
    )
    if local_fingerprint and remote_valid and remote_fingerprint != local_fingerprint:
        match = "mismatch"
        reason = "installed integration files differ from checkout"
    elif not remote_valid:
        match = "unverified"
        reason = "installed fingerprint endpoint unavailable or invalid"
    elif not local_fingerprint:
        match = "unverified"
        reason = "checkout integration files could not be scanned"
    elif not clean:
        match = "unverified"
        reason = "checkout subtree is dirty or Git attribution is unavailable"
    elif not commit_valid:
        match = "unverified"
        reason = "checkout Git commit unavailable or invalid"
    elif same:
        match = "verified"
        reason = None
    else:
        match = "unverified"
        reason = "installed integration fingerprint could not be compared"
    commit = checkout_commit if match == "verified" else unknown(reason or "unverified")
    return {
        "installed_manifest_version": (
            observed(version, "ha_ws_manifest_get")
            if version
            else unknown("manifest/get absent, unsupported, or unauthorized")
        ),
        "installed_commit": commit,
        "runner_checkout_commit": checkout_commit,
        "runner_checkout_manifest_version": (
            observed(checkout_version, "runner_repository_manifest")
            if checkout_version
            else unknown("runner manifest unavailable")
        ),
        "build_match": match,
        "installed_fingerprint": observed(remote_fingerprint, "ha_ws_hil_build")
        if remote_fingerprint
        else unknown(reason),
        "checkout_fingerprint": observed(local_fingerprint, "runner_integration_files")
        if local_fingerprint
        else unknown(reason),
        "checkout_clean": clean,
        "verification_reason": reason,
        "qualification_allowed": False,
        "qualification_blocker": reason if match != "verified" else None,
    }


def _linux_memory_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        if isinstance(page_size, int) and isinstance(pages, int):
            return page_size * pages
    except ValueError, OSError, AttributeError:
        pass
    return None


def runner_host() -> dict[str, Any]:
    """Describe the CLI computer, never misidentify it as the HA host."""
    system = _safe_label(platform.system())
    machine = _safe_label(platform.machine())
    kernel = _safe_label(platform.release())
    cpu = _safe_label(platform.processor())
    ram = _linux_memory_bytes()
    return {
        "os": (
            observed(system, "runner_platform") if system else unknown("not provided")
        ),
        "architecture": (
            observed(machine, "runner_platform") if machine else unknown("not provided")
        ),
        "kernel": (
            observed(kernel, "runner_platform") if kernel else unknown("not provided")
        ),
        "cpu": observed(cpu, "runner_platform") if cpu else unknown("not provided"),
        "memory_bytes": (
            observed(ram, "runner_os") if ram is not None else unknown("not available")
        ),
        "python_version": observed(platform.python_version(), "runner_runtime"),
        "container_detected": observed(
            Path("/.dockerenv").exists(), "runner_local_probe"
        ),
    }


def build_environment(
    ha_config: dict[str, Any],
    manifest: dict[str, Any] | None,
    scanner_candidates: dict[str, int] | None = None,
    remote_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Allowlist fields: never serialize the raw HA configuration/registry."""
    version = _safe_label(ha_config.get("version"))
    installation = _safe_label(ha_config.get("installation_type"))
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": now_utc(),
        "home_assistant": {
            "version": (
                observed(version, "ha_rest_config")
                if version
                else unknown("missing version")
            ),
            "installation_type": (
                observed(installation, "ha_rest_config")
                if installation
                else unknown("not exposed by standard /api/config")
            ),
            "host": {
                "os": unknown("requires an authorized HA host collector"),
                "kernel": unknown("requires an authorized HA host collector"),
                "cpu": unknown("requires an authorized HA host collector"),
                "memory_bytes": unknown("requires an authorized HA host collector"),
                "container_runtime": unknown(
                    "requires an authorized HA host collector"
                ),
                "bluez_version": unknown("requires an authorized HA host collector"),
                "python_version": unknown("not exposed by standard HA API"),
            },
            "bluetooth_adapters": unknown(
                "adapter model, firmware and physical location not exposed here"
            ),
            "bluetooth_scanner_candidates": (
                {
                    "value": scanner_candidates,
                    "source": "ha_ws_bluetooth_subscribe_scanner_details_admin",
                    "verified": True,
                }
                if scanner_candidates is not None
                else unknown(
                    "admin-only Bluetooth scanner inventory unavailable, "
                    "denied or no initial event received"
                )
            ),
            "esphome_active_proxy": unknown(
                "remote scanners do not prove ESPHome identity or GATT capability"
            ),
        },
        "runner_host": runner_host(),
        "integration": integration_build(manifest, remote_fingerprint),
    }


def check_ha_version(value: Any) -> None:
    """Reject older/unknown HA versions instead of assuming API compatibility."""
    if not isinstance(value, str):
        raise HAAPIError("Unable to verify Home Assistant version")
    match = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d+)(?:[a-z0-9.-]+)?", value)
    if match is None or (int(match[1]), int(match[2])) < (2026, 8):
        raise HAAPIError(
            "Unsupported or unrecognized Home Assistant version (need 2026.8+)"
        )


def safe_state_value(key: str, value: Any) -> str | int | float | None:
    """Never export arbitrary state or attributes from a possibly untrusted device."""
    if not isinstance(value, str) or value in {"unknown", "unavailable"}:
        return None
    if key in BINARY_KEYS:
        return value if value in {"on", "off"} else None
    if key in {"hardware_version", "firmware_version"}:
        return safe_version(value)
    if key in SENSOR_KEYS and re.fullmatch(r"-?\d{1,8}(?:\.\d{1,5})?", value):
        number = float(value)
        return int(number) if number.is_integer() else number
    return None


def read_only_sample(api: HAClient, device: Device) -> dict[str, Any]:
    """Read known entity keys and return only bounded normalized values."""
    values: dict[str, Any] = {}
    for key in SENSOR_KEYS + BINARY_KEYS:
        entity_id = device.state_entities.get(key)
        if entity_id is None:
            values[key] = unknown("no enabled integration-owned entity")
            continue
        try:
            state = api.entity_state(entity_id)
        except HAAPIError:
            values[key] = unknown("entity state request failed")
            continue
        data = safe_state_value(key, state.get("state") if state else None)
        values[key] = (
            observed(data, "ha_rest_entity_state")
            if data is not None
            else unknown("state not available or not safely representable")
        )
    return values


def new_run_id() -> str:
    """Generate a non-identifying identifier."""
    return secrets.token_hex(8)


def build_session(
    *,
    run_id: str,
    requested_route: str,
    device: Device,
    environment: dict[str, Any],
    sample: dict[str, Any],
) -> dict[str, Any]:
    """Preflight cannot award HIL qualification without verified runtime capture."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": now_utc(),
        "mode": "read_only_preflight",
        "device": {
            "model": observed(device.model, "ha_device_registry"),
            "hardware_version": (
                observed(device.hardware, "ha_device_registry")
                if device.hardware
                else unknown("no validated hardware revision in registry")
            ),
            "firmware_version": (
                observed(device.firmware, "ha_device_registry")
                if device.firmware
                else unknown("no validated firmware in registry")
            ),
            "integration_entry_state": (
                observed(device.entry_state, "ha_ws_config_entries_get")
                if device.entry_state
                else unknown("config-entry state unavailable or ambiguous")
            ),
        },
        "route": {
            "requested": requested_route,
            "observed": unknown("no connection-route proof in the standard HA API"),
            "qualification_allowed": False,
        },
        "telemetry": sample,
        "preflight": evaluate_preflight(device, sample),
        "integration_build": environment["integration"],
        "qualification_status": "inconclusive",
        "reason": (
            "read-only API observations are not route-specific GATT/physical evidence"
        ),
    }


def evaluate_preflight(device: Device, sample: dict[str, Any]) -> dict[str, Any]:
    """Evaluate HA integration, BLE connection and status freshness separately.

    These are observations through HA's public entity API, not hardware HIL
    qualification. A disabled telemetry_available entity yields *unknown*,
    never a false positive or a fabricated failure.
    """
    if device.entry_state == "loaded":
        entry = "ready"
    elif device.entry_state is None:
        entry = "unknown"
    else:
        entry = "incomplete"

    def binary_state(key: str) -> str:
        record = sample.get(key)
        if not isinstance(record, dict) or record.get("verified") is not True:
            return "unknown"
        if record.get("value") == "on":
            return "ready"
        if record.get("value") == "off":
            return "incomplete"
        return "unknown"

    checks = {
        "entry_loaded": entry,
        "connected": binary_state("connected"),
        "telemetry_fresh": binary_state("telemetry_available"),
    }
    if "incomplete" in checks.values():
        overall = "incomplete"
    elif "unknown" in checks.values():
        overall = "inconclusive"
    else:
        overall = "ready"
    return {"overall": overall, "checks": checks}
