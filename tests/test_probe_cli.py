"""CLI integration contract for private, HA-owned config-flow probe evidence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.allpowers_ble.hil_probe import ProbeCapture
from custom_components.allpowers_ble.protocol import encode_status_request
from custom_components.allpowers_ble.route_attribution import ConnectedRoute
from scripts.hil import cli
from scripts.hil.api import HAAPIError
from tests.test_hil_cli import FakeHA, allpowers_registry as _registry_fixture


@pytest.fixture
def allpowers_registry():
    return _registry_fixture.__wrapped__()


class ProbeHA(FakeHA):
    """Authenticated WS fake exposing one form, without making a BLE connection."""

    def __init__(self, registry):
        super().__init__(registry)
        self.flows = [
            {
                "flow_id": "flow-a",
                "source": "user",
                "step": "user",
                "candidates": [{"token": "opaque-A", "label": "Candidate 1"}],
            }
        ]
        self.capture = None
        self.outcome = "success"
        self.tamper = None
        self.pending = 0
        self.selected = None

    def websocket(self, commands):
        typ = commands[0]["type"]
        self.calls.append(("ws", typ))
        if typ == "allpowers_ble/hil/probe/list":
            return [{"flows": self.flows}]
        if typ == "allpowers_ble/hil/probe/arm":
            msg = commands[0]
            self.selected = (msg["flow_id"], msg["candidate_token"])
            self.capture = ProbeCapture(
                msg["flow_id"],
                "AA:BB:CC:DD:EE:FF",
                msg["candidate_token"],
                "admin",
                msg["run_id"],
                msg["scenario_id"],
                msg["requested_route"],
            )
            return [
                {
                    "capture_id": self.capture.capture_id,
                    "schema_version": 2,
                    "ttl_seconds": 120,
                }
            ]
        if typ == "allpowers_ble/hil/probe/stop":
            self.capture.finish("interrupted", reason="requested_stop")
            return [self.capture.snapshot()]
        if typ != "allpowers_ble/hil/probe/fetch":
            raise AssertionError(typ)
        if self.pending:
            self.pending -= 1
            return [self.capture.snapshot()]
        capture = self.capture
        assert capture is not None
        if not capture.closed:
            capture.record("probe_started", session_generation=1)
            capture.record("connect_attempt", session_generation=1)
            capture.observe_route(
                1,
                ConnectedRoute(
                    "active_proxy",
                    "esphome",
                    "esphome_scanner_binding",
                    proxy_id="PRIVATE-PROXY-ID",
                ),
            )
            capture.record(
                "write_attempt",
                session_generation=1,
                frame=encode_status_request(),
                direction="tx",
                transaction_id=None,
            )
            capture.record("write_ok", session_generation=1)
            capture.record(
                "notification_received",
                session_generation=1,
                frame=b"\xaa\x65",
                direction="rx",
            )
            capture.record("notification_decoded", session_generation=1)
            capture.record("probe_status_received", session_generation=1)
            capture.finish(self.outcome, reason="safe_reason")
        snapshot = capture.snapshot()
        if self.tamper == "summary":
            snapshot["observed_route"] = "local_adapter"
        elif self.tamper == "scope":
            snapshot["capture_scope"] = "runtime_entry"
        elif self.tamper == "flow":
            snapshot["flow_id"] = "flow-foreign"
        elif self.tamper == "complete":
            snapshot["capture_complete"] = False
        return [snapshot]


def run_probe(monkeypatch, tmp_path: Path, fake: ProbeHA, *args: str) -> int:
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    return cli.main(
        [
            "probe-trace",
            "--ha-url",
            "https://example.test",
            "--non-interactive",
            "--output-root",
            str(tmp_path / "captures"),
            *args,
        ]
    )


def test_probe_cli_succeeds_with_real_v2_private_route_and_no_fake_gatt(
    monkeypatch, tmp_path, capsys, allpowers_registry
):
    fake = ProbeHA(allpowers_registry)
    code = run_probe(monkeypatch, tmp_path, fake, "--route", "local_adapter")
    assert code == 0
    output = capsys.readouterr().out
    assert "Probe outcome: success" in output
    assert "Observed connection route: active_proxy" in output
    assert "Physical HIL qualification: not permitted" in output
    assert fake.selected == ("flow-a", "opaque-A")
    assert [c for c in fake.calls if c[1] == "allpowers_ble/hil/probe/arm"]
    assert not any("flow/configure" in call[1] for call in fake.calls)
    (directory,) = list((tmp_path / "captures").iterdir())
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in directory.iterdir())
    names = {path.name for path in directory.iterdir()}
    assert names == {
        "environment.json",
        "session.json",
        "events.jsonl",
        "frames.jsonl",
        "trace.json",
    }
    metadata = json.loads((directory / "trace.json").read_text())
    assert metadata["capture_scope"] == "config_probe"
    assert metadata["capture_complete"] is True
    assert metadata["probe_outcome"] == "success"
    assert metadata["observed_route"] == "active_proxy"
    assert metadata["requested_route"] == "local_adapter"
    assert metadata["qualification_allowed"] is False
    assert "PRIVATE-PROXY-ID" in (directory / "trace.json").read_text()
    for name in ("environment.json", "session.json", "events.jsonl", "frames.jsonl"):
        assert "PRIVATE-PROXY-ID" not in (directory / name).read_text()
        assert "AA:BB:CC" not in (directory / name).read_text()
        assert "opaque-A" not in (directory / name).read_text()


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", "interrupted"])
def test_probe_cli_failed_or_skipped_outcome_is_saved_but_never_passes(
    monkeypatch, tmp_path, allpowers_registry, outcome
):
    fake = ProbeHA(allpowers_registry)
    fake.outcome = outcome
    assert run_probe(monkeypatch, tmp_path, fake) == 2
    (directory,) = list((tmp_path / "captures").iterdir())
    metadata = json.loads((directory / "trace.json").read_text())
    assert metadata["probe_outcome"] == outcome
    assert metadata["capture_complete"] is (outcome in ("failure", "cancelled"))
    assert metadata["qualification_allowed"] is False


@pytest.mark.parametrize("tamper", ["summary", "scope", "flow", "complete"])
def test_probe_cli_rejects_tampered_private_evidence_before_raw_disk(
    monkeypatch, tmp_path, allpowers_registry, tamper
):
    fake = ProbeHA(allpowers_registry)
    fake.tamper = tamper
    assert run_probe(monkeypatch, tmp_path, fake) == 2
    (directory,) = list((tmp_path / "captures").iterdir())
    assert (directory / "environment.json").exists()
    assert not (directory / "events.jsonl").exists()
    assert not (directory / "trace.json").exists()


def test_probe_cli_polls_only_bounded_private_fetch_without_configuring(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = ProbeHA(allpowers_registry)
    fake.pending = 2
    assert run_probe(monkeypatch, tmp_path, fake) == 0
    assert fake.calls.count(("ws", "allpowers_ble/hil/probe/fetch")) == 3
    assert not any("flow/configure" in typ for _, typ in fake.calls)


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("empty", 2),
        ("bad_response", 2),
        ("ambiguous", 2),
        ("missing", 2),
    ],
)
def test_probe_cli_refuses_unknown_and_ambiguous_selection(
    monkeypatch, tmp_path, allpowers_registry, changed, expected
):
    fake = ProbeHA(allpowers_registry)
    if changed == "empty":
        fake.flows = []
    elif changed == "bad_response":
        fake.flows = [None]
    elif changed == "ambiguous":
        fake.flows[0]["candidates"].append(
            {"token": "opaque-B", "label": "Candidate 2"}
        )
    elif changed == "missing":
        fake.flows[0]["candidates"] = []
    assert run_probe(monkeypatch, tmp_path, fake) == expected
    assert not any(call[1] == "allpowers_ble/hil/probe/arm" for call in fake.calls)


def test_probe_cli_explicit_opaque_or_ordinal_selection(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = ProbeHA(allpowers_registry)
    fake.flows[0]["candidates"].append(
        {"token": "opaque-B", "label": "Candidate 2"}
    )
    assert run_probe(
        monkeypatch, tmp_path, fake, "--flow-id", "flow-a", "--candidate", "2"
    ) == 0
    assert fake.selected == ("flow-a", "opaque-B")


def test_probe_cli_interactive_choice_does_not_show_internal_identifiers(
    monkeypatch, tmp_path, capsys, allpowers_registry
):
    fake = ProbeHA(allpowers_registry)
    fake.flows[0]["candidates"].append(
        {"token": "opaque-B", "label": "Candidate 2"}
    )
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setattr("builtins.input", lambda *_: "2")
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    code = cli.main(
        [
            "probe-trace",
            "--ha-url",
            "https://example.test",
            "--output-root",
            str(tmp_path / "results"),
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert "Candidate 2" in stdout
    assert "opaque-A" not in stdout
    assert "AA:BB:CC" not in stdout
    assert fake.selected == ("flow-a", "opaque-B")


def test_probe_cli_noninteractive_ttl_timeout_stops_without_ble_requests(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = ProbeHA(allpowers_registry)
    fake.pending = 200
    assert run_probe(monkeypatch, tmp_path, fake) == 2
    assert ("ws", "allpowers_ble/hil/probe/stop") in fake.calls
    assert fake.capture.probe_outcome == "interrupted"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("probe_outcome", "unknown"),
        ("probe_state", "armed"),
        ("flow_id", ""),
        ("capture_complete", False),
        ("termination_reason", "runtime_stopped"),
        ("schema_version", 1),
    ],
)
def test_probe_cli_schema_validation_denies_false_qualification(field, value):
    capture = ProbeCapture(
        "flow-a", "AA:BB:CC:DD:EE:FF", "token", "admin",
        "run12345", "scenario", "auto",
    )
    capture.record("probe_started", session_generation=1)
    capture.finish("success")
    result = capture.snapshot()
    result[field] = value
    with pytest.raises(HAAPIError):
        cli._validate_trace_result(
            result,
            run_id="run12345",
            scenario_id="scenario",
            capture_id=capture.capture_id,
            requested_route="auto",
            expected_version=2,
            expected_scope="config_probe",
        )


def test_probe_cli_rejects_candidate_address_used_as_opaque_token():
    params = cli.make_parser().parse_args(
        [
            "probe-trace",
            "--ha-url",
            "https://example.test",
            "--non-interactive",
            "--candidate",
            "AA:BB:CC:DD:EE:FF",
        ]
    )
    with pytest.raises(HAAPIError):
        cli._select_probe(
            {
                "flows": [
                    {
                        "flow_id": "flow-a",
                        "candidates": [
                            {"token": "opaque-A", "label": "Candidate 1"}
                        ],
                    }
                ]
            },
            params,
        )


def test_probe_cli_private_save_failure_scrubs_only_trace_files(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = ProbeHA(allpowers_registry)
    original_open = cli.os.open
    calls = 0

    def fail_second_jsonl(path, flags, mode=0o777):
        nonlocal calls
        if str(path).endswith(".jsonl"):
            calls += 1
            if calls == 2:
                raise OSError("PRIVATE PATH ERROR")
        return original_open(path, flags, mode)

    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setattr(cli.os, "open", fail_second_jsonl)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    assert cli.main(
        [
            "probe-trace",
            "--ha-url",
            "https://example.test",
            "--non-interactive",
            "--output-root",
            str(tmp_path / "private"),
        ]
    ) == 2
    (directory,) = list((tmp_path / "private").iterdir())
    assert not (directory / "events.jsonl").exists()
    assert not (directory / "frames.jsonl").exists()
    assert not (directory / "trace.json").exists()
    assert (directory / "session.json").exists()
