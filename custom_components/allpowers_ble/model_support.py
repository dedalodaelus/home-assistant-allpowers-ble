"""Model detection and compatibility metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
import re
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Per-model command authorization flags."""

    read_telemetry: bool
    write_output_controls: bool
    write_settings_controls: bool
    write_settings_keepalive: bool


READ_ONLY_CAPABILITIES = ModelCapabilities(
    read_telemetry=True,
    write_output_controls=False,
    write_settings_controls=False,
    write_settings_keepalive=False,
)

FULL_R600_CAPABILITIES = ModelCapabilities(
    read_telemetry=True,
    write_output_controls=True,
    write_settings_controls=True,
    write_settings_keepalive=True,
)


_VERIFIED_R600_REVISIONS = {
    ("0.3", 0x03): "r600-hw-0.3",
}

_DEVICE_SUFFIX = r"(?:\s+[A-F0-9]{4})?"
_R600_IDENTITY_RE = re.compile(
    rf"^(?:(?:ALLPOWERS|AP)\s+)?R600(?:\s+V\d+(?:\.\d+)*)?{_DEVICE_SUFFIX}$"
)
_VOLIX_P1800_IDENTITY_RE = re.compile(
    rf"^VOLIX\s+P1800(?:\s+V\d+(?:\.\d+)*)?{_DEVICE_SUFFIX}$"
)
_AP_S_IDENTITY_RE = re.compile(
    rf"^AP\s+(S\d{{3,4}})(?:\s+V\d+(?:\.\d+)*)?{_DEVICE_SUFFIX}$"
)
_AP_R1500_LITE_IDENTITY_RE = re.compile(
    rf"^AP\s+R1500\s+LITE(?:\s+V\d+(?:\.\d+)*)?{_DEVICE_SUFFIX}$"
)


class ModelFeature(StrEnum):
    """Stable feature identifiers shared with the HIL evidence matrix."""

    AC_OUTPUT = "ac_output"
    DC_OUTPUT = "dc_output"
    LIGHT_OUTPUT = "light_output"
    ECO_MODE = "eco_mode"
    WORK_MODE = "work_mode"
    ECO_TIMEOUT = "eco_timeout"
    CAR_CHARGER = "car_charger"
    TELEMETRY = "telemetry"


