"""Command line preflight and read-only snapshot for HIL issue #228."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import os
from pathlib import Path
import sys
from typing import Any

from .api import HAAPIError, HAClient
from .discovery import Device, choose_device, find_devices
from .environment import (
    build_environment,
    build_session,
    check_ha_version,
    new_run_id,
    read_only_sample,
)
from .storage import write_run


def make_parser() -> argparse.ArgumentParser:
    """Declare a small CLI; never accept tokens on the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.hil",
        description="Non-destructive ALLPOWERS HIL preflight and read-only collector",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--ha-url",
        default=None,
        help="HA origin (or set HA_HIL_URL); never include tokens",
    )
    common.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Explicitly allow plaintext HTTP to a remote HA server (unsafe)",
    )
    common.add_argument(
        "--device", help="Device index (1-based) or unambiguous model; not an entity ID"
    )
    common.add_argument(
        "--output-root",
        type=Path,
        default=Path("hil-results"),
        help="Local run directory",
    )
    common.add_argument(
        "--non-interactive", action="store_true", help="Never prompt for input"
    )
    common.add_argument(
        "--route",
        choices=("auto", "local_adapter", "active_proxy"),
        default="auto",
        help="Requested BLE route; never treated as proof of the actual route",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "doctor", parents=[common], help="Check read-only prerequisites"
    )
    commands.add_parser("run", parents=[common], help="Capture a read-only snapshot")
    return parser


def _credential(args: argparse.Namespace) -> tuple[str, str]:
    url = args.ha_url or os.environ.get("HA_HIL_URL", "")
    if not url:
        raise HAAPIError("Set --ha-url or HA_HIL_URL to your Home Assistant origin")
    token = os.environ.get("HA_HIL_TOKEN", "")
    if not token:
        if args.non_interactive:
            raise HAAPIError("Set HA_HIL_TOKEN for non-interactive runs")
        token = getpass.getpass("Home Assistant long-lived access token: ")
    return url, token


def _discovery(
    api: HAClient,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[Device]]:
    config = api.config()
    check_ha_version(config.get("version"))
    entities, devices, manifest, entries = api.discovery()
    return config, manifest, find_devices(entities, devices, entries)


def _device_list(devices: list[Device]) -> None:
    print(f"ALLPOWERS devices: {len(devices)}")
    for index, device in enumerate(devices, start=1):
        print(
            f"  [{index}] {device.model} "
            f"(hardware {device.hardware or 'unknown'}, "
            f"firmware {device.firmware or 'unknown'}, "
            f"integration {device.entry_state or 'unverified'})"
        )


def execute(args: argparse.Namespace) -> int:
    """Execute only GET and read-only WebSocket commands."""
    url, token = _credential(args)
    api = HAClient(url, token, allow_insecure_http=args.allow_insecure_http)
    config, manifest, devices = _discovery(api)
    environment = build_environment(config, manifest)
    print("Home Assistant:", environment["home_assistant"]["version"]["value"])
    installed = environment["integration"]["installed_manifest_version"]["value"]
    print("ALLPOWERS manifest version:", installed or "unknown")
    print("Installed integration commit: unverified by the standard HA API")
    _device_list(devices)
    print("Bluetooth connection route: unverified (no route instrumentation yet)")
    print(
        "HA host / adapter / proxy details: unavailable without an authorized collector"
    )
    if args.command == "doctor":
        if not devices:
            print(
                "No integration-owned devices found; verify setup and "
                "registry permissions"
            )
            return 2
        inspected = (
            [choose_device(devices, args.device, non_interactive=args.non_interactive)]
            if args.device
            else devices
        )
        if any(device.entry_state != "loaded" for device in inspected):
            print(
                "Preflight incomplete: integration entry not loaded or state "
                "unverified; check Settings > Devices & services. "
                "Registry discovery remains available."
            )
            return 2
        print(
            "Preflight complete: integration loaded; read-only checks only; "
            "no HIL qualification granted"
        )
        return 0

    selected = choose_device(devices, args.device, non_interactive=args.non_interactive)
    run_id = new_run_id()
    sample = read_only_sample(api, selected)
    session = build_session(
        run_id=run_id,
        requested_route=args.route,
        device=selected,
        environment=environment,
        sample=sample,
    )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_dir = write_run(
        args.output_root, f"{timestamp}_{run_id}", environment, session
    )
    print("Read-only preflight saved to:", output_dir)
    print(
        "Qualification: inconclusive (no GATT trace, physical proof or verified route)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse and execute, with transport details never printed on failure."""
    args = make_parser().parse_args(argv)
    try:
        return execute(args)
    except (HAAPIError, KeyboardInterrupt, EOFError) as ex:
        if isinstance(ex, HAAPIError):
            print(f"HIL preflight failed: {ex}", file=sys.stderr)
        else:
            print("HIL preflight interrupted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
