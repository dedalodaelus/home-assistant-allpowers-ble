"""Tests for conservative model-family detection."""

from dataclasses import replace
from typing import cast

import pytest

from custom_components.allpowers_ble.model_support import (
    FeatureQualification,
    ModelFeature,
    ModelSupport,
    PhysicalPresence,
    canonical_model_identity,
    has_specific_model_identity,
    identify_model,
    model_feature_capability,
    resolve_runtime_model_support,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("AP R600 V2.0", "R600"),
        ("AP R600 V2.0 CE3A", "R600"),
        ("ALLPOWERS R600", "R600"),
        ("allpowers r600", "R600"),
        ("R600", "R600"),
        ("Living room R600", None),
        ("AP R600 kitchen", None),
        ("AP R600 V2.0 TEST", None),
        ("AP R6000", None),
        ("AP S", None),
        ("AP S300", "AP S300"),
        ("AP S500 V2.0 CE3A", "AP S500"),
        ("VOLIX P1800 CE3A", "VOLIX P1800"),
        ("AP R1500 LITE", "AP R1500 LITE"),
        ("AP R1500 LITE 1A2B", "AP R1500 LITE"),
        ("AP R1500 LITE kitchen", None),
    ),
)
def test_model_identity_is_specific_and_canonical(
    name: str, expected: str | None
) -> None:
    assert canonical_model_identity(name) == expected
    assert has_specific_model_identity(name) is (expected is not None)


