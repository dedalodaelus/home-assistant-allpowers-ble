"""Route-aware v2 trace semantics and fail-closed private CLI validation."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from custom_components.allpowers_ble.hil_trace import Capture
from custom_components.allpowers_ble.protocol import encode_status_request
from custom_components.allpowers_ble.route_attribution import ConnectedRoute
from scripts.hil import cli
from scripts.hil.api import HAAPIError
from tests.test_hil_cli import TraceHA, allpowers_registry as _registry_fixture


@pytest.fixture
def allpowers_registry():
    """Reuse the existing CLI fixture for v2 route-proof roundtrips."""
    return _registry_fixture.__wrapped__()



LOCAL = ConnectedRoute("local_adapter", "bluez", "bluez_device_path", adapter_id="hci1")
PROXY = ConnectedRoute(
    "active_proxy",
    "esphome",
    "esphome_scanner_binding",
    proxy_id="PRIVATE-PROXY-ID",
)
UNKNOWN = ConnectedRoute(evidence="no_ha_wrapper")


def route_capture(route=LOCAL, *, requested="local_adapter"):
    capture = Capture("entry", "admin", "run12345", "status", requested)
    capture.observe_route(1, route)
    capture.record(
        "write_attempt",
        frame=encode_status_request(),
        direction="tx",
        session_generation=1,
        transaction_id=None,
    )
    capture.stop()
    return capture.snapshot()


def validate(result):
    cli._validate_trace_result(
        result,
        run_id=result["run_id"],
        scenario_id=result["scenario_id"],
        capture_id=result["capture_id"],
        requested_route=result["requested_route"],
        expected_version=2,
    )


@pytest.mark.parametrize("route", [LOCAL, PROXY, UNKNOWN])
def test_v2_accepts_real_capture_with_correct_connected_proof(route):
    result = route_capture(route)
    validate(result)
    assert result["schema_version"] == 2
    assert result["observed_route"] == route.observed_route
    assert result["route_observations"][0]["observed_route"] == route.observed_route
    assert result["qualification_allowed"] is False
    assert result["events"][0]["event"] == "route_observed"
    assert result["frames"][0]["observed_route"] == route.observed_route
    assert "PRIVATE-PROXY-ID" not in str(result["events"])
    assert "PRIVATE-PROXY-ID" not in str(result["frames"])


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "duplicate",
        "route",
        "backend",
        "evidence",
        "adapter",
        "proxy",
        "summary",
        "event_route",
        "frame_route",
        "cross_gen",
        "missing_event",
        "future_seq",
        "old_seq",
        "invalid_time",
        "timestamp",
        "private_key",
        "version_mismatch",
        "missing_observation",
    ],
)
def test_v2_rejects_malformed_route_and_frame_evidence(change):
    result = route_capture()
    observation = result["route_observations"][0]
    if change == "generation":
        observation["session_generation"] = 2
    elif change == "duplicate":
        result["route_observations"].append(observation.copy())
    elif change == "route":
        observation["observed_route"] = "active_proxy"
    elif change == "backend":
        observation["backend_kind"] = "esphome"
    elif change == "evidence":
        observation["evidence"] = "esphome_scanner_binding"
    elif change == "adapter":
        observation["adapter_id"] = "hci9"
    elif change == "proxy":
        observation["proxy_id"] = "private-proxy"
    elif change == "summary":
        result["observed_route"] = "mixed"
    elif change == "event_route":
        result["events"][0]["observed_route"] = "unverified"
    elif change == "frame_route":
        result["frames"][0]["observed_route"] = "active_proxy"
    elif change == "cross_gen":
        result["frames"][0]["session_generation"] = 3
        result["events"][1]["session_generation"] = 3
    elif change == "missing_event":
        result["events"] = result["events"][1:]
        result["event_count"] = len(result["events"])
    elif change == "future_seq":
        observation["seq"] = 5
    elif change == "old_seq":
        observation["seq"] = 0
    elif change == "invalid_time":
        observation["elapsed_ms"] = -1
    elif change == "timestamp":
        observation["utc"] = "2026-11-05T10:00:00+01:00"
    elif change == "private_key":
        observation["raw_device_path"] = "/org/bluez/hci1/dev_SECRET"
    elif change == "version_mismatch":
        result["events"][0]["schema_version"] = 1
    else:
        result["route_observations"] = []
        result["observed_route"] = "unverified"
    with pytest.raises(HAAPIError):
        validate(result)


def test_v2_mixed_then_unknown_route_summary_and_original_frame_identity():
    capture = Capture("entry", "admin", "run12345", "status", "auto")
    capture.record("connect_attempt", session_generation=1)
    capture.observe_route(1, LOCAL)
    capture.record(
        "write_attempt",
        frame=encode_status_request(),
        direction="tx",
        session_generation=1,
    )
    first = deepcopy(capture.frames[0])
    capture.observe_route(2, PROXY)
    capture.record(
        "write_attempt",
        frame=encode_status_request(),
        direction="tx",
        session_generation=2,
    )
    capture.stop()
    result = capture.snapshot()
    validate(result)
    assert result["observed_route"] == "mixed"
    assert result["frames"][0] == first
    assert result["frames"][1]["observed_route"] == "active_proxy"
    assert result["events"][0]["observed_route"] == "unverified"

    second = result["route_observations"][1]
    second.update(
        observed_route="unverified",
        backend_kind="unknown",
        evidence="not_connected",
    )
    del second["proxy_id"]
    result["observed_route"] = "unverified"
    for row in result["events"] + result["frames"]:
        if row["session_generation"] == 2:
            row["observed_route"] = "unverified"
            if row["event"] == "route_observed":
                row["backend_kind"] = "unknown"
                row["evidence"] = "not_connected"
    validate(result)


class V2TraceHA(TraceHA):
    """Return a private v2 capture over the CLI's authenticated WS contract."""

    def websocket(self, commands):
        response = super().websocket(commands)
        if commands[0]["type"] == "allpowers_ble/hil/start":
            response[0]["schema_version"] = 2
            return response
        capture = Capture(
            "entry", "admin", self.run_id, self.scenario_id, self.requested_route
        )
        capture.observe_route(1, PROXY)
        capture.record(
            "write_attempt",
            session_generation=1,
            frame=encode_status_request(),
            direction="tx",
            transaction_id=None,
        )
        capture.stop()
        result = capture.snapshot()
        result["capture_id"] = "private-capture"
        if self.bad_result == "bad_v2_summary":
            result["observed_route"] = "local_adapter"
        return [result]


