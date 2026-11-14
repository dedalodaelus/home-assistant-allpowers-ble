"""Offline contract and security tests for the standalone HIL preflight CLI."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import subprocess
import builtins
from types import ModuleType
from urllib.error import HTTPError
import zipfile

import pytest

from scripts.hil import api, cli, discovery, environment, storage


@pytest.fixture
def allpowers_registry():
    """Realistic 2026.10 registry data; IDs stay in memory."""
    devices = [
        {
            "id": "secret-device-id-012345",
            "config_entry_id": "secret-entry-1",
            "identifiers": [["allpowers_ble", "AA:BB:CC:DD:EE:FF"]],
            "connections": [["bluetooth", "AA:BB:CC:DD:EE:FF"]],
            "model": "R600",
            "name": "My very private power station",
            "hw_version": "0.3",
            "sw_version": "0.2",
        },
        {
            "id": "foreign-device-id",
            "config_entry_id": "some-other-entry",
            "identifiers": [["esphome", "abc"]],
            "model": "R600",
        },
    ]
    entities = [
        {
            "device_id": "secret-device-id-012345",
            "platform": "allpowers_ble",
            "entity_id": "sensor.my_name_battery",
            "unique_id": "AA:BB:CC:DD:EE:FF_battery",
            "disabled_by": None,
        },
        {
            "device_id": "secret-device-id-012345",
            "platform": "allpowers_ble",
            "entity_id": "sensor.my_name_hardware_version",
            "unique_id": "AA:BB:CC:DD:EE:FF_hardware_version",
            "disabled_by": None,
        },
        {
            "device_id": "secret-device-id-012345",
            "platform": "allpowers_ble",
            "entity_id": "sensor.my_name_firmware_version",
            "unique_id": "AA:BB:CC:DD:EE:FF_firmware_version",
            "disabled_by": None,
        },
        {
            "device_id": "secret-device-id-012345",
            "platform": "allpowers_ble",
            "entity_id": "binary_sensor.my_name_connected",
            "unique_id": "AA:BB:CC:DD:EE:FF_connected",
            "disabled_by": None,
        },
        {
            "device_id": "secret-device-id-012345",
            "platform": "allpowers_ble",
            "entity_id": "binary_sensor.my_name_telemetry_available",
            "unique_id": "AA:BB:CC:DD:EE:FF_telemetry_available",
            "disabled_by": None,
        },
        {
            "device_id": "foreign-device-id",
            "platform": "esphome",
            "entity_id": "sensor.foreign_battery",
            "unique_id": "abc_battery",
        },
    ]
    entries = [
        {"entry_id": "secret-entry-1", "domain": "allpowers_ble", "state": "loaded"}
    ]
    return entities, devices, entries


class FakeHA:
    def __init__(self, data):
        self.entities, self.devices, self.entries = data
        self.calls = []
        self.state_overrides = {}
        self.scanners = None

    def scanner_candidates(self):
        self.calls.append(("ws", "bluetooth_scanner_candidates"))
        return self.scanners

    def config(self):
        self.calls.append(("get", "/api/config"))
        return {
            "version": "2026.10.4",
            "latitude": 39.4699,
            "longitude": -0.3763,
            "location_name": "My House",
            "time_zone": "Europe/Madrid",
        }

    def discovery(self):
        self.calls.append(("ws", "registry"))
        return self.entities, self.devices, {"version": "0.3.0"}, self.entries

    def hil_capabilities(self, entry_id):
        self.calls.append(("ws", "hil_capabilities", entry_id))
        return {
            "schema_version": 1,
            "model": "R600",
            "profile": "r600-hw-0.3",
            "classification": "verified",
            "verified": True,
            "identity_conflicted": False,
            "hardware_revision": "0.3",
            "raw_hardware_revision": 3,
            "firmware_revision": "0.2",
            "raw_firmware_revision": 2,
            "capabilities": {
                "read_telemetry": True,
                "write_output_controls": True,
                "write_settings_controls": True,
                "write_settings_keepalive": True,
                "car_charger_enabled": False,
            },
            "connected": True,
            "status_fresh": True,
            "settings_fresh": True,
            "session_generation": 3,
            "pending_transaction": False,
        }

    def control_state(self, key, entity_id):
        self.calls.append(("control", key, entity_id))
        return {"available": True, "state": "off", "options": []}

    def entity_state(self, entity_id):
        self.calls.append(("get", entity_id))
        if entity_id in self.state_overrides:
            return {"state": self.state_overrides[entity_id]}
        if entity_id.endswith("_connected"):
            return {"state": "on"}
        if entity_id.endswith("_telemetry_available"):
            return {"state": "on"}
        if entity_id.endswith("_battery"):
            return {"state": "76", "attributes": {"secret": "10.0.0.100"}}
        if entity_id.endswith("_hardware_version"):
            return {"state": "0.3"}
        if entity_id.endswith("_firmware_version"):
            return {"state": "0.2"}
        return None


def make_args(*argv):
    return cli.make_parser().parse_args(list(argv))


def test_url_validation_rejects_plaintext_remote_and_embedded_secrets():
    assert (
        api.normalize_url("https://example.test:8123/") == "https://example.test:8123"
    )
    assert api.normalize_url("http://127.0.0.1:8123") == "http://127.0.0.1:8123"
    for value in (
        "http://10.0.0.4:8123",
        "https://user:password@example.test",
        "https://example.test/api",
        "https://example.test?token=hidden",
        "https://example.test#secret",
        "ftp://example.test",
        "https://example.test:bad",
    ):
        with pytest.raises(api.HAAPIError):
            api.normalize_url(value)
    assert (
        api.normalize_url("http://10.0.0.4:8123", allow_insecure_http=True)
        == "http://10.0.0.4:8123"
    )
    assert api.normalize_url("HTTPS://example.test:8123/") == (
        "https://example.test:8123"
    )
    assert api.normalize_url("HtTpS://example.test:8123") == (
        "https://example.test:8123"
    )


def test_https_remains_enforced_no_disable_tls_switch():
    parser = cli.make_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["doctor", "--no-verify-tls"])
    assert "--token" not in parser.format_help()


def test_registry_selects_by_integration_owner_not_vendor_name(allpowers_registry):
    entities, devices, entries = allpowers_registry
    choices = discovery.find_devices(entities, devices, entries)
    assert len(choices) == 1
    assert choices[0].model == "R600"
    assert choices[0].hardware == "0.3"
    assert choices[0].state_entities["battery"] == "sensor.my_name_battery"
    assert discovery.choose_device(choices, None, non_interactive=True) is choices[0]


def test_guided_read_only_run_saves_allowlisted_runtime_capabilities(
    monkeypatch, tmp_path, allpowers_registry
):
    """Explicit suite reads current metadata but never persists HA identifiers."""
    fake = FakeHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    output_root = tmp_path / "scenario"

    assert (
        cli.main(
            [
                "run",
                "--suite",
                "read-only",
                "--ha-url",
                "https://example.test",
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )

    (run_dir,) = tuple(output_root.iterdir())
    report = json.loads((run_dir / "private" / "session.json").read_text())
    assert report["runtime_capabilities"]["profile"] == "r600-hw-0.3"
    assert report["controls"]["car_charger"]["eligible"] is False
    assert "secret-entry-1" not in json.dumps(report)
    assert "AA:BB:CC:DD:EE:FF" not in json.dumps(report)
    assert ("ws", "hil_capabilities", "secret-entry-1") in fake.calls

    from scripts.hil.evidence import export_archive, verify_public_archive

    assert cli.main(["report", str(run_dir)]) == 0
    archive = export_archive(run_dir, confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        public = {name: exported.read(name) for name in exported.namelist()}
        session = json.loads(public["public/session.json"])
        report_text = public["public/report.md"].decode()
    combined = b"".join(public.values())
    assert session["data"]["mode"] == "read_only_preflight"
    assert session["data"]["route"]["requested"] == "auto"
    assert session["data"]["preflight"]["checks"] == {
        "connected": "ready",
        "entry_loaded": "ready",
        "telemetry_fresh": "ready",
    }
    assert session["data"]["telemetry"]["battery"] == {
        "value": 76,
        "verified": True,
    }
    assert (
        session["data"]["runtime_capabilities"]["capabilities"]["read_telemetry"]
        is True
    )
    assert "battery `76`" in report_text
    assert "connected `ready`" in report_text
    assert "Requested route: `auto`" in report_text
    assert b"secret-device-id-012345" not in combined
    assert b"secret-entry-1" not in combined
    assert b"10.0.0.100" not in combined
    assert b"PRIVATE_TOKEN" not in combined
    assert verify_public_archive(archive)["qualification_status"] == "inconclusive"


def test_tier_plan_is_persisted_and_privacy_safe_on_export(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = FakeHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "read_only_sample", lambda *_args: {"sample": "fresh"})
    monkeypatch.setattr(
        cli, "_has_verified_telemetry", lambda sample: sample == {"sample": "fresh"}
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: "1")
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    output_root = tmp_path / "guided-tier"

    assert (
        cli.main(
            [
                "run",
                "--tier",
                "bronze",
                "--route",
                "active_proxy",
                "--ha-url",
                "https://example.test",
                "--output-root",
                str(output_root),
            ]
        )
        == 2
    )

    (run_dir,) = tuple(output_root.iterdir())
    private_session = json.loads((run_dir / "private" / "session.json").read_text())
    assert private_session["test_plan"]["requested_tier"] == "bronze"
    assert private_session["test_plan"]["attained_tier"] is None
    tier_execution = json.loads(
        (run_dir / "private" / "tier-execution.json").read_text()
    )
    assert tier_execution["device"] == {
        "model": "R600",
        "hardware_version": "0.3",
        "firmware_version": "0.2",
    }
    assert tier_execution["requested_route"] == "active_proxy"
    assert tier_execution["verified_routes"] == []
    assert (
        tier_execution["build_provenance"]
        == private_session["test_plan"]["device"]["build_provenance"]
    )

    from scripts.hil.evidence import export_archive, verify_public_archive

    archive = export_archive(run_dir, confirmed=True)
    with zipfile.ZipFile(archive) as exported:
        public = {name: exported.read(name) for name in exported.namelist()}
    session = json.loads(public["public/session.json"])
    bronze_evidence = json.loads(public["public/tier-bronze-read-only.json"])
    combined = b"".join(public.values())
    tier_plan = session["data"]["test_plan"]
    assert tier_plan["requested_tier"] == "bronze"
    assert tier_plan["attained_tier"] is None
    assert tier_plan["device"]["verified_route"] is None
    assert bronze_evidence["protocol_probe"] == "inconclusive"
    assert bronze_evidence["qualification_status"] == "inconclusive"
    assert session["data"]["route"]["requested"] == "active_proxy"
    assert b"secret-device-id-012345" not in combined
    assert b"secret-entry-1" not in combined
    assert b"PRIVATE_TOKEN" not in combined
    assert verify_public_archive(archive)["qualification_status"] == "inconclusive"


@pytest.mark.parametrize("include_trace", [True, False])
def test_gold_executor_dispatches_existing_suites_and_reuses_recovery_evidence(
    monkeypatch, tmp_path, include_trace
):
    from argparse import Namespace

    from scripts.hil import outputs, recovery, settings
    from scripts.hil.guided import build_tier_plan

    api = type("FakeHA", (), {"origin": "https://example.test"})()
    device = discovery.Device(
        device_id="fake-device",
        model="R600",
        hardware="0.3",
        firmware="1.0",
        state_entities={},
        entry_state="loaded",
        control_entities={
            "ac_output": "switch.ac",
            "dc_output": "switch.dc",
            "light": "switch.light",
            "eco_mode": "switch.eco",
            "work_mode": "select.mode",
            "eco_timeout": "select.timeout",
            "car_charger": "switch.car",
        },
        config_entry_id="fake-entry",
    )
    capabilities = {
        "model": "R600",
        "hardware_revision": "0.3",
        "firmware_revision": "1.0",
        "features": {
            key: {"physical_presence": "present", "production_authorized": True}
            for key in (
                "ac_output",
                "dc_output",
                "light_output",
                "eco_mode",
                "work_mode",
                "eco_timeout",
                "car_charger",
            )
        },
    }
    controls = {
        key: {"available": True, "eligible": True, "state": "off"}
        for key in (
            "ac_output",
            "dc_output",
            "light",
            "eco_mode",
            "work_mode",
            "eco_timeout",
            "car_charger",
        )
    }
    plan = build_tier_plan(
        "gold", capabilities, controls, observed_route="local_adapter"
    )
    calls = []

    def fake_output(_api, args, _device, _environment, out, _run_id, _services):
        calls.append((args.suite, args.scenario_id))
        (out / "checkpoint.json").write_text(
            json.dumps({"steps": [{"outcome": "pass"}]}), encoding="utf-8"
        )
        if include_trace:
            (out / "trace.json").write_text("{}", encoding="utf-8")
        return 0

    def fake_settings(_api, args, _device, out, _run_id, _services):
        calls.append((args.suite, args.scenario_id))
        (out / "checkpoint.json").write_text(
            json.dumps({"steps": [{"outcome": "pass"}]}), encoding="utf-8"
        )
        if include_trace:
            (out / "trace.json").write_text("{}", encoding="utf-8")
        return 0

    def fake_recovery(_api, args, _device, out, _run_id, _services, _resume):
        calls.append((args.suite, args.scenario_id))
        (out / "checkpoint.json").write_text(
            json.dumps(
                {
                    "steps": [
                        {"step_id": "baseline", "outcome": "pass"},
                        {
                            "step_id": "operator_action",
                            "outcome": "pass",
                            "action": "route interruption and restoration",
                            "operator_confirmed": True,
                        },
                        {
                            "step_id": "recovered_session",
                            "outcome": "pass",
                            "session_generation_before": 1,
                            "session_generation_after": 2,
                            "disconnect_observed": False,
                            "connected": True,
                            "status_fresh": True,
                            "fresh_status_telemetry": True,
                            "route_verified": True,
                            "trace_complete": True,
                            "physical_outcome": "yes",
                            "route": "local_adapter",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        if include_trace:
            (out / "trace.json").write_text(
                json.dumps(
                    {
                        "complete": True,
                        "observed_route": "local_adapter",
                    }
                ),
                encoding="utf-8",
            )
        assert args.guided_reconnect_required is True
        return 0

    monkeypatch.setattr(outputs, "_execute_output_suite", fake_output)
    monkeypatch.setattr(settings, "_execute_settings_suite", fake_settings)
    monkeypatch.setattr(recovery, "_execute_recovery_suite", fake_recovery)
    from dataclasses import replace

    base_services = cli._suite_services()
    monkeypatch.setattr(
        cli,
        "_suite_services",
        lambda: replace(
            base_services,
            read_only_sample=lambda *_args: {"sample": "fresh"},
            has_verified_telemetry=lambda sample: sample == {"sample": "fresh"},
            prompt=lambda _question: "1",
        ),
    )
    args = Namespace(route="local_adapter", output_root=tmp_path, non_interactive=False)
    root = storage.write_run(tmp_path, "tier-parent", {}, {})

    assert (
        cli._execute_tier_suite(
            api, args, device, {}, root, "parent-run", plan, capabilities
        )
        == 2
    )
    assert calls.count(("recovery", None)) == (1 if include_trace else 0)
    assert ("outputs", "output_ac_cycle") in calls
    assert (("settings", "settings_eco_mode") in calls) is include_trace
    child_runs = list(tmp_path.glob("tier_*/private/session.json"))
    assert child_runs
    for session_path in child_runs:
        child_session = json.loads(session_path.read_text())
        assert re.fullmatch(r"[a-f0-9]{16}", child_session["run_id"])
        assert session_path.parent.parent.name == f"tier_{child_session['run_id']}"
    execution = json.loads((root / "tier-execution.json").read_text())
    assert execution["review_state"] == "pending"
    scenario_steps = [
        step
        for step in execution["steps"]
        if step.get("scenario_id") in {"output_ac_cycle", "settings_eco_mode"}
    ]
    assert all((step["outcome"] == "pass") is include_trace for step in scenario_steps)
    assert all(
        step["machine_evidence"]
        for step in execution["steps"]
        if step["outcome"] == "pass"
    )
    for step in execution["steps"]:
        for artifact in step.get("machine_evidence", []):
            path = (root / artifact["path"]).resolve()
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]


def test_tier_executor_stops_after_unrestored_candidate_mutation(monkeypatch, tmp_path):
    from argparse import Namespace

    from scripts.hil.discovery import Device
    from scripts.hil.storage import write_run

    calls = []

    def uncertain(*args, **kwargs):
        calls.append(args[5])
        return {
            "step_id": args[5],
            "outcome": "inconclusive",
            "state_safe_to_continue": False,
            "reason": "restoration was not authorized; manual inspection required",
        }

    monkeypatch.setattr(cli, "_execute_experimental_output_step", uncertain)
    root = write_run(tmp_path, "stop-after-uncertain", {}, {})
    device = Device(
        device_id="fake-device",
        model="R600",
        hardware=None,
        firmware=None,
        state_entities={},
        entry_state="loaded",
        config_entry_id="fake-entry",
        control_entities={},
    )
    capabilities = {
        "features": {
            name: {
                "physical_presence": "present",
                "candidate_hil_eligible": True,
                "production_authorized": False,
            }
            for name in ("dc_output", "ac_output")
        }
    }
    plan = {
        "requested_tier": "gold",
        "steps": [{"id": "dc_output_cycle"}, {"id": "ac_output_cycle"}],
    }

    assert (
        cli._execute_tier_suite(
            object(),
            Namespace(route="auto", output_root=tmp_path, non_interactive=False),
            device,
            {},
            root,
            "parent-run",
            plan,
            capabilities,
        )
        == 2
    )

    execution = json.loads((root / "tier-execution.json").read_text())
    assert calls == ["dc_output_cycle"]
    assert [step["outcome"] for step in execution["steps"]] == [
        "inconclusive",
        "blocked",
    ]
    assert "not attempted" in execution["steps"][1]["reason"]


def test_tier_executor_skips_absent_and_blocks_unknown_without_ble_write(
    tmp_path,
):
    from argparse import Namespace

    from scripts.hil.discovery import Device
    from scripts.hil.storage import write_run

    class FakeHA:
        def __init__(self):
            self.commands = []

        def websocket(self, commands):
            self.commands.extend(commands)
            raise AssertionError("ineligible controls must not start a GATT trace")

    api = FakeHA()
    device = Device(
        device_id="fake-device",
        model="R1500 LITE",
        hardware="0.1",
        firmware="1.0",
        state_entities={},
        entry_state="loaded",
        config_entry_id="fake-entry",
        control_entities={"light": "switch.light", "ac_output": "switch.ac"},
    )
    root = write_run(tmp_path, "profile-eligibility", {}, {})
    capabilities = {
        "features": {
            "light_output": {"physical_presence": "absent"},
            "ac_output": {"physical_presence": "unknown"},
        }
    }

    result = cli._execute_tier_suite(
        api,
        Namespace(route="auto", output_root=tmp_path, non_interactive=False),
        device,
        {},
        root,
        "parent-run",
        {
            "requested_tier": "silver",
            "steps": [{"id": "light_output_cycle"}, {"id": "ac_output_cycle"}],
        },
        capabilities,
    )

    execution = json.loads((root / "tier-execution.json").read_text())
    assert result == 2
    assert [step["outcome"] for step in execution["steps"]] == [
        "not_applicable",
        "blocked",
    ]
    assert "substantiates physical absence" in execution["steps"][0]["reason"]
    assert "unknown or unreviewed" in execution["steps"][1]["reason"]
    assert api.commands == []


def test_guided_basic_reconnect_rejects_vendor_app_stability(monkeypatch, tmp_path):
    from argparse import Namespace

    from scripts.hil import recovery
    from scripts.hil.discovery import Device
    from scripts.hil.storage import write_run

    class FakeHA:
        origin = "https://example.test"

    def stable_vendor_observation(
        _api, args, _device, output_dir, _run_id, _services, _resume
    ):
        assert args.guided_reconnect_required is True
        (output_dir / "checkpoint.json").write_text(
            json.dumps(
                {
                    "steps": [
                        {"step_id": "baseline", "outcome": "pass"},
                        {
                            "step_id": "operator_action",
                            "outcome": "pass",
                            "action": "vendor-app contention",
                            "operator_confirmed": True,
                            "operator_app_result": "connected",
                        },
                        {
                            "step_id": "recovered_session",
                            "outcome": "pass",
                            "session_generation_before": 4,
                            "session_generation_after": 4,
                            "disconnect_observed": False,
                            "connected": True,
                            "status_fresh": True,
                            "fresh_status_telemetry": True,
                            "route_verified": True,
                            "trace_complete": True,
                            "route": "local_adapter",
                            "ha_observation": "ha_stable",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "trace.json").write_text(
            json.dumps({"complete": True, "observed_route": "local_adapter"}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(recovery, "_execute_recovery_suite", stable_vendor_observation)
    device = Device(
        device_id="fake-device",
        model="R600",
        hardware="0.3",
        firmware="1.0",
        state_entities={},
        entry_state="loaded",
        config_entry_id="fake-entry",
        control_entities={},
    )
    parent = write_run(tmp_path, "guided-reconnect-parent", {}, {})
    result = cli._execute_tier_suite(
        FakeHA(),
        Namespace(route="local_adapter", output_root=tmp_path, non_interactive=False),
        device,
        {},
        parent,
        "0123456789abcdef",
        {"requested_tier": "silver", "steps": [{"id": "basic_reconnect"}]},
        {"features": {}},
    )

    execution = json.loads((parent / "tier-execution.json").read_text())
    step = execution["steps"][0]
    assert result == 2
    assert step["outcome"] == "inconclusive"
    assert "mandatory reconnect requires" in step["reason"]


def test_noninteractive_tier_read_only_failure_records_inconclusive_without_writes(
    monkeypatch, tmp_path
):
    from argparse import Namespace
    from types import SimpleNamespace

    from scripts.hil import outputs
    from scripts.hil.discovery import Device
    from scripts.hil.storage import write_run

    def fake_output(_api, _args, _device, _environment, out, _run_id, _services):
        (out / "checkpoint.json").write_text(
            json.dumps(
                {
                    "steps": [
                        {
                            "outcome": "inconclusive",
                            "reason": "no interactive authorization was given",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (out / "trace.json").write_text("{}", encoding="utf-8")
        return 2

    monkeypatch.setattr(outputs, "_execute_output_suite", fake_output)
    base_services = cli._suite_services()
    monkeypatch.setattr(cli, "_suite_services", lambda: base_services)
    root = write_run(tmp_path, "noninteractive-tier", {}, {})
    device = Device(
        device_id="fake-device",
        model="R600",
        hardware=None,
        firmware=None,
        state_entities={},
        entry_state="loaded",
        config_entry_id="fake-entry",
        control_entities={},
    )
    plan = {
        "requested_tier": "silver",
        "steps": [{"id": "read_only_probe"}, {"id": "dc_output_cycle"}],
    }
    capabilities = {
        "features": {
            "dc_output": {
                "physical_presence": "present",
                "production_authorized": True,
            }
        }
    }

    result = cli._execute_tier_suite(
        SimpleNamespace(origin="https://example.test"),
        Namespace(route="local_adapter", output_root=tmp_path, non_interactive=True),
        device,
        {},
        root,
        "parent-run",
        plan,
        capabilities,
    )

    assert result == 2
    execution = json.loads((root / "tier-execution.json").read_text())
    assert execution["steps"][0]["outcome"] == "inconclusive"
    assert execution["steps"][1]["outcome"] == "inconclusive"
    assert "no interactive authorization" in execution["steps"][1]["reason"]
    assert not list(tmp_path.rglob("frames.jsonl"))


@pytest.mark.parametrize(
    ("answers", "expected", "writes", "failure_mode"),
    [
        (["1", "1", "1", "1", "1"], "pass", [True, False], None),
        (["2"], "skipped", [], None),
        (["1", "2"], "skipped", [], None),
        (["1", "1"], "inconclusive", [], "grant_denied"),
        (["1", "1"], "inconclusive", [True], "disconnect"),
        (["1", "1", "1", "1"], "inconclusive", [True, False], "restore_ambiguous"),
        (["1", "1", "1", "2"], "inconclusive", [True], None),
    ],
)
def test_candidate_tier_step_uses_two_grants_and_separate_restore_consent(
    monkeypatch, tmp_path, answers, expected, writes, failure_mode
):
    from argparse import Namespace
    from contextlib import contextmanager
    from dataclasses import replace

    device = discovery.Device(
        device_id="synthetic-device",
        model="S500",
        hardware="1",
        firmware="1",
        state_entities={},
        entry_state="loaded",
        control_entities={"dc_output": "switch.dc"},
        config_entry_id="synthetic-entry",
    )

    class FakeHA:
        origin = "https://example.test"
        state = "off"
        writes = []
        connected = True

        def hil_capabilities(self, _entry):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": self.connected,
                "status_fresh": True,
                "pending_transaction": False,
                "session_generation": 9,
                "features": {
                    "dc_output": {
                        "physical_presence": "present",
                        "candidate_hil_eligible": True,
                        "production_authorized": False,
                    }
                },
            }

        def control_state(self, _feature, _entity):
            return {"available": True, "state": self.state}

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                return [{"schema_version": 2, "capture_id": "capture-change"}]
            return [
                {
                    "schema_version": 2,
                    "run_id": "0123456789abcdef",
                    "scenario_id": "output_dc_cycle",
                    "capture_id": command["capture_id"],
                    "requested_route": "auto",
                    "observed_route": "local_adapter",
                    "complete": True,
                    "events": [],
                    "frames": [],
                }
            ]

        @contextmanager
        def websocket_session(self):
            yield self

        def experimental_output(
            self,
            _entry,
            _feature,
            target,
            *,
            authorization_confirmed,
            execution_confirmed,
        ):
            if failure_mode == "grant_denied":
                raise api.HAAPIError("synthetic expired grant")
            assert authorization_confirmed is True
            assert execution_confirmed is True
            self.writes.append(target)
            if failure_mode == "disconnect":
                self.connected = False
            elif failure_mode != "restore_ambiguous" or target:
                self.state = "on" if target else "off"
            if failure_mode == "restore_ambiguous" and not target:
                return {
                    "outcome": "inconclusive",
                    "confirmed": False,
                    "route": "local_adapter",
                    "session_generation": 9,
                }
            return {
                "outcome": "confirmed",
                "confirmed": True,
                "route": "local_adapter",
                "session_generation": 9,
            }

    fake = FakeHA()
    prompt_answers = iter(answers)
    services = replace(
        cli._suite_services(),
        prompt=lambda _question: next(prompt_answers),
        validate_trace_result=lambda *_args, **_kwargs: None,
        correlate_transaction=lambda *_args, **_kwargs: {
            "session_generation": 9,
            "kind": "output",
        },
    )
    result = cli._execute_experimental_output_step(
        fake,
        Namespace(route="auto", non_interactive=False),
        device,
        tmp_path,
        "synthetic-run",
        "dc_output_cycle",
        "dc_output",
        services,
    )
    assert result["outcome"] == expected
    assert fake.writes == writes
    if writes:
        evidence = json.loads((tmp_path / "tier-dc_output_cycle.json").read_text())
        if expected == "pass":
            assert len(evidence["actions"]) == 2
            assert all(action["confirmed"] for action in evidence["actions"])
            assert evidence["physical_change_outcome"] == "correct"
            assert evidence["physical_restore_outcome"] == "correct"
        else:
            assert evidence["outcome"] == "inconclusive"
            assert "physical_outcome" not in evidence


def test_tier_executor_dispatches_candidate_through_grant_and_restore(
    monkeypatch, tmp_path
):
    """Exercise tier dispatch through the candidate HIL executor with Fake HA."""
    from argparse import Namespace
    from contextlib import contextmanager
    from dataclasses import replace

    device = discovery.Device(
        device_id="synthetic-device",
        model="S500",
        hardware="1",
        firmware="1",
        state_entities={},
        entry_state="loaded",
        control_entities={"dc_output": "switch.dc"},
        config_entry_id="synthetic-entry",
    )

    class FakeHA:
        origin = "https://example.test"

        def __init__(self):
            self.state = "off"
            self.writes = []

        def hil_capabilities(self, _entry):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "pending_transaction": False,
                "session_generation": 9,
                "features": {
                    "dc_output": {
                        "physical_presence": "present",
                        "candidate_hil_eligible": True,
                        "production_authorized": False,
                    }
                },
            }

        def control_state(self, _feature, _entity):
            return {"available": True, "state": self.state}

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.capture_id = "capture-guided"
                return [{"schema_version": 2, "capture_id": self.capture_id}]
            return [
                {
                    "schema_version": 2,
                    "run_id": "parent-run",
                    "scenario_id": "output_dc_cycle",
                    "capture_id": command["capture_id"],
                    "requested_route": "auto",
                    "observed_route": "local_adapter",
                    "complete": True,
                    "events": [],
                    "frames": [],
                }
            ]

        @contextmanager
        def websocket_session(self):
            yield self

        def experimental_output(
            self,
            _entry,
            _feature,
            target,
            *,
            authorization_confirmed,
            execution_confirmed,
        ):
            assert authorization_confirmed is True
            assert execution_confirmed is True
            self.writes.append(target)
            self.state = "on" if target else "off"
            return {
                "outcome": "confirmed",
                "confirmed": True,
                "route": "local_adapter",
                "session_generation": 9,
            }

    fake = FakeHA()
    answers = iter(["1", "1", "1", "1", "1"])
    base_services = cli._suite_services()
    monkeypatch.setattr(
        cli,
        "_suite_services",
        lambda: replace(
            base_services,
            prompt=lambda _question: next(answers),
            validate_trace_result=lambda *_args, **_kwargs: None,
            correlate_transaction=lambda *_args, **_kwargs: {
                "session_generation": 9,
                "kind": "output",
            },
        ),
    )
    root = storage.write_run(tmp_path, "guided-parent", {}, {})
    plan = {"requested_tier": "silver", "steps": [{"id": "dc_output_cycle"}]}
    capabilities = {
        "features": {
            "dc_output": {
                "physical_presence": "present",
                "candidate_hil_eligible": True,
                "production_authorized": False,
            }
        }
    }

    assert (
        cli._execute_tier_suite(
            fake,
            Namespace(route="auto", output_root=tmp_path, non_interactive=False),
            device,
            {},
            root,
            "parent-run",
            plan,
            capabilities,
        )
        == 0
    )
    assert fake.writes == [True, False]
    execution = json.loads((root / "tier-execution.json").read_text())
    assert execution["review_state"] == "pending"
    assert execution["steps"][0]["outcome"] == "pass"
    artifact = root / execution["steps"][0]["machine_evidence"][0]["path"]
    evidence = json.loads(artifact.read_text())
    assert evidence["outcome"] == "pass"
    assert [action["route"] for action in evidence["actions"]] == [
        "local_adapter",
        "local_adapter",
    ]
    for action in evidence["actions"]:
        assert action["trace_complete"] is True
        for trace_artifact in action["trace_evidence"]:
            trace_path = root / trace_artifact["path"]
            assert (
                hashlib.sha256(trace_path.read_bytes()).hexdigest()
                == trace_artifact["sha256"]
            )


def test_discovery_without_optional_config_entries(allpowers_registry):
    entities, devices, _ = allpowers_registry
    choices = discovery.find_devices(entities, devices, None)
    assert [device.model for device in choices] == ["R600"]


def test_discovery_ignores_registry_entries_with_contradictory_ownership(
    allpowers_registry,
):
    entities, devices, _ = allpowers_registry
    wrong_entry = [{"entry_id": "foreign-entry", "domain": "allpowers_ble"}]
    assert discovery.find_devices(entities, devices, wrong_entry) == []


def test_discovery_ignores_disabled_ambiguous_and_spoofed_entities(allpowers_registry):
    entities, devices, entries = allpowers_registry
    entities = list(entities)
    entities.append(
        {
            "platform": "allpowers_ble",
            "device_id": "secret-device-id-012345",
            "entity_id": "sensor.duplicate_battery",
            "unique_id": "another_battery",
            "disabled_by": None,
        }
    )
    entities.append(
        {
            "platform": "allpowers_ble",
            "device_id": "secret-device-id-012345",
            "entity_id": "sensor.disabled_input_power",
            "unique_id": "another_input_power",
            "disabled_by": "integration",
        }
    )
    choices = discovery.find_devices(entities, devices, entries)
    assert "battery" not in choices[0].state_entities
    assert "input_power" not in choices[0].state_entities


def test_unknown_models_and_firmware_never_reveal_device_supplied_strings():
    assert discovery.safe_model("R600 AA:BB:CC:DD:EE:FF") == "unknown"
    assert discovery.safe_version("0.2 secret-name") is None
    assert discovery.safe_model("S300") == "S300"
    assert discovery.safe_version("0.2") == "0.2"
    assert discovery.safe_version("0xF1") == "0xF1"
    assert discovery.safe_version("0x03") == "0x03"
    assert discovery.safe_version("0Xf1") == "0xF1"
    assert discovery.safe_version("0xF1 secret") is None
    assert discovery.safe_version("0xAABBCCDD") is None
    assert discovery.safe_version("0x") is None
    assert discovery.safe_version("0xGG") is None


def test_multiple_device_selection_requires_index_in_noninteractive_mode():
    choices = [
        discovery.Device("secret-id-1", "R600", "0.3", None, {}),
        discovery.Device("secret-id-2", "R600", "0.3", None, {}),
    ]
    with pytest.raises(api.HAAPIError, match="Multiple devices"):
        discovery.choose_device(choices, None, non_interactive=True)
    with pytest.raises(api.HAAPIError, match="ambiguous"):
        discovery.choose_device(choices, "R600", non_interactive=True)
    assert discovery.choose_device(choices, "2", non_interactive=True) is choices[1]
    with pytest.raises(api.HAAPIError):
        discovery.choose_device([], "1", non_interactive=True)


def test_compatibility_version_is_explicit():
    environment.check_ha_version("2026.10.4")
    with pytest.raises(api.HAAPIError, match="Unsupported"):
        environment.check_ha_version("2026.7.4")
    with pytest.raises(api.HAAPIError, match="verify"):
        environment.check_ha_version(None)
    with pytest.raises(api.HAAPIError, match="Unsupported"):
        environment.check_ha_version("untrusted")


def test_public_environment_allowlists_ha_config_and_labels():
    payload = environment.build_environment(
        {
            "version": "2026.10.4",
            "location_name": "private home",
            "longitude": -0.3763,
            "latitude": 39.4699,
            "time_zone": "private-zone",
            "installation_type": "Home Assistant Container",
        },
        {"version": "0.3.0", "secret": "AA:BB:CC:DD:EE:FF"},
    )
    public = json.dumps(payload)
    assert "private" not in public
    assert "39.4699" not in public
    assert "AA:BB" not in public
    assert (
        payload["home_assistant"]["installation_type"]["value"]
        == "Home Assistant Container"
    )
    assert payload["integration"]["qualification_allowed"] is False
    assert payload["home_assistant"]["host"]["bluez_version"]["value"] is None
    assert "runner_host" in payload


def test_manifest_version_mismatch_does_not_claim_fingerprint_proof(monkeypatch):
    monkeypatch.setattr(environment, "local_manifest_version", lambda: "0.3.0")
    monkeypatch.setattr(
        environment, "local_commit", lambda: environment.unknown("test")
    )
    mismatch = environment.integration_build({"version": "1.2.0"})
    assert mismatch["build_match"] == "unverified"
    assert mismatch["qualification_allowed"] is False
    same_version = environment.integration_build({"version": "0.3.0"})
    assert same_version["build_match"] == "unverified"
    assert same_version["qualification_allowed"] is False
    assert environment.integration_build(None)["installed_commit"]["value"] is None


def test_integration_fingerprint_is_deterministic_and_ignores_generated_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    package = tmp_path / "custom_components" / "allpowers_ble"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text('{"version":"0.3.0"}')
    (package / "module.py").write_text("VALUE = 1")
    first = environment.local_integration_fingerprint()
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "module.cpython.pyc").write_bytes(b"generated")
    assert environment.local_integration_fingerprint() == first
    (package / "module.py").write_text("VALUE = 2")
    assert environment.local_integration_fingerprint() != first


def test_fingerprint_match_requires_clean_checkout_and_commit(monkeypatch):
    fingerprint = "a" * 64
    monkeypatch.setattr(
        environment, "_local_integration_snapshot", lambda: (fingerprint, [])
    )
    monkeypatch.setattr(environment, "_checkout_clean", lambda paths: True)
    monkeypatch.setattr(environment, "local_manifest_version", lambda: "0.3.0")
    monkeypatch.setattr(
        environment,
        "local_commit",
        lambda: environment.observed("b" * 40, "runner_git_checkout"),
    )
    verified = environment.integration_build({"version": "0.3.0"}, fingerprint)
    assert verified["build_match"] == "verified"
    assert verified["installed_commit"]["value"] == "b" * 40
    assert (
        environment.integration_build({"version": "0.3.0"}, "c" * 64)["build_match"]
        == "mismatch"
    )
    monkeypatch.setattr(environment, "_checkout_clean", lambda paths: False)
    dirty = environment.integration_build({"version": "0.3.0"}, fingerprint)
    assert dirty["build_match"] == "unverified"
    assert dirty["installed_commit"]["value"] is None
    assert "dirty" in dirty["verification_reason"]
    monkeypatch.setattr(environment, "_checkout_clean", lambda paths: True)
    monkeypatch.setattr(
        environment, "local_commit", lambda: environment.unknown("git unavailable")
    )
    missing_sha = environment.integration_build({"version": "0.3.0"}, fingerprint)
    assert missing_sha["build_match"] == "unverified"
    assert missing_sha["installed_commit"]["value"] is None
    assert (
        missing_sha["verification_reason"]
        == "checkout Git commit unavailable or invalid"
    )


def test_fingerprint_api_fails_closed_for_unsupported_or_invalid_response(monkeypatch):
    client = api.HAClient("https://example.test", "token")
    monkeypatch.setattr(
        client,
        "websocket",
        lambda commands: [{"schema_version": 1, "fingerprint": "a" * 64}],
    )
    assert client.integration_build_fingerprint() == "a" * 64
    monkeypatch.setattr(
        client,
        "websocket",
        lambda commands: [{"schema_version": 1, "fingerprint": "not-a-digest"}],
    )
    assert client.integration_build_fingerprint() is None
    monkeypatch.setattr(
        client,
        "websocket",
        lambda commands: [
            {"schema_version": 1, "fingerprint": "a" * 64, "path": "/config"}
        ],
    )
    assert client.integration_build_fingerprint() is None

    def unsupported(commands):
        raise api.HAAPIError("unsupported")

    monkeypatch.setattr(client, "websocket", unsupported)
    assert client.integration_build_fingerprint() is None


def test_checkout_clean_requires_exact_tracked_integration_subtree(
    monkeypatch, tmp_path
):
    root = tmp_path / "repo"
    package = root / "custom_components" / "allpowers_ble"
    package.mkdir(parents=True)
    module = package / "module.py"
    manifest = package / "manifest.json"
    module.write_text("VALUE = 1\n")
    manifest.write_text('{"version":"0.3.0"}')
    (root / ".gitignore").write_text("custom_components/allpowers_ble/*.ignored\n")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=test@example.test",
            "add",
            ".",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    monkeypatch.setattr(environment, "ROOT", root)
    files = [module, manifest]
    assert environment._checkout_clean(files) is True

    extra = package / "untracked.py"
    extra.write_text("EXTRA = True\n")
    assert environment._checkout_clean([*files, extra]) is False
    extra.unlink()

    ignored = package / "generated.ignored"
    ignored.write_text("LOADABLE = True\n")
    assert environment._checkout_clean([*files, ignored]) is False
    ignored.unlink()

    module.write_text("VALUE = 2\n")
    assert environment._checkout_clean(files) is False
    module.write_text("VALUE = 1\n")
    manifest.unlink()
    assert environment._checkout_clean([module]) is False


def test_read_only_snapshot_excludes_rest_attributes_and_arbitrary_states(
    allpowers_registry,
):
    fake = FakeHA(allpowers_registry)
    chosen = discovery.find_devices(*allpowers_registry)[0]
    sample = environment.read_only_sample(fake, chosen)
    assert sample["battery"]["value"] == 76
    assert sample["firmware_version"]["value"] == "0.2"
    assert "secret" not in json.dumps(sample)
    assert sample["output_power"]["value"] is None
    assert environment.safe_state_value("battery", "my secret") is None
    assert environment.safe_state_value("connected", "another") is None


def test_doctor_performs_read_only_checks_and_writes_nothing(
    monkeypatch, capsys, tmp_path, allpowers_registry
):
    fake = FakeHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SECRET_BEARER_TOKEN")
    args = make_args(
        "doctor",
        "--ha-url",
        "https://example.test",
        "--non-interactive",
        "--output-root",
        str(tmp_path / "no-results"),
    )
    assert cli.execute(args) == 0
    captured = capsys.readouterr().out
    assert "ALLPOWERS devices: 1" in captured
    assert "SECRET_BEARER_TOKEN" not in captured
    assert "secret-device-id" not in captured
    assert not (tmp_path / "no-results").exists()
    assert all(call[0] in {"get", "ws"} for call in fake.calls)


def test_doctor_prints_verified_installed_commit(
    monkeypatch, capsys, allpowers_registry
):
    fingerprint = "a" * 64
    commit = "b" * 40
    fake = FakeHA(allpowers_registry)
    fake.integration_build_fingerprint = lambda: fingerprint
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        environment, "_local_integration_snapshot", lambda: (fingerprint, [])
    )
    monkeypatch.setattr(environment, "_checkout_clean", lambda paths: True)
    monkeypatch.setattr(
        environment,
        "local_commit",
        lambda: environment.observed(commit, "runner_git_checkout"),
    )
    monkeypatch.setenv("HA_HIL_TOKEN", "test-token")

    assert (
        cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"])
        == 0
    )
    output = capsys.readouterr().out
    assert "Installed integration build: verified" in output
    assert f"Installed integration commit: {commit}" in output


def test_run_writes_private_allowlisted_json_without_identifiers(
    monkeypatch, tmp_path, capsys, allpowers_registry
):
    fake = FakeHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "MY_PRIVATE_TOKEN")
    args = make_args(
        "run",
        "--ha-url",
        "https://my-secret-host.local:8123",
        "--device",
        "1",
        "--route",
        "active_proxy",
        "--non-interactive",
        "--output-root",
        str(tmp_path / "results"),
    )
    assert cli.execute(args) == 0
    dirs = list((tmp_path / "results").iterdir())
    assert len(dirs) == 1
    assert {p.name for p in dirs[0].iterdir()} == {"private", "public"}
    private = dirs[0] / "private"
    public_files = {p.name for p in private.iterdir()}
    assert public_files == {"session.json", "environment.json"}
    data = json.loads((private / "session.json").read_text())
    content = " ".join(p.read_text() for p in private.iterdir())
    for secret in (
        "MY_PRIVATE_TOKEN",
        "my-secret-host",
        "AA:BB:CC:DD:EE:FF",
        "secret-entry-1",
        "secret-device-id",
        "sensor.my_name",
        "39.4699",
        "10.0.0.100",
        "My very private",
    ):
        assert secret not in content
    assert data["route"]["requested"] == "active_proxy"
    assert data["route"]["observed"]["value"] is None
    assert data["qualification_status"] == "inconclusive"
    assert data["integration_build"]["qualification_allowed"] is False
    assert data["device"]["integration_entry_state"]["value"] == "loaded"
    assert "secret-entry-1" not in content
    assert re.fullmatch(r"[a-f0-9]{16}", data["run_id"])
    assert "MY_PRIVATE_TOKEN" not in capsys.readouterr().out
    assert (dirs[0].stat().st_mode & 0o077) == 0
    for file in dirs[0].iterdir():
        assert (file.stat().st_mode & 0o077) == 0


def test_run_no_device_and_noninteractive_missing_token_fail_safely(
    monkeypatch, capsys, allpowers_registry
):
    fake = FakeHA(([], [], []))
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SECRET_VALUE")
    assert (
        cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"])
        == 2
    )
    assert (
        cli.main(["run", "--ha-url", "https://example.test", "--non-interactive"]) == 2
    )
    assert "SECRET_VALUE" not in capsys.readouterr().err
    monkeypatch.delenv("HA_HIL_TOKEN")
    assert (
        cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"])
        == 2
    )


def test_output_symlink_rejected(tmp_path):
    destination = tmp_path / "real"
    destination.mkdir()
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(api.HAAPIError, match="symlink"):
        storage.write_run(link, "2026_test", {}, {})


def test_bounded_rest_handles_redirect_http_error_and_invalid_json(monkeypatch):
    client = api.HAClient("https://example.test", "SECRET_TOKEN")

    class FakeOpener:
        def open(self, req, timeout):
            assert req.get_header("Authorization") == "Bearer SECRET_TOKEN"
            raise HTTPError(req.full_url, 302, "Location: secret", {}, None)

    monkeypatch.setattr(api.request, "build_opener", lambda *args: FakeOpener())
    with pytest.raises(api.HAAPIError, match="HTTP 302") as exc:
        client.config()
    assert "SECRET_TOKEN" not in str(exc.value)
    assert "example.test" not in str(exc.value)
    with pytest.raises(api.HAAPIError):
        api._load_json(b"not-json", "REST")
    with pytest.raises(api.HAAPIError):
        api._json_object([], "REST")


def test_websocket_auth_and_command_protocol_never_echo_sensitive_error(monkeypatch):
    sent = []

    class FakeWS:
        def __init__(self, replies):
            self.replies = iter(replies)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def recv(self, timeout):
            return json.dumps(next(self.replies))

        def send(self, message):
            sent.append(json.loads(message))

    replies = [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True, "result": [1, 2]},
    ]
    # Tests must not depend on optional HIL requirements being installed in CI.
    websocket_package = ModuleType("websockets")
    websocket_package.__path__ = []
    exceptions_module = ModuleType("websockets.exceptions")
    exceptions_module.WebSocketException = type("WebSocketException", (Exception,), {})
    sync_package = ModuleType("websockets.sync")
    sync_package.__path__ = []
    client_module = ModuleType("websockets.sync.client")
    client_module.connect = lambda url, **kwargs: FakeWS(replies)
    for name, module in (
        ("websockets", websocket_package),
        ("websockets.exceptions", exceptions_module),
        ("websockets.sync", sync_package),
        ("websockets.sync.client", client_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    ha = api.HAClient("https://example.test", "PRIVATE_TOKEN")
    assert ha.websocket([{"type": "config/device_registry/list"}]) == [[1, 2]]
    assert sent[0] == {"type": "auth", "access_token": "PRIVATE_TOKEN"}
    assert sent[1]["type"] == "config/device_registry/list"

    denied = [
        {"type": "auth_required"},
        {"type": "auth_invalid", "message": "PRIVATE_TOKEN"},
    ]
    client_module.connect = lambda url, **kwargs: FakeWS(denied)
    with pytest.raises(api.HAAPIError, match="authentication failed") as exc:
        ha.websocket([{"type": "manifest/get"}])
    assert "PRIVATE_TOKEN" not in str(exc.value)


@pytest.mark.parametrize(
    ("outcome", "confirmed"),
    [("confirmed", True), ("unchanged", False), ("inconclusive", False)],
)
def test_experimental_write_sequence_reuses_one_authenticated_socket(
    monkeypatch, outcome, confirmed
):
    sent = []
    opened = []
    receive_timeouts = []

    class PersistentWS:
        def __init__(self):
            self.replies = iter(
                [
                    {"type": "auth_required"},
                    {"type": "auth_ok"},
                    {
                        "id": 1,
                        "type": "result",
                        "success": True,
                        "result": {"schema_version": 1, "token": "opaque-token"},
                    },
                    {
                        "id": 2,
                        "type": "result",
                        "success": True,
                        "result": {
                            "schema_version": 1,
                            "outcome": outcome,
                            "confirmed": confirmed,
                        },
                    },
                    {
                        "id": 3,
                        "type": "result",
                        "success": False,
                        "error": {"code": "invalid_grant"},
                    },
                ]
            )

        def recv(self, timeout):
            receive_timeouts.append(timeout)
            return json.dumps(next(self.replies))

        def send(self, message):
            sent.append(json.loads(message))

        def close(self):
            pass

    def connect(url, **kwargs):
        opened.append((url, kwargs))
        return PersistentWS()

    websocket_package = ModuleType("websockets")
    websocket_package.__path__ = []
    sync_package = ModuleType("websockets.sync")
    sync_package.__path__ = []
    client_module = ModuleType("websockets.sync.client")
    client_module.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", websocket_package)
    monkeypatch.setitem(sys.modules, "websockets.sync", sync_package)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client_module)
    ha = api.HAClient("https://example.test", "PRIVATE_TOKEN")
    with ha.websocket_session() as session:
        result = session.experimental_output(
            "entry-a",
            "dc_output",
            True,
            authorization_confirmed=True,
            execution_confirmed=True,
        )
    assert result == {
        "schema_version": 1,
        "outcome": outcome,
        "confirmed": confirmed,
    }
    assert len(opened) == 1
    assert [command["id"] for command in sent[1:]] == [1, 2, 3]
    assert [command["type"] for command in sent[1:]] == [
        "allpowers_ble/hil/write/authorize",
        "allpowers_ble/hil/write/execute",
        "allpowers_ble/hil/write/revoke",
    ]
    assert sent[0] == {"type": "auth", "access_token": "PRIVATE_TOKEN"}
    assert receive_timeouts[3] >= 327.0


def test_persistent_websocket_normalizes_transport_exception(monkeypatch):
    websocket_package = ModuleType("websockets")
    websocket_package.__path__ = []
    exceptions_module = ModuleType("websockets.exceptions")
    websocket_error = type("WebSocketException", (Exception,), {})
    exceptions_module.WebSocketException = websocket_error
    monkeypatch.setitem(sys.modules, "websockets", websocket_package)
    monkeypatch.setitem(sys.modules, "websockets.exceptions", exceptions_module)

    class BrokenSocket:
        def send(self, _message):
            pass

        def recv(self, *, timeout):
            raise websocket_error("synthetic transport failure")

    session = api.AuthenticatedWebSocketSession(
        api.HAClient("https://example.test", "PRIVATE_TOKEN")
    )
    session._socket = BrokenSocket()
    with pytest.raises(api.HAAPIError, match="WebSocket request failed"):
        session.command({"type": "allpowers_ble/hil/write/execute"})


@pytest.mark.parametrize("schema_version", [2, None])
def test_authorize_response_schema_mismatch_never_executes(monkeypatch, schema_version):
    sent = []

    class PersistentWS:
        def __init__(self):
            self.replies = iter(
                [
                    {"type": "auth_required"},
                    {"type": "auth_ok"},
                    {
                        "id": 1,
                        "type": "result",
                        "success": True,
                        "result": {
                            "schema_version": schema_version,
                            "token": "opaque-token",
                        },
                    },
                    {
                        "id": 2,
                        "type": "result",
                        "success": True,
                        "result": {"revoked": True},
                    },
                ]
            )

        def recv(self, timeout):
            del timeout
            return json.dumps(next(self.replies))

        def send(self, message):
            sent.append(json.loads(message))

        def close(self):
            pass

    websocket_package = ModuleType("websockets")
    websocket_package.__path__ = []
    sync_package = ModuleType("websockets.sync")
    sync_package.__path__ = []
    client_module = ModuleType("websockets.sync.client")
    client_module.connect = lambda *_args, **_kwargs: PersistentWS()
    monkeypatch.setitem(sys.modules, "websockets", websocket_package)
    monkeypatch.setitem(sys.modules, "websockets.sync", sync_package)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client_module)

    ha = api.HAClient("https://example.test", "PRIVATE_TOKEN")
    with ha.websocket_session() as session:
        with pytest.raises(api.HAAPIError, match="invalid HIL grant"):
            session.experimental_output(
                "entry-a",
                "dc_output",
                True,
                authorization_confirmed=True,
                execution_confirmed=True,
            )
    assert [command["type"] for command in sent[1:]] == [
        "allpowers_ble/hil/write/authorize",
        "allpowers_ble/hil/write/revoke",
    ]


def test_mixed_case_https_never_downgrades_websocket(monkeypatch):
    """Regression: validated HTTPS origins must never send a token over ws://."""
    attempted_urls = []

    class FakeWS:
        def __init__(self):
            self.replies = iter([{"type": "auth_required"}, {"type": "auth_ok"}])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def recv(self, timeout):
            return json.dumps(next(self.replies))

        def send(self, message):
            assert json.loads(message)["access_token"] == "SECRET_TOKEN"

    websocket_package = ModuleType("websockets")
    websocket_package.__path__ = []
    exceptions_module = ModuleType("websockets.exceptions")
    exceptions_module.WebSocketException = type("WebSocketException", (Exception,), {})
    sync_package = ModuleType("websockets.sync")
    sync_package.__path__ = []
    client_module = ModuleType("websockets.sync.client")

    def fake_connect(url, **kwargs):
        attempted_urls.append(url)
        return FakeWS()

    client_module.connect = fake_connect
    for name, module in (
        ("websockets", websocket_package),
        ("websockets.exceptions", exceptions_module),
        ("websockets.sync", sync_package),
        ("websockets.sync.client", client_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    for origin in (
        "HTTPS://example.test:8123",
        "HtTpS://example.test:8123",
        "https://example.test:8123",
    ):
        ha = api.HAClient(origin, "SECRET_TOKEN")
        assert ha.websocket([]) == []
        assert attempted_urls[-1] == "wss://example.test:8123/api/websocket"
    ha = api.HAClient("http://127.0.0.1:8123", "SECRET_TOKEN")
    assert ha.websocket([]) == []
    assert attempted_urls[-1] == "ws://127.0.0.1:8123/api/websocket"


@pytest.mark.parametrize(
    ("state", "expected_exit"),
    [
        ("loaded", 0),
        ("not_loaded", 2),
        ("setup_error", 2),
        ("setup_retry", 2),
        ("migration_error", 2),
        ("setup_in_progress", 2),
        ("unload_in_progress", 2),
        ("future_unknown_state", 2),
        (None, 2),
    ],
)
def test_doctor_reports_actual_config_entry_readiness(
    monkeypatch, capsys, allpowers_registry, state, expected_exit
):
    """A stale device-registry row cannot qualify as a running integration."""
    fake = FakeHA(allpowers_registry)
    fake.entries[0]["state"] = state
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SECRET_VALUE")
    assert (
        cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"])
        == expected_exit
    )
    output = capsys.readouterr().out
    if state in discovery.CONFIG_ENTRY_STATES:
        assert state in output
    else:
        assert "unverified" in output
    assert "SECRET_VALUE" not in output
    assert "secret-entry-1" not in output
    if expected_exit:
        assert "Preflight inconclusive or incomplete" in output
    else:
        assert "Preflight ready" in output


def test_doctor_missing_entry_permissions_remains_unknown_but_run_is_available(
    monkeypatch, tmp_path, capsys, allpowers_registry
):
    """Missing optional entry data must not block a read-only snapshot."""
    entities, devices, _ = allpowers_registry
    fake = FakeHA((entities, devices, None))
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SECRET_VALUE")
    args = ["--ha-url", "https://example.test", "--non-interactive"]
    assert cli.main(["doctor", *args]) == 2
    assert cli.main(["run", *args, "--output-root", str(tmp_path)]) == 0
    sessions = list(tmp_path.glob("*/private/session.json"))
    assert len(sessions) == 1
    state = json.loads(sessions[0].read_text())["device"]["integration_entry_state"]
    assert state["value"] is None
    assert state["verified"] is False
    assert "SECRET_VALUE" not in capsys.readouterr().out


def test_doctor_detects_disabled_entries(monkeypatch, capsys, allpowers_registry):
    fake = FakeHA(allpowers_registry)
    fake.entries[0]["disabled_by"] = "user"
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "SECRET_VALUE")
    assert cli.main(["doctor", "--ha-url", "https://example.test"]) == 2
    output = capsys.readouterr().out
    assert "integration disabled" in output
    assert "SECRET_VALUE" not in output


def test_hex_firmware_in_registry_and_telemetry_is_preserved(allpowers_registry):
    entities, devices, entries = allpowers_registry
    devices[0]["sw_version"] = "0xF1"
    devices[0]["hw_version"] = "0x03"
    selected = discovery.find_devices(entities, devices, entries)[0]
    assert selected.firmware == "0xF1"
    assert selected.hardware == "0x03"
    assert environment.safe_state_value("firmware_version", "0xF1") == "0xF1"
    assert environment.safe_state_value("hardware_version", "0x03") == "0x03"


def test_ws_unauthorized_manifest_keeps_device_registry(monkeypatch):
    ha = api.HAClient("https://example.test", "SECRET")

    def mock_websocket(commands):
        if commands[0]["type"] == "config/entity_registry/list":
            return [[], []]
        raise api.HAAPIError("Home Assistant WebSocket command rejected (unauthorized)")

    monkeypatch.setattr(ha, "websocket", mock_websocket)
    assert ha.discovery() == ([], [], None, None)


def test_json_storage_excludes_internal_ids_when_public_types_are_used(
    tmp_path, allpowers_registry
):
    device = discovery.find_devices(*allpowers_registry)[0]
    env = environment.build_environment({"version": "2026.10.4"}, {"version": "0.3.0"})
    session = environment.build_session(
        run_id="0123456789abcdef",
        requested_route="auto",
        device=device,
        environment=env,
        sample={},
    )
    folder = storage.write_run(tmp_path, "safe", env, session)
    saved = (folder / "session.json").read_text()
    assert "secret-device-id" not in saved
    assert "AA:BB" not in saved
    assert session["route"]["qualification_allowed"] is False


def test_checkpoint_round_trip_is_versioned_private_and_run_bound(tmp_path):
    run_id = "0123456789abcdef"
    folder = storage.write_run(
        tmp_path, "resume-safe", {"ok": True}, {"run_id": run_id}
    )
    fingerprint = "a" * 64
    steps = [{"step_id": "baseline", "outcome": "pass"}]
    storage.write_scenario_progress(
        folder, run_id, "recovery_reconnect", fingerprint, steps
    )

    loaded_dir, checkpoint = storage.load_scenario_checkpoint(folder)

    assert loaded_dir == folder
    assert checkpoint["schema_version"] == 2
    assert checkpoint["scenario_id"] == "recovery_reconnect"
    assert checkpoint["device_fingerprint"] == fingerprint
    assert checkpoint["steps"] == steps
    assert (folder / "checkpoint.json").stat().st_mode & 0o077 == 0


def test_checkpoint_loader_rejects_symlinked_checkpoint(tmp_path):
    run_id = "0123456789abcdef"
    folder = storage.write_run(tmp_path, "resume-link", {}, {"run_id": run_id})
    storage.write_scenario_progress(folder, run_id, "recovery_reconnect", "b" * 64, [])
    saved = folder / "checkpoint.saved"
    (folder / "checkpoint.json").rename(saved)
    (folder / "checkpoint.json").symlink_to(saved.name)

    with pytest.raises(api.HAAPIError, match="private regular files"):
        storage.load_scenario_checkpoint(folder)


def test_checkpoint_loader_rejects_legacy_schema(tmp_path):
    run_id = "0123456789abcdef"
    folder = storage.write_run(tmp_path, "resume-old", {}, {"run_id": run_id})
    storage.write_scenario_progress(folder, run_id, "recovery_reconnect", "c" * 64, [])
    payload = json.loads((folder / "checkpoint.json").read_text())
    payload["schema_version"] = 1
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    (folder / "checkpoint.json").write_text(encoded)
    (folder / "scenarios.json").write_text(encoded)

    with pytest.raises(api.HAAPIError, match="schema or run identity"):
        storage.load_scenario_checkpoint(folder)


def test_output_suite_checkpoint_cannot_be_resumed():
    args = cli.make_parser().parse_args(
        ["run", "--suite", "outputs", "--resume", "/tmp/not-read"]
    )

    with pytest.raises(api.HAAPIError, match="scenario cannot be resumed"):
        cli.execute(args)


@pytest.mark.parametrize(
    ("suite", "scenario_id", "requested_action"),
    [
        ("outputs", "output_dc_cycle", "switch"),
        ("outputs", "output_light_cycle", "switch"),
        ("outputs", "output_car_charger_cycle", "switch"),
        ("settings", "settings_eco_mode", "switch"),
        ("settings", "settings_work_mode", "select"),
        ("settings", "settings_eco_timeout", "select"),
    ],
)
def test_scenario_selection_is_a_side_effect_free_dry_run(
    suite, scenario_id, requested_action, capsys
):
    args = cli.make_parser().parse_args(
        ["run", "--suite", suite, "--scenario-id", scenario_id, "--dry-run"]
    )

    assert cli.execute(args) == 0
    output = capsys.readouterr().out
    assert f"{scenario_id} [{suite}]" in output
    assert "ac_on [outputs]" not in output
    assert f"requested actions: {requested_action}" in output


@pytest.mark.parametrize(
    "failure_mode",
    [
        "safe",
        "pending",
        "ambiguous",
        "race_after_cleanup_trace",
        "pending_after_restore",
        "feature_denied_before_restore",
        "feature_denied_at_restore_write",
    ],
)
@pytest.mark.parametrize(
    ("scenario_id", "control_key"),
    [
        ("output_ac_cycle", "ac_output"),
        ("output_car_charger_cycle", "car_charger"),
        ("output_dc_cycle", "dc_output"),
        ("output_light_cycle", "light"),
    ],
)
def test_output_scenario_restoration_requires_fresh_safe_state(
    monkeypatch, tmp_path, failure_mode, scenario_id, control_key
):
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    class RestoreHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.state = "off"
            self.starts = 0
            self.capability_calls = 0
            self.actions = []
            self.pending = False
            self.off_attempts = 0

        def hil_capabilities(self, entry_id):
            self.capability_calls += 1
            result = {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": self.pending,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 7,
                "capabilities": {
                    "write_output_controls": True,
                    "write_settings_controls": True,
                    "car_charger_enabled": True,
                },
            }
            if failure_mode in {
                "feature_denied_before_restore",
                "feature_denied_at_restore_write",
            }:
                denied_call = (
                    5 if failure_mode == "feature_denied_before_restore" else 6
                )
                result.update(
                    {
                        "schema_version": 2,
                        "features": {
                            "light_output" if control_key == "light" else control_key: {
                                "production_authorized": self.capability_calls
                                < denied_call,
                                "physical_presence": "present",
                                "candidate_hil_eligible": False,
                                "runtime_ready": True,
                            }
                        },
                    }
                )
            return result

        def control_state(self, key, entity_id):
            return {"available": True, "state": self.state}

        def call_control(self, key, entity_id, value):
            self.actions.append(value)
            if value == "off":
                self.off_attempts += 1
                if (
                    failure_mode
                    in {
                        "ambiguous",
                        "race_after_cleanup_trace",
                        "pending_after_restore",
                    }
                    and self.off_attempts == 1
                ):
                    raise cli.HAAPIError("service acknowledgement lost")
            self.state = value
            if value == "off" and failure_mode == "pending_after_restore":
                self.pending = True

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.starts += 1
                if self.starts == 3 and failure_mode == "race_after_cleanup_trace":
                    self.state = "off"
                if self.starts == 2 and failure_mode not in {
                    "ambiguous",
                    "race_after_cleanup_trace",
                    "pending_after_restore",
                }:
                    self.pending = failure_mode == "pending"
                    raise cli.HAAPIError("trace unavailable")
                return [
                    {
                        "schema_version": 2,
                        "capture_id": f"capture-{self.starts}",
                    }
                ]
            return [
                {
                    "schema_version": 2,
                    "capture_id": command["capture_id"],
                    "events": [],
                    "frames": [],
                    "complete": True,
                    "observed_route": "local_adapter",
                }
            ]

    api_fake = RestoreHA()
    output_dir = tmp_path / "restore-run"
    output_dir.mkdir(mode=0o700)
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        control_entities={control_key: f"switch.private_{control_key}"},
        config_entry_id="entry-safe",
    )
    inputs = iter(
        ("1", "1", "1", "3", "1", "1")
        if failure_mode
        in {"ambiguous", "race_after_cleanup_trace", "pending_after_restore"}
        else ("1", "1", "1", "1")
    )
    clock = [10_000.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(cli, "input", lambda _prompt: next(inputs), raising=False)
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "correlate_transaction",
        lambda trace, **kwargs: {"transaction_id": trace["capture_id"]},
    )

    def save_trace(trace_dir, trace):
        (trace_dir / "trace.json").write_text(json.dumps(trace))

    monkeypatch.setattr(cli, "_save_private_trace", save_trace)
    result = cli._execute_output_suite(
        api_fake,
        SimpleNamespace(non_interactive=False, route="auto", scenario_id=scenario_id),
        device,
        {},
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    cleanup = checkpoint["steps"][-1]
    assert result == 2
    if failure_mode == "safe":
        assert cleanup.get("service_attempted") is not True
        assert cleanup["cleanup_attempted"] is True
        assert api_fake.actions == ["on", "off"]
        assert cleanup["cleanup_service_attempted"] is True
        assert cleanup["cleanup_outcome"] == "pass"
        assert cleanup["cleanup_post_state"] == "off"
        assert cleanup["cleanup_trace"] == f"traces/{control_key}_cleanup"
        assert "pre-write trace setup failure" in cleanup["cleanup_reason"]
    elif failure_mode == "pending":
        assert cleanup.get("service_attempted") is not True
        assert cleanup["cleanup_attempted"] is True
        assert api_fake.actions == ["on"]
        assert cleanup.get("cleanup_service_attempted") is not True
        assert cleanup["cleanup_reason"].startswith("fresh same-session")
    elif failure_mode == "ambiguous":
        assert api_fake.actions == ["on", "off", "off"]
        assert cleanup["service_attempted"] is True
        assert cleanup["cleanup_authorized"] is True
        assert cleanup["cleanup_service_attempted"] is True
        assert cleanup["cleanup_outcome"] == "pass"
        assert cleanup["cleanup_post_state"] == "off"
        assert cleanup["outcome"] == "inconclusive"
        assert "after ambiguous write" in cleanup["cleanup_reason"]
    elif failure_mode == "pending_after_restore":
        assert api_fake.actions == ["on", "off", "off"]
        assert cleanup["cleanup_service_attempted"] is True
        assert cleanup["cleanup_post_state"] == "off"
        assert cleanup["cleanup_outcome"] == "inconclusive"
        assert cleanup["cleanup_status"] == "restoration_inconclusive"
    elif failure_mode in {
        "feature_denied_before_restore",
        "feature_denied_at_restore_write",
    }:
        assert api_fake.actions == ["on"]
        assert cleanup.get("cleanup_service_attempted") is not True
        assert cleanup["outcome"] == "inconclusive"
        if failure_mode == "feature_denied_at_restore_write":
            assert cleanup["cleanup_status"] == "restoration_blocked_at_write_boundary"
    else:
        assert api_fake.actions == ["on", "off"]
        assert cleanup["service_attempted"] is True
        assert cleanup["cleanup_authorized"] is True
        assert cleanup.get("cleanup_service_attempted") is not True
        assert cleanup["cleanup_status"] == "restoration_blocked_at_write_boundary"
        assert "after the restoration trace was armed" in cleanup["cleanup_reason"]


def test_car_charger_output_scenario_requires_integration_opt_in(tmp_path):
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    class DisabledCarChargerHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.actions = []
            self.trace_starts = 0

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": False,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 3,
                "capabilities": {
                    "write_output_controls": True,
                    "write_settings_controls": True,
                    "car_charger_enabled": False,
                },
            }

        def control_state(self, key, entity_id):
            return {"available": True, "state": "off"}

        def call_control(self, key, entity_id, value):
            self.actions.append((key, value))

        def websocket(self, commands):
            if commands[0]["type"].endswith("/start"):
                self.trace_starts += 1
            raise AssertionError("a disabled car charger must not start a trace")

    output_dir = tmp_path / "car-disabled"
    output_dir.mkdir(mode=0o700)
    api_fake = DisabledCarChargerHA()
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        control_entities={"car_charger": "switch.private_car_charger"},
        config_entry_id="entry-safe",
    )

    result = cli._execute_output_suite(
        api_fake,
        SimpleNamespace(
            non_interactive=False,
            route="auto",
            scenario_id="output_car_charger_cycle",
        ),
        device,
        {},
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    assert result == 2
    assert api_fake.actions == []
    assert api_fake.trace_starts == 0
    assert checkpoint["steps"][0]["reason"] == (
        "live capability or expected pre-state check failed"
    )


@pytest.mark.parametrize(
    ("scenario_id", "control_key", "initial", "options"),
    [
        ("settings_eco_mode", "eco_mode", "off", ["off", "on"]),
        (
            "settings_work_mode",
            "work_mode",
            "mute",
            ["mute", "standard", "fast"],
        ),
        (
            "settings_eco_timeout",
            "eco_timeout",
            "six_hours",
            ["one_hour", "two_hours", "four_hours", "six_hours"],
        ),
    ],
)
def test_settings_scenarios_restore_original_option_with_fresh_evidence(
    monkeypatch, tmp_path, scenario_id, control_key, initial, options
):
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    class SettingsHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.state = initial
            self.actions = []
            self.capture = 0

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": False,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 9,
                "capabilities": {"write_settings_controls": True},
            }

        def control_state(self, key, entity_id):
            del key, entity_id
            return (
                api.HAClient.control_state(
                    self, control_key, f"switch.private_{control_key}"
                )
                if control_key == "eco_mode"
                else api.HAClient.control_state(
                    self, control_key, f"select.private_{control_key}"
                )
            )

        def entity_state(self, entity_id):
            del entity_id
            attributes = {"options": options} if control_key != "eco_mode" else {}
            return {"state": self.state, "attributes": attributes}

        def call_control(self, key, entity_id, value):
            self.actions.append((key, value))
            self.state = value

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.capture += 1
                return [{"schema_version": 2, "capture_id": f"settings-{self.capture}"}]
            return [
                {
                    "schema_version": 2,
                    "capture_id": command["capture_id"],
                    "events": [],
                    "frames": [],
                    "complete": True,
                    "observed_route": "local_adapter",
                }
            ]

    api_fake = SettingsHA()
    output_dir = tmp_path / scenario_id
    output_dir.mkdir(mode=0o700)
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        control_entities={control_key: f"select.private_{control_key}"},
        config_entry_id="entry-safe",
    )
    inputs = iter(("1", "1", "1", "1"))
    monkeypatch.setattr(cli, "input", lambda _prompt: next(inputs), raising=False)
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "correlate_transaction",
        lambda trace, **kwargs: {"transaction_id": trace["capture_id"]},
    )

    def save_trace(trace_dir, trace):
        (trace_dir / "trace.json").write_text(json.dumps(trace))

    monkeypatch.setattr(cli, "_save_private_trace", save_trace)
    result = cli._execute_settings_suite(
        api_fake,
        SimpleNamespace(non_interactive=False, route="auto", scenario_id=scenario_id),
        device,
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    assert result == 0
    assert api_fake.actions == [
        (
            control_key,
            "on"
            if control_key == "eco_mode"
            else "standard"
            if control_key == "work_mode"
            else "two_hours",
        ),
        (control_key, initial),
    ]
    assert [step["outcome"] for step in checkpoint["steps"]] == ["pass", "pass"]
    assert api_fake.state == initial


@pytest.mark.parametrize(
    ("cleanup_state", "trace_unavailable"),
    [
        ("safe", False),
        ("unchanged", False),
        ("pending", False),
        ("pending_after_restore", False),
        ("generation", False),
        ("race_after_cleanup_trace", False),
        ("safe", True),
        ("feature_denied", False),
    ],
)
def test_ambiguous_settings_write_restores_only_with_fresh_idle_same_session(
    monkeypatch, tmp_path, cleanup_state, trace_unavailable
):
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    class AmbiguousSettingsHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.state = "off"
            self.actions = []
            self.pending = False
            self.generation = 9
            self.captures = 0
            self.capability_calls = 0

        def hil_capabilities(self, entry_id):
            self.capability_calls += 1
            result = {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": self.pending,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": self.generation,
                "capabilities": {"write_settings_controls": True},
            }
            if cleanup_state == "feature_denied":
                result.update(
                    {
                        "schema_version": 2,
                        "features": {
                            "eco_mode": {
                                "production_authorized": self.capability_calls < 5,
                                "physical_presence": "present",
                                "candidate_hil_eligible": False,
                                "runtime_ready": True,
                            }
                        },
                    }
                )
            return result

        def control_state(self, key, entity_id):
            del key, entity_id
            return {
                "available": self.state in {"off", "on"},
                "state": self.state if self.state in {"off", "on"} else None,
                "options": ["off", "on"],
            }

        def call_control(self, key, entity_id, value):
            self.actions.append((key, value))
            if value == "on":
                if cleanup_state == "unchanged":
                    raise cli.HAAPIError("service acknowledgement lost")
                self.state = "on"
                if cleanup_state == "pending":
                    self.pending = True
                elif cleanup_state == "generation":
                    self.generation += 1
                raise cli.HAAPIError("service acknowledgement lost")
            self.state = value
            if value == "off" and cleanup_state == "pending_after_restore":
                self.pending = True

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.captures += 1
                if self.captures == 2 and cleanup_state == "race_after_cleanup_trace":
                    self.state = "off"
                return [
                    {"schema_version": 2, "capture_id": f"ambiguous-{self.captures}"}
                ]
            if trace_unavailable:
                raise cli.HAAPIError("capture could not be finalized")
            return [
                {
                    "schema_version": 2,
                    "capture_id": command["capture_id"],
                    "events": [],
                    "frames": [],
                    "complete": True,
                    "observed_route": "local_adapter",
                }
            ]

    api_fake = AmbiguousSettingsHA()
    output_dir = tmp_path / f"{cleanup_state}-{trace_unavailable}"
    output_dir.mkdir(mode=0o700)
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        control_entities={"eco_mode": "switch.private_eco_mode"},
        config_entry_id="entry-safe",
    )
    answers = iter(("1", "1", "1") if trace_unavailable else ("1", "3", "1", "1"))
    clock = [10_000.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(cli, "input", lambda _prompt: next(answers), raising=False)
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "correlate_transaction",
        lambda trace, **kwargs: {"transaction_id": trace["capture_id"]},
    )
    monkeypatch.setattr(
        cli,
        "_save_private_trace",
        lambda trace_dir, trace: (trace_dir / "trace.json").write_text(
            json.dumps(trace)
        ),
    )

    result = cli._execute_settings_suite(
        api_fake,
        SimpleNamespace(
            non_interactive=False,
            route="auto",
            scenario_id="settings_eco_mode",
        ),
        device,
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    change = checkpoint["steps"][0]
    assert result == 2
    assert change["service_attempted"] is True
    assert change["post_state"] == ("off" if cleanup_state == "unchanged" else "on")
    if cleanup_state == "safe":
        restoration = checkpoint["steps"][1]
        assert api_fake.actions == [("eco_mode", "on"), ("eco_mode", "off")]
        assert change["cleanup_status"] == (
            "restoration_inconclusive" if trace_unavailable else "restoration_confirmed"
        )
        assert restoration["outcome"] == (
            "inconclusive" if trace_unavailable else "pass"
        )
    elif cleanup_state == "feature_denied":
        assert api_fake.actions == [("eco_mode", "on")]
        assert change["cleanup_status"] == "manual_inspection_required"
        assert api_fake.state == "on"
    elif cleanup_state == "race_after_cleanup_trace":
        assert api_fake.actions == [("eco_mode", "on")]
        assert change["cleanup_status"] == "restoration_inconclusive"
        assert checkpoint["steps"][1]["step_id"] == "restore_after_ambiguous_write"
        assert checkpoint["steps"][1].get("service_attempted") is not True
        assert "changed after trace start" in checkpoint["steps"][1]["reason"]
    elif cleanup_state == "pending_after_restore":
        assert api_fake.actions == [("eco_mode", "on"), ("eco_mode", "off")]
        assert change["cleanup_status"] == "restoration_inconclusive"
        assert checkpoint["steps"][1]["outcome"] == "inconclusive"
        assert api_fake.state == "off"
    elif cleanup_state == "unchanged":
        assert api_fake.actions == [("eco_mode", "on")]
        assert change["cleanup_status"] == "original_state_confirmed_no_restore_needed"
        assert api_fake.state == "off"
    else:
        assert api_fake.actions == [("eco_mode", "on")]
        assert change["cleanup_status"] == "manual_inspection_required"
        assert change["cleanup_reason"].startswith("fresh same-session state")


@pytest.mark.parametrize("include_control_frame", [True, False])
@pytest.mark.parametrize("requested_route", ["auto", "active_proxy"])
def test_eco_scenario_requires_real_validated_trace_and_matching_control_frame(
    monkeypatch, tmp_path, include_control_frame, requested_route
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from custom_components.allpowers_ble.protocol import SettingsData, WorkMode
    from custom_components.allpowers_ble.protocol import encode_settings_control
    from scripts.hil.discovery import Device

    class TracedSettingsHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.state = "off"
            self.capture = 0
            self.actions = []
            self.requested_route = requested_route

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": False,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 9,
                "capabilities": {"write_settings_controls": True},
            }

        def entity_state(self, entity_id):
            assert entity_id == "switch.private_eco_mode"
            return {"state": self.state}

        def control_state(self, key, entity_id):
            return api.HAClient.control_state(self, key, entity_id)

        def call_control(self, key, entity_id, value):
            self.actions.append((key, value))
            self.state = value

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.capture += 1
                return [{"schema_version": 2, "capture_id": f"eco-{self.capture}"}]
            return [self.trace(command["capture_id"])]

        def trace(self, capture_id):
            utc = datetime.now(UTC).isoformat()
            transaction_id = f"tx-eco-{self.capture}"
            common = {
                "schema_version": 2,
                "utc": utc,
                "run_id": "0123456789abcdef",
                "scenario_id": "settings_eco_mode",
                "session_generation": 9,
                "layer": "gatt_api",
                "observed_route": "local_adapter",
            }
            route_event = {
                **common,
                "seq": 1,
                "elapsed_ms": 0,
                "event": "route_observed",
                "backend_kind": "bluez",
                "evidence": "bluez_device_path",
            }
            frame = encode_settings_control(
                SettingsData(
                    eco_enabled=self.state == "on",
                    work_mode=WorkMode.MUTE,
                    car_charger_enabled=False,
                    eco_timeout_hours=2,
                    hardware_version="0.3",
                    firmware_version="1.1",
                    raw_flags=0xA1 if self.state == "on" else 0xA0,
                    raw_hardware_version=3,
                    raw_firmware_version=0x11,
                )
            )
            write_event = {
                **common,
                "seq": 2,
                "elapsed_ms": 1,
                "event": "write_attempt",
                "transaction_id": transaction_id,
                "direction": "tx",
            }
            events = [
                route_event,
                write_event,
                {
                    **common,
                    "seq": 3,
                    "elapsed_ms": 2,
                    "event": "transaction_submitted",
                    "transaction_id": transaction_id,
                    "transaction_kind": "settings",
                },
                {
                    **common,
                    "seq": 4,
                    "elapsed_ms": 3,
                    "event": "transaction_confirmed",
                    "transaction_id": transaction_id,
                    "reason": "matching_validated_telemetry",
                },
            ]
            frames = (
                [
                    {
                        **write_event,
                        "hex": frame.hex(),
                        "command_type": "settings_control",
                        "frame_validation": "validated_tx",
                    }
                ]
                if include_control_frame
                else []
            )
            return {
                "schema_version": 2,
                "capture_scope": "runtime_entry",
                "capture_id": capture_id,
                "run_id": "0123456789abcdef",
                "scenario_id": "settings_eco_mode",
                "requested_route": self.requested_route,
                "observed_route": "local_adapter",
                "capture_layer": "gatt_api_not_rf",
                "private_raw_bytes": True,
                "qualification_allowed": False,
                "closed": True,
                "termination_reason": "requested_stop",
                "expired": False,
                "complete": True,
                "dropped_events": 0,
                "dropped_frames": 0,
                "events": events,
                "frames": frames,
                "event_count": len(events),
                "frame_count": len(frames),
                "has_gatt_frames": bool(frames),
                "route_observations": [
                    {
                        "session_generation": 9,
                        "seq": 1,
                        "utc": utc,
                        "elapsed_ms": 0,
                        "observed_route": "local_adapter",
                        "backend_kind": "bluez",
                        "evidence": "bluez_device_path",
                        "adapter_id": "hci0",
                    }
                ],
            }

    api_fake = TracedSettingsHA()
    output_dir = tmp_path / f"real-trace-{include_control_frame}"
    output_dir.mkdir(mode=0o700)
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        control_entities={"eco_mode": "switch.private_eco_mode"},
        config_entry_id="entry-safe",
    )
    answers = iter(("1", "1", "1", "1"))
    monkeypatch.setattr(cli, "input", lambda _prompt: next(answers), raising=False)

    result = cli._execute_settings_suite(
        api_fake,
        SimpleNamespace(
            non_interactive=False,
            route=requested_route,
            scenario_id="settings_eco_mode",
        ),
        device,
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    if include_control_frame and requested_route == "auto":
        assert result == 0
        assert api_fake.actions == [("eco_mode", "on"), ("eco_mode", "off")]
        assert [step["outcome"] for step in checkpoint["steps"]] == ["pass", "pass"]
    else:
        assert result == 2
        assert api_fake.actions == [("eco_mode", "on"), ("eco_mode", "off")]
        assert checkpoint["steps"][0]["outcome"] == "inconclusive"
        assert checkpoint["steps"][0]["cleanup_status"] == ("restoration_inconclusive")
        assert checkpoint["steps"][1]["step_id"] == "restore_after_ambiguous_write"
        assert checkpoint["steps"][1]["outcome"] == "inconclusive"


@pytest.mark.parametrize(
    ("failed_check", "failed_value"),
    [
        ("pending_transaction", True),
        ("settings_fresh", False),
        ("write_settings_controls", False),
        ("feature_denied", True),
        ("feature_absent", True),
        ("feature_not_ready", True),
        ("feature_denied_after_trace", True),
        ("feature_absent_after_trace", True),
        ("feature_not_ready_after_trace", True),
    ],
)
def test_settings_scenario_blocks_before_write_without_fresh_safe_preflight(
    monkeypatch, tmp_path, failed_check, failed_value
):
    from dataclasses import replace
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    base_services = cli._suite_services
    monkeypatch.setattr(
        cli,
        "_suite_services",
        lambda: replace(base_services(), prompt=lambda _message: "1"),
    )

    class BlockedSettingsHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.actions = []
            self.capability_calls = 0

        def hil_capabilities(self, entry_id):
            self.capability_calls += 1
            result = {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": False,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 9,
                "capabilities": {"write_settings_controls": True},
            }
            if failed_check == "write_settings_controls":
                result["capabilities"][failed_check] = failed_value
            elif failed_check.startswith("feature_"):
                result["schema_version"] = 2
                after_trace = failed_check.endswith("_after_trace")
                denied = self.capability_calls >= 2 if after_trace else True
                result["features"] = {
                    "work_mode": {
                        "production_authorized": (
                            not denied or "denied" not in failed_check
                        ),
                        "physical_presence": (
                            "absent"
                            if "absent" in failed_check and denied
                            else "present"
                        ),
                        "candidate_hil_eligible": False,
                        "runtime_ready": (not ("not_ready" in failed_check and denied)),
                    }
                }
            else:
                result[failed_check] = failed_value
            return result

        def control_state(self, key, entity_id):
            return {"available": True, "state": "mute", "options": ["mute", "standard"]}

        def call_control(self, key, entity_id, value):
            self.actions.append((key, value))

    output_dir = tmp_path / failed_check
    output_dir.mkdir(mode=0o700)
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        control_entities={"work_mode": "select.private_mode"},
        config_entry_id="entry-safe",
    )
    api_fake = BlockedSettingsHA()
    result = cli._execute_settings_suite(
        api_fake,
        SimpleNamespace(
            non_interactive=False,
            route="auto",
            scenario_id="settings_work_mode",
        ),
        device,
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    assert result == 2
    assert api_fake.actions == []
    if failed_check.endswith("_after_trace"):
        assert checkpoint["steps"][0]["outcome"] == "inconclusive"
        assert checkpoint["steps"][0].get("service_attempted") is not True
    else:
        assert checkpoint["steps"][0]["outcome"] == "not_applicable"


def test_no_ambient_http_proxy_can_receive_loopback_bearer(monkeypatch):
    """Use urllib's actual proxy dispatch with an in-memory HTTP transport.

    HA's unit-test harness forbids real sockets. If the client regresses to
    the default ProxyHandler, urllib will deliver this dummy bearer token to
    the fake proxy host instead of the intended loopback host.
    """
    seen = []

    class FakeResponse:
        code = 200
        status = 200
        msg = "OK"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b'{"version":"2026.10.4"}'

        def info(self):
            return {}

    class MemoryHTTPHandler(api.request.HTTPHandler):
        def http_open(self, req):
            seen.append((req.host, req.get_header("Authorization")))
            return FakeResponse()

    original_build_opener = api.request.build_opener

    def build_with_memory_transport(*handlers):
        return original_build_opener(*handlers, MemoryHTTPHandler())

    monkeypatch.setattr(api.request, "build_opener", build_with_memory_transport)
    for name in ("http_proxy", "HTTP_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://untrusted.proxy.invalid:8080")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "")

    client = api.HAClient("http://127.0.0.1:8123", "DUMMY_TEST_TOKEN")
    assert client.config()["version"] == "2026.10.4"
    assert seen == [("127.0.0.1:8123", "Bearer DUMMY_TEST_TOKEN")]


