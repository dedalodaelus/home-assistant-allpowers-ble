"""Public, allowlisted environment and read-only sample collection."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
from typing import Any

from . import SCHEMA_VERSION
from .api import HAAPIError, HAClient
from .discovery import BINARY_KEYS, SENSOR_KEYS, Device, safe_version

ROOT = Path(__file__).resolve().parents[2]
VALUE_LIMIT = 200


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


def integration_build(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """A manifest version alone never proves deployed byte identity."""
    version = _safe_label(manifest.get("version")) if manifest else None
    checkout_version = local_manifest_version()
    mismatch = bool(version and checkout_version and version != checkout_version)
    return {
        "installed_manifest_version": (
            observed(version, "ha_ws_manifest_get")
            if version
            else unknown("manifest/get absent, unsupported, or unauthorized")
        ),
        "installed_commit": unknown("not exposed by the standard Home Assistant API"),
        "runner_checkout_commit": local_commit(),
        "runner_checkout_manifest_version": (
            observed(checkout_version, "runner_repository_manifest")
            if checkout_version
            else unknown("runner manifest unavailable")
        ),
        "build_match": "mismatch" if mismatch else "unverified",
        "qualification_allowed": False,
        "qualification_blocker": (
            "installed and checkout manifest versions differ"
            if mismatch
            else "installed code fingerprint is not independently verified"
        ),
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
    ha_config: dict[str, Any], manifest: dict[str, Any] | None
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
                "adapter and route metadata not available here"
            ),
            "esphome_active_proxy": unknown(
                "proxy GATT capability not verified via public API"
            ),
        },
        "runner_host": runner_host(),
        "integration": integration_build(manifest),
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
        "integration_build": environment["integration"],
        "qualification_status": "inconclusive",
        "reason": (
            "read-only API observations are not route-specific GATT/physical evidence"
        ),
    }
