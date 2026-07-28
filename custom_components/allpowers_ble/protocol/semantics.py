"""Profile-driven semantic validation for write authorization."""

from __future__ import annotations

from .codec import STATUS_AC_MASK, STATUS_DC_MASK, STATUS_LIGHT_MASK, VALID_ECO_TIMEOUTS
from .models import SettingsData, StatusData

_R600_HW_03_PROFILE = "r600-hw-0.3"
_KNOWN_R600_STATUS_MASK = STATUS_DC_MASK | STATUS_AC_MASK | STATUS_LIGHT_MASK


def status_write_validation_errors(profile: str, status: StatusData) -> tuple[str, ...]:
    """Return semantic validation errors that should block output writes."""
    if profile != _R600_HW_03_PROFILE:
        return ()

    errors: list[str] = []
    unknown_status_bits = status.raw_flags & ~_KNOWN_R600_STATUS_MASK
    if unknown_status_bits:
        errors.append(
            "status contains unknown flag bits "
            f"0x{unknown_status_bits:02X}; cannot safely preserve unrelated output flags"
        )
    return tuple(errors)


def settings_write_validation_errors(
    profile: str,
    settings: SettingsData,
) -> tuple[str, ...]:
    """Return semantic validation errors that should block settings writes."""
    if profile != _R600_HW_03_PROFILE:
        return ()

    errors: list[str] = []
    if settings.work_mode is None:
        errors.append(
            "settings report a reserved work mode value; cannot safely reuse this snapshot"
        )
    if settings.eco_timeout_hours not in VALID_ECO_TIMEOUTS:
        errors.append(
            "settings report unsupported ECO timeout "
            f"{settings.eco_timeout_hours}; cannot safely reuse this snapshot"
        )
    return tuple(errors)