@pytest.mark.parametrize("invalid", [False, True])
def test_v2_cli_roundtrip_private_files_never_public_ids(
    monkeypatch, tmp_path, allpowers_registry, invalid
):
    fake = V2TraceHA(allpowers_registry)
    if invalid:
        fake.bad_result = "bad_v2_summary"
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    root = tmp_path / "results"
    code = cli.main(
        [
            "trace",
            "--ha-url",
            "https://example.test",
            "--non-interactive",
            "--duration",
            "1",
            "--route",
            "local_adapter",
            "--output-root",
            str(root),
        ]
    )
    assert code == (2 if invalid else 0)
    (directory,) = list(root.iterdir())
    if invalid:
        assert not (directory / "frames.jsonl").exists()
        assert not (directory / "trace.json").exists()
    else:
        private = json.loads((directory / "trace.json").read_text())
        assert private["schema_version"] == 2
        assert private["observed_route"] == "active_proxy"
        assert private["requested_route"] == "local_adapter"
        assert private["route_observations"][0]["proxy_id"] == "PRIVATE-PROXY-ID"
        assert private["qualification_allowed"] is False
        for name in (
            "environment.json",
            "session.json",
            "events.jsonl",
            "frames.jsonl",
        ):
            assert "PRIVATE-PROXY-ID" not in (directory / name).read_text()
