"""Model detection and compatibility metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelSupport:
    """Known compatibility information for an advertised device name."""

    model: str
    supported: bool
    verified: bool
    reason: str | None = None


def identify_model(name: str | None) -> ModelSupport:
    """Identify known protocol families without guessing across incompatible units."""
    normalized = (name or "").strip().upper()

    if "R600" in normalized:
        return ModelSupport(model="R600", supported=True, verified=True)
    if normalized.startswith("AP S500") or normalized.startswith("AP S700"):
        return ModelSupport(
            model=normalized.removeprefix("AP "),
            supported=False,
            verified=False,
            reason="This hardware revision is known to use a different protocol",
        )
    if normalized.startswith("AP S"):
        return ModelSupport(
            model=normalized.removeprefix("AP "),
            supported=True,
            verified=False,
            reason="Protocol family match; hardware verification is still required",
        )
    if normalized.startswith("ALLPOWERS"):
        return ModelSupport(
            model="BLE power station",
            supported=True,
            verified=False,
            reason="Generic ALLPOWERS advertisement; verified by GATT probing",
        )
    return ModelSupport(
        model="BLE power station",
        supported=True,
        verified=False,
        reason="Matched by service UUID; verified by GATT probing",
    )
