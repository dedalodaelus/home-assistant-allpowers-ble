#!/usr/bin/env python3
"""Compare cached REST states with live HA template reads without device writes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from getpass import getpass
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ALIASES = ("battery", "connected", "telemetry_available")
STATE_FIELDS = ("state", "last_changed", "last_updated", "last_reported")
COUNTERS = (
    "notifications",
    "valid_packets",
    "parser_discards",
    "protocol_errors",
    "connection_attempts",
    "successful_connections",
    "disconnects",
    "reconnects",
)
LIVE_TEMPLATE = """
{% set target = config_entry_id(entity_ids.battery) %}
{% set ns = namespace(entities={}) %}
{% for alias, entity_id in entity_ids.items() %}
  {% set s = states[entity_id] %}
  {% set item = {
    'present': s is not none,
    'same_entry': target is not none and config_entry_id(entity_id) == target,
    'allpowers_entry': target is not none and config_entry_attr(target, 'domain') == 'allpowers_ble',
    'entry_loaded': target is not none and config_entry_attr(target, 'state') == 'loaded',
    'state': s.state if s is not none else none,
    'last_changed': s.last_changed.isoformat() if s is not none else none,
    'last_updated': s.last_updated.isoformat() if s is not none else none,
    'last_reported': s.last_reported.isoformat() if s is not none else none
  } %}
  {% set ns.entities = dict(ns.entities, **{alias: item}) %}
{% endfor %}
{{ {'sampled_at': utcnow().isoformat(), 'entry_id': target, 'entities': ns.entities} | to_json }}
"""


def utcnow() -> str:
    """Return a UTC capture timestamp."""
    return datetime.now(timezone.utc).isoformat()


def request_json(
    base_url: str, token: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    """Read REST state or evaluate the fixed read-only template."""
    request = Request(
        base_url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def capture_sample(base_url: str, token: str, entity_ids: dict[str, str]) -> dict:
    """Bracket direct live readings with REST observations of the exact same IDs."""
    started = utcnow()
    rest: dict[str, dict] = {}
    for alias, entity_id in entity_ids.items():
        state = request_json(base_url, token, f"/api/states/{entity_id}")
        rest[alias] = {key: state.get(key) for key in STATE_FIELDS}
    live = request_json(
        base_url,
        token,
        "/api/template",
        {"template": LIVE_TEMPLATE, "variables": {"entity_ids": entity_ids}},
    )
    diagnostics = {}
    entry_id = live.get("entry_id")
    if entry_id and all(
        item["same_entry"] and item["allpowers_entry"]
        for item in live["entities"].values()
    ):
        diagnostics_started = utcnow()
        try:
            payload = request_json(
                base_url, token, f"/api/diagnostics/config_entry/{entry_id}"
            )["data"]
            snapshot = payload["snapshot"]
            diagnostics = {
                "snapshot": {
                    key: snapshot.get(key)
                    for key in (
                        "connected",
                        "last_packet_at",
                        "status_monotonic",
                        "settings_monotonic",
                    )
                },
                "snapshot_statistics": {
                    key: snapshot.get("statistics", {}).get(key) for key in COUNTERS
                },
            }
        except HTTPError as error:
            diagnostics = {"http_status": error.code}
        diagnostics["started_at"] = diagnostics_started
        diagnostics["finished_at"] = utcnow()
    # Whitelist scalar values only. Never write IDs, attributes, or API responses.
    entities = {}
    for alias in ALIASES:
        direct = live["entities"][alias]
        state = request_json(base_url, token, f"/api/states/{entity_ids[alias]}")
        entities[alias] = {
            "rest_before": rest[alias],
            "live": {
                key: direct[key]
                for key in (
                    *STATE_FIELDS,
                    "present",
                    "same_entry",
                    "allpowers_entry",
                    "entry_loaded",
                )
            },
            "rest_after": {key: state.get(key) for key in STATE_FIELDS},
        }
    return {
        "started_at": started,
        "live_sampled_at": live["sampled_at"],
        "finished_at": utcnow(),
        "entities": entities,
        "diagnostics": diagnostics,
    }


def compare_samples(first: dict, second: dict) -> dict:
    """Report deltas without treating a stationary REST cache as a telemetry freeze."""
    result = {}
    for alias in ALIASES:
        before, after = first["entities"][alias], second["entities"][alias]
        verified = all(
            sample["live"][key] is True
            for sample in (before, after)
            for key in ("present", "same_entry", "allpowers_entry", "entry_loaded")
        )
        live_before = before["live"]["last_reported"]
        live_after = after["live"]["last_reported"]
        advanced = (
            live_before is not None
            and live_after is not None
            and datetime.fromisoformat(live_after) > datetime.fromisoformat(live_before)
        )
        rest_stationary = (
            before["rest_after"]["last_reported"]
            == after["rest_after"]["last_reported"]
        )
        result[alias] = {
            "ownership_verified": verified,
            "live_report_advanced": advanced,
            "rest_report_stationary": rest_stationary,
            "cached_rest_discrepancy_observed": verified
            and advanced
            and rest_stationary,
        }
    return result


def main() -> int:
    """Prompt privately for the endpoint, entity IDs, and an existing admin token."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=90)
    parser.add_argument(
        "--output", type=Path, default=Path("telemetry-publication.json")
    )
    args = parser.parse_args()
    if not 1 <= args.interval <= 600:
        parser.error("interval must be between 1 and 600 seconds")
    base_url = input("Home Assistant URL: ").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("provide an http(s) Home Assistant URL without credentials/query")
    entity_ids = {alias: input(f"Entity ID ({alias}): ").strip() for alias in ALIASES}
    for alias, entity_id in entity_ids.items():
        domain = "sensor" if alias == "battery" else "binary_sensor"
        if not re.fullmatch(rf"{domain}\.[a-z0-9_]+", entity_id):
            parser.error(f"invalid entity ID for {alias}")
    token = getpass("Existing Home Assistant admin token (hidden): ").strip()
    try:
        samples = [capture_sample(base_url, token, entity_ids)]
        print(f"First sample captured. Waiting {args.interval:g} seconds…", flush=True)
        time.sleep(args.interval)
        samples.append(capture_sample(base_url, token, entity_ids))
        result = {"samples": samples, "comparison": compare_samples(*samples)}
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except HTTPError as error:
        print(
            f"Capture failed: HTTP {error.code}; no raw response or credentials logged."
        )
        return 1
    except (URLError, TimeoutError, ValueError, KeyError, OSError) as error:
        print(f"Capture failed ({type(error).__name__}); no raw response logged.")
        return 1
    print(json.dumps(result["comparison"], indent=2))
    print(f"Sanitized evidence saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
