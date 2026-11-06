"""Evidence classification must distinguish cache behavior and missing reports."""

from copy import deepcopy
import json

import pytest

from scripts import capture_telemetry_publication as capture
from scripts.capture_telemetry_publication import ALIASES, compare_samples


@pytest.mark.parametrize(
    ("owned", "live_advanced", "rest_advanced", "cached"),
    [
        (True, True, False, True),
        (False, True, False, False),
        (True, False, False, False),
        (True, True, True, False),
    ],
)
def test_classification_requires_live_advancement_and_verified_ownership(
    owned, live_advanced, rest_advanced, cached
):
    first_time = "2026-11-01T01:00:00+00:00"
    second_time = "2026-11-01T01:01:30+00:00"
    first = {
        "entities": {
            alias: {
                "rest_after": {"last_reported": first_time},
                "live": {
                    "last_reported": first_time,
                    "present": True,
                    "same_entry": True,
                    "allpowers_entry": True,
                    "entry_loaded": True,
                },
            }
            for alias in ALIASES
        }
    }
    second = deepcopy(first)
    for entity in second["entities"].values():
        entity["live"]["same_entry"] = owned
        if live_advanced:
            entity["live"]["last_reported"] = second_time
        if rest_advanced:
            entity["rest_after"]["last_reported"] = second_time
    for result in compare_samples(first, second).values():
        assert result["ownership_verified"] is owned
        assert result["live_report_advanced"] is live_advanced
        assert result["rest_report_stationary"] is not rest_advanced
        assert result["cached_rest_discrepancy_observed"] is cached


@pytest.mark.parametrize("rest_advances", [False, True], ids=["cached", "fresh"])
def test_capture_accepts_cached_and_fresh_http_responses(monkeypatch, rest_advances):
    """Capture and sanitize both current and corrected REST representations."""
    entity_ids = {
        alias: f"{'sensor' if alias == 'battery' else 'binary_sensor'}.private_{alias}"
        for alias in ALIASES
    }
    first_time = "2026-11-01T01:00:00+00:00"
    second_time = "2026-11-01T01:01:30+00:00"
    timestamp = first_time

    def request(_url, _token, path, payload=None):
        if path == "/api/template":
            assert payload["variables"]["entity_ids"] == entity_ids
            return {
                "entry_id": "private_entry",
                "sampled_at": timestamp,
                "entities": {
                    alias: {
                        "state": "78",
                        "last_changed": first_time,
                        "last_updated": first_time,
                        "last_reported": timestamp,
                        "present": True,
                        "same_entry": True,
                        "allpowers_entry": True,
                        "entry_loaded": True,
                    }
                    for alias in ALIASES
                },
            }
        if path == "/api/diagnostics/config_entry/private_entry":
            return {"data": {"snapshot": {}, "title": "Private station"}}
        assert path.removeprefix("/api/states/") in entity_ids.values()
        return {
            "entity_id": path.removeprefix("/api/states/"),
            "state": "78",
            "last_changed": first_time,
            "last_updated": first_time,
            "last_reported": timestamp if rest_advances else first_time,
            "attributes": {"friendly_name": "Private station"},
        }

    monkeypatch.setattr(capture, "request_json", request)
    first = capture.capture_sample("http://private-host", "private_token", entity_ids)
    timestamp = second_time
    second = capture.capture_sample("http://private-host", "private_token", entity_ids)
    for result in compare_samples(first, second).values():
        assert result["ownership_verified"]
        assert result["live_report_advanced"]
        assert result["cached_rest_discrepancy_observed"] is not rest_advances
    evidence = json.dumps([first, second])
    for secret in (
        "private-host",
        "private_token",
        "private_entry",
        "Private station",
        *entity_ids.values(),
    ):
        assert secret not in evidence