class PhysicalPresence(StrEnum):
    """Evidence-backed physical presence; protocol bits do not establish it."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class FeatureQualification(StrEnum):
    """Evidence-matrix state, kept separate from either authorization."""

    PENDING = "pending"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class ModelFeatureCapability:
    """Immutable metadata for one feature in an exact model profile."""

    feature: ModelFeature | str
    physical_presence: PhysicalPresence
    production_authorized: bool = False
    hil_candidate_eligible: bool = False
    qualification: FeatureQualification = FeatureQualification.PENDING
    read_authorized: bool = False
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class ModelFeatureProfile:
    """Immutable feature inventory scoped to a model and hardware revision."""

    profile: str
    model: str
    hardware_revision: str
    firmware_evidence: str | None
    features: Mapping[ModelFeature, ModelFeatureCapability]


def _feature_profile(
    profile: str,
    model: str,
    hardware_revision: str,
    *,
    physical: Mapping[ModelFeature, PhysicalPresence],
    production: frozenset[ModelFeature] = frozenset(),
    readable: frozenset[ModelFeature] = frozenset(),
    evidence: Mapping[ModelFeature, str] | None = None,
    firmware_evidence: str | None = None,
) -> ModelFeatureProfile:
    evidence = evidence or {}
    features = {
        feature: ModelFeatureCapability(
            feature=feature,
            physical_presence=physical.get(feature, PhysicalPresence.UNKNOWN),
            production_authorized=feature in production,
            hil_candidate_eligible=False,
            qualification=FeatureQualification.PENDING,
            read_authorized=feature in readable,
            evidence=evidence.get(feature, ""),
        )
        for feature in ModelFeature
    }
    return ModelFeatureProfile(
        profile=profile,
        model=model,
        hardware_revision=hardware_revision,
        firmware_evidence=firmware_evidence,
        features=MappingProxyType(features),
    )


_R600_FEATURE_PRODUCTION = frozenset(
    {
        ModelFeature.AC_OUTPUT,
        ModelFeature.DC_OUTPUT,
        ModelFeature.LIGHT_OUTPUT,
        ModelFeature.ECO_MODE,
        ModelFeature.WORK_MODE,
        ModelFeature.ECO_TIMEOUT,
        ModelFeature.CAR_CHARGER,
    }
)
MODEL_FEATURE_PROFILES: Mapping[str, ModelFeatureProfile] = MappingProxyType(
    {
        profile: _feature_profile(
            profile,
            model,
            "0.3",
            physical={},
            production=_R600_FEATURE_PRODUCTION,
            readable=frozenset({ModelFeature.TELEMETRY}),
            evidence={
                feature: "Transitional production allowance from ADR 0017; "
                "physical qualification remains pending."
                for feature in _R600_FEATURE_PRODUCTION
            },
        )
        for profile, model in (
            ("r600-hw-0.3", "R600"),
            ("volix-p1800-hw-0.3", "VOLIX P1800"),
        )
    }
    | {
        "ap-r1500-lite-hw-0.1-evidence": _feature_profile(
            "ap-r1500-lite-hw-0.1-evidence",
            "AP R1500 LITE",
            "0.1",
            firmware_evidence="0.6 reported in issue #248; provenance only",
            physical={
                ModelFeature.AC_OUTPUT: PhysicalPresence.PRESENT,
                ModelFeature.DC_OUTPUT: PhysicalPresence.PRESENT,
                ModelFeature.LIGHT_OUTPUT: PhysicalPresence.ABSENT,
            },
            readable=frozenset({ModelFeature.TELEMETRY}),
            evidence={
                ModelFeature.AC_OUTPUT: (
                    "AC off/on were reported in issue #248. Follow-ups also report "
                    "about 20 seconds of telemetry silence/reconnects and an AC-off "
                    "service call that returned success without changing the output. "
                    "The earlier E2 was attributed to a pre-existing fault after "
                    "restart; no write authorization."
                ),
                ModelFeature.DC_OUTPUT: (
                    "DC on/off were reported in issue #248. A DC write was once "
                    "followed by about 20 seconds of telemetry silence/reconnect; "
                    "no write authorization."
                ),
                ModelFeature.LIGHT_OUTPUT: (
                    "No physical light reported; echoed protocol bit 0x20 is not proof."
                ),
                ModelFeature.ECO_MODE: (
                    "ECO on/off transitions were reported in issue #248. Settings "
                    "writes were followed by telemetry silence/reconnects; no write "
                    "authorization."
                ),
                ModelFeature.WORK_MODE: (
                    "Work-mode transitions were reported in issue #248. Settings "
                    "writes were followed by telemetry silence/reconnects; no write "
                    "authorization."
                ),
                ModelFeature.ECO_TIMEOUT: (
                    "ECO timeout 6 h to 2 h and back was reported in issue #248. "
                    "Settings writes were followed by telemetry silence/reconnects; "
                    "no write authorization."
                ),
                ModelFeature.CAR_CHARGER: (
                    "Car-charger control was not tested in issue #248."
                ),
            },
        )
    }
)


def model_feature_capability(
    support: ModelSupport, feature: ModelFeature | str
) -> ModelFeatureCapability:
    """Return effective feature metadata for a consistent resolved device."""
    try:
        feature_id = ModelFeature(feature)
    except ValueError:
        # Unknown feature IDs have no permission and no physical inference.
        return ModelFeatureCapability(
            feature=str(feature),
            physical_presence=PhysicalPresence.UNKNOWN,
            evidence="Unknown feature identifier",
        )
    if not isinstance(support, ModelSupport):
        return ModelFeatureCapability(
            feature=feature_id,
            physical_presence=PhysicalPresence.UNKNOWN,
            evidence="A resolved model support record is required",
        )

    profile_id = support.profile
    profile_model: str | None = None
    legacy_write_enabled = False
    if (
        support.model == "R600"
        and profile_id == "r600-hw-0.3"
        and support.supported
        and support.verified
        and support.classification == "verified"
    ):
        profile_model = "R600"
        legacy_write_enabled = (
            support.capabilities.write_output_controls
            if feature_id
            in {
                ModelFeature.AC_OUTPUT,
                ModelFeature.DC_OUTPUT,
                ModelFeature.LIGHT_OUTPUT,
            }
            else support.capabilities.write_settings_controls
        )
    elif (
        support.model == "VOLIX P1800"
        and profile_id == "r600-hw-0.3"
        and support.supported
        and support.verified
        and support.classification == "verified"
    ):
        # Keep the historical shared R600 code-profile alias for VOLIX.
        profile_id = "volix-p1800-hw-0.3"
        profile_model = "VOLIX P1800"
        legacy_write_enabled = (
            support.capabilities.write_output_controls
            if feature_id
            in {
                ModelFeature.AC_OUTPUT,
                ModelFeature.DC_OUTPUT,
                ModelFeature.LIGHT_OUTPUT,
            }
            else support.capabilities.write_settings_controls
        )
    elif (
        support.model == "AP R1500 LITE"
        and profile_id == "ap-r1500-lite-hw-0.1-evidence"
        and support.supported
        and not support.verified
        and support.classification == "experimental_read_only"
    ):
        profile_model = "AP R1500 LITE"

    record = MODEL_FEATURE_PROFILES.get(profile_id)
    if record is not None and record.model == profile_model:
        declaration = record.features[feature_id]
        return replace(
            declaration,
            production_authorized=(
                declaration.production_authorized and legacy_write_enabled
            ),
            read_authorized=(
                declaration.read_authorized and support.capabilities.read_telemetry
            ),
        )
    # Preserve the established telemetry-only fallback without granting writes.
    return ModelFeatureCapability(
        feature=feature_id,
        physical_presence=PhysicalPresence.UNKNOWN,
        read_authorized=(
            feature_id is ModelFeature.TELEMETRY and support.capabilities.read_telemetry
        ),
        evidence="No consistent exact model/revision feature profile is available",
    )


def canonical_model_identity(name: str | None) -> str | None:
    """Return a stable family identity for a specifically identifying name."""
    normalized = (name or "").strip().upper()
    if _R600_IDENTITY_RE.fullmatch(normalized):
        return "R600"
    if _VOLIX_P1800_IDENTITY_RE.fullmatch(normalized):
        return "VOLIX P1800"
    if _AP_R1500_LITE_IDENTITY_RE.fullmatch(normalized):
        return "AP R1500 LITE"
    match = _AP_S_IDENTITY_RE.fullmatch(normalized)
    if match:
        return f"AP {match.group(1)}"
    return None


def has_specific_model_identity(name: str | None) -> bool:
    """Return whether a name identifies a model family rather than a placeholder."""
    return canonical_model_identity(name) is not None


@dataclass(frozen=True, slots=True)
class ModelSupport:
    """Known compatibility information for an advertised device name."""

    model: str
    supported: bool
    verified: bool
    classification: str
    profile: str
    capabilities: ModelCapabilities
    evidence: str
    reason: str | None = None


def identify_model(
    name: str | None,
    *,
    hardware_version: str | None = None,
    raw_hardware_version: int | None = None,
) -> ModelSupport:
    """Identify known protocol families without guessing across incompatible units."""
    normalized = (name or "").strip().upper()
    identity = canonical_model_identity(name)
    normalized_hw = (hardware_version or "").strip()
    revision_profile: str | None = None
    if normalized_hw and raw_hardware_version is not None:
        revision_profile = _VERIFIED_R600_REVISIONS.get(
            (normalized_hw, raw_hardware_version)
        )

    if identity == "VOLIX P1800" and revision_profile is not None:
        return ModelSupport(
            model="VOLIX P1800",
            supported=True,
            verified=True,
            classification="verified",
            profile=revision_profile,
            capabilities=FULL_R600_CAPABILITIES,
            evidence=(
                "VOLIX P1800 reports the verified R600 hardware revision and uses the "
                "same status/settings frames"
            ),
        )

    if identity == "R600" and revision_profile is not None:
        return ModelSupport(
            model="R600",
            supported=True,
            verified=True,
            classification="verified",
            profile=revision_profile,
            capabilities=FULL_R600_CAPABILITIES,
            evidence=(
                "Verified R600 profile matched by model family and hardware revision"
            ),
        )

    if identity == "AP R1500 LITE":
        if normalized_hw == "0.1":
            return ModelSupport(
                model="AP R1500 LITE",
                supported=True,
                verified=False,
                classification="experimental_read_only",
                profile="ap-r1500-lite-hw-0.1-evidence",
                capabilities=READ_ONLY_CAPABILITIES,
                evidence=(
                    "Exact AP R1500 LITE hardware 0.1 evidence profile; firmware 0.6 "
                    "is provenance only and grants no write authorization"
                ),
                reason="Physical controls remain read-only pending maintainer review",
            )
        return ModelSupport(
            model="AP R1500 LITE",
            supported=True,
            verified=False,
            classification="experimental_read_only",
            profile="ap-r1500-lite-unverified-revision",
            capabilities=READ_ONLY_CAPABILITIES,
            evidence="AP R1500 LITE identity without the exact hardware 0.1 revision",
            reason="Only the documented hardware 0.1 evidence profile is known",
        )

    if identity == "R600":
        return ModelSupport(
            model="R600",
            supported=True,
            verified=False,
            classification="experimental_read_only",
            profile="r600-unverified-revision",
            capabilities=READ_ONLY_CAPABILITIES,
            evidence=(
                "R600 family detected but hardware revision is not in the verified "
                "capability map"
            ),
            reason=(
                "R600 detected, but this hardware revision is not yet verified for "
                "write controls"
            ),
        )
    if identity in {"AP S500", "AP S700"}:
        return ModelSupport(
            model=normalized.removeprefix("AP "),
            supported=False,
            verified=False,
            classification="rejected",
            profile="rejected-known-different-protocol",
            capabilities=READ_ONLY_CAPABILITIES,
            evidence="Known incompatible protocol family",
            reason="This hardware revision is known to use a different protocol",
        )
    if identity is not None and identity.startswith("AP S"):
        return ModelSupport(
            model=identity.removeprefix("AP "),
            supported=True,
            verified=False,
            classification="experimental_read_only",
            profile="ap-s-experimental",
            capabilities=READ_ONLY_CAPABILITIES,
            evidence=(
                "Protocol family candidate passed discovery/probe gates but lacks "
                "write-capability verification"
            ),
            reason=(
                "Protocol family match; hardware verification is still required and "
                "write controls remain disabled"
            ),
        )
    if normalized.startswith("ALLPOWERS"):
        return ModelSupport(
            model="BLE power station",
            supported=True,
            verified=False,
            classification="experimental_read_only",
            profile="generic-allpowers-experimental",
            capabilities=READ_ONLY_CAPABILITIES,
            evidence="Generic advertisement with probe-only validation",
            reason=(
                "Generic ALLPOWERS advertisement; only telemetry is enabled until "
                "write capabilities are verified"
            ),
        )
    return ModelSupport(
        model="BLE power station",
        supported=True,
        verified=False,
        classification="experimental_read_only",
        profile="service-uuid-experimental",
        capabilities=READ_ONLY_CAPABILITIES,
        evidence="Service UUID candidate with probe-only validation",
        reason=(
            "Matched by service UUID; only telemetry is enabled until write "
            "capabilities are verified"
        ),
    )


def resolve_runtime_model_support(
    name: str | None,
    *,
    hardware_version: str | None = None,
    raw_hardware_version: int | None = None,
    identity_conflicted: bool = False,
) -> ModelSupport:
    """Apply one shared runtime authorization policy to the observed evidence."""
    support = identify_model(
        name,
        hardware_version=hardware_version,
        raw_hardware_version=raw_hardware_version,
    )
    if not identity_conflicted:
        return support
    return replace(
        support,
        verified=False,
        classification="identity_conflict",
        profile="identity-conflict-read-only",
        capabilities=READ_ONLY_CAPABILITIES,
        reason="Conflicting model identities require a fresh identifying observation",
    )
