"""Shared test helpers."""

from __future__ import annotations

from collections.abc import Callable
import os

import pytest

if os.environ.get("USE_REAL_HOMEASSISTANT") != "1":
    from tests.ha_stubs import install

    install()

from custom_components.allpowers_ble.protocol.codec import append_xor_checksum


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup() -> None:
    """Allow Unix sockets in real-HA mode while keeping host restrictions."""
    if os.environ.get("USE_REAL_HOMEASSISTANT") != "1":
        return

    import pytest_socket

    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


def build_notification(command: int, payload: bytes) -> bytes:
    """Build a checksum-valid vendor notification frame."""
    prefix = bytes((0xA5, 0x65, 0x00, 0x00, 0x00, len(payload), command))
    return append_xor_checksum(prefix + payload)


@pytest.fixture
def notification_builder() -> Callable[[int, bytes], bytes]:
    """Return a notification-frame builder."""
    return build_notification


@pytest.fixture
def status_frame(notification_builder: Callable[[int, bytes], bytes]) -> bytes:
    """Return a representative status frame."""
    return notification_builder(
        0x01,
        bytes((0x13, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78)),
    )


@pytest.fixture
def settings_frame(notification_builder: Callable[[int, bytes], bytes]) -> bytes:
    """Return settings with unknown bits that writes must preserve."""
    return notification_builder(
        0x03,
        bytes((0xB5, 4, 0xAA, 0x55, 0x03, 0xAF)),
    )
