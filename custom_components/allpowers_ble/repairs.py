"""Home Assistant Repairs lifecycle for persistent actionable failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import AllpowersSnapshot

if TYPE_CHECKING:
    from .coordinator import AllpowersConfigEntry

NO_ROUTE_PERSISTENCE_UPDATES = 3
WATCHDOG_RESET_PERSISTENCE = 3

ISSUE_NO_ROUTE = "persistent_no_route"
ISSUE_WATCHDOG = "repeated_watchdog_resets"
ISSUE_INVALID_MIGRATION = "invalid_migrated_options"
ISSUE_FAILED_UNLOAD = "failed_unload"


@callback
def async_unload_block_reason(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> str | None:
    """Block changes to a runtime unloading or left in Core's terminal state."""
    if entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
        return "unload_in_progress"
    if entry.state is ConfigEntryState.FAILED_UNLOAD:
        async_create_unload_failed_issue(hass, entry)
        return ISSUE_FAILED_UNLOAD
    return None


@callback
def async_observe_unload_result(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> None:
    """Observe Core cleanup, which runs after the integration unload returns."""
    completed = False

    @callback
    def state_changed() -> None:
        nonlocal completed
        if completed:
            return
        if entry.state is ConfigEntryState.FAILED_UNLOAD:
            async_create_unload_failed_issue(hass, entry)
        elif entry.state is not ConfigEntryState.NOT_LOADED:
            return
        completed = True
        # Do not mutate Core's callback list during its state-change iteration.
        hass.loop.call_soon(unsubscribe)

    # Unload callbacks run before Core sets the final state, so this subscription
    # must outlive them and unsubscribe itself on completion instead.
    unsubscribe = entry.async_on_state_change(state_changed)


@callback
def async_create_unload_failed_issue(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> None:
    """Expose Core's terminal unload failure with restart-only recovery."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{entry.entry_id}_{ISSUE_FAILED_UNLOAD}",
        issue_domain=DOMAIN,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_FAILED_UNLOAD,
        translation_placeholders={"entry_title": entry.title},
        learn_more_url=(
            "https://github.com/dedalodaelus/home-assistant-allpowers-ble"
            "/blob/main/docs/troubleshooting.md#integration-unload-failed"
        ),
    )


@callback
def async_delete_unload_failed_issue(
    hass: HomeAssistant, entry: AllpowersConfigEntry
) -> None:
    """Dismiss the restart requirement only after a successful new setup."""
    ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_{ISSUE_FAILED_UNLOAD}")


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