@pytest.mark.parametrize(
    ("hardware_version", "raw_hardware_version", "expected_profile"),
    (("0.3", 0x03, "r600-hw-0.3"),),
)
@pytest.mark.parametrize("name", ["ALLPOWERS R600", "AP R600 V2.0 CE3A"])
def test_r600_is_verified(
    name: str,
    hardware_version: str,
    raw_hardware_version: int,
    expected_profile: str,
) -> None:
    support = identify_model(
        name,
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
    for name in (
        "AP S500",
        "AP S500 V2 CE3A",
        "AP S500 V2.0 CE3A",
        "AP S700 V2",
        "AP S700 V2 CE3A",
        "AP S700 V2.0 CE3A",
    ):
        support = identify_model(name)
        assert support.supported is False
        assert support.verified is False
        assert support.classification == "rejected"
        assert "different protocol" in (support.reason or "")
        assert (
            model_feature_capability(support, ModelFeature.TELEMETRY).read_authorized
            is False
        )


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
    assert (
        model_feature_capability(support, ModelFeature.TELEMETRY).read_authorized
        is True
    )


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


def test_r1500_lite_hw_01_is_evidence_only_and_fail_closed() -> None:
    support = identify_model("AP R1500 LITE 1A2B", hardware_version="0.1")

    assert support.profile == "ap-r1500-lite-hw-0.1-evidence"
    assert support.capabilities.write_output_controls is False
    assert support.capabilities.write_settings_controls is False
    assert (
        model_feature_capability(support, ModelFeature.AC_OUTPUT).physical_presence
        is PhysicalPresence.PRESENT
    )
    for feature in (
        ModelFeature.AC_OUTPUT,
        ModelFeature.DC_OUTPUT,
        ModelFeature.LIGHT_OUTPUT,
        ModelFeature.ECO_MODE,
        ModelFeature.WORK_MODE,
        ModelFeature.ECO_TIMEOUT,
        ModelFeature.CAR_CHARGER,
    ):
        capability = model_feature_capability(support, feature)
        assert capability.production_authorized is False
        assert capability.hil_candidate_eligible is False
    assert (
        model_feature_capability(support, ModelFeature.LIGHT_OUTPUT).physical_presence
        is PhysicalPresence.ABSENT
    )
    assert (
        model_feature_capability(support, ModelFeature.LIGHT_OUTPUT).qualification
        is FeatureQualification.PENDING
    )
    assert (
        model_feature_capability(support, ModelFeature.DC_OUTPUT).physical_presence
        is PhysicalPresence.PRESENT
    )
    assert (
        model_feature_capability(support, ModelFeature.ECO_MODE).physical_presence
        is PhysicalPresence.UNKNOWN
    )


def test_r1500_lite_other_revision_and_conflict_do_not_match_evidence_profile() -> None:
    other_revision = identify_model("AP R1500 LITE", hardware_version="0.2")
    assert other_revision.profile == "ap-r1500-lite-unverified-revision"
    assert (
        model_feature_capability(
            other_revision, ModelFeature.DC_OUTPUT
        ).production_authorized
        is False
    )

    contradictory_revision = identify_model(
        "AP R1500 LITE", hardware_version="0.1", raw_hardware_version=0x02
    )
    assert contradictory_revision.profile == "ap-r1500-lite-unverified-revision"
    assert (
        model_feature_capability(
            contradictory_revision, ModelFeature.AC_OUTPUT
        ).production_authorized
        is False
    )

    conflicted = resolve_runtime_model_support(
        "AP R1500 LITE", hardware_version="0.1", identity_conflicted=True
    )
    assert conflicted.profile == "identity-conflict-read-only"
    assert (
        model_feature_capability(
            conflicted, ModelFeature.AC_OUTPUT
        ).production_authorized
        is False
    )


def test_feature_metadata_preserves_transitional_r600_and_volix_permissions() -> None:
    for name in ("AP R600", "VOLIX P1800"):
        support = identify_model(
            name, hardware_version="0.3", raw_hardware_version=0x03
        )
        for feature in (
            ModelFeature.AC_OUTPUT,
            ModelFeature.DC_OUTPUT,
            ModelFeature.LIGHT_OUTPUT,
            ModelFeature.ECO_MODE,
            ModelFeature.WORK_MODE,
            ModelFeature.ECO_TIMEOUT,
            ModelFeature.CAR_CHARGER,
        ):
            capability = model_feature_capability(support, feature)
            assert capability.production_authorized is True
            assert capability.hil_candidate_eligible is False
            assert capability.qualification is FeatureQualification.PENDING


def test_unknown_feature_profile_and_metadata_are_fail_closed_and_immutable() -> None:
    support = identify_model("Unknown")
    unknown = model_feature_capability(support, "future_control")
    assert unknown.production_authorized is False
    assert unknown.hil_candidate_eligible is False

    missing_support = model_feature_capability(
        cast(ModelSupport, "r600-hw-0.3"), ModelFeature.AC_OUTPUT
    )
    assert missing_support.production_authorized is False

    support = identify_model("AP R600", hardware_version="0.3", raw_hardware_version=3)
    with pytest.raises((AttributeError, TypeError)):
        model_feature_capability(
            support, ModelFeature.AC_OUTPUT
        ).production_authorized = False


def test_feature_permissions_require_consistent_verified_model_support() -> None:
    verified = identify_model(
        "AP R600", hardware_version="0.3", raw_hardware_version=0x03
    )
    readonly = replace(
        identify_model("AP R600", hardware_version="9.9", raw_hardware_version=0x99),
        profile="r600-hw-0.3",
    )
    mismatched_model = replace(verified, model="AP R1500 LITE")
    readonly_capabilities = replace(
        verified, capabilities=identify_model("Unknown").capabilities
    )

    for inconsistent in (readonly, mismatched_model, readonly_capabilities):
        for feature in (ModelFeature.AC_OUTPUT, ModelFeature.ECO_MODE):
            assert (
                model_feature_capability(inconsistent, feature).production_authorized
                is False
            )


def test_unknown_revision_and_identity_conflict_cannot_use_writable_profile() -> None:
    unknown_revision = identify_model(
        "AP R600", hardware_version="9.9", raw_hardware_version=0x99
    )
    conflicted = resolve_runtime_model_support(
        "AP R600",
        hardware_version="0.3",
        raw_hardware_version=0x03,
        identity_conflicted=True,
    )

    for unresolved in (unknown_revision, conflicted):
        assert (
            model_feature_capability(
                unresolved, ModelFeature.AC_OUTPUT
            ).production_authorized
            is False
        )


def test_absent_physical_feature_does_not_assign_hil_qualification() -> None:
    support = identify_model("AP R1500 LITE", hardware_version="0.1")
    light = model_feature_capability(support, ModelFeature.LIGHT_OUTPUT)

    assert light.physical_presence is PhysicalPresence.ABSENT
    assert light.qualification is FeatureQualification.PENDING
    assert light.production_authorized is False
    assert light.hil_candidate_eligible is False
