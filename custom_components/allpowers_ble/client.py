"""Active BLE transport for ALLPOWERS devices through Home Assistant Bluetooth."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
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
    NOTIFY_UUID,
    OUTPUT_SHADOW_TIMEOUT,
    SERVICE_UUID,
    SETTINGS_SHADOW_TIMEOUT,
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
    updated_settings,
)

_LOGGER = logging.getLogger(__name__)


class AllpowersClientError(RuntimeError):
    """Base transport exception."""


class DeviceNotFoundError(AllpowersClientError):
    """Raised when Home Assistant has no connectable route to the address."""


class UnsupportedDeviceError(AllpowersClientError):
    """Raised when the advertised device does not expose the expected GATT API."""


class NotConnectedError(AllpowersClientError):
    """Raised when a command is attempted without an active GATT connection."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of a non-persistent config-flow probe."""

    status: StatusData
    settings: SettingsData | None
    model_support: ModelSupport


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
        self._write_lock = asyncio.Lock()
        self._disconnect_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._connected = False
        self._connected_monotonic: float | None = None
        self._status: StatusData | None = None
        self._settings: SettingsData | None = None
        self._status_monotonic: float | None = None
        self._settings_monotonic: float | None = None
        self._last_packet_monotonic: float | None = None
        self._last_status_request_monotonic: float | None = None
        self._last_settings_keepalive_monotonic: float | None = None
        self._rssi: int | None = None
        self._last_connected_at: datetime | None = None
        self._last_disconnected_at: datetime | None = None
        self._last_packet_at: datetime | None = None
        self._last_error: str | None = None
        self._statistics = ConnectionStatistics()

        self._output_shadow: tuple[bool, bool, bool, float] | None = None
        self._settings_shadow: tuple[SettingsData, float] | None = None
        self._initial_settings_keepalive_pending = options.settings_keepalive
        self._reported_freshness = (False, False)

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
        async with self._write_lock:
            await self._write_frame_unlocked(encode_status_request())
            self._last_status_request_monotonic = self._loop_time()

    async def async_reconnect(self, reason: str = "Manual reconnect") -> None:
        """Disconnect now so the connection loop can establish a fresh route."""
        self._last_error = reason
        self._disconnect_event.set()
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
        async with self._write_lock:
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
            self._output_shadow = (target_dc, target_ac, target_light, now)
            self._record_settings_write_activity(now)
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
        async with self._write_lock:
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
            self._settings_shadow = (target, now)
            self._record_settings_write_activity(now)
        self._schedule_status_refresh()

    async def async_send_settings_keepalive(self) -> None:
        """Re-send the current settings snapshot to keep vendor state alive."""
        async with self._write_lock:
            current = self._safe_settings_snapshot()
            await self._write_frame_unlocked(encode_settings_control(current))
            now = self._loop_time()
            self._settings_shadow = (current, now)
            self._record_settings_write_activity(now)
        self._schedule_status_refresh()

    def _safe_output_snapshot(self) -> tuple[bool, bool, bool]:
        if not self._connected:
            raise StateUnavailableError(
                "A fresh status snapshot is required before changing outputs"
            )
        now = self._loop_time()
        if self._output_shadow is not None:
            dc, ac, light, timestamp = self._output_shadow
            if now - timestamp <= OUTPUT_SHADOW_TIMEOUT:
                return dc, ac, light
            self._output_shadow = None

        if (
            self._status is None
            or self._status_monotonic is None
            or now - self._status_monotonic > self._options.stale_timeout
        ):
            raise StateUnavailableError(
                "A fresh status snapshot is required before changing outputs"
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
        if self._settings_shadow is not None:
            settings, timestamp = self._settings_shadow
            if now - timestamp <= SETTINGS_SHADOW_TIMEOUT:
                return settings
            self._settings_shadow = None

        if (
            self._settings is None
            or self._settings_monotonic is None
            or now - self._settings_monotonic > self._options.settings_stale_timeout
        ):
            raise StateUnavailableError(
                "A fresh settings snapshot is required before changing settings"
            )
        return self._settings

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
            await self._sleep_until_retry(delay)
            delay = min(delay * 2, self._options.reconnect_max_delay)

    async def _connect_once(self) -> None:
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

        support = identify_model(self._advertised_name)
        if not support.supported:
            raise UnsupportedDeviceError(support.reason or "Unsupported model")

        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self._advertised_name,
            disconnected_callback=self._disconnected_callback,
            max_attempts=3,
            ble_device_callback=self._fresh_ble_device,
            use_services_cache=True,
        )
        try:
            (
                notify_characteristic,
                write_characteristic,
            ) = _required_characteristics(client)
            self._client = client
            self._write_characteristic = write_characteristic

            # A new GATT session must never inherit parser fragments or freshness
            # timestamps from a previous connection. Cached values remain available
            # for diagnostics, but cannot authorize writes until refreshed.
            self._decoder = NotificationStreamDecoder()
            self._ready_event.clear()
            self._status_monotonic = None
            self._settings_monotonic = None
            self._last_packet_monotonic = None
            self._last_status_request_monotonic = None
            self._last_settings_keepalive_monotonic = None
            self._output_shadow = None
            self._settings_shadow = None
            self._initial_settings_keepalive_pending = self._options.settings_keepalive
            self._reported_freshness = (False, False)

            await client.start_notify(notify_characteristic, self._notification_handler)
        except Exception:
            try:
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await client.disconnect()
            except (TimeoutError, *BLEAK_RETRY_EXCEPTIONS):
                pass
            raise

        now = self._loop_time()
        self._connected = True
        self._connected_monotonic = now
        self._last_connected_at = _utcnow()
        self._last_error = None
        self._statistics = replace(
            self._statistics,
            successful_connections=self._statistics.successful_connections + 1,
        )
        self._emit_update()
        await self.async_request_status()

    async def _disconnect_client(self) -> None:
        async with self._disconnect_lock:
            client = self._client
            was_connected = self._connected
            self._client = None
            self._write_characteristic = None
            self._connected = False
            self._connected_monotonic = None
            self._output_shadow = None
            self._settings_shadow = None
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

    def _disconnected_callback(self, client: BleakClient) -> None:
        del client
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._disconnect_event.set)

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
        discarded_before = self._decoder.discarded_frames
        packets = self._decoder.feed(data)
        discarded = self._decoder.discarded_frames - discarded_before
        if discarded:
            self._statistics = replace(
                self._statistics,
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
                self._output_shadow = None
                self._ready_event.set()
            elif isinstance(packet, SettingsData):
                self._settings = packet
                self._settings_monotonic = now
                self._settings_shadow = None
            elif isinstance(packet, DeviceNameData) and packet.name:
                self._advertised_name = packet.name

        if packets or discarded:
            self._reported_freshness = self._freshness(self._loop_time())
            self._emit_update()

    async def _write_frame_unlocked(self, frame: bytes) -> None:
        client = self._client
        characteristic = self._write_characteristic
        if (
            not self._connected
            or client is None
            or not client.is_connected
            or characteristic is None
        ):
            raise NotConnectedError("The ALLPOWERS device is not connected")

        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                await client.write_gatt_char(characteristic, frame, response=False)
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
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                continue
            except TimeoutError:
                pass

            if not self._connected:
                continue
            now = self._loop_time()
            self._emit_freshness_change(now)

            packet_reference = self._last_packet_monotonic or self._connected_monotonic
            if (
                packet_reference is not None
                and now - packet_reference > self._options.watchdog_timeout
            ):
                self._statistics = replace(
                    self._statistics,
                    watchdog_resets=self._statistics.watchdog_resets + 1,
                )
                await self.async_reconnect("Protocol watchdog expired")
                continue

            if (
                self._last_status_request_monotonic is None
                or now - self._last_status_request_monotonic
                >= self._options.status_interval
            ):
                try:
                    await self.async_request_status()
                except (NotConnectedError, TimeoutError, *BLEAK_RETRY_EXCEPTIONS):
                    continue

            if not self._options.settings_keepalive:
                continue

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
            if not should_send_initial and not should_send_periodic:
                continue
            try:
                await self.async_send_settings_keepalive()
            except (
                StateUnavailableError,
                NotConnectedError,
                TimeoutError,
                *BLEAK_RETRY_EXCEPTIONS,
            ):
                continue

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

    client = await establish_connection(
        BleakClientWithServiceCache,
        device,
        advertised_name,
        max_attempts=3,
        ble_device_callback=fresh_device,
        use_services_cache=True,
    )
    decoder = NotificationStreamDecoder()
    status: StatusData | None = None
    settings: SettingsData | None = None
    status_event = asyncio.Event()

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
        notify_characteristic, write_characteristic = _required_characteristics(client)
        await client.start_notify(notify_characteristic, notification_handler)
        await client.write_gatt_char(
            write_characteristic, encode_status_request(), response=False
        )
        async with asyncio.timeout(timeout):
            await status_event.wait()
        if status is None:  # pragma: no cover - event invariant
            raise TimeoutError("Status event was set without a status frame")
        return ProbeResult(status=status, settings=settings, model_support=support)
    finally:
        if client.is_connected:
            try:
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
