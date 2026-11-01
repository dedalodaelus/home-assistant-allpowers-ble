"""Evidence classification must distinguish cache behavior and missing reports."""

from copy import deepcopy

import pytest

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
