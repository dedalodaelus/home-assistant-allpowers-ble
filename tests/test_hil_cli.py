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


def test_manifest_version_mismatch_blocks_build_proof(monkeypatch):
    monkeypatch.setattr(environment, "local_manifest_version", lambda: "0.3.0")
    monkeypatch.setattr(
        environment, "local_commit", lambda: environment.unknown("test")
    )
    mismatch = environment.integration_build({"version": "1.2.0"})
    assert mismatch["build_match"] == "mismatch"
    assert mismatch["qualification_allowed"] is False
    same_version = environment.integration_build({"version": "0.3.0"})
    assert same_version["build_match"] == "unverified"
    assert same_version["qualification_allowed"] is False
    assert environment.integration_build(None)["installed_commit"]["value"] is None


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
    public_files = {p.name for p in dirs[0].iterdir()}
    assert public_files == {"session.json", "environment.json"}
    data = json.loads((dirs[0] / "session.json").read_text())
    content = " ".join(p.read_text() for p in dirs[0].iterdir())
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
    sessions = list(tmp_path.glob("*/session.json"))
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
    path = next(tmp_path.iterdir()) / "session.json"
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
    env = json.loads((next(tmp_path.iterdir()) / "environment.json").read_text())
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
    (directory,) = list(output.iterdir())
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
    (directory,) = list(output.iterdir())
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
    (directory,) = list(root.iterdir())
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
    (directory,) = list(root.iterdir())
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
    (directory,) = list(root.iterdir())
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
    (directory,) = list(root.iterdir())
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
    (directory,) = list(root.iterdir())
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
    (directory,) = list(root.iterdir())
    trace = json.loads((directory / "trace.json").read_text())
    assert trace["frame_count"] == 1
    assert trace["dropped_events"] == 1
    assert trace["complete"] is False
    assert trace["qualification_allowed"] is False