def test_rest_disables_proxy_handler_even_under_https(monkeypatch):
    """Verify the default urllib proxy handler is replaced, not just bypassed."""
    observed_handlers = []

    class FakeOpener:
        def open(self, req, timeout):
            del timeout
            raise HTTPError(req.full_url, 503, "fake", {}, None)

    def spy(*handlers):
        observed_handlers.extend(handlers)
        return FakeOpener()

    monkeypatch.setattr(api.request, "build_opener", spy)
    client = api.HAClient("https://example.test", "DUMMY_TEST_TOKEN")
    with pytest.raises(api.HAAPIError, match="HTTP 503"):
        client.config()
    assert any(
        isinstance(handler, api.request.ProxyHandler) and handler.proxies == {}
        for handler in observed_handlers
    )
    assert any(
        isinstance(handler, api._NoRedirectHandler) for handler in observed_handlers
    )


@pytest.mark.parametrize(
    ("connected", "telemetry", "expected"),
    [
        ("on", "on", "ready"),
        ("off", "on", "incomplete"),
        ("on", "off", "incomplete"),
        ("unknown", "on", "inconclusive"),
        ("on", "unknown", "inconclusive"),
        ("unavailable", "unavailable", "inconclusive"),
    ],
)
def test_doctor_checks_connection_and_telemetry_independently(
    monkeypatch, capsys, allpowers_registry, connected, telemetry, expected
):
    fake = FakeHA(allpowers_registry)
    fake.state_overrides = {
        "binary_sensor.my_name_connected": connected,
        "binary_sensor.my_name_telemetry_available": telemetry,
    }
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "DUMMY_TEST_TOKEN")
    result = cli.main(
        ["doctor", "--ha-url", "https://example.test", "--non-interactive"]
    )
    assert result == (0 if expected == "ready" else 2)
    output = capsys.readouterr().out
    assert f"overall={expected}" in output
    assert "DUMMY_TEST_TOKEN" not in output
    assert "HIL qualification" in output or "no HIL qualification" in output


