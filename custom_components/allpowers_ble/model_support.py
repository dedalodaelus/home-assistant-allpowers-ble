"""Model detection and compatibility metadata."""

from __future__ import annotations

from dataclasses import dataclass


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
    normalized_hw = (hardware_version or "").strip()
    revision_profile: str | None = None
    if normalized_hw and raw_hardware_version is not None:
        revision_profile = _VERIFIED_R600_REVISIONS.get(
            (normalized_hw, raw_hardware_version)
        )

    if "R600" in normalized and revision_profile is not None:
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

    if "R600" in normalized:
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
    if normalized.startswith("AP S500") or normalized.startswith("AP S700"):
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
    if normalized.startswith("AP S"):
        return ModelSupport(
            model=normalized.removeprefix("AP "),
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
