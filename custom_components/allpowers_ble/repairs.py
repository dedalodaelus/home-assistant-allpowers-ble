"""Home Assistant Repairs lifecycle for persistent actionable failures."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import AllpowersSnapshot

NO_ROUTE_PERSISTENCE_UPDATES = 3
WATCHDOG_RESET_PERSISTENCE = 3

ISSUE_NO_ROUTE = "persistent_no_route"
ISSUE_WATCHDOG = "repeated_watchdog_resets"
ISSUE_INVALID_MIGRATION = "invalid_migrated_options"


@dataclass(slots=True)
class _RepairTrackingState:
    """Per-entry repair tracking state for deduplication and debounce."""

    no_route_streak: int = 0
    watchdog_baseline: int = 0
    active_no_route: bool = False
    active_watchdog: bool = False


class AllpowersRepairsManager:
    """Emit low-noise Home Assistant Repairs for actionable persistent states."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        entry_title: str,
        initial_snapshot: AllpowersSnapshot,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._entry_title = entry_title
        self._state = _RepairTrackingState(
            watchdog_baseline=initial_snapshot.statistics.watchdog_resets
        )

    @callback
    def evaluate(self, snapshot: AllpowersSnapshot, *, status_is_fresh: bool) -> None:
        """Create or dismiss Repairs from current coordinator state."""
        self._evaluate_persistent_no_route(snapshot)
        self._evaluate_repeated_watchdog(snapshot, status_is_fresh=status_is_fresh)

    @callback
    def clear(self) -> None:
        """Delete Repairs owned by this config entry."""
        if self._state.active_no_route:
            self._delete_issue(ISSUE_NO_ROUTE)
            self._state.active_no_route = False
        if self._state.active_watchdog:
            self._delete_issue(ISSUE_WATCHDOG)
            self._state.active_watchdog = False

    @callback
    def _evaluate_persistent_no_route(self, snapshot: AllpowersSnapshot) -> None:
        no_route = not snapshot.connected and (snapshot.last_error or "").startswith(
            "DeviceNotFoundError:"
        )
        if no_route:
            self._state.no_route_streak += 1
            if (
                self._state.no_route_streak >= NO_ROUTE_PERSISTENCE_UPDATES
                and not self._state.active_no_route
            ):
                self._create_issue(
                    ISSUE_NO_ROUTE,
                    translation_key=ISSUE_NO_ROUTE,
                    severity=ir.IssueSeverity.WARNING,
                )
                self._state.active_no_route = True
            return

        self._state.no_route_streak = 0
        if self._state.active_no_route:
            self._delete_issue(ISSUE_NO_ROUTE)
            self._state.active_no_route = False

    @callback
    def _evaluate_repeated_watchdog(
        self,
        snapshot: AllpowersSnapshot,
        *,
        status_is_fresh: bool,
    ) -> None:
        repeated_watchdog = (
            snapshot.statistics.watchdog_resets - self._state.watchdog_baseline
            >= WATCHDOG_RESET_PERSISTENCE
        )
        should_raise = repeated_watchdog and not status_is_fresh

        if should_raise and not self._state.active_watchdog:
            self._create_issue(
                ISSUE_WATCHDOG,
                translation_key=ISSUE_WATCHDOG,
                severity=ir.IssueSeverity.WARNING,
            )
            self._state.active_watchdog = True
            return

        if status_is_fresh:
            self._state.watchdog_baseline = snapshot.statistics.watchdog_resets
            if self._state.active_watchdog:
                self._delete_issue(ISSUE_WATCHDOG)
                self._state.active_watchdog = False

    def _create_issue(
        self,
        issue_key: str,
        *,
        translation_key: str,
        severity: ir.IssueSeverity,
    ) -> None:
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._issue_id(issue_key),
            issue_domain=DOMAIN,
            is_fixable=True,
            is_persistent=True,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders={"entry_title": self._entry_title},
            learn_more_url=(
                "https://github.com/dedalodaelus/home-assistant-allpowers-ble"
                "/blob/main/docs/troubleshooting.md#home-assistant-repairs"
            ),
        )

    def _delete_issue(self, issue_key: str) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(issue_key))

    def _issue_id(self, issue_key: str) -> str:
        return f"{self._entry_id}_{issue_key}"