def test_disabled_telemetry_sensor_is_unknown_not_hardware_failure(
    monkeypatch, capsys, allpowers_registry
):
    entities, devices, entries = allpowers_registry
    for entity in entities:
        if entity.get("entity_id") == "binary_sensor.my_name_telemetry_available":
            entity["disabled_by"] = "integration"
    fake = FakeHA((entities, devices, entries))
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "DUMMY_TEST_TOKEN")
    assert (
        cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"])
        == 2
    )
    output = capsys.readouterr().out
    assert "BLE=ready" in output
    assert "telemetry=unknown" in output
    assert "overall=inconclusive" in output
    assert "disabled by default" in output


def test_degraded_run_is_still_recorded_with_preflight_result(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = FakeHA(allpowers_registry)
    fake.state_overrides["binary_sensor.my_name_connected"] = "off"
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "DUMMY_TEST_TOKEN")
    args = make_args(
        "run",
        "--ha-url",
        "https://example.test",
        "--non-interactive",
        "--output-root",
        str(tmp_path),
    )
    assert cli.execute(args) == 0
    path = next(tmp_path.iterdir()) / "private" / "session.json"
    session = json.loads(path.read_text())
    assert session["preflight"]["overall"] == "incomplete"
    assert session["preflight"]["checks"]["connected"] == "incomplete"
    assert session["qualification_status"] == "inconclusive"
    assert session["route"]["observed"]["value"] is None


