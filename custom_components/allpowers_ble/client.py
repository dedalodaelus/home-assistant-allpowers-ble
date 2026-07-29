"""Active BLE transport for ALLPOWERS devices through Home Assistant Bluetooth."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
from random import Random
from time import monotonic
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS,
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    COMMAND_REFRESH_DELAY,
    CONNECTION_ATTEMPTS,
    NOTIFY_UUID,
    SERVICE_UUID,
    WRITE_TIMEOUT,
    WRITE_UUID,
)
from .model_support import ModelSupport, identify_model
from .models import AllpowersSnapshot, ConnectionStatistics
from .options import ConnectionOptions
from .protocol import (
    DeviceNameData,
    NotificationStreamDecoder,
    SettingsData,
    StateUnavailableError,
    StatusData,
    WorkMode,
    encode_output_control,
    encode_settings_control,
    encode_status_request,
    settings_write_validation_errors,
    status_write_validation_errors,
    updated_settings,
)

_LOGGER = logging.getLogger(__name__)

_RECONNECT_JITTER_RATIO = 0.15
_RSSI_UPDATE_MIN_DELTA = 3
_RSSI_UPDATE_MAX_INTERVAL = 30.0


class AllpowersClientError(RuntimeError):
    """Base transport exception."""


class DeviceNotFoundError(AllpowersClientError):
    """Raised when Home Assistant has no connectable route to the address."""


class UnsupportedDeviceError(AllpowersClientError):
    """Raised when the advertised device does not expose the expected GATT API."""


class ProbeConnectionTimeoutError(AllpowersClientError, TimeoutError):
    """Raised when probe connection setup exceeds the configured deadline."""


class ProbeStatusTimeoutError(AllpowersClientError, TimeoutError):
    """Raised when probe status telemetry is not received before the deadline."""


class ProbeGattValidationError(AllpowersClientError):
    """Raised when probe GATT validation fails for a connectable device."""


class ProbeNotificationSetupError(AllpowersClientError):
    """Raised when probe notification subscription cannot be established."""


class NotConnectedError(AllpowersClientError):
    """Raised when a command is attempted without an active GATT connection."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of a non-persistent config-flow probe."""

    status: StatusData
    settings: SettingsData | None
    model_support: ModelSupport


@dataclass(frozen=True, slots=True)
class PendingOutputTransaction:
    """Pending output command awaiting explicit on-device confirmation."""

    session_generation: int
    source_version: int
    target_dc: bool
    target_ac: bool
    target_light: bool
    sent_monotonic: float
    confirm_deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class PendingSettingsTransaction:
    """Pending settings command awaiting explicit on-device confirmation."""

    session_generation: int
    source_version: int
    target: SettingsData
    sent_monotonic: float
    confirm_deadline_monotonic: float


