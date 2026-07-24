"""Tests for conservative model-family detection."""

from custom_components.allpowers_ble.model_support import identify_model


def test_r600_is_verified() -> None:
    support = identify_model("ALLPOWERS R600")

    assert support.model == "R600"
    assert support.supported is True
    assert support.verified is True
    assert support.reason is None


def test_s500_and_s700_are_rejected() -> None:
    for name in ("AP S500", "AP S700 V2"):
        support = identify_model(name)
        assert support.supported is False
        assert support.verified is False
        assert "different protocol" in (support.reason or "")


def test_ap_s_family_is_unverified() -> None:
    support = identify_model("AP S300")

    assert support.model == "S300"
    assert support.supported is True
    assert support.verified is False


def test_generic_allpowers_requires_probe() -> None:
    support = identify_model("ALLPOWERS Power Station")

    assert support.supported is True
    assert support.model == "BLE power station"
    assert "GATT" in (support.reason or "")


def test_service_uuid_only_candidate_requires_probe() -> None:
    support = identify_model("Unknown")

    assert support.supported is True
    assert support.verified is False
    assert "service UUID" in (support.reason or "")


def test_unnamed_candidate_requires_probe() -> None:
    support = identify_model(None)

    assert support.supported is True
    assert support.verified is False
    assert "service UUID" in (support.reason or "")