def test_bluetooth_scanner_subscription_collects_only_aggregate_counts(monkeypatch):
    """HA 2026.10.4 sends a WS result followed by a scanner add event."""
    sent = []
    replies = [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True, "result": None},
        {
            "id": 1,
            "type": "event",
            "event": {
                "add": [
                    {
                        "source": "AA:BB:CC:DD:EE:FF",
                        "name": "Home secret adapter",
                        "adapter": "hci0",
                        "connectable": False,
                        "scanner_type": "uart",
                    },
                    {
                        "source": "AB:12:23:34:45:56",
                        "name": "Private ESPHome name",
                        "adapter": "secret-proxy",
                        "connectable": True,
                        "scanner_type": "remote",
                    },
                ]
            },
        },
    ]

    class FakeWS:
        def __init__(self):
            self.answers = iter(replies)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def recv(self, timeout):
            del timeout
            return json.dumps(next(self.answers))

        def send(self, value):
            sent.append(json.loads(value))

    websocket_module = ModuleType("websockets")
    websocket_module.__path__ = []
    exception_module = ModuleType("websockets.exceptions")
    exception_module.WebSocketException = type("WebSocketException", (Exception,), {})
    sync_module = ModuleType("websockets.sync")
    sync_module.__path__ = []
    sync_client = ModuleType("websockets.sync.client")
    sync_client.connect = lambda *args, **kwargs: FakeWS()
    for name, module in (
        ("websockets", websocket_module),
        ("websockets.exceptions", exception_module),
        ("websockets.sync", sync_module),
        ("websockets.sync.client", sync_client),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    result = api.HAClient(
        "https://example.test", "DUMMY_TEST_TOKEN"
    ).scanner_candidates()
    assert result == {
        "total_scanners": 2,
        "connectable_scanners": 1,
        "remote_scanners": 1,
        "connectable_remote_scanners": 1,
    }
    assert sent[-1] == {"id": 1, "type": "bluetooth/subscribe_scanner_details"}
    text = json.dumps(result)
    assert "AA:BB" not in text
    assert "secret-proxy" not in text
    assert "Private ESPHome" not in text


def test_bluetooth_scanner_denied_or_no_data_remains_unknown(monkeypatch):
    client = api.HAClient("https://example.test", "DUMMY_TEST_TOKEN")

    def denied(*args, **kwargs):
        raise api.HAAPIError("Home Assistant WebSocket command rejected (unauthorized)")

    monkeypatch.setattr(client, "websocket", denied)
    assert client.scanner_candidates() is None
    monkeypatch.setattr(client, "websocket", lambda *args, **kwargs: [None])
    assert client.scanner_candidates() is None
    monkeypatch.setattr(
        client, "websocket", lambda *args, **kwargs: [{"add": [{"name": "private"}]}]
    )
    assert client.scanner_candidates() is None


def test_scanner_inventory_environment_is_privacy_safe(
    monkeypatch, allpowers_registry, tmp_path
):
    fake = FakeHA(allpowers_registry)
    fake.scanners = {
        "total_scanners": 2,
        "connectable_scanners": 1,
        "remote_scanners": 1,
        "connectable_remote_scanners": 1,
    }
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "DUMMY_TEST_TOKEN")
    assert (
        cli.execute(
            make_args(
                "run",
                "--ha-url",
                "https://example.test",
                "--non-interactive",
                "--output-root",
                str(tmp_path),
            )
        )
        == 0
    )
    env = json.loads(
        (next(tmp_path.iterdir()) / "private" / "environment.json").read_text()
    )
    scanners = env["home_assistant"]["bluetooth_scanner_candidates"]
    assert scanners["value"] == fake.scanners
    assert scanners["source"] == "ha_ws_bluetooth_subscribe_scanner_details_admin"
    assert env["home_assistant"]["esphome_active_proxy"]["value"] is None
    assert env["integration"]["qualification_allowed"] is False


