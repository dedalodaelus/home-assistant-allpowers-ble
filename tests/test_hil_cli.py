"""Offline contract and security tests for the standalone HIL preflight CLI."""

from __future__ import annotations

import json
import re
import sys
from types import ModuleType
from urllib.error import HTTPError

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
            "device_id": "foreign-device-id",
            "platform": "esphome",
            "entity_id": "sensor.foreign_battery",
            "unique_id": "abc_battery",
        },
    ]
    entries = [{"entry_id": "secret-entry-1", "domain": "allpowers_ble"}]
    return entities, devices, entries


class FakeHA:
    def __init__(self, data):
        self.entities, self.devices, self.entries = data
        self.calls = []

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

    def entity_state(self, entity_id):
        self.calls.append(("get", entity_id))
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
    assert api.normalize_url("https://example.test:8123/") == "https://example.test:8123"
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
    assert api.normalize_url(
        "http://10.0.0.4:8123", allow_insecure_http=True
    ) == "http://10.0.0.4:8123"


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


def test_discovery_without_optional_config_entries(allpowers_registry):
    entities, devices, _ = allpowers_registry
    choices = discovery.find_devices(entities, devices, None)
    assert [device.model for device in choices] == ["R600"]


def test_discovery_ignores_registry_entries_with_contradictory_ownership(allpowers_registry):
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
    assert payload["home_assistant"]["installation_type"]["value"] == "Home Assistant Container"
    assert payload["integration"]["qualification_allowed"] is False
    assert payload["home_assistant"]["host"]["bluez_version"]["value"] is None
    assert "runner_host" in payload


def test_manifest_version_mismatch_blocks_build_proof(monkeypatch):
    monkeypatch.setattr(environment, "local_manifest_version", lambda: "0.3.0")
    monkeypatch.setattr(environment, "local_commit", lambda: environment.unknown("test"))
    mismatch = environment.integration_build({"version": "1.2.0"})
    assert mismatch["build_match"] == "mismatch"
    assert mismatch["qualification_allowed"] is False
    same_version = environment.integration_build({"version": "0.3.0"})
    assert same_version["build_match"] == "unverified"
    assert same_version["qualification_allowed"] is False
    assert environment.integration_build(None)["installed_commit"]["value"] is None


def test_read_only_snapshot_excludes_rest_attributes_and_arbitrary_states(allpowers_registry):
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
        "doctor", "--ha-url", "https://example.test", "--non-interactive",
        "--output-root", str(tmp_path / "no-results"),
    )
    assert cli.execute(args) == 0
    captured = capsys.readouterr().out
    assert "ALLPOWERS devices: 1" in captured
    assert "SECRET_BEARER_TOKEN" not in captured
    assert "secret-device-id" not in captured
    assert not (tmp_path / "no-results").exists()
    assert all(call[0] in {"get", "ws"} for call in fake.calls)


def test_run_writes_private_allowlisted_json_without_identifiers(
    monkeypatch, tmp_path, capsys, allpowers_registry
):
    fake = FakeHA(allpowers_registry)
    monkeypatch.setattr(cli, "HAClient", lambda *args, **kwargs: fake)
    monkeypatch.setenv("HA_HIL_TOKEN", "MY_PRIVATE_TOKEN")
    args = make_args(
        "run", "--ha-url", "https://my-secret-host.local:8123",
        "--device", "1", "--route", "active_proxy", "--non-interactive",
        "--output-root", str(tmp_path / "results"),
    )
    assert cli.execute(args) == 0
    dirs = list((tmp_path / "results").iterdir())
    assert len(dirs) == 1
    public_files = {p.name for p in dirs[0].iterdir()}
    assert public_files == {"session.json", "environment.json"}
    data = json.loads((dirs[0] / "session.json").read_text())
    content = " ".join(p.read_text() for p in dirs[0].iterdir())
    for secret in (
        "MY_PRIVATE_TOKEN", "my-secret-host", "AA:BB:CC:DD:EE:FF",
        "secret-entry-1", "secret-device-id", "sensor.my_name",
        "39.4699", "10.0.0.100", "My very private",
    ):
        assert secret not in content
    assert data["route"]["requested"] == "active_proxy"
    assert data["route"]["observed"]["value"] is None
    assert data["qualification_status"] == "inconclusive"
    assert data["integration_build"]["qualification_allowed"] is False
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
    assert cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"]) == 2
    assert cli.main(["run", "--ha-url", "https://example.test", "--non-interactive"]) == 2
    assert "SECRET_VALUE" not in capsys.readouterr().err
    monkeypatch.delenv("HA_HIL_TOKEN")
    assert cli.main(["doctor", "--ha-url", "https://example.test", "--non-interactive"]) == 2


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
        {"type": "auth_required"}, {"type": "auth_ok"},
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
        {"type": "auth_required"}, {"type": "auth_invalid", "message": "PRIVATE_TOKEN"}
    ]
    client_module.connect = lambda url, **kwargs: FakeWS(denied)
    with pytest.raises(api.HAAPIError, match="authentication failed") as exc:
        ha.websocket([{"type": "manifest/get"}])
    assert "PRIVATE_TOKEN" not in str(exc.value)


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
        run_id="0123456789abcdef", requested_route="auto", device=device,
        environment=env, sample={},
    )
    folder = storage.write_run(tmp_path, "safe", env, session)
    saved = (folder / "session.json").read_text()
    assert "secret-device-id" not in saved
    assert "AA:BB" not in saved
    assert session["route"]["qualification_allowed"] is False
