"""Smoke-test the integration against the real Home Assistant package."""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.homeassistant

MODULES = (
    "custom_components.allpowers_ble",
    "custom_components.allpowers_ble.binary_sensor",
    "custom_components.allpowers_ble.button",
    "custom_components.allpowers_ble.client",
    "custom_components.allpowers_ble.config_flow",
    "custom_components.allpowers_ble.coordinator",
    "custom_components.allpowers_ble.diagnostics",
    "custom_components.allpowers_ble.entity",
    "custom_components.allpowers_ble.number",
    "custom_components.allpowers_ble.select",
    "custom_components.allpowers_ble.sensor",
    "custom_components.allpowers_ble.switch",
)


def test_imports_against_installed_home_assistant() -> None:
    """Import every Home Assistant-facing module with real dependencies."""
    for module_name in MODULES:
        importlib.import_module(module_name)