def test_scanner_subscription_no_event_is_bounded_and_unknown(monkeypatch):
    """No scanners can produce a subscription ack without an initial event."""
    replies = iter(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": None},
        ]
    )

    class FakeWS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def recv(self, timeout):
            try:
                return json.dumps(next(replies))
            except StopIteration as ex:
                assert timeout <= 1.5
                raise TimeoutError("no scanner event") from ex

        def send(self, message):
            pass

    websocket_package = ModuleType("websockets")
    websocket_package.__path__ = []
    exception_module = ModuleType("websockets.exceptions")
    exception_module.WebSocketException = type("WebSocketException", (Exception,), {})
    sync_module = ModuleType("websockets.sync")
    sync_module.__path__ = []
    sync_client = ModuleType("websockets.sync.client")
    sync_client.connect = lambda url, **kwargs: FakeWS()
    for name, module in (
        ("websockets", websocket_package),
        ("websockets.exceptions", exception_module),
        ("websockets.sync", sync_module),
        ("websockets.sync.client", sync_client),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    ha = api.HAClient("https://example.test", "DUMMY_TEST_TOKEN")
    assert ha.scanner_candidates() is None


def test_scanner_snapshot_rejects_non_scanner_subscriptions():
    ha = api.HAClient("https://example.test", "DUMMY_TEST_TOKEN")
    with pytest.raises(api.HAAPIError, match="Unsupported scanner"):
        ha.websocket([{"type": "call_service"}], scanner_snapshot=True)


class TraceHA(FakeHA):
    """Simulated authenticated WS response, including lost-stop retry."""

    def __init__(self, registry):
        super().__init__(registry)
        self.run_id = None
        self.scenario_id = None
        self.closed = False
        self.lose_stop_reply = False
        self.tamper = False
        self.no_frames = False
        self.runtime_stopped = False
        self.bad_result = None
        self.requested_route = None

    def websocket(self, commands):
        command = commands[0]
        typ = command["type"]
        self.calls.append(("ws", typ))
        if typ == "allpowers_ble/hil/start":
            self.run_id = command["run_id"]
            self.scenario_id = command["scenario_id"]
            self.requested_route = command["requested_route"]
            return [
                {
                    "schema_version": 1,
                    "capture_id": "private-capture",
                    "ttl_seconds": 120,
                }
            ]
        if typ == "allpowers_ble/hil/stop":
            self.closed = True
            if self.lose_stop_reply:
                self.lose_stop_reply = False
                raise api.HAAPIError("Lost stop acknowledgement")
        if typ not in ("allpowers_ble/hil/stop", "allpowers_ble/hil/fetch"):
            raise AssertionError("Unexpected command")
        common = {
            "schema_version": 1,
            "seq": 1,
            "utc": "2026-11-05T00:00:00+00:00",
            "elapsed_ms": 1.0,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "layer": "gatt_api",
            "session_generation": 1,
            "observed_route": "unverified",
            "event": "write_attempt",
            "transaction_id": None,
        }
        events = [common.copy()]
        frames = (
            []
            if self.no_frames
            else [
                {
                    **common,
                    "direction": "tx",
                    "command_type": "status_request",
                    "frame_validation": "validated_tx",
                    "hex": "a565b1000106010000000000",
                }
            ]
        )
        result = {
            "schema_version": 1,
            "capture_id": "private-capture",
            "run_id": "incorrect" if self.tamper else self.run_id,
            "scenario_id": self.scenario_id,
            "requested_route": self.requested_route,
            "capture_layer": "gatt_api_not_rf",
            "private_raw_bytes": True,
            "has_gatt_frames": bool(frames),
            "closed": self.closed,
            "expired": False,
            "termination_reason": (
                "runtime_stopped" if self.runtime_stopped else "requested_stop"
            ),
            "qualification_allowed": False,
            "observed_route": "unverified",
            "complete": self.closed and not self.runtime_stopped,
            "dropped_events": 0,
            "dropped_frames": 0,
            "event_count": len(events),
            "frame_count": len(frames),
            "events": events,
            "frames": frames,
        }
        if self.bad_result == "missing_schema":
            result.pop("schema_version")
        elif self.bad_result == "negative_drop":
            result["dropped_events"] = -1
        elif self.bad_result == "bad_hex":
            frames[0]["hex"] = "oops"
        elif self.bad_result == "odd_hex":
            frames[0]["hex"] = "abc"
        elif self.bad_result == "orphan_frame":
            frames[0]["seq"] = 2
        elif self.bad_result == "duplicate_seq":
            events.append(events[0].copy())
            result["event_count"] = len(events)
        elif self.bad_result == "out_of_order":
            events[0]["seq"] = 2
            events.append({**common, "seq": 1})
            result["event_count"] = len(events)
        elif self.bad_result == "row_scenario":
            frames[0]["scenario_id"] = "wrong"
        elif self.bad_result == "bad_timestamp":
            events[0]["utc"] = "invalid"
        elif self.bad_result == "nonutc_timestamp":
            events[0]["utc"] = "2026-11-05T01:00:00+01:00"
        elif self.bad_result == "missing_timing":
            events[0].pop("elapsed_ms")
        elif self.bad_result == "bad_generation":
            events[0]["session_generation"] = "ble-secret"
        elif self.bad_result == "bad_direction":
            frames[0]["direction"] = "rx"
        elif self.bad_result == "bad_opcode":
            frames[0]["command_type"] = "invalid_command"
        elif self.bad_result == "bad_validation":
            frames[0]["frame_validation"] = "unvalidated_chunk"
        elif self.bad_result == "bad_event":
            events[0]["event"] = "invalid event"
        elif self.bad_result == "bad_count":
            result["frame_count"] = 7
        elif self.bad_result == "contradictory_complete":
            result["complete"] = False
        elif self.bad_result == "not_private":
            result["private_raw_bytes"] = False
        elif self.bad_result == "wrong_route":
            result["requested_route"] = "other"
        elif self.bad_result == "invalid_frames":
            result["frames"] = [{"seq": 1}, None]
        elif self.bad_result == "frame_generation_mismatch":
            frames[0]["session_generation"] = 2
        elif self.bad_result == "frame_time_mismatch":
            frames[0]["elapsed_ms"] = 2.0
        elif self.bad_result == "frame_utc_mismatch":
            frames[0]["utc"] = "2026-11-05T00:00:01+00:00"
        elif self.bad_result == "frame_transaction_mismatch":
            frames[0]["transaction_id"] = "tx-different-1"
        elif self.bad_result == "frame_transaction_missing":
            frames[0].pop("transaction_id")
        elif self.bad_result == "elapsed_backwards":
            events.append({**common, "seq": 2, "elapsed_ms": 0.5})
            result["event_count"] = len(events)
        elif self.bad_result == "equal_elapsed":
            events.append({**common, "seq": 2})
            result["event_count"] = len(events)
        elif self.bad_result == "observer_loss":
            result["dropped_events"] = 1
            result["complete"] = False
        elif self.bad_result == "orphan_with_drops":
            frames[0]["seq"] = 2
            result["dropped_events"] = 1
            result["complete"] = False
        elif self.bad_result == "matching_with_drops_but_mismatched":
            frames[0]["transaction_id"] = "tx-different-1"
            result["dropped_events"] = 1
            result["complete"] = False
        return [result]


@pytest.mark.parametrize("lose_reply", [False, True])
def test_trace_cli_private_jsonl_agrees_with_manifest_and_recovers_lost_stop(
    monkeypatch, tmp_path, allpowers_registry, lose_reply
):
    fake = TraceHA(allpowers_registry)
    fake.lose_stop_reply = lose_reply
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    output = tmp_path / "results"
    rc = cli.main(
        [
            "trace",
            "--ha-url",
            "https://example.test",
            "--non-interactive",
            "--scenario-id",
            "outputs-rejected",
            "--duration",
            "1",
            "--route",
            "active_proxy",
            "--output-root",
            str(output),
        ]
    )
    assert rc == 0
    (run_root,) = list(output.iterdir())
    directory = run_root / "private"
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert {p.name for p in directory.iterdir()} == {
        "environment.json",
        "session.json",
        "events.jsonl",
        "frames.jsonl",
        "trace.json",
    }
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in directory.iterdir())
    meta = json.loads((directory / "trace.json").read_text())
    events = [
        json.loads(s) for s in (directory / "events.jsonl").read_text().splitlines()
    ]
    frames = [
        json.loads(s) for s in (directory / "frames.jsonl").read_text().splitlines()
    ]
    session = json.loads((directory / "session.json").read_text())
    assert meta["run_id"] == session["run_id"] == events[0]["run_id"]
    assert meta["scenario_id"] == events[0]["scenario_id"] == "outputs-rejected"
    assert meta["event_count"] == len(events) == 1
    assert meta["frame_count"] == len(frames) == 1
    assert meta["closed"] and meta["complete"]
    assert not meta["qualification_allowed"]
    assert session["qualification_status"] == "inconclusive"
    assert frames[0]["command_type"] == "status_request"
    assert any(call == ("ws", "allpowers_ble/hil/stop") for call in fake.calls)
    assert lose_reply == any(
        call == ("ws", "allpowers_ble/hil/fetch") for call in fake.calls
    )


