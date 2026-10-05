"""Tests for conservative model-family detection."""

import pytest

from custom_components.allpowers_ble.model_support import identify_model


@pytest.mark.parametrize(
    ("hardware_version", "raw_hardware_version", "expected_profile"),
    (("0.3", 0x03, "r600-hw-0.3"),),
)
def test_r600_is_verified(
    hardware_version: str,
    raw_hardware_version: int,
    expected_profile: str,
) -> None:
    support = identify_model(
        "ALLPOWERS R600",
        hardware_version=hardware_version,
        raw_hardware_version=raw_hardware_version,
    )

    assert support.model == "R600"
    assert support.supported is True
    assert support.verified is True
    assert support.classification == "verified"
    assert support.profile == expected_profile
    assert support.capabilities.write_output_controls is True
    assert support.capabilities.write_settings_controls is True
    assert support.reason is None


def test_r600_unknown_revision_is_read_only_experimental() -> None:
    support = identify_model(
        "ALLPOWERS R600",
        hardware_version="9.9",
        raw_hardware_version=0x99,
    )

    assert support.supported is True
    assert support.verified is False
    assert support.classification == "experimental_read_only"
    assert support.capabilities.read_telemetry is True
    assert support.capabilities.write_output_controls is False
    assert support.capabilities.write_settings_controls is False
    assert support.capabilities.write_settings_keepalive is False


def test_s500_and_s700_are_rejected() -> None:
    for name in ("AP S500", "AP S700 V2"):
        support = identify_model(name)
        assert support.supported is False
        assert support.verified is False
        assert support.classification == "rejected"
        assert "different protocol" in (support.reason or "")


def test_ap_s_family_is_unverified() -> None:
    support = identify_model("AP S300")

    assert support.model == "S300"
    assert support.supported is True
    assert support.verified is False
    assert support.classification == "experimental_read_only"
    assert support.capabilities.write_output_controls is False
    assert support.capabilities.write_settings_controls is False


def test_generic_allpowers_requires_probe() -> None:
    support = identify_model("ALLPOWERS Power Station")

    assert support.supported is True
    assert support.model == "BLE power station"
    assert support.classification == "experimental_read_only"
    assert "telemetry" in (support.reason or "")


def test_service_uuid_only_candidate_requires_probe() -> None:
    support = identify_model("Unknown")

    assert support.supported is True
    assert support.verified is False
    assert support.classification == "experimental_read_only"
    assert "service UUID" in (support.reason or "")


def test_unnamed_candidate_requires_probe() -> None:
    support = identify_model(None)

    assert support.supported is True
    assert support.verified is False
    assert "service UUID" in (support.reason or "")


def test_volix_p1800_hw_0_3_uses_verified_r600_profile() -> None:
    support = identify_model(
        "VOLIX P1800",
        hardware_version="0.3",
        raw_hardware_version=0x03,
    )

    assert support.model == "VOLIX P1800"
    assert support.supported is True
    assert support.verified is True
    assert support.classification == "verified"
    assert support.profile == "r600-hw-0.3"
    assert support.capabilities.write_output_controls is True
    assert support.capabilities.write_settings_controls is True
    assert support.reason is None


def test_volix_p1800_unknown_revision_stays_read_only() -> None:
    for kwargs in ({}, {"hardware_version": "9.9", "raw_hardware_version": 0x99}):
        support = identify_model("VOLIX P1800", **kwargs)
        assert support.supported is True
        assert support.verified is False
        assert support.capabilities.write_output_controls is False
        assert support.capabilities.write_settings_controls is False


def test_other_volix_models_are_not_verified() -> None:
    support = identify_model(
        "VOLIX P2400",
        hardware_version="0.3",
        raw_hardware_version=0x03,
    )

    assert support.verified is False
    assert support.capabilities.write_output_controls is False
