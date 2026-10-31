"""Model detection and compatibility metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re


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


def canonical_model_identity(name: str | None) -> str | None:
    """Return a stable family identity for a specifically identifying name."""
    normalized = (name or "").strip().upper()
    if _R600_IDENTITY_RE.fullmatch(normalized):
        return "R600"
    if _VOLIX_P1800_IDENTITY_RE.fullmatch(normalized):
        return "VOLIX P1800"
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