@pytest.mark.parametrize(
    ("mode", "exit_code"),
    [
        ("tamper", 2),
        ("no_frames", 2),
        ("interrupt", 2),
    ],
)
def test_trace_cli_never_qualifies_missing_or_interrupted_evidence(
    monkeypatch, tmp_path, allpowers_registry, mode, exit_code
):
    fake = TraceHA(allpowers_registry)
    fake.tamper = mode == "tamper"
    fake.no_frames = mode == "no_frames"
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")

    def sleep_once(_):
        if mode == "interrupt":
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sleep", sleep_once)
    output = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--non-interactive",
                "--duration",
                "1",
                "--output-root",
                str(output),
            ]
        )
        == exit_code
    )
    (run_root,) = list(output.iterdir())
    directory = run_root / "private"
    session = json.loads((directory / "session.json").read_text())
    assert session["qualification_status"] == "inconclusive"
    assert fake.closed
    if mode in ("tamper", "interrupt"):
        assert not (directory / "trace.json").exists()


@pytest.mark.parametrize("scenario,duration", [("not safe!", "1"), ("ok", "31")])
def test_trace_cli_rejects_bad_scenario_and_oversized_run_before_capture(
    monkeypatch, tmp_path, allpowers_registry, scenario, duration
):
    fake = TraceHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    root = tmp_path / "no-results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--non-interactive",
                "--duration",
                duration,
                "--scenario-id",
                scenario,
                "--output-root",
                str(root),
            ]
        )
        == 2
    )
    assert not root.exists()
    assert all("allpowers_ble/hil/start" not in c for c in fake.calls)


def test_runtime_stopped_trace_returns_two_even_when_it_has_valid_frames(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = TraceHA(allpowers_registry)
    fake.runtime_stopped = True
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    root = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--duration",
                "1",
                "--non-interactive",
                "--output-root",
                str(root),
            ]
        )
        == 2
    )
    (run_root,) = list(root.iterdir())
    directory = run_root / "private"
    trace = json.loads((directory / "trace.json").read_text())
    assert trace["frame_count"] == 1
    assert trace["termination_reason"] == "runtime_stopped"
    assert trace["complete"] is False
    assert trace["qualification_allowed"] is False
    assert (
        json.loads((directory / "session.json").read_text())["qualification_status"]
        == "inconclusive"
    )


@pytest.mark.parametrize(
    "mode",
    [
        "missing_schema",
        "negative_drop",
        "bad_hex",
        "odd_hex",
        "orphan_frame",
        "duplicate_seq",
        "out_of_order",
        "row_scenario",
        "bad_timestamp",
        "nonutc_timestamp",
        "missing_timing",
        "bad_generation",
        "bad_direction",
        "bad_opcode",
        "bad_validation",
        "bad_event",
        "bad_count",
        "contradictory_complete",
        "not_private",
        "wrong_route",
        "invalid_frames",
        "frame_generation_mismatch",
        "frame_time_mismatch",
        "frame_utc_mismatch",
        "frame_transaction_mismatch",
        "frame_transaction_missing",
        "elapsed_backwards",
        "matching_with_drops_but_mismatched",
    ],
)
def test_trace_cli_fails_closed_on_malformed_v1_evidence(
    monkeypatch, tmp_path, allpowers_registry, mode
):
    fake = TraceHA(allpowers_registry)
    fake.bad_result = mode
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    root = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--duration",
                "1",
                "--non-interactive",
                "--output-root",
                str(root),
            ]
        )
        == 2
    )
    (run_root,) = list(root.iterdir())
    directory = run_root / "private"
    assert (directory / "environment.json").exists()
    assert (directory / "session.json").exists()
    assert not (directory / "events.jsonl").exists()
    assert not (directory / "frames.jsonl").exists()
    assert not (directory / "trace.json").exists()


@pytest.mark.parametrize(
    ("mode", "expected_return"),
    [("equal_elapsed", 0), ("orphan_with_drops", 2)],
)
def test_trace_cli_accepts_equal_timestamps_and_explains_dropped_orphan(
    monkeypatch, tmp_path, allpowers_registry, mode, expected_return
):
    fake = TraceHA(allpowers_registry)
    fake.bad_result = mode
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    root = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--duration",
                "1",
                "--non-interactive",
                "--output-root",
                str(root),
            ]
        )
        == expected_return
    )
    (run_root,) = list(root.iterdir())
    directory = run_root / "private"
    metadata = json.loads((directory / "trace.json").read_text())
    events = [
        json.loads(value)
        for value in (directory / "events.jsonl").read_text().splitlines()
    ]
    frames = [
        json.loads(value)
        for value in (directory / "frames.jsonl").read_text().splitlines()
    ]
    assert metadata["qualification_allowed"] is False
    assert events[0]["elapsed_ms"] == 1.0
    if mode == "equal_elapsed":
        assert events[1]["elapsed_ms"] == events[0]["elapsed_ms"]
        assert metadata["complete"] is True
    else:
        assert metadata["dropped_events"] == 1
        assert metadata["complete"] is False
        assert frames[0]["seq"] not in {row["seq"] for row in events}


def test_private_trace_second_file_open_failure_erases_all_partial_bytes(
    monkeypatch, tmp_path, allpowers_registry, capsys
):
    from pathlib import Path

    fake = TraceHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    actual_open = cli.os.open

    def fail_second_private_file(path, flags, mode=0o777):
        if Path(path).name == "frames.jsonl":
            raise OSError("PRIVATE path/unsafe-space")
        return actual_open(path, flags, mode)

    monkeypatch.setattr(cli.os, "open", fail_second_private_file)
    root = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--duration",
                "1",
                "--non-interactive",
                "--output-root",
                str(root),
            ]
        )
        == 2
    )
    (run_root,) = list(root.iterdir())
    directory = run_root / "private"
    assert sorted(p.name for p in directory.iterdir()) == [
        "environment.json",
        "session.json",
    ]
    assert (
        json.loads((directory / "session.json").read_text())["qualification_status"]
        == "inconclusive"
    )
    log = capsys.readouterr()
    assert "PRIVATE path" not in log.err
    assert "Private HIL trace could not be saved" in log.err


def test_private_trace_manifest_write_failure_scrubs_created_jsonl(
    monkeypatch, tmp_path, allpowers_registry
):
    fake = TraceHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    actual_dump = cli.json.dump

    def fail_manifest(data, stream, *args, **kwargs):
        if isinstance(data, dict) and data.get("capture_id") == "private-capture":
            stream.write("PRIVATE_PARTIAL")
            raise OSError("full disk")
        return actual_dump(data, stream, *args, **kwargs)

    monkeypatch.setattr(cli.json, "dump", fail_manifest)
    root = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--duration",
                "1",
                "--non-interactive",
                "--output-root",
                str(root),
            ]
        )
        == 2
    )
    (run_root,) = list(root.iterdir())
    directory = run_root / "private"
    assert not (directory / "trace.json").exists()
    assert not (directory / "events.jsonl").exists()
    assert not (directory / "frames.jsonl").exists()
    assert (directory / "session.json").exists()


def test_trace_cli_with_observer_loss_fails_with_valid_gatt_frame(
    monkeypatch, tmp_path, allpowers_registry
):
    """Recorded GATT bytes cannot make a failed observer capture successful."""
    fake = TraceHA(allpowers_registry)
    fake.bad_result = "observer_loss"
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    root = tmp_path / "results"
    assert (
        cli.main(
            [
                "trace",
                "--ha-url",
                "https://example.test",
                "--non-interactive",
                "--duration",
                "1",
                "--output-root",
                str(root),
            ]
        )
        == 2
    )
    (run_root,) = list(root.iterdir())
    directory = run_root / "private"
    trace = json.loads((directory / "trace.json").read_text())
    assert trace["frame_count"] == 1
    assert trace["dropped_events"] == 1
    assert trace["complete"] is False
    assert trace["qualification_allowed"] is False


@pytest.mark.parametrize("command", ("doctor", "run", "trace"))
def test_cli_route_text_distinguishes_preflight_and_connected_trace(
    monkeypatch, tmp_path, capsys, allpowers_registry, command
):
    """Requested route is never proof; trace prints the actual v2 route."""
    if command == "trace":
        from tests.test_route_cli_v2 import V2TraceHA

        fake = V2TraceHA(allpowers_registry)
    else:
        fake = FakeHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setenv("HA_HIL_TOKEN", "PRIVATE_TOKEN")
    output_root = tmp_path / command
    args = [
        command,
        "--ha-url",
        "https://example.test",
        "--non-interactive",
        "--route",
        "local_adapter",
        "--output-root",
        str(output_root),
    ]
    if command == "trace":
        args.extend(["--duration", "1"])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert (
        "Bluetooth route: not verified by preflight; "
        "use 'trace' for connected-backend attribution"
    ) in output
    assert "no route instrumentation yet" not in output
    if command == "trace":
        assert "Observed connection route: active_proxy" in output
        (run_root,) = list(output_root.iterdir())
        directory = run_root / "private"
        trace = json.loads((directory / "trace.json").read_text())
        session = json.loads((directory / "session.json").read_text())
        assert trace["schema_version"] == 2
        assert trace["requested_route"] == "local_adapter"
        assert session["route"]["requested"] == "local_adapter"
        assert trace["observed_route"] == "active_proxy"
        assert trace["qualification_allowed"] is False
    else:
        assert "Observed connection route:" not in output


def test_discovery_maps_only_unambiguous_integration_control_entities():
    """Renamed controls resolve by owner/domain/key and ambiguous keys disappear."""
    entities = [
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "switch.renamed_ac",
            "unique_id": "registry_ac_output",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "switch.second_ac",
            "unique_id": "registry_ac_output",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "switch.dc_power",
            "unique_id": "registry_dc_output",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "switch.car_charger",
            "unique_id": "registry_car_charger",
            "config_entry_id": "entry-1",
            "disabled_by": "user",
        },
        {
            "device_id": "device-1",
            "platform": "foreign_integration",
            "entity_id": "select.work_mode",
            "unique_id": "registry_work_mode",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "sensor.work_mode",
            "unique_id": "registry_work_mode",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
        {
            "device_id": "other-device",
            "platform": "allpowers_ble",
            "entity_id": "select.work_mode",
            "unique_id": "registry_work_mode",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
    ]
    devices = [
        {
            "id": "device-1",
            "config_entry_id": "entry-1",
            "identifiers": [["allpowers_ble", "private-registry-id"]],
            "model": "R600",
        }
    ]
    entries = [{"entry_id": "entry-1", "domain": "allpowers_ble", "state": "loaded"}]

    (device,) = discovery.find_devices(entities, devices, entries)

    assert device.control_entities == {"dc_output": "switch.dc_power"}
    assert device.config_entry_id == "entry-1"


def test_discovery_maps_only_linked_integration_read_only_refresh_button():
    entities = [
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "button.device_refresh",
            "unique_id": "registry_refresh",
            "config_entry_id": "entry-1",
            "disabled_by": None,
        },
        {
            "device_id": "device-1",
            "platform": "allpowers_ble",
            "entity_id": "button.foreign_refresh",
            "unique_id": "registry_refresh",
            "config_entry_id": "entry-2",
            "disabled_by": None,
        },
    ]
    devices = [
        {
            "id": "device-1",
            "config_entry_id": "entry-1",
            "identifiers": [["allpowers_ble", "private-registry-id"]],
            "model": "R600",
        }
    ]
    entries = [{"entry_id": "entry-1", "domain": "allpowers_ble", "state": "loaded"}]

    (device,) = discovery.find_devices(entities, devices, entries)

    assert device.refresh_entity == "button.device_refresh"


def test_control_state_filters_unavailable_states_and_unknown_options(monkeypatch):
    """Only current values and select choices defined by the integration survive."""
    client = api.HAClient("http://127.0.0.1:8123", "token")
    payloads = {
        "select.mode": {
            "state": "standard",
            "attributes": {"options": ["mute", "standard", "fast", "unsafe"]},
        },
        "switch.offline": {"state": "unavailable", "attributes": {}},
    }
    monkeypatch.setattr(client, "entity_state", payloads.__getitem__)

    assert client.control_state("work_mode", "select.mode") == {
        "available": True,
        "state": "standard",
        "options": ["fast", "mute", "standard"],
    }
    assert client.control_state("ac_output", "switch.offline") == {
        "available": False,
        "state": None,
        "options": [],
    }


def test_call_control_only_sends_allowlisted_service_targets(monkeypatch):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    commands = []

    def websocket(rows):
        commands.extend(rows)
        return [{"context": {}}]

    monkeypatch.setattr(client, "websocket", websocket)
    client.call_control("ac_output", "switch.station_ac", "on")
    client.call_control("work_mode", "select.station_mode", "fast")
    assert commands == [
        {
            "type": "call_service",
            "domain": "switch",
            "service": "turn_on",
            "target": {"entity_id": "switch.station_ac"},
        },
        {
            "type": "call_service",
            "domain": "select",
            "service": "select_option",
            "service_data": {"option": "fast"},
            "target": {"entity_id": "select.station_mode"},
        },
    ]


def test_request_read_only_refresh_uses_only_the_integration_button(monkeypatch):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    commands = []
    monkeypatch.setattr(
        client,
        "websocket",
        lambda rows: commands.extend(rows) or [{"context": {}}],
    )

    client.request_read_only_refresh("button.station_refresh")

    assert commands == [
        {
            "type": "call_service",
            "domain": "button",
            "service": "press",
            "target": {"entity_id": "button.station_refresh"},
        }
    ]
    with pytest.raises(api.HAAPIError):
        client.request_read_only_refresh("button.other_domain_press")