class AllpowersBLEClient:
    """Maintain one active connection and serialize safe protocol commands."""

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        address: str,
        advertised_name: str,
        options: ConnectionOptions,
    ) -> None:
        self._hass = hass
        self.address = address.upper()
        self._advertised_name = advertised_name
        self._options = options

        self._client: BleakClient | None = None
        self._write_characteristic: BleakGATTCharacteristic | None = None
        self._decoder = NotificationStreamDecoder()
        self._update_callback: Callable[[], None] | None = None

        self._connection_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._disconnect_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._maintenance_wakeup = asyncio.Event()
        # One lock owns every operation that mutates or uses the active client.
        self._operation_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_generation_counter = 0
        self._active_session_generation: int | None = None

        self._connected = False
        self._connected_monotonic: float | None = None
        self._status: StatusData | None = None
        self._settings: SettingsData | None = None
        self._status_monotonic: float | None = None
        self._settings_monotonic: float | None = None
        self._status_version = 0
        self._settings_version = 0
        self._last_packet_monotonic: float | None = None
        self._last_status_request_monotonic: float | None = None
        self._last_settings_keepalive_monotonic: float | None = None
        self._rssi: int | None = None
        self._last_connected_at: datetime | None = None
        self._last_disconnected_at: datetime | None = None
        self._last_packet_at: datetime | None = None
        self._last_error: str | None = None
        self._statistics = ConnectionStatistics()

        self._pending_output_transaction: PendingOutputTransaction | None = None
        self._pending_settings_transaction: PendingSettingsTransaction | None = None
        self._output_blocked_until_version: int | None = None
        self._settings_blocked_until_version: int | None = None
        self._initial_settings_keepalive_pending = options.settings_keepalive
        self._reported_freshness = (False, False)
        self._reconnect_jitter = Random(self.address).uniform
        self._last_published_rssi: int | None = None
        self._last_rssi_publish_monotonic: float | None = None

    @property
    def options(self) -> ConnectionOptions:
        """Return current runtime options."""
        return self._options

    @property
    def advertised_name(self) -> str:
        """Return the most recent advertisement name."""
        return self._advertised_name

    def set_update_callback(self, callback: Callable[[], None] | None) -> None:
        """Register the coordinator callback invoked for every state change."""
        self._update_callback = callback

    def update_advertisement(self, service_info: Any) -> None:
        """Consume a Home Assistant Bluetooth advertisement callback."""
        changed = False
        name = str(service_info.name or self._advertised_name)
        rssi = int(service_info.rssi) if service_info.rssi is not None else None
        if name != self._advertised_name:
            self._advertised_name = name
            changed = True
        if rssi != self._rssi:
            self._rssi = rssi
            if self._should_publish_rssi(rssi):
                changed = True
        if changed:
            self._emit_update()

    async def async_start(self) -> None:
        """Start connection and maintenance tasks."""
        if self._connection_task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._disconnect_event.clear()
        self._maintenance_wakeup.clear()
        self._connection_task = asyncio.create_task(
            self._connection_loop(), name=f"allpowers-ble-{self.address}"
        )
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name=f"allpowers-maintenance-{self.address}"
        )

    async def async_stop(self) -> None:
        """Stop all tasks and release the Bluetooth connection slot."""
        self._stop_event.set()
        self._disconnect_event.set()
        self._maintenance_wakeup.set()
        tasks = [
            task
            for task in (
                self._refresh_task,
                self._maintenance_task,
                self._connection_task,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_task = None
        self._maintenance_task = None
        self._connection_task = None
        await self._disconnect_client()
        self._loop = None
        self._update_callback = None

    async def async_wait_ready(self, timeout: float) -> None:
        """Wait until the first valid status notification is received."""
        async with asyncio.timeout(timeout):
            await self._ready_event.wait()

    async def async_apply_options(self, options: ConnectionOptions) -> None:
        """Apply validated options without tearing down the connection."""
        keepalive_was_enabled = self._options.settings_keepalive
        self._options = options
        if options.settings_keepalive and not keepalive_was_enabled:
            self._initial_settings_keepalive_pending = True
        elif not options.settings_keepalive:
            self._initial_settings_keepalive_pending = False
        self._reported_freshness = self._freshness(self._loop_time())
        self._wake_maintenance_loop()
        self._emit_update()

    def snapshot(self) -> AllpowersSnapshot:
        """Return one immutable snapshot for coordinator consumers."""
        return AllpowersSnapshot(
            connected=self._connected,
            status=self._status,
            settings=self._settings,
            status_monotonic=self._status_monotonic,
            settings_monotonic=self._settings_monotonic,
            last_packet_monotonic=self._last_packet_monotonic,
            rssi=self._rssi,
            advertised_name=self._advertised_name,
            last_connected_at=self._last_connected_at,
            last_disconnected_at=self._last_disconnected_at,
            last_packet_at=self._last_packet_at,
            last_error=self._last_error,
            statistics=self._statistics,
        )

    async def async_request_status(self) -> None:
        """Request a fresh status notification."""
        async with self._operation_lock:
            await self._write_frame_unlocked(encode_status_request())
            self._last_status_request_monotonic = self._loop_time()

    async def async_reconnect(self, reason: str = "Manual reconnect") -> None:
        """Disconnect now so the connection loop can establish a fresh route."""
        self._last_error = reason
        self._disconnect_event.set()
        self._wake_maintenance_loop()
        await self._disconnect_client()

    async def async_set_ac(self, enabled: bool) -> None:
        """Set AC output while preserving DC and light state."""
        await self._async_set_output(ac=enabled)

    async def async_set_dc(self, enabled: bool) -> None:
        """Set DC output while preserving AC and light state."""
        await self._async_set_output(dc=enabled)

    async def async_set_light(self, enabled: bool) -> None:
        """Set light output while preserving AC and DC state."""
        await self._async_set_output(light=enabled)

    async def _async_set_output(
        self,
        *,
        dc: bool | None = None,
        ac: bool | None = None,
        light: bool | None = None,
    ) -> None:
        async with self._operation_lock:
            self._require_output_write_capability_unlocked()
            current_dc, current_ac, current_light = self._safe_output_snapshot()
            target_dc = current_dc if dc is None else dc
            target_ac = current_ac if ac is None else ac
            target_light = current_light if light is None else light
            await self._write_frame_unlocked(
                encode_output_control(
                    dc=target_dc,
                    ac=target_ac,
                    light=target_light,
                )
            )
            now = self._loop_time()
            active_generation = self._active_session_generation
            if active_generation is None:
                raise NotConnectedError("The ALLPOWERS device is not connected")
            self._pending_output_transaction = PendingOutputTransaction(
                session_generation=active_generation,
                source_version=self._output_source_version(),
                target_dc=target_dc,
                target_ac=target_ac,
                target_light=target_light,
                sent_monotonic=now,
                confirm_deadline_monotonic=now + self._options.stale_timeout,
            )
        self._schedule_status_refresh()

    async def async_set_eco(self, enabled: bool) -> None:
        """Set ECO mode while preserving every unrelated settings bit."""
        await self._async_update_settings(eco_enabled=enabled)

    async def async_set_work_mode(self, mode: WorkMode) -> None:
        """Set charging work mode while preserving every unrelated setting."""
        await self._async_update_settings(work_mode=mode)

    async def async_set_eco_timeout(self, hours: int) -> None:
        """Set ECO shutdown timeout."""
        await self._async_update_settings(eco_timeout_hours=hours)

    async def async_set_car_charger(self, enabled: bool) -> None:
        """Set the optional car-charger bit when explicitly enabled by the user."""
        if not self._options.enable_car_charger:
            raise StateUnavailableError(
                "Car charger control is disabled in integration options"
            )
        await self._async_update_settings(car_charger_enabled=enabled)

    async def _async_update_settings(
        self,
        *,
        eco_enabled: bool | None = None,
        work_mode: WorkMode | None = None,
        car_charger_enabled: bool | None = None,
        eco_timeout_hours: int | None = None,
    ) -> None:
        async with self._operation_lock:
            self._require_settings_write_capability_unlocked()
            current = self._safe_settings_snapshot()
            target = updated_settings(
                current,
                eco_enabled=eco_enabled,
                work_mode=work_mode,
                car_charger_enabled=car_charger_enabled,
                eco_timeout_hours=eco_timeout_hours,
            )
            await self._write_frame_unlocked(encode_settings_control(target))
            now = self._loop_time()
            active_generation = self._active_session_generation
            if active_generation is None:
                raise NotConnectedError("The ALLPOWERS device is not connected")
            self._pending_settings_transaction = PendingSettingsTransaction(
                session_generation=active_generation,
                source_version=self._settings_source_version(),
                target=target,
                sent_monotonic=now,
                confirm_deadline_monotonic=now + self._options.settings_stale_timeout,
            )
            self._record_settings_write_activity(now)
        self._schedule_status_refresh()

    async def async_send_settings_keepalive(self) -> None:
        """Re-send the current settings snapshot to keep vendor state alive."""
        async with self._operation_lock:
            self._require_settings_keepalive_capability_unlocked()
            current = self._safe_settings_snapshot()
            await self._write_frame_unlocked(encode_settings_control(current))
            now = self._loop_time()
            active_generation = self._active_session_generation
            if active_generation is None:
                raise NotConnectedError("The ALLPOWERS device is not connected")
            self._pending_settings_transaction = PendingSettingsTransaction(
                session_generation=active_generation,
                source_version=self._settings_source_version(),
                target=current,
                sent_monotonic=now,
                confirm_deadline_monotonic=now + self._options.settings_stale_timeout,
            )
            self._record_settings_write_activity(now)
        self._schedule_status_refresh()

    def _safe_output_snapshot(self) -> tuple[bool, bool, bool]:
        if not self._connected:
            raise StateUnavailableError(
                "A fresh status snapshot is required before changing outputs"
            )
        now = self._loop_time()
        pending = self._pending_output_transaction
        if pending is not None:
            if now <= pending.confirm_deadline_monotonic:
                return pending.target_dc, pending.target_ac, pending.target_light
            self._pending_output_transaction = None
            self._output_blocked_until_version = self._status_version + 1
            raise StateUnavailableError(
                "Output command confirmation timed out; wait for a fresh status update"
            )

        blocked_until = self._output_blocked_until_version
        if blocked_until is not None and self._status_version < blocked_until:
            raise StateUnavailableError(
                "Output command state is ambiguous; wait for a fresh status update"
            )

        if (
            self._status is None
            or self._status_monotonic is None
            or now - self._status_monotonic > self._options.stale_timeout
        ):
            raise StateUnavailableError(
                "A fresh status snapshot is required before changing outputs"
            )

        support = self._runtime_model_support()
        errors = status_write_validation_errors(support.profile, self._status)
        if errors:
            raise StateUnavailableError(
                "Output writes are blocked by semantic validation: " + "; ".join(errors)
            )
        return (
            self._status.dc_enabled,
            self._status.ac_enabled,
            self._status.light_enabled,
        )

    def _safe_settings_snapshot(self) -> SettingsData:
        if not self._connected:
            raise StateUnavailableError(
                "A fresh settings snapshot is required before changing settings"
            )
        now = self._loop_time()
        pending = self._pending_settings_transaction
        if pending is not None:
            if now <= pending.confirm_deadline_monotonic:
                return pending.target
            self._pending_settings_transaction = None
            self._settings_blocked_until_version = self._settings_version + 1
            raise StateUnavailableError(
                "Settings command confirmation timed out; wait for a fresh settings update"
            )

        blocked_until = self._settings_blocked_until_version
        if blocked_until is not None and self._settings_version < blocked_until:
            raise StateUnavailableError(
                "Settings command state is ambiguous; wait for a fresh settings update"
            )

        if (
            self._settings is None
            or self._settings_monotonic is None
            or now - self._settings_monotonic > self._options.settings_stale_timeout
        ):
            raise StateUnavailableError(
                "A fresh settings snapshot is required before changing settings"
            )

        support = self._runtime_model_support()
        errors = settings_write_validation_errors(support.profile, self._settings)
        if errors:
            raise StateUnavailableError(
                "Settings writes are blocked by semantic validation: "
                + "; ".join(errors)
            )
        return self._settings

    def _runtime_model_support(self) -> ModelSupport:
        """Return model support resolved from the latest revision-aware snapshot."""
        settings = self._settings
        return identify_model(
            self._advertised_name,
            hardware_version=settings.hardware_version if settings else None,
            raw_hardware_version=settings.raw_hardware_version if settings else None,
        )

    def _require_output_write_capability_unlocked(self) -> None:
        """Reject output writes when the active model profile is read-only."""
        support = self._runtime_model_support()
        if support.capabilities.write_output_controls:
            return
        raise StateUnavailableError(
            f"Unsupported output command for active model profile: {support.profile}"
        )

    def _require_settings_write_capability_unlocked(self) -> None:
        """Reject settings writes when the active model profile is read-only."""
        support = self._runtime_model_support()
        if support.capabilities.write_settings_controls:
            return
        raise StateUnavailableError(
            f"Unsupported settings command for active model profile: {support.profile}"
        )

    def _require_settings_keepalive_capability_unlocked(self) -> None:
        """Reject keepalive writes when the active model profile disallows them."""
        support = self._runtime_model_support()
        if support.capabilities.write_settings_keepalive:
            return
        raise StateUnavailableError(
            "Unsupported settings keepalive command for active model profile: "
            f"{support.profile}"
        )

    async def _connection_loop(self) -> None:
        delay = 1.0
        ever_connected = False
        while not self._stop_event.is_set():
            self._disconnect_event.clear()
            try:
                await self._connect_once()
                if ever_connected:
                    self._statistics = replace(
                        self._statistics,
                        reconnects=self._statistics.reconnects + 1,
                    )
                ever_connected = True
                delay = 1.0
                await self._disconnect_event.wait()
            except asyncio.CancelledError:
                raise
            except (
                DeviceNotFoundError,
                UnsupportedDeviceError,
                NotConnectedError,
                TimeoutError,
                *BLEAK_RETRY_EXCEPTIONS,
            ) as ex:
                self._last_error = f"{type(ex).__name__}: {ex}"
                _LOGGER.debug("Connection attempt failed for %s: %s", self.address, ex)
            except Exception as ex:  # pragma: no cover - defensive transport boundary
                self._last_error = f"{type(ex).__name__}: {ex}"
                _LOGGER.exception("Unexpected ALLPOWERS BLE connection error")
            finally:
                await self._disconnect_client()
                self._emit_update()

            if self._stop_event.is_set():
                break
            await self._sleep_until_retry(self._retry_delay_with_jitter(delay))
            delay = min(delay * 2, self._options.reconnect_max_delay)

    async def _connect_once(self) -> None:
        session_generation = self._activate_session_generation()
        self._statistics = replace(
            self._statistics,
            connection_attempts=self._statistics.connection_attempts + 1,
        )
        self._emit_update()

        device = self._fresh_ble_device()
        if device is None:
            raise DeviceNotFoundError(
                "No connectable Bluetooth adapter or proxy can currently reach "
                f"{self.address}"
            )

        def fresh_device_for_retry() -> BLEDevice:
            return self._fresh_ble_device() or device

        support = identify_model(self._advertised_name)
        if not support.supported:
            raise UnsupportedDeviceError(support.reason or "Unsupported model")

        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self._advertised_name,
            disconnected_callback=self._make_disconnected_callback(session_generation),
            max_attempts=CONNECTION_ATTEMPTS,
            ble_device_callback=fresh_device_for_retry,
            use_services_cache=True,
        )
        try:
            (
                notify_characteristic,
                write_characteristic,
            ) = _required_characteristics(client)
            async with self._operation_lock:
                self._client = client
                self._write_characteristic = write_characteristic

                # A new GATT session must never inherit parser fragments or freshness
                # timestamps from a previous connection. Cached values remain available
                # for diagnostics, but cannot authorize writes until refreshed.
                self._status_monotonic = None
                self._settings_monotonic = None
                self._last_packet_monotonic = None
                self._last_status_request_monotonic = None
                self._last_settings_keepalive_monotonic = None
                self._status_version = 0
                self._settings_version = 0
                self._pending_output_transaction = None
                self._pending_settings_transaction = None
                self._output_blocked_until_version = None
                self._settings_blocked_until_version = None
                self._initial_settings_keepalive_pending = (
                    self._options.settings_keepalive
                )
                self._reported_freshness = (False, False)

                await client.start_notify(
                    notify_characteristic,
                    self._make_notification_handler(session_generation, client),
                )

                now = self._loop_time()
                self._connected = True
                self._connected_monotonic = now
                self._last_connected_at = _utcnow()
                self._last_error = None
                self._statistics = replace(
                    self._statistics,
                    successful_connections=self._statistics.successful_connections + 1,
                )

                await self._write_frame_unlocked(encode_status_request())
                self._last_status_request_monotonic = self._loop_time()
                self._wake_maintenance_loop()
        except Exception:
            try:
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await client.disconnect()
            except (TimeoutError, *BLEAK_RETRY_EXCEPTIONS):
                pass
            raise
        self._emit_update()

    async def _disconnect_client(self) -> None:
        async with self._operation_lock:
            client = self._client
            was_connected = self._connected
            self._active_session_generation = None
            self._ready_event.clear()
            self._client = None
            self._write_characteristic = None
            self._connected = False
            self._connected_monotonic = None
            self._pending_output_transaction = None
            self._pending_settings_transaction = None
            self._output_blocked_until_version = None
            self._settings_blocked_until_version = None
            self._initial_settings_keepalive_pending = False
            self._reported_freshness = (False, False)

            refresh_task = self._refresh_task
            self._refresh_task = None
            if refresh_task is not None and refresh_task is not asyncio.current_task():
                refresh_task.cancel()

            if was_connected:
                self._last_disconnected_at = _utcnow()
                self._statistics = replace(
                    self._statistics,
                    disconnects=self._statistics.disconnects + 1,
                )
            if client is not None and client.is_connected:
                try:
                    async with asyncio.timeout(WRITE_TIMEOUT):
                        await client.disconnect()
                except (TimeoutError, *BLEAK_RETRY_EXCEPTIONS) as ex:
                    _LOGGER.debug("Disconnect failed for %s: %s", self.address, ex)
            if was_connected:
                self._emit_update()
        self._wake_maintenance_loop()

    def _disconnected_callback(self, client: BleakClient) -> None:
        del client
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._disconnect_event.set)

    def _activate_session_generation(self) -> int:
        """Start a new BLE session generation and reset session-scoped state."""
        self._session_generation_counter += 1
        self._active_session_generation = self._session_generation_counter
        self._decoder = NotificationStreamDecoder()
        self._ready_event.clear()
        return self._session_generation_counter

    def _make_disconnected_callback(
        self,
        generation: int,
    ) -> Callable[[BleakClient], None]:
        def _callback(client: BleakClient) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            if generation != self._active_session_generation:
                return
            if self._client is not client:
                return
            loop.call_soon_threadsafe(self._disconnect_event.set)

        return _callback

    def _make_notification_handler(
        self,
        generation: int,
        expected_client: BleakClient,
    ) -> Callable[[BleakGATTCharacteristic, bytearray], None]:
        def _handler(
            characteristic: BleakGATTCharacteristic,
            data: bytearray,
        ) -> None:
            if generation != self._active_session_generation:
                return
            if self._client is not expected_client:
                return
            self._notification_handler(characteristic, data)

        return _handler

    def _notification_handler(
        self,
        characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        del characteristic
        self._statistics = replace(
            self._statistics,
            notifications=self._statistics.notifications + 1,
        )
        discarded_bytes_before = self._decoder.discarded_bytes
        discarded_before = self._decoder.discarded_frames
        packets = self._decoder.feed(data)
        discarded_bytes = self._decoder.discarded_bytes - discarded_bytes_before
        discarded = self._decoder.discarded_frames - discarded_before
        if discarded_bytes or discarded:
            self._statistics = replace(
                self._statistics,
                parser_discards=self._statistics.parser_discards + discarded_bytes,
                protocol_errors=self._statistics.protocol_errors + discarded,
            )

        for packet in packets:
            now = self._loop_time()
            self._last_packet_monotonic = now
            self._last_packet_at = _utcnow()
            self._statistics = replace(
                self._statistics,
                valid_packets=self._statistics.valid_packets + 1,
            )
            if isinstance(packet, StatusData):
                self._status = packet
                self._status_monotonic = now
                self._status_version += 1
                if (
                    self._output_blocked_until_version is not None
                    and self._status_version >= self._output_blocked_until_version
                ):
                    self._output_blocked_until_version = None
                pending_output = self._pending_output_transaction
                if pending_output is not None:
                    if (
                        pending_output.session_generation
                        != self._active_session_generation
                    ):
                        self._pending_output_transaction = None
                    elif now > pending_output.confirm_deadline_monotonic:
                        self._pending_output_transaction = None
                        self._output_blocked_until_version = self._status_version + 1
                    elif (
                        packet.dc_enabled == pending_output.target_dc
                        and packet.ac_enabled == pending_output.target_ac
                        and packet.light_enabled == pending_output.target_light
                    ):
                        self._pending_output_transaction = None
                self._ready_event.set()
            elif isinstance(packet, SettingsData):
                self._settings = packet
                self._settings_monotonic = now
                self._settings_version += 1
                if (
                    self._settings_blocked_until_version is not None
                    and self._settings_version >= self._settings_blocked_until_version
                ):
                    self._settings_blocked_until_version = None
                pending_settings = self._pending_settings_transaction
                if pending_settings is not None:
                    if (
                        pending_settings.session_generation
                        != self._active_session_generation
                    ):
                        self._pending_settings_transaction = None
                    elif now > pending_settings.confirm_deadline_monotonic:
                        self._pending_settings_transaction = None
                        self._settings_blocked_until_version = (
                            self._settings_version + 1
                        )
                    elif packet == pending_settings.target:
                        self._pending_settings_transaction = None
            elif isinstance(packet, DeviceNameData) and packet.name:
                self._advertised_name = packet.name

        if packets or discarded or discarded_bytes:
            self._reported_freshness = self._freshness(self._loop_time())
            self._wake_maintenance_loop()
            self._emit_update()

    async def _write_frame_unlocked(self, frame: bytes) -> None:
        client = self._client
        characteristic = self._write_characteristic
        active_generation = self._active_session_generation
        if (
            not self._connected
            or client is None
            or not client.is_connected
            or characteristic is None
            or active_generation is None
        ):
            raise NotConnectedError("The ALLPOWERS device is not connected")

        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                await client.write_gatt_char(characteristic, frame, response=False)
            if (
                self._active_session_generation != active_generation
                or self._client is not client
                or not self._connected
            ):
                raise NotConnectedError("The ALLPOWERS device session changed")
        except (TimeoutError, *BLEAK_RETRY_EXCEPTIONS) as ex:
            self._statistics = replace(
                self._statistics,
                write_errors=self._statistics.write_errors + 1,
            )
            self._last_error = f"{type(ex).__name__}: {ex}"
            self._disconnect_event.set()
            self._emit_update()
            raise

    def _schedule_status_refresh(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(
            self._delayed_status_refresh(),
            name=f"allpowers-command-refresh-{self.address}",
        )

    async def _delayed_status_refresh(self) -> None:
        try:
            await asyncio.sleep(COMMAND_REFRESH_DELAY)
            await self.async_request_status()
        except asyncio.CancelledError:
            raise
        except (NotConnectedError, TimeoutError, *BLEAK_RETRY_EXCEPTIONS):
            return
        finally:
            if self._refresh_task is asyncio.current_task():
                self._refresh_task = None

    async def _maintenance_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected:
                await self._wait_for_maintenance_wakeup(timeout=None)
                continue

            now = self._loop_time()
            action_taken = False
            self._emit_freshness_change(now)

            status_reference = self._status_monotonic or self._connected_monotonic
            if (
                status_reference is not None
                and now - status_reference > self._options.watchdog_timeout
            ):
                self._statistics = replace(
                    self._statistics,
                    watchdog_resets=self._statistics.watchdog_resets + 1,
                    telemetry_watchdog_resets=(
                        self._statistics.telemetry_watchdog_resets + 1
                    ),
                )
                await self.async_reconnect("Telemetry watchdog expired")
                action_taken = True

            if not action_taken:
                packet_reference = (
                    self._last_packet_monotonic or self._connected_monotonic
                )
                if (
                    packet_reference is not None
                    and now - packet_reference > self._options.watchdog_timeout
                ):
                    self._statistics = replace(
                        self._statistics,
                        watchdog_resets=self._statistics.watchdog_resets + 1,
                        transport_watchdog_resets=(
                            self._statistics.transport_watchdog_resets + 1
                        ),
                    )
                    await self.async_reconnect("Transport watchdog expired")
                    action_taken = True

            if not action_taken:
                if self._options.settings_keepalive:
                    should_send_initial = (
                        self._initial_settings_keepalive_pending
                        and self._settings is not None
                        and self._settings_monotonic is not None
                        and now - self._settings_monotonic
                        <= self._options.settings_stale_timeout
                    )
                    reference = (
                        self._last_settings_keepalive_monotonic
                        or self._settings_monotonic
                        or self._connected_monotonic
                    )
                    should_send_periodic = (
                        reference is not None
                        and now - reference >= self._options.settings_keepalive_interval
                    )
                    if should_send_initial or should_send_periodic:
                        try:
                            await self.async_send_settings_keepalive()
                        except (
                            StateUnavailableError,
                            NotConnectedError,
                            TimeoutError,
                            *BLEAK_RETRY_EXCEPTIONS,
                        ):
                            pass
                        action_taken = True

            if not action_taken and (
                self._last_status_request_monotonic is None
                or now - self._last_status_request_monotonic
                >= self._options.status_interval
            ):
                try:
                    await self.async_request_status()
                except (NotConnectedError, TimeoutError, *BLEAK_RETRY_EXCEPTIONS):
                    pass
                action_taken = True

            next_deadline = self._next_maintenance_deadline(now)
            timeout: float | None = 0.001 if action_taken else None
            if timeout is None and next_deadline is not None:
                timeout = max(0.001, next_deadline - now)
            await self._wait_for_maintenance_wakeup(timeout)

    def _wake_maintenance_loop(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._maintenance_wakeup.set()
            return
        loop.call_soon_threadsafe(self._maintenance_wakeup.set)

    async def _wait_for_maintenance_wakeup(self, timeout: float | None) -> None:
        stop_wait = asyncio.create_task(self._stop_event.wait())
        wake_wait = asyncio.create_task(self._maintenance_wakeup.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_wait, wake_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                exception = task.exception()
                if exception is not None and not isinstance(exception, TimeoutError):
                    raise exception
            if wake_wait in done and wake_wait.exception() is None:
                self._maintenance_wakeup.clear()
        finally:
            for task in (stop_wait, wake_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_wait, wake_wait, return_exceptions=True)

    def _next_maintenance_deadline(self, now: float) -> float | None:
        deadlines: list[float] = []

        status_reference = self._status_monotonic or self._connected_monotonic
        if status_reference is not None:
            deadlines.append(status_reference + self._options.watchdog_timeout)

        packet_reference = self._last_packet_monotonic or self._connected_monotonic
        if packet_reference is not None:
            deadlines.append(packet_reference + self._options.watchdog_timeout)

        if self._last_status_request_monotonic is None:
            deadlines.append(now)
        else:
            deadlines.append(
                self._last_status_request_monotonic + self._options.status_interval
            )

        if self._options.settings_keepalive:
            should_send_initial = (
                self._initial_settings_keepalive_pending
                and self._settings is not None
                and self._settings_monotonic is not None
                and now - self._settings_monotonic
                <= self._options.settings_stale_timeout
            )
            if should_send_initial:
                deadlines.append(now)
            else:
                reference = (
                    self._last_settings_keepalive_monotonic
                    or self._settings_monotonic
                    or self._connected_monotonic
                )
                if reference is not None:
                    deadlines.append(
                        reference + self._options.settings_keepalive_interval
                    )

        if self._status_monotonic is not None:
            status_freshness_deadline = (
                self._status_monotonic + self._options.stale_timeout
            )
            if now <= status_freshness_deadline:
                deadlines.append(status_freshness_deadline)

        if self._settings_monotonic is not None:
            settings_freshness_deadline = (
                self._settings_monotonic + self._options.settings_stale_timeout
            )
            if now <= settings_freshness_deadline:
                deadlines.append(settings_freshness_deadline)

        return min(deadlines)

    async def _sleep_until_retry(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            return

    def _fresh_ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(
            self._hass, self.address, connectable=True
        )

    def _loop_time(self) -> float:
        return monotonic()

    def _retry_delay_with_jitter(self, delay: float) -> float:
        """Return a bounded reconnect delay with symmetric jitter."""
        bounded_delay = min(max(0.0, delay), self._options.reconnect_max_delay)
        jitter_span = bounded_delay * _RECONNECT_JITTER_RATIO
        jitter = self._reconnect_jitter(-jitter_span, jitter_span)
        return min(
            max(0.0, bounded_delay + jitter),
            self._options.reconnect_max_delay,
        )

    def _should_publish_rssi(self, rssi: int | None) -> bool:
        """Throttle RSSI-only updates while preserving meaningful changes."""
        if rssi is None:
            should_publish = self._last_published_rssi is not None
            if should_publish:
                self._last_published_rssi = None
                self._last_rssi_publish_monotonic = self._loop_time()
            return should_publish

        now = self._loop_time()
        if self._last_published_rssi is None:
            self._last_published_rssi = rssi
            self._last_rssi_publish_monotonic = now
            return True

        if abs(rssi - self._last_published_rssi) >= _RSSI_UPDATE_MIN_DELTA:
            self._last_published_rssi = rssi
            self._last_rssi_publish_monotonic = now
            return True

        last_publish = self._last_rssi_publish_monotonic
        if last_publish is not None and now - last_publish >= _RSSI_UPDATE_MAX_INTERVAL:
            self._last_published_rssi = rssi
            self._last_rssi_publish_monotonic = now
            return True
        return False

    def _freshness(self, now: float) -> tuple[bool, bool]:
        status_is_fresh = (
            self._connected
            and self._status_monotonic is not None
            and now - self._status_monotonic <= self._options.stale_timeout
        )
        settings_are_fresh = (
            self._connected
            and self._settings_monotonic is not None
            and now - self._settings_monotonic <= self._options.settings_stale_timeout
        )
        return status_is_fresh, settings_are_fresh

    def _emit_freshness_change(self, now: float) -> None:
        freshness = self._freshness(now)
        if freshness == self._reported_freshness:
            return
        self._reported_freshness = freshness
        self._emit_update()

    def _record_settings_write_activity(self, now: float) -> None:
        self._last_settings_keepalive_monotonic = now
        self._initial_settings_keepalive_pending = False

    def _output_source_version(self) -> int:
        pending = self._pending_output_transaction
        if pending is not None:
            return pending.source_version
        return self._status_version

    def _settings_source_version(self) -> int:
        pending = self._pending_settings_transaction
        if pending is not None:
            return pending.source_version
        return self._settings_version

    def _emit_update(self) -> None:
        if self._update_callback is not None:
            self._update_callback()


async def async_probe_device(
    hass: HomeAssistant,
    *,
    address: str,
    advertised_name: str,
    timeout: float,
) -> ProbeResult:
    """Connect, verify GATT characteristics, and require one valid status frame."""
    normalized_address = address.upper()
    support = identify_model(advertised_name)
    if not support.supported:
        raise UnsupportedDeviceError(support.reason or "Unsupported model")

    def fresh_device() -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(
            hass, normalized_address, connectable=True
        )

    device = fresh_device()
    if device is None:
        raise DeviceNotFoundError(
            f"No connectable Bluetooth adapter or proxy can reach {normalized_address}"
        )

    def fresh_device_for_retry() -> BLEDevice:
        return fresh_device() or device

    deadline = monotonic() + timeout

    def remaining(stage: str) -> float:
        del stage
        return max(0.001, deadline - monotonic())

    try:
        async with asyncio.timeout(remaining("connection")):
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                advertised_name,
                max_attempts=CONNECTION_ATTEMPTS,
                ble_device_callback=fresh_device_for_retry,
                use_services_cache=True,
            )
    except TimeoutError as ex:
        raise ProbeConnectionTimeoutError(
            f"Probe connection timed out for {normalized_address}"
        ) from ex

    decoder = NotificationStreamDecoder()
    status: StatusData | None = None
    settings: SettingsData | None = None
    status_event = asyncio.Event()
    notify_characteristic: BleakGATTCharacteristic | None = None
    notify_started = False

    def notification_handler(
        characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        nonlocal status, settings
        del characteristic
        for packet in decoder.feed(data):
            if isinstance(packet, StatusData):
                status = packet
                status_event.set()
            elif isinstance(packet, SettingsData):
                settings = packet

    try:
        try:
            notify_characteristic, write_characteristic = _required_characteristics(
                client
            )
        except UnsupportedDeviceError as ex:
            raise ProbeGattValidationError(str(ex)) from ex

        try:
            async with asyncio.timeout(remaining("notify")):
                await client.start_notify(notify_characteristic, notification_handler)
            notify_started = True
        except TimeoutError as ex:
            raise ProbeNotificationSetupError(
                f"Probe notification setup timed out for {normalized_address}"
            ) from ex
        except BLEAK_RETRY_EXCEPTIONS as ex:
            raise ProbeNotificationSetupError(
                f"Probe notification setup failed for {normalized_address}: {ex}"
            ) from ex

        try:
            async with asyncio.timeout(remaining("status_request")):
                await client.write_gatt_char(
                    write_characteristic, encode_status_request(), response=False
                )
        except TimeoutError as ex:
            raise ProbeNotificationSetupError(
                f"Probe status request timed out for {normalized_address}"
            ) from ex
        except BLEAK_RETRY_EXCEPTIONS as ex:
            raise ProbeNotificationSetupError(
                f"Probe status request failed for {normalized_address}: {ex}"
            ) from ex

        try:
            async with asyncio.timeout(remaining("status")):
                await status_event.wait()
        except TimeoutError as ex:
            raise ProbeStatusTimeoutError(
                f"Probe status timed out for {normalized_address}"
            ) from ex

        if status is None:  # pragma: no cover - event invariant
            raise TimeoutError("Status event was set without a status frame")
        resolved_support = identify_model(
            advertised_name,
            hardware_version=settings.hardware_version if settings else None,
            raw_hardware_version=(settings.raw_hardware_version if settings else None),
        )
        return ProbeResult(
            status=status,
            settings=settings,
            model_support=resolved_support,
        )
    finally:
        if client.is_connected:
            try:
                stop_notify = getattr(client, "stop_notify", None)
                if (
                    notify_started
                    and notify_characteristic is not None
                    and callable(stop_notify)
                ):
                    async with asyncio.timeout(WRITE_TIMEOUT):
                        await stop_notify(notify_characteristic)
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await client.disconnect()
            except (TimeoutError, *BLEAK_RETRY_EXCEPTIONS):
                _LOGGER.debug(
                    "Probe disconnect failed for %s", normalized_address, exc_info=True
                )


def _required_characteristics(
    client: BleakClient,
) -> tuple[BleakGATTCharacteristic, BleakGATTCharacteristic]:
    services = client.services
    if services.get_service(SERVICE_UUID) is None:
        raise UnsupportedDeviceError(f"Required service {SERVICE_UUID} is missing")
    notify_characteristic = services.get_characteristic(NOTIFY_UUID)
    write_characteristic = services.get_characteristic(WRITE_UUID)
    if notify_characteristic is None or write_characteristic is None:
        raise UnsupportedDeviceError(
            f"Required characteristics {NOTIFY_UUID} and {WRITE_UUID} are missing"
        )
    return notify_characteristic, write_characteristic


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
