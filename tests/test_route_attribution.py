"""Connection-time route attribution is strict, private and fail-closed."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.allpowers_ble import route_attribution as routes


class Wrapper:
    """Connected HA wrapper stand-in; a non-wrapper must fail."""

    def __init__(self, backend, scanner, *, connected=True):
        self.is_connected = connected
        self._backend = backend
        self._connected_scanner = scanner


class Local:
    def __init__(self, adapter="hci0", index=0):
        self.adapter = adapter
        self.adapter_idx = index
        self.source = "advertisement-is-not-proof"


class BlueZ:
    def __init__(self, path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", *, connected=True):
        self.is_connected = connected
        self._device_path = path


class ESPHomeScanner:
    def __init__(self, source="proxy-private-id"):
        self.source = source


class ESPHomeClient:
    def __init__(self, scanner, source="proxy-private-id", *, connected=True):
        self.is_connected = connected
        self._scanner = scanner
        self._source = source


@pytest.fixture(autouse=True)
def fake_ha_backend_types(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_backend_types",
        lambda: (Wrapper, Local, BlueZ, ESPHomeScanner, ESPHomeClient),
    )


@pytest.mark.parametrize("index", [0, 1])
def test_proven_local_backend_requires_exact_bluez_adapter_path(index):
    adapter = f"hci{index}"
    scanner = Local(adapter, index)
    backend = BlueZ(f"/org/bluez/{adapter}/dev_AA_BB_00_FF")
    observed = routes.connected_route(Wrapper(backend, scanner))
    assert observed == routes.ConnectedRoute(
        "local_adapter", "bluez", "bluez_device_path", adapter_id=adapter
    )
    assert not hasattr(observed, "_backend")
    assert not hasattr(observed, "_scanner")
    assert "AA_BB" not in str(observed)
    with pytest.raises(FrozenInstanceError):
        observed.observed_route = "active_proxy"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("adapter", "index", "path"),
    [
        ("hci0", 1, "/org/bluez/hci0/dev_AA_BB"),
        ("hci0", 0, "/org/bluez/hci1/dev_AA_BB"),
        ("hci", None, "/org/bluez/hci0/dev_AA_BB"),
        ("hciabc", 0, "/org/bluez/hci0/dev_AA_BB"),
        ("hci0", 0, "/org/bluez/hci0/not_a_device"),
        ("hci0", 0, "/org/bluez/hci0/dev_AA_BB\nSECRET"),
    ],
)
def test_wrong_or_forged_bluez_path_is_never_proven(adapter, index, path):
    observed = routes.connected_route(Wrapper(BlueZ(path), Local(adapter, index)))
    assert observed.observed_route == "unverified"
    assert observed.evidence == "bluez_path_mismatch"
    assert observed.adapter_id is None


def test_valid_active_proxy_requires_bound_scanner_and_backend():
    scanner = ESPHomeScanner()
    observed = routes.connected_route(Wrapper(ESPHomeClient(scanner), scanner))
    assert observed == routes.ConnectedRoute(
        "active_proxy", "esphome", "esphome_scanner_binding",
        proxy_id="proxy-private-id",
    )


@pytest.mark.parametrize("reason", ["wrong-scanner", "wrong-source", "empty-source", "secret-source"])
def test_proxy_identity_spoofing_not_proven(reason):
    scanner = ESPHomeScanner()
    backend = ESPHomeClient(scanner)
    if reason == "wrong-scanner":
        backend._scanner = ESPHomeScanner()
    elif reason == "wrong-source":
        backend._source = "other"
    elif reason == "empty-source":
        scanner.source = ""
        backend._source = ""
    else:
        scanner.source = "proxy\nPRIVATE"
        backend._source = scanner.source
    observed = routes.connected_route(Wrapper(backend, scanner))
    assert observed.observed_route == "unverified"
    assert observed.proxy_id is None


@pytest.mark.parametrize(
    ("wrapped", "expected"),
    [
        (False, "no_ha_wrapper"),
        (True, "not_connected"),
    ],
)
def test_wrapper_and_connected_state_are_not_inferred(wrapped, expected):
    scanner = Local()
    backend = BlueZ()
    client = Wrapper(backend, scanner, connected=False) if wrapped else object()
    assert routes.connected_route(client).evidence == expected


def test_missing_tracking_incomplete_backend_and_mixed_types_are_unknown():
    scanner = Local()
    for client, reason in (
        (Wrapper(None, scanner), "missing_tracking"),
        (Wrapper(BlueZ(), None), "missing_tracking"),
        (Wrapper(BlueZ(connected=False), scanner), "not_connected"),
        (Wrapper(ESPHomeClient(ESPHomeScanner()), scanner), "local_backend_mismatch"),
        (Wrapper(BlueZ(), ESPHomeScanner()), "esphome_backend_mismatch"),
        (Wrapper(BlueZ(), SimpleNamespace(source="remote")), "unsupported_backend"),
    ):
        evidence = routes.connected_route(client)
        assert evidence.observed_route == "unverified"
        assert evidence.evidence == reason


def test_extractor_handles_faulty_attributes_and_optional_import_failure():
    scanner = Local()
    backend = BlueZ()
    client = Wrapper(backend, scanner)
    with patch.object(routes, "_backend_types", side_effect=ImportError):
        assert routes.connected_route(client).evidence == "inspection_failed"
    client._connected_scanner = Local("hciXXX", 0)
    assert routes.connected_route(client).evidence == "bluez_path_mismatch"

    class Bad:
        @property
        def is_connected(self):
            raise RuntimeError("PRIVATE:DEVICE:MAC")

    assert routes.connected_route(Wrapper(Bad(), scanner)).evidence == "inspection_failed"
    assert "PRIVATE" not in repr(routes.connected_route(Wrapper(Bad(), scanner)))