@pytest.mark.parametrize(
    ("key", "entity_id", "value", "domain", "service", "service_data"),
    [
        (key, f"switch.station_{key}", value, "switch", service, None)
        for key in ("ac_output", "dc_output", "light", "eco_mode", "car_charger")
        for value, service in (("on", "turn_on"), ("off", "turn_off"))
    ]
    + [
        (
            "work_mode",
            "select.station_mode",
            option,
            "select",
            "select_option",
            {"option": option},
        )
        for option in ("mute", "standard", "fast")
    ]
    + [
        (
            "eco_timeout",
            "select.station_timeout",
            option,
            "select",
            "select_option",
            {"option": option},
        )
        for option in ("one_hour", "two_hours", "four_hours", "six_hours")
    ],
)
def test_call_control_maps_supported_controls_to_narrow_services(
    monkeypatch, key, entity_id, value, domain, service, service_data
):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    commands = []

    def websocket(rows):
        commands.extend(rows)
        return [{"context": {}}]

    monkeypatch.setattr(client, "websocket", websocket)
    client.call_control(key, entity_id, value)

    expected = {
        "type": "call_service",
        "domain": domain,
        "service": service,
        "target": {"entity_id": entity_id},
    }
    if service_data is not None:
        expected["service_data"] = service_data
    assert commands == [expected]


@pytest.mark.parametrize(
    ("key", "entity_id", "value"),
    [
        ("ac_output", "switch.station_ac", "toggle"),
        ("work_mode", "select.station_mode", "unsafe"),
        ("unknown", "switch.station_ac", "on"),
        ("ac_output", "light.station_ac", "on"),
    ],
)
def test_call_control_rejects_unallowlisted_actions(key, entity_id, value):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    with pytest.raises(api.HAAPIError):
        client.call_control(key, entity_id, value)


def test_recovery_suite_only_observes_operator_action_and_new_route(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    class RecoveryHA:
        origin = "https://ha.example.test"

        def __init__(self):
            # The operator confirms after restoring the route, so the first
            # poll may already see the new session without a transient down state.
            self.capabilities = iter(((True, 4), (True, 5)))
            self.commands = []

        def hil_capabilities(self, entry_id):
            connected, generation = next(self.capabilities)
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": connected,
                "status_fresh": connected,
                "session_generation": generation,
            }

        def websocket(self, commands):
            self.commands.extend(commands)
            if commands[0]["type"].endswith("/start"):
                return [
                    {
                        "schema_version": 2,
                        "capture_id": "capture-safe",
                        "ttl_seconds": 120,
                    }
                ]
            return [
                {
                    "schema_version": 2,
                    "observed_route": "local_adapter",
                    "complete": True,
                    "events": [
                        {
                            "event": "notification_decoded",
                            "session_generation": 5,
                            "decoded_packet_types": ["StatusData"],
                        }
                    ],
                }
            ]

    api_fake = RecoveryHA()
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "run"
    output_dir.mkdir(mode=0o700)
    answers = iter(("1", "1", "1"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_save_private_trace", lambda *args, **kwargs: None)

    result = cli._execute_recovery_suite(
        api_fake,
        SimpleNamespace(
            non_interactive=False,
            route="auto",
            guided_reconnect_required=True,
        ),
        device,
        output_dir,
        "run-safe",
    )

    assert result == 0
    assert api_fake.commands[0]["type"] == "allpowers_ble/hil/start"
    assert [command["type"] for command in api_fake.commands[1:]] == [
        "allpowers_ble/hil/fetch",
        "allpowers_ble/hil/stop",
    ]
    assert all(
        command["type"] not in {"call_service", "allpowers_ble/command"}
        for command in api_fake.commands
    )
    stored = json.loads((output_dir / "scenarios.json").read_text())
    assert stored["steps"][-1]["outcome"] == "pass"
    assert stored["steps"][-1]["disconnect_observed"] is False
    assert stored["steps"][-1]["session_generation_before"] == 4
    assert stored["steps"][-1]["session_generation_after"] == 5
    assert stored["steps"][-1]["physical_outcome"] == "yes"


def test_guided_reconnect_waits_for_delayed_status_with_real_v2_trace_validation(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from custom_components.allpowers_ble.hil_trace import Capture
    from custom_components.allpowers_ble.route_attribution import ConnectedRoute

    class DelayedReconnectHA:
        origin = "https://ha.example.test"

        def __init__(self, clock):
            self.clock = clock
            self.capture = None
            self.capture_started_at = None
            self.status_recorded = False
            self.capability_calls = 0

        def hil_capabilities(self, entry_id):
            self.capability_calls += 1
            generation = 4 if self.capability_calls == 1 else 5
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": generation,
            }

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.capture = Capture(
                    "entry-safe",
                    "operator",
                    "0123456789abcdef",
                    "recovery_reconnect",
                    "auto",
                )
                self.capture_started_at = self.clock[0]
                self.capture.record("capture_started", session_generation=5)
                self.capture.observe_route(
                    5,
                    ConnectedRoute(
                        "active_proxy",
                        "esphome",
                        "esphome_scanner_binding",
                        proxy_id="private-proxy-id",
                    ),
                )
                return [
                    {
                        "schema_version": 2,
                        "capture_id": self.capture.capture_id,
                        "ttl_seconds": 120,
                    }
                ]

            if command["type"].endswith("/fetch"):
                if (
                    not self.status_recorded
                    and self.clock[0] - self.capture_started_at >= 8
                ):
                    self.capture.record(
                        "notification_decoded",
                        session_generation=5,
                        decoded_packets=1,
                        decoded_packet_types=["StatusData"],
                        characteristic_uuid="0000fff1-0000-1000-8000-00805f9b34fb",
                        inferred_model_profile="r600",
                        session_hardware_revision="0.3",
                        discarded_frames=0,
                        discarded_bytes=0,
                    )
                    self.status_recorded = True
                return [self.capture.snapshot()]

            self.capture.stop()
            return [self.capture.snapshot()]

    clock = [100.0]
    api_fake = DelayedReconnectHA(clock)
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "guided-delayed-status"
    output_dir.mkdir(mode=0o700)
    answers = iter(("1", "1", "1"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    result = cli._execute_recovery_suite(
        api_fake,
        SimpleNamespace(
            non_interactive=False,
            route="auto",
            guided_reconnect_required=True,
        ),
        device,
        output_dir,
        "0123456789abcdef",
    )

    stored = json.loads((output_dir / "scenarios.json").read_text())
    observation = stored["steps"][-1]
    assert result == 0
    assert api_fake.capability_calls >= 2
    assert api_fake.status_recorded is True
    assert clock[0] - api_fake.capture_started_at >= 8
    assert observation["disconnect_observed"] is False
    assert observation["session_generation_before"] == 4
    assert observation["session_generation_after"] == 5
    assert observation["fresh_status_telemetry"] is True
    assert observation["route_verified"] is True
    assert observation["trace_complete"] is True
    assert observation["physical_outcome"] == "yes"
    assert observation["outcome"] == "pass"


def test_recovery_transient_api_failure_does_not_prove_new_ble_session(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    class TransientFailureHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.calls = 0
            self.trace_starts = 0

        def hil_capabilities(self, entry_id):
            self.calls += 1
            if self.calls == 2:
                raise cli.HAAPIError("transient websocket failure")
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": 4,
            }

        def websocket(self, commands):
            if commands[0]["type"].endswith("/start"):
                self.trace_starts += 1
                return [{"schema_version": 2, "capture_id": "unexpected"}]
            raise AssertionError("same-generation API recovery must not capture")

    api_fake = TransientFailureHA()
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "transient-recovery"
    output_dir.mkdir(mode=0o700)
    clock = [100.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(cli, "sleep", advance)
    answers = iter(("1", "1", "1"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    result = cli._execute_recovery_suite(
        api_fake,
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "run-transient",
    )

    stored = json.loads((output_dir / "scenarios.json").read_text())
    assert result == 2
    assert api_fake.trace_starts == 0
    assert stored["steps"][-1]["outcome"] == "inconclusive"
    assert stored["steps"][-1]["disconnect_observed"] is False


def test_recovery_resume_observes_only_and_does_not_repeat_operator_action(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    class RecoveryHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.commands = []

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": 5,
            }

        def websocket(self, commands):
            self.commands.extend(commands)
            if commands[0]["type"].endswith("/start"):
                return [{"schema_version": 2, "capture_id": "capture-resumed"}]
            return [
                {
                    "schema_version": 2,
                    "observed_route": "active_proxy",
                    "complete": True,
                }
            ]

    api_fake = RecoveryHA()
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "resume"
    output_dir.mkdir(mode=0o700)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "1")
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_save_private_trace", lambda *args, **kwargs: None)
    fingerprint = cli._device_fingerprint("0123456789abcdef", device, api_fake.origin)
    resume_state = {
        "run_id": "0123456789abcdef",
        "scenario_id": "recovery_reconnect",
        "device_fingerprint": fingerprint,
        "steps": [
            {
                "step_id": "baseline",
                "outcome": "pass",
                "session_generation": 4,
            },
            {
                "step_id": "operator_action",
                "outcome": "pass",
                "action": "route interruption and restoration",
                "operator_confirmed": True,
            },
        ],
    }

    result = cli._execute_recovery_suite(
        api_fake,
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "0123456789abcdef",
        resume_state,
    )

    assert result == 0
    assert len(prompts) == 1
    assert [command["type"] for command in api_fake.commands] == [
        "allpowers_ble/hil/start",
        "allpowers_ble/hil/stop",
    ]


@pytest.mark.parametrize("app_answer", ("1", "2", "3"))
def test_vendor_app_result_is_separate_from_machine_observed_ha_stability(
    monkeypatch, tmp_path, app_answer
):
    from types import SimpleNamespace

    class StableHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.commands = []
            self.calls = 0

        def hil_capabilities(self, entry_id):
            self.calls += 1
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": 4,
            }

        def websocket(self, commands):
            self.commands.extend(commands)
            if commands[0]["type"].endswith("/start"):
                return [
                    {
                        "schema_version": 2,
                        "capture_id": "stable-capture",
                        "ttl_seconds": 120,
                    }
                ]
            return [
                {
                    "schema_version": 2,
                    "observed_route": "active_proxy",
                    "complete": True,
                    "events": [
                        {
                            "event": "notification_decoded",
                            "session_generation": 4,
                            "decoded_packet_types": ["StatusData"],
                        }
                    ],
                }
            ]

    api_fake = StableHA()
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "vendor-stable"
    output_dir.mkdir(mode=0o700)
    answers = iter(("2", "1", app_answer))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    clock = [100.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_save_private_trace", lambda *args, **kwargs: None)

    result = cli._execute_recovery_suite(
        api_fake,
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "stable-run",
    )

    stored = json.loads((output_dir / "scenarios.json").read_text())
    action = next(
        step for step in stored["steps"] if step["step_id"] == "operator_action"
    )
    observation = stored["steps"][-1]
    assert result == (2 if app_answer == "3" else 0)
    assert (
        action["operator_app_result"]
        == (
            {"1": "could_not_connect", "2": "connected", "3": "unable_or_unwilling"}[
                app_answer
            ]
        )
    )
    assert observation["ha_observation"] == (
        "inconclusive" if app_answer == "3" else "ha_stable"
    )
    assert observation["fresh_status_telemetry"] is True
    assert observation["outcome"] == ("inconclusive" if app_answer == "3" else "pass")


@pytest.mark.parametrize(
    ("status_delay", "trace_disconnect"), ((20, False), (None, False), (20, True))
)
def test_vendor_app_waits_for_fresh_status_with_real_v2_trace_validation(
    monkeypatch, tmp_path, status_delay, trace_disconnect
):
    from types import SimpleNamespace

    from custom_components.allpowers_ble.hil_trace import Capture
    from custom_components.allpowers_ble.route_attribution import ConnectedRoute

    class DelayedStatusHA:
        origin = "https://ha.example.test"

        def __init__(self, clock):
            self.clock = clock
            self.capture = None
            self.capture_started_at = None
            self.status_recorded = False

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": 4,
            }

        def websocket(self, commands):
            command = commands[0]
            if command["type"].endswith("/start"):
                self.capture = Capture(
                    "entry-safe",
                    "operator",
                    "0123456789abcdef",
                    "recovery_reconnect",
                    "auto",
                )
                self.capture_started_at = self.clock[0]
                self.capture.record("capture_started", session_generation=4)
                self.capture.observe_route(
                    4,
                    ConnectedRoute(
                        "active_proxy",
                        "esphome",
                        "esphome_scanner_binding",
                        proxy_id="private-proxy-id",
                    ),
                )
                if trace_disconnect:
                    self.capture.record("disconnect", session_generation=4)
                return [
                    {
                        "schema_version": 2,
                        "capture_id": self.capture.capture_id,
                        "ttl_seconds": 120,
                    }
                ]

            if command["type"].endswith("/fetch"):
                if (
                    status_delay is not None
                    and not self.status_recorded
                    and self.clock[0] - self.capture_started_at >= status_delay
                ):
                    self.capture.record(
                        "notification_decoded",
                        session_generation=4,
                        decoded_packets=1,
                        decoded_packet_types=["StatusData"],
                        characteristic_uuid="0000fff1-0000-1000-8000-00805f9b34fb",
                        inferred_model_profile="r600",
                        session_hardware_revision="0.3",
                        discarded_frames=0,
                        discarded_bytes=0,
                    )
                    self.status_recorded = True
                return [self.capture.snapshot()]

            self.capture.record("capture_stopped", session_generation=4)
            self.capture.stop()
            return [self.capture.snapshot()]

    clock = [100.0]
    api_fake = DelayedStatusHA(clock)
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / f"delayed-status-{status_delay}"
    output_dir.mkdir(mode=0o700)
    answers = iter(("2", "1", "1"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    result = cli._execute_recovery_suite(
        api_fake,
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "0123456789abcdef",
    )

    stored = json.loads((output_dir / "scenarios.json").read_text())
    observation = stored["steps"][-1]
    assert api_fake.capture_started_at is not None
    assert api_fake.clock[0] - api_fake.capture_started_at >= (status_delay or 119)
    evidence_is_complete = status_delay is not None and not trace_disconnect
    assert result == (0 if evidence_is_complete else 2)
    assert observation["trace_complete"] is True
    assert observation["fresh_status_telemetry"] is (status_delay is not None)
    assert observation["ha_observation"] == (
        "ha_stable" if evidence_is_complete else "inconclusive"
    )
    assert observation["outcome"] == (
        "pass" if evidence_is_complete else "inconclusive"
    )


@pytest.mark.parametrize(
    ("observed_route", "complete", "packet_types"),
    (
        ("active_proxy", True, ["SettingsData"]),
        ("unverified", True, ["StatusData"]),
        ("active_proxy", False, ["StatusData"]),
    ),
)
def test_vendor_app_stability_is_inconclusive_without_complete_fresh_route_evidence(
    monkeypatch, tmp_path, observed_route, complete, packet_types
):
    from types import SimpleNamespace

    class StableWithoutTelemetry:
        origin = "https://ha.example.test"

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": 4,
            }

        def websocket(self, commands):
            if commands[0]["type"].endswith("/start"):
                return [
                    {"schema_version": 2, "capture_id": "no-status", "ttl_seconds": 120}
                ]
            return [
                {
                    "schema_version": 2,
                    "observed_route": observed_route,
                    "complete": complete,
                    "events": [
                        {
                            "event": "notification_decoded",
                            "session_generation": 4,
                            "decoded_packet_types": packet_types,
                        }
                    ],
                }
            ]

    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "vendor-stale"
    output_dir.mkdir(mode=0o700)
    answers = iter(("2", "1", "3"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    clock = [100.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_save_private_trace", lambda *args, **kwargs: None)

    result = cli._execute_recovery_suite(
        StableWithoutTelemetry(),
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "stale-run",
    )
    stored = json.loads((output_dir / "scenarios.json").read_text())
    assert result == 2
    assert stored["steps"][-1]["outcome"] == "inconclusive"


def test_vendor_app_observed_disconnect_requires_and_records_new_session(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    class RecoveredHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.snapshots = iter(
                (
                    (True, 3),  # baseline
                    (False, 3),  # observed disconnect
                    (True, 4),  # new session
                )
            )

        def hil_capabilities(self, entry_id):
            connected, generation = next(self.snapshots)
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": connected,
                "status_fresh": connected,
                "session_generation": generation,
            }

        def websocket(self, commands):
            if commands[0]["type"].endswith("/start"):
                return [
                    {
                        "schema_version": 2,
                        "capture_id": "recovered-capture",
                        "ttl_seconds": 120,
                    }
                ]
            return [
                {
                    "schema_version": 2,
                    "observed_route": "active_proxy",
                    "complete": True,
                    "events": [
                        {
                            "event": "notification_decoded",
                            "session_generation": 4,
                            "decoded_packet_types": ["StatusData"],
                        }
                    ],
                }
            ]

    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "vendor-recovered"
    output_dir.mkdir(mode=0o700)
    answers = iter(("2", "1", "1"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(cli, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_save_private_trace", lambda *args, **kwargs: None)

    result = cli._execute_recovery_suite(
        RecoveredHA(),
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "recovered-run",
    )
    stored = json.loads((output_dir / "scenarios.json").read_text())
    observation = stored["steps"][-1]
    assert result == 0
    assert observation["disconnect_observed"] is True
    assert observation["ha_observation"] == "ha_recovered"
    assert observation["session_generation_before"] == 3
    assert observation["session_generation_after"] == 4
    assert observation["outcome"] == "pass"


def test_vendor_app_resume_gap_cannot_be_classified_as_ha_stable(monkeypatch, tmp_path):
    from types import SimpleNamespace

    class ResumedHA:
        origin = "https://ha.example.test"

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "session_generation": 4,
            }

        def websocket(self, commands):
            if commands[0]["type"].endswith("/start"):
                return [
                    {
                        "schema_version": 2,
                        "capture_id": "resumed-capture",
                        "ttl_seconds": 120,
                    }
                ]
            return [
                {
                    "schema_version": 2,
                    "observed_route": "active_proxy",
                    "complete": True,
                    "events": [
                        {
                            "event": "notification_decoded",
                            "session_generation": 4,
                            "decoded_packet_types": ["StatusData"],
                        }
                    ],
                }
            ]

    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "vendor-resumed"
    output_dir.mkdir(mode=0o700)
    fingerprint = cli._device_fingerprint(
        "resume-run", device, "https://ha.example.test", "auto"
    )
    resume_state = {
        "run_id": "resume-run",
        "scenario_id": "recovery_reconnect",
        "device_fingerprint": fingerprint,
        "steps": [
            {"step_id": "baseline", "outcome": "pass", "session_generation": 4},
            {
                "step_id": "operator_action",
                "outcome": "pass",
                "action": "vendor-app contention",
                "operator_confirmed": True,
                "operator_app_result": "connected",
            },
        ],
    }
    clock = [100.0]
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_save_private_trace", lambda *args, **kwargs: None)

    result = cli._execute_recovery_suite(
        ResumedHA(),
        SimpleNamespace(non_interactive=False, route="auto"),
        device,
        output_dir,
        "resume-run",
        resume_state,
    )
    stored = json.loads((output_dir / "scenarios.json").read_text())
    observation = stored["steps"][-1]
    assert result == 2
    assert observation["ha_observation"] == "inconclusive"
    assert observation["outcome"] == "inconclusive"


def test_device_fingerprint_is_run_and_server_scoped():
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        config_entry_id="entry-safe",
    )
    same = cli._device_fingerprint("0123456789abcdef", device, "https://ha.example")
    assert same == cli._device_fingerprint(
        "0123456789abcdef", device, "https://ha.example"
    )
    assert same != cli._device_fingerprint(
        "fedcba9876543210", device, "https://ha.example"
    )
    assert same != cli._device_fingerprint(
        "0123456789abcdef", device, "https://other.example"
    )
    assert same != cli._device_fingerprint(
        "0123456789abcdef", device, "https://ha.example", "active_proxy"
    )


def test_soak_requires_a_verified_normalized_telemetry_value():
    assert cli._has_verified_telemetry(
        {"battery": {"value": 89, "source": "ha_rest_entity_state", "verified": True}}
    )
    assert not cli._has_verified_telemetry(
        {"battery": {"value": None, "source": "unavailable", "verified": False}}
    )
    assert not cli._has_verified_telemetry({"battery": 89})
    non_measurements = {
        key: {"value": value, "source": "ha_rest_entity_state", "verified": True}
        for key, value in {
            "connected": "on",
            "telemetry_available": "on",
            "hardware_version": "0.3",
            "firmware_version": "0.2",
        }.items()
    }
    assert not cli._has_verified_telemetry(non_measurements)
    assert not cli._has_verified_telemetry(
        {"battery": {"value": True, "source": "ha_rest_entity_state", "verified": True}}
    )


@pytest.mark.parametrize(
    "failure",
    [
        "changed_state",
        "capabilities",
        "control_state",
        "feature_denied",
        "feature_absent",
        "feature_not_ready",
    ],
)
def test_output_suite_rechecks_pre_state_after_authorization(
    monkeypatch, tmp_path, failure
):
    from types import SimpleNamespace

    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
        control_entities={"ac_output": "switch.private_ac"},
    )

    class HA:
        origin = "https://ha.example.test"
        state = "off"
        actions = []
        commands = []
        capability_calls = 0
        control_calls = 0

        def hil_capabilities(self, _entry):
            self.capability_calls += 1
            if failure == "capabilities" and self.capability_calls > 1:
                raise cli.HAAPIError("capability snapshot unavailable")
            result = {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "pending_transaction": False,
                "status_fresh": True,
                "session_generation": 3,
                "capabilities": {"write_output_controls": True},
            }
            if failure.startswith("feature_"):
                authorized = self.capability_calls == 1
                result.update(
                    {
                        "schema_version": 2,
                        "features": {
                            "ac_output": {
                                "production_authorized": authorized
                                if failure == "feature_denied"
                                else True,
                                "physical_presence": "absent"
                                if failure == "feature_absent"
                                and self.capability_calls > 1
                                else "present",
                                "candidate_hil_eligible": False,
                                "runtime_ready": (
                                    False
                                    if failure == "feature_not_ready"
                                    and self.capability_calls > 1
                                    else True
                                ),
                            }
                        },
                    }
                )
            return result

        def control_state(self, _key, _entity):
            self.control_calls += 1
            if failure == "control_state" and self.control_calls > 1:
                raise cli.HAAPIError("control state unavailable")
            return {"available": True, "state": self.state}

        def call_control(self, *_args):
            self.actions.append(_args)

        def websocket(self, commands):
            command = commands[0]
            self.commands.append(command["type"])
            if command["type"].endswith("/start"):
                return [{"schema_version": 2, "capture_id": "capture"}]
            return [{}]

    api = HA()
    output_dir = tmp_path / "run"
    output_dir.mkdir(mode=0o700)

    def authorize(_prompt):
        if failure == "changed_state":
            api.state = "on"  # Simulate another HA actor changing it at the prompt.
        return "1"

    monkeypatch.setattr(cli, "input", authorize, raising=False)
    result = cli._execute_output_suite(
        api,
        SimpleNamespace(
            non_interactive=False, route="auto", scenario_id="output_ac_cycle"
        ),
        device,
        {},
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    assert result == 2
    assert api.actions == []
    assert api.commands == ["allpowers_ble/hil/start", "allpowers_ble/hil/stop"]
    assert checkpoint["steps"][0]["outcome"] == "inconclusive"
    assert "pre-write recheck" in checkpoint["steps"][0]["reason"]


def test_soak_without_numeric_measurements_cannot_start_a_passing_segment(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )

    class SoakHA:
        origin = "https://ha.example.test"

        def hil_capabilities(self, _entry):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 1,
                "model": "R600",
                "profile": "r600-hw-0.3",
                "settings_options": {
                    "keepalive_enabled": True,
                    "keepalive_interval_seconds": 540,
                    "stale_timeout_seconds": 600,
                },
                "capabilities": {"write_settings_keepalive": True},
            }

    def interrupt_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "monotonic", lambda: 100.0)
    monkeypatch.setattr(cli, "sleep", interrupt_sleep)
    monkeypatch.setattr(
        cli,
        "read_only_sample",
        lambda *_: {
            key: {"value": value, "source": "ha_rest_entity_state", "verified": True}
            for key, value in {
                "connected": "on",
                "telemetry_available": "on",
                "hardware_version": "0.3",
                "firmware_version": "0.2",
            }.items()
        },
    )
    output_dir = tmp_path / "soak-non-measurements"
    output_dir.mkdir(mode=0o700)

    result = cli._execute_soak_suite(
        SoakHA(),
        SimpleNamespace(route="auto"),
        device,
        output_dir,
        "0123456789abcdef",
    )

    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    sample_lines = (output_dir / "soak_samples_001.jsonl").read_text().splitlines()
    assert result == 2
    assert checkpoint["steps"] == []
    assert json.loads(sample_lines[-1])["event"] == "sample_gap"


class _SoakObserverStarted(Exception):
    """Stop gate tests at the first point where soak observation would begin."""


def _soak_gate_services(prompt):
    from types import SimpleNamespace

    def start_observer():
        raise _SoakObserverStarted

    scenario = SimpleNamespace(
        id="soak_24h_observation",
        parameters={"duration_seconds": 86_400, "trace_interval_seconds": 3_600},
    )
    return SimpleNamespace(
        load_scenarios=lambda: [scenario],
        device_fingerprint=lambda *_: "private-test-fingerprint",
        write_scenario_progress=lambda *_: None,
        monotonic=start_observer,
        prompt=prompt,
    )


def _soak_gate_snapshot(*, enabled, supported=True, include_settings=True):
    snapshot = {
        "verified": True,
        "identity_conflicted": False,
        "connected": True,
        "status_fresh": True,
        "session_generation": 1,
        "capabilities": {"write_settings_keepalive": supported},
    }
    if include_settings:
        snapshot["settings_options"] = {
            "keepalive_enabled": enabled,
            "keepalive_interval_seconds": 540,
            "stale_timeout_seconds": 600,
        }
    return snapshot


def _run_soak_gate(api_fake, services, tmp_path, *, non_interactive, allow=False):
    from types import SimpleNamespace

    from scripts.hil.soak import _execute_soak_suite

    device = SimpleNamespace(config_entry_id="entry-safe", entry_state="loaded")
    return _execute_soak_suite(
        api_fake,
        SimpleNamespace(
            route="auto",
            non_interactive=non_interactive,
            allow_keepalive_disabled=allow,
        ),
        device,
        tmp_path,
        "0123456789abcdef",
        services,
    )


def test_soak_keepalive_gate_blocks_noninteractive_without_override(tmp_path, capsys):
    class FakeHA:
        origin = "https://ha.example.test"
        websocket_calls = []

        def hil_capabilities(self, _entry):
            return _soak_gate_snapshot(enabled=False)

        def websocket(self, commands):
            self.websocket_calls.extend(commands)
            return []

    api_fake = FakeHA()
    samples = []
    services = _soak_gate_services(prompt=lambda _question: "")
    services.read_only_sample = lambda *_: samples.append("sample") or {}

    result = _run_soak_gate(api_fake, services, tmp_path, non_interactive=True)

    assert result == 2
    assert "Enable settings keepalive" in capsys.readouterr().out
    assert samples == []
    assert api_fake.websocket_calls == []


def test_soak_keepalive_gate_rereads_after_interactive_enable(tmp_path):
    class FakeHA:
        origin = "https://ha.example.test"
        websocket_calls = []

        def __init__(self):
            self.snapshots = [
                _soak_gate_snapshot(enabled=False),
                _soak_gate_snapshot(enabled=True),
            ]
            self.capability_reads = 0

        def hil_capabilities(self, _entry):
            self.capability_reads += 1
            return self.snapshots.pop(0)

        def websocket(self, commands):
            self.websocket_calls.extend(commands)
            return []

    api_fake = FakeHA()
    prompts = []
    services = _soak_gate_services(
        prompt=lambda question: prompts.append(question) or ""
    )

    with pytest.raises(_SoakObserverStarted):
        _run_soak_gate(api_fake, services, tmp_path, non_interactive=False)

    assert api_fake.capability_reads == 2
    assert len(prompts) == 1
    assert api_fake.websocket_calls == []


def test_soak_keepalive_override_starts_with_warning_and_no_writes(tmp_path, capsys):
    class FakeHA:
        origin = "https://ha.example.test"
        websocket_calls = []

        def hil_capabilities(self, _entry):
            return _soak_gate_snapshot(enabled=False)

        def websocket(self, commands):
            self.websocket_calls.extend(commands)
            return []

    api_fake = FakeHA()
    services = _soak_gate_services(prompt=lambda _question: "")

    with pytest.raises(_SoakObserverStarted):
        _run_soak_gate(
            api_fake,
            services,
            tmp_path,
            non_interactive=True,
            allow=True,
        )

    assert "explicit override" in capsys.readouterr().out
    assert api_fake.websocket_calls == []


@pytest.mark.parametrize(
    ("include_settings", "supported"),
    [(False, True), (True, False)],
)
def test_soak_keepalive_override_still_requires_verified_supported_option(
    tmp_path, include_settings, supported
):
    class FakeHA:
        origin = "https://ha.example.test"
        websocket_calls = []

        def hil_capabilities(self, _entry):
            return _soak_gate_snapshot(
                enabled=False,
                supported=supported,
                include_settings=include_settings,
            )

        def websocket(self, commands):
            self.websocket_calls.extend(commands)
            return []

    api_fake = FakeHA()
    services = _soak_gate_services(prompt=lambda _question: "")

    result = _run_soak_gate(
        api_fake,
        services,
        tmp_path,
        non_interactive=True,
        allow=True,
    )

    assert result == 2
    assert api_fake.websocket_calls == []


def test_soak_fake_clock_requires_and_records_full_24_hours(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta
    import os
    from types import SimpleNamespace

    from scripts.hil.discovery import Device

    class FakeClock:
        value = 10_000.0

        def monotonic(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds

    class FakeDateTime:
        @staticmethod
        def now(tz=UTC):
            base = datetime(2026, 1, 1, tzinfo=tz)
            return base + timedelta(seconds=clock.value - 10_000.0)

    class SoakHA:
        origin = "https://ha.example.test"

        def __init__(self):
            self.capture = 0
            self.commands = []

        def hil_capabilities(self, entry_id):
            return {
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "settings_fresh": True,
                "session_generation": 1,
                "model": "R600",
                "profile": "r600-hw-0.3",
                "settings_options": {
                    "keepalive_enabled": True,
                    "keepalive_interval_seconds": 540,
                    "stale_timeout_seconds": 600,
                },
                "capabilities": {"write_settings_keepalive": True},
            }

        def websocket(self, commands):
            command = commands[0]
            self.commands.append(command["type"])
            if command["type"].endswith("/start"):
                self.capture += 1
                return [
                    {
                        "schema_version": 2,
                        "capture_id": f"capture-{self.capture}",
                        "ttl_seconds": 30,
                    }
                ]
            return [
                {
                    "schema_version": 2,
                    "capture_id": command["capture_id"],
                    "observed_route": "local_adapter",
                    "complete": True,
                }
            ]

    clock = FakeClock()
    api_fake = SoakHA()
    device = Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        entry_state="loaded",
        config_entry_id="entry-safe",
    )
    output_dir = tmp_path / "soak"
    output_dir.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "monotonic", clock.monotonic)
    monkeypatch.setattr(cli, "sleep", clock.sleep)
    monkeypatch.setattr(cli, "datetime", FakeDateTime)
    monkeypatch.setattr(
        cli,
        "read_only_sample",
        lambda *_: {
            "battery": {"value": 89, "source": "ha_rest_entity_state", "verified": True}
        },
    )
    monkeypatch.setattr(cli, "_validate_trace_result", lambda *args, **kwargs: None)

    def save_trace(trace_dir, trace):
        fd = os.open(
            trace_dir / "trace.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"observed_route": trace["observed_route"]}, stream)

    monkeypatch.setattr(cli, "_save_private_trace", save_trace)

    result = cli._execute_soak_suite(
        api_fake,
        SimpleNamespace(route="auto"),
        device,
        output_dir,
        "0123456789abcdef",
    )

    assert result == 0
    checkpoint = json.loads((output_dir / "checkpoint.json").read_text())
    segment = checkpoint["steps"][-1]
    assert segment["outcome"] == "pass"
    assert segment["measured_seconds"] >= 86400
    assert segment["sample_count"] == 289
    assert segment["trace_count"] >= 24
    assert len(api_fake.commands) == 48
    assert all(
        command in {"allpowers_ble/hil/start", "allpowers_ble/hil/stop"}
        for command in api_fake.commands
    )
    assert (output_dir / "soak_samples_001.jsonl").exists()


def test_scenario_control_eligibility_requires_fresh_authoritative_capabilities():
    device = discovery.Device(
        device_id="private-device",
        model="R600",
        hardware="0.3",
        firmware="0.2",
        state_entities={},
        control_entities={
            "ac_output": "switch.renamed_ac",
            "eco_mode": "switch.renamed_eco",
            "car_charger": "switch.renamed_car",
        },
    )

    class Controls:
        @staticmethod
        def control_state(key, entity_id):
            return {"available": True, "state": "off", "options": []}

    capabilities = {
        "connected": True,
        "pending_transaction": False,
        "status_fresh": True,
        "settings_fresh": False,
        "capabilities": {
            "write_output_controls": True,
            "write_settings_controls": True,
            "car_charger_enabled": False,
        },
    }

    result = cli._scenario_controls(Controls(), device, capabilities)

    assert result["ac_output"]["eligible"] is True
    assert result["eco_mode"]["eligible"] is False
    assert result["car_charger"]["eligible"] is False
    assert result["dc_output"]["available"] is False


def test_capability_snapshot_is_strict_and_discards_unrecognized_fields(monkeypatch):
    """Normalize the admin-only reply without retaining IDs or unknown fields."""
    client = api.HAClient("http://127.0.0.1:8123", "token")
    response = {
        "schema_version": 1,
        "model": "R600",
        "profile": "r600-hw-0.3",
        "classification": "verified",
        "verified": True,
        "identity_conflicted": False,
        "hardware_revision": "0.3",
        "raw_hardware_revision": 3,
        "firmware_revision": "0.2",
        "raw_firmware_revision": 2,
        "capabilities": {
            "read_telemetry": True,
            "write_output_controls": True,
            "write_settings_controls": True,
            "write_settings_keepalive": True,
            "car_charger_enabled": False,
        },
        "connected": True,
        "status_fresh": True,
        "settings_fresh": True,
        "session_generation": 4,
        "pending_transaction": False,
        "entry_id": "private-entry",
        "raw_address": "AA:BB:CC:DD:EE:FF",
    }
    calls = []
    monkeypatch.setattr(
        client, "websocket", lambda commands: calls.append(commands) or [response]
    )

    normalized = client.hil_capabilities("entry-123")

    assert calls == [
        [{"type": "allpowers_ble/hil/capabilities", "entry_id": "entry-123"}]
    ]
    assert normalized["model"] == "R600"
    assert normalized["session_generation"] == 4
    assert "entry_id" not in normalized
    assert "raw_address" not in normalized


def test_capability_snapshot_rejects_unsafe_model_and_flags(monkeypatch):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    monkeypatch.setattr(
        client,
        "websocket",
        lambda commands: [
            {
                "schema_version": 1,
                "model": "R600 AA:BB:CC:DD:EE:FF",
                "profile": "r600-hw-0.3",
                "classification": "verified",
                "verified": True,
                "identity_conflicted": False,
                "connected": True,
                "status_fresh": True,
                "settings_fresh": True,
                "capabilities": {},
            }
        ],
    )

    with pytest.raises(api.HAAPIError):
        client.hil_capabilities("entry-123")


def test_capability_snapshot_accepts_strict_v2_feature_facts(monkeypatch):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    response = {
        "schema_version": 2,
        "model": "R600",
        "profile": "r600-hw-0.3",
        "classification": "verified",
        "verified": True,
        "identity_conflicted": False,
        "hardware_revision": "0.3",
        "raw_hardware_revision": 3,
        "firmware_revision": None,
        "raw_firmware_revision": None,
        "capabilities": {
            "read_telemetry": True,
            "write_output_controls": True,
            "write_settings_controls": True,
            "write_settings_keepalive": True,
            "car_charger_enabled": False,
        },
        "connected": True,
        "status_fresh": True,
        "settings_fresh": True,
        "session_generation": 1,
        "pending_transaction": False,
        "features": {
            name: {
                "physical_presence": "unknown",
                "production_authorized": True,
                "candidate_hil_eligible": False,
                "runtime_ready": True,
            }
            for name in (
                "ac_output",
                "dc_output",
                "light_output",
                "eco_mode",
                "work_mode",
                "eco_timeout",
                "car_charger",
                "telemetry",
            )
        },
    }
    monkeypatch.setattr(client, "websocket", lambda _commands: [response])
    normalized = client.hil_capabilities("entry-123")
    assert normalized["features"]["ac_output"]["runtime_ready"] is True


@pytest.mark.parametrize(
    ("model", "profile"),
    [
        ("AP R1500 LITE", "ap-r1500-lite-hw-0.1-evidence"),
        ("S300", "ap-s300-evidence"),
    ],
)
def test_capability_snapshot_accepts_read_only_models_and_redacts_identity(
    monkeypatch, model, profile
):
    client = api.HAClient("http://127.0.0.1:8123", "token")
    names = (
        "ac_output",
        "dc_output",
        "light_output",
        "eco_mode",
        "work_mode",
        "eco_timeout",
        "car_charger",
        "telemetry",
    )
    response = {
        "schema_version": 2,
        "model": model,
        "profile": profile,
        "classification": "experimental_read_only",
        "verified": False,
        "identity_conflicted": False,
        "hardware_revision": "0.1",
        "raw_hardware_revision": 1,
        "firmware_revision": None,
        "raw_firmware_revision": None,
        "capabilities": {
            "read_telemetry": True,
            "write_output_controls": False,
            "write_settings_controls": False,
            "write_settings_keepalive": False,
            "car_charger_enabled": False,
        },
        "features": {
            name: {
                "physical_presence": "absent" if name == "light_output" else "present",
                "production_authorized": False,
                "candidate_hil_eligible": False,
                "runtime_ready": True,
            }
            for name in names
        },
        "connected": True,
        "status_fresh": True,
        "settings_fresh": True,
        "session_generation": 1,
        "pending_transaction": False,
        "raw_address": "AA:BB:CC:DD:EE:FF",
    }
    monkeypatch.setattr(client, "websocket", lambda _commands: [response])
    normalized = client.hil_capabilities("entry-123")
    assert normalized["model"] == model
    assert normalized["features"]["light_output"]["physical_presence"] == "absent"
    assert not normalized["features"]["ac_output"]["production_authorized"]
    assert not normalized["features"]["dc_output"]["production_authorized"]
    assert "raw_address" not in normalized
