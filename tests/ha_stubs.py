"""Minimal dependency stubs for fast unit tests without Home Assistant installed."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import datetime as dt
from enum import Enum
import json
from types import ModuleType
import sys
import time
from typing import Any, Generic, TypeVar


class FakeBleakError(Exception):
    """Synthetic bleak error used by transport tests."""


class FakeCharacteristic:
    """Synthetic GATT characteristic."""


class FakeDevice:
    """Synthetic BLE device."""


class FakeClient:
    """Small BleakClient replacement recording writes."""

    def __init__(self) -> None:
        self.is_connected = True
        self.writes: list[bytes] = []
        self.disconnect_calls = 0
        self.raise_on_write: Exception | None = None
        self.services: Any = None
        self.notification_callback: Callable[..., None] | None = None

    async def write_gatt_char(
        self,
        characteristic: object,
        data: bytes,
        *,
        response: bool,
    ) -> None:
        del characteristic, response
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.writes.append(bytes(data))

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def start_notify(
        self,
        characteristic: object,
        callback: Callable[..., None],
    ) -> None:
        del characteristic
        self.notification_callback = callback


class _StrEnum(str, Enum):
    """Small string enum base for Home Assistant constants."""


class Platform(_StrEnum):
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"
    SELECT = "select"
    BUTTON = "button"
    NUMBER = "number"


class EntityCategory(_StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class UnitOfTime(_StrEnum):
    MINUTES = "min"
    SECONDS = "s"


class UnitOfPower(_StrEnum):
    WATT = "W"


class SensorDeviceClass(_StrEnum):
    BATTERY = "battery"
    DURATION = "duration"
    POWER = "power"
    SIGNAL_STRENGTH = "signal_strength"


class SensorStateClass(_StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class BinarySensorDeviceClass(_StrEnum):
    BATTERY_CHARGING = "battery_charging"
    CONNECTIVITY = "connectivity"
    POWER = "power"


class ButtonDeviceClass(_StrEnum):
    RESTART = "restart"


class NumberMode(_StrEnum):
    BOX = "box"


class IssueSeverity(_StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    key: str
    translation_key: str | None = None
    icon: str | None = None
    device_class: Any = None
    native_unit_of_measurement: Any = None
    state_class: Any = None
    suggested_display_precision: int | None = None
    entity_category: Any = None
    entity_registry_enabled_default: bool = True


class SensorEntityDescription(_EntityDescription):
    """Sensor description stub."""


class BinarySensorEntityDescription(_EntityDescription):
    """Binary sensor description stub."""


class SwitchEntityDescription(_EntityDescription):
    """Switch description stub."""


class _Entity:
    """Entity base with the few attributes exercised by unit tests."""

    hass: Any


class SensorEntity(_Entity):
    pass


class BinarySensorEntity(_Entity):
    pass


class SwitchEntity(_Entity):
    pass


class ButtonEntity(_Entity):
    pass


class SelectEntity(_Entity):
    pass


class NumberEntity(_Entity):
    pass


T = TypeVar("T")


class ConfigEntry(Generic[T]):
    """Config entry stub with runtime data and unload callbacks."""

    def __init__(
        self,
        *,
        title: str = "ALLPOWERS R600",
        data: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        entry_id: str = "entry-id",
        version: int = 1,
        minor_version: int = 1,
        unique_id: str | None = None,
    ) -> None:
        self.title = title
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.entry_id = entry_id
        self.version = version
        self.minor_version = minor_version
        self.unique_id = unique_id
        self.runtime_data: T
        self._unload_callbacks: list[Callable[..., Any]] = []

    def async_on_unload(self, callback_fn: Callable[..., Any]) -> None:
        self._unload_callbacks.append(callback_fn)

    def add_update_listener(self, listener: Callable[..., Any]) -> Callable[[], None]:
        self._update_listener = listener
        return lambda: None


class _FlowBase:
    """Config-flow helpers returning Home Assistant-like result dictionaries."""

    def __init_subclass__(cls, *, domain: str | None = None, **kwargs: Any) -> None:
        del domain
        super().__init_subclass__(**kwargs)

    async def async_set_unique_id(
        self,
        unique_id: str,
        *,
        raise_on_progress: bool = True,
    ) -> None:
        del raise_on_progress
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def _abort_if_unique_id_mismatch(self) -> None:
        return None

    def _async_current_ids(self, *, include_ignore: bool) -> set[str]:
        del include_ignore
        return set()

    def _get_reconfigure_entry(self) -> ConfigEntry[Any]:
        entry = getattr(self, "_reconfigure_entry", None)
        if entry is None:
            raise RuntimeError("Reconfigure entry is not set")
        return entry

    def _set_confirm_only(self) -> None:
        self.confirm_only = True

    def async_abort(self, *, reason: str) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    def async_show_form(
        self,
        *,
        step_id: str,
        data_schema: Any,
        errors: Mapping[str, str] | None = None,
        description_placeholders: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": dict(errors or {}),
            "description_placeholders": dict(description_placeholders or {}),
        }

    def async_create_entry(
        self,
        *,
        title: str,
        data: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {"type": "create_entry", "title": title, "data": dict(data)}
        if options is not None:
            result["options"] = dict(options)
        return result

    def async_update_reload_and_abort(
        self,
        entry: ConfigEntry[Any],
        *,
        data_updates: Mapping[str, Any] | None = None,
        options_updates: Mapping[str, Any] | None = None,
        reason: str = "reconfigure_successful",
        reload_even_if_entry_is_unchanged: bool = True,
    ) -> dict[str, Any]:
        del reload_even_if_entry_is_unchanged
        if data_updates is not None:
            merged_data = dict(entry.data)
            merged_data.update(data_updates)
            self.hass.config_entries.async_update_entry(entry, data=merged_data)
        if options_updates is not None:
            merged_options = dict(entry.options)
            merged_options.update(options_updates)
            self.hass.config_entries.async_update_entry(entry, options=merged_options)
        return self.async_abort(reason=reason)


class ConfigFlow(_FlowBase):
    pass


class OptionsFlow(_FlowBase):
    config_entry: ConfigEntry[Any]


ConfigFlowResult = dict[str, Any]


class section:
    """Minimal data-entry section wrapper used by config/option forms."""

    def __init__(self, schema: Any, options: Mapping[str, Any] | None = None) -> None:
        self.schema = schema
        self.options = dict(options or {})


class HomeAssistantError(RuntimeError):
    def __init__(
        self,
        message: str | None = None,
        *,
        translation_domain: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> None:
        if message is None and translation_key is not None:
            message = translation_key
        super().__init__(message or "")
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = (
            dict(translation_placeholders) if translation_placeholders else None
        )


class ConfigEntryNotReady(RuntimeError):
    pass


class HomeAssistant:
    pass


def callback(func: T) -> T:
    return func


class DataUpdateCoordinator(Generic[T]):
    """Push coordinator stub."""

    def __init__(
        self,
        hass: Any,
        logger: Any,
        *,
        config_entry: ConfigEntry[Any],
        name: str,
        update_interval: Any,
        always_update: bool,
    ) -> None:
        del logger, name, update_interval, always_update
        self.hass = hass
        self.config_entry = config_entry
        self.data: T
        self.update_count = 0

    def async_set_updated_data(self, data: T) -> None:
        self.data = data
        self.update_count += 1


C = TypeVar("C")


class CoordinatorEntity(Generic[C], _Entity):
    def __init__(self, coordinator: C) -> None:
        self.coordinator = coordinator
        self.hass = getattr(coordinator, "hass", None)


@dataclass
class DeviceInfo:
    identifiers: set[tuple[str, str]]
    connections: set[tuple[str, str]]
    manufacturer: str
    model: str
    name: str
    hw_version: str | None = None
    sw_version: str | None = None


@dataclass
class DeviceEntry:
    name: str | None = None
    model: str | None = None
    manufacturer: str | None = None


@dataclass
class BluetoothServiceInfoBleak:
    name: str | None
    address: str
    rssi: int | None = None
    service_uuids: list[str] | tuple[str, ...] = ()
    connectable: bool = True
    manufacturer_data: dict[int, bytes] | None = None
    service_data: dict[str, bytes] | None = None
    source: str = "local"
    device: Any = None
    advertisement: Any = None
    time: float = 0
    tx_power: int | None = None


class BluetoothScanningMode(_StrEnum):
    PASSIVE = "passive"


class BluetoothCallbackMatcher(dict[str, Any]):
    pass


class _Required:
    def __init__(self, key: str, default: Any = None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash((self.key, self.default))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Required) and (self.key, self.default) == (
            other.key,
            other.default,
        )


class _Schema:
    def __init__(self, schema: Mapping[Any, Any]) -> None:
        self.schema = dict(schema)

    def __call__(self, value: Any) -> Any:
        return value


class _Range:
    def __init__(self, *, min: float | None = None, max: float | None = None) -> None:
        self.min = min
        self.max = max


class _In:
    def __init__(self, container: Any) -> None:
        self.container = container


async def _async_request_active_scan(hass: Any) -> None:
    del hass


def _async_ble_device_from_address(*args: Any, **kwargs: Any) -> FakeDevice:
    del args, kwargs
    return FakeDevice()


def _async_discovered_service_info(*args: Any, **kwargs: Any) -> list[Any]:
    del args, kwargs
    return []


def _async_register_callback(*args: Any, **kwargs: Any) -> Callable[[], None]:
    del args, kwargs
    return lambda: None


async def _establish_connection(*args: Any, **kwargs: Any) -> FakeClient:
    del args, kwargs
    return FakeClient()


def _async_redact_data(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "**REDACTED**" if key in keys else _async_redact_data(item, keys)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_async_redact_data(item, keys) for item in value]
    return value


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _utc_from_timestamp(value: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(value, tz=dt.UTC)


def _get_default_time_zone() -> dt.tzinfo:
    return dt.UTC


def _set_default_time_zone(value: dt.tzinfo) -> None:
    del value


def _create_eager_task(coro: Any) -> asyncio.Task[Any]:
    return asyncio.create_task(coro)


def _get_scheduled_timer_handles(loop: asyncio.AbstractEventLoop) -> list[Any]:
    del loop
    return []


def _json_loads(value: str | bytes) -> Any:
    return json.loads(value)


def _json_loads_object(value: str | bytes) -> dict[str, Any]:
    result = json.loads(value)
    return result if isinstance(result, dict) else {}


def _load_yaml_dict(value: str) -> dict[str, Any]:
    del value
    return {}


def _log_exception(format_err: Any, *args: Any) -> None:
    del format_err, args


def _time_tracker_timestamp() -> float:
    return _utcnow().timestamp()


@dataclass
class _IssueRecord:
    issue_id: str
    translation_key: str


class _IssueRegistry:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, str], _IssueRecord] = {}

    def async_get_issue(self, domain: str, issue_id: str) -> _IssueRecord | None:
        return self.issues.get((domain, issue_id))


_ISSUES = _IssueRegistry()


def _issue_async_get(hass: Any) -> _IssueRegistry:
    del hass
    return _ISSUES


def _issue_async_create_issue(
    hass: Any,
    domain: str,
    issue_id: str,
    *,
    translation_key: str,
    **kwargs: Any,
) -> None:
    del hass, kwargs
    _ISSUES.issues[(domain, issue_id)] = _IssueRecord(
        issue_id=issue_id,
        translation_key=translation_key,
    )


def _issue_async_delete_issue(hass: Any, domain: str, issue_id: str) -> None:
    del hass
    _ISSUES.issues.pop((domain, issue_id), None)


def install() -> None:
    """Install all dependency stubs into ``sys.modules``."""
    bleak = ModuleType("bleak")
    bleak.BleakClient = FakeClient
    backends = ModuleType("bleak.backends")
    characteristic = ModuleType("bleak.backends.characteristic")
    characteristic.BleakGATTCharacteristic = FakeCharacteristic
    device = ModuleType("bleak.backends.device")
    device.BLEDevice = FakeDevice

    connector = ModuleType("bleak_retry_connector")
    connector.BLEAK_RETRY_EXCEPTIONS = (FakeBleakError,)
    connector.BleakClientWithServiceCache = FakeClient
    connector.establish_connection = _establish_connection

    homeassistant = ModuleType("homeassistant")
    components = ModuleType("homeassistant.components")

    bluetooth = ModuleType("homeassistant.components.bluetooth")
    bluetooth.BluetoothServiceInfoBleak = BluetoothServiceInfoBleak
    bluetooth.BluetoothScanningMode = BluetoothScanningMode
    bluetooth.async_ble_device_from_address = _async_ble_device_from_address
    bluetooth.async_discovered_service_info = _async_discovered_service_info
    bluetooth.async_register_callback = _async_register_callback
    bluetooth.async_request_active_scan = _async_request_active_scan
    components.bluetooth = bluetooth

    bluetooth_match = ModuleType("homeassistant.components.bluetooth.match")
    bluetooth_match.ADDRESS = "address"
    bluetooth_match.BluetoothCallbackMatcher = BluetoothCallbackMatcher

    diagnostics = ModuleType("homeassistant.components.diagnostics")
    diagnostics.async_redact_data = _async_redact_data

    sensor = ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorEntity = SensorEntity
    sensor.SensorEntityDescription = SensorEntityDescription
    sensor.SensorStateClass = SensorStateClass

    binary_sensor = ModuleType("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass
    binary_sensor.BinarySensorEntity = BinarySensorEntity
    binary_sensor.BinarySensorEntityDescription = BinarySensorEntityDescription

    switch = ModuleType("homeassistant.components.switch")
    switch.SwitchEntity = SwitchEntity
    switch.SwitchEntityDescription = SwitchEntityDescription

    button = ModuleType("homeassistant.components.button")
    button.ButtonDeviceClass = ButtonDeviceClass
    button.ButtonEntity = ButtonEntity

    select = ModuleType("homeassistant.components.select")
    select.SelectEntity = SelectEntity

    number = ModuleType("homeassistant.components.number")
    number.NumberEntity = NumberEntity
    number.NumberMode = NumberMode

    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigFlowResult = ConfigFlowResult
    config_entries.OptionsFlow = OptionsFlow

    data_entry_flow = ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.section = section

    const = ModuleType("homeassistant.const")
    const.CONF_ADDRESS = "address"
    const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    const.PERCENTAGE = "%"
    const.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
    const.EntityCategory = EntityCategory
    const.Platform = Platform
    const.UnitOfPower = UnitOfPower
    const.UnitOfTime = UnitOfTime

    core = ModuleType("homeassistant.core")
    core.HomeAssistant = HomeAssistant
    core.callback = callback

    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady
    exceptions.HomeAssistantError = HomeAssistantError

    helpers = ModuleType("homeassistant.helpers")
    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddConfigEntryEntitiesCallback = Callable[..., None]

    device_registry = ModuleType("homeassistant.helpers.device_registry")
    device_registry.CONNECTION_BLUETOOTH = "bluetooth"
    device_registry.DeviceEntry = DeviceEntry
    device_registry.DeviceInfo = DeviceInfo

    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.CoordinatorEntity = CoordinatorEntity
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator

    issue_registry = ModuleType("homeassistant.helpers.issue_registry")
    issue_registry.IssueSeverity = IssueSeverity
    issue_registry.async_get = _issue_async_get
    issue_registry.async_create_issue = _issue_async_create_issue
    issue_registry.async_delete_issue = _issue_async_delete_issue

    event = ModuleType("homeassistant.helpers.event")
    event.time_tracker_utcnow = _utcnow
    event.time_tracker_timestamp = _time_tracker_timestamp

    util = ModuleType("homeassistant.util")
    util.utcnow = _utcnow

    util_dt = ModuleType("homeassistant.util.dt")
    util_dt.UTC = dt.UTC
    util_dt.DEFAULT_TIME_ZONE = dt.UTC
    util_dt.utcnow = _utcnow
    util_dt.as_utc = _as_utc
    util_dt.utc_from_timestamp = _utc_from_timestamp
    util_dt.get_default_time_zone = _get_default_time_zone
    util_dt.set_default_time_zone = _set_default_time_zone

    util_location = ModuleType("homeassistant.util.location")

    util_async = ModuleType("homeassistant.util.async_")
    util_async.create_eager_task = _create_eager_task
    util_async.get_scheduled_timer_handles = _get_scheduled_timer_handles

    util_json = ModuleType("homeassistant.util.json")
    util_json.json_loads = _json_loads
    util_json.json_loads_object = _json_loads_object
    util_json.JsonObjectType = dict[str, Any]

    util_yaml = ModuleType("homeassistant.util.yaml")
    util_yaml.load_yaml_dict = _load_yaml_dict

    util_logging = ModuleType("homeassistant.util.logging")
    util_logging.log_exception = _log_exception

    util_ulid = ModuleType("homeassistant.util.ulid")
    util_uuid = ModuleType("homeassistant.util.uuid")
    util_event_type = ModuleType("homeassistant.util.event_type")
    util_event_type.EventType = str
    util_signal_type = ModuleType("homeassistant.util.signal_type")
    util_signal_type.SignalType = str
    util_unit_system = ModuleType("homeassistant.util.unit_system")
    util_unit_system.METRIC_SYSTEM = object()

    util.dt = util_dt
    util.location = util_location
    util.async_ = util_async
    util.json = util_json
    util.yaml = util_yaml
    util.logging = util_logging
    util.ulid = util_ulid
    util.uuid = util_uuid
    util.event_type = util_event_type
    util.signal_type = util_signal_type
    util.unit_system = util_unit_system

    runner = ModuleType("homeassistant.runner")
    runner.monotonic = time.monotonic

    homeassistant.components = components
    homeassistant.util = util
    homeassistant.helpers = helpers
    homeassistant.runner = runner

    voluptuous = ModuleType("voluptuous")
    voluptuous.Schema = _Schema
    voluptuous.Required = _Required
    voluptuous.All = lambda *validators: validators
    voluptuous.Coerce = lambda target: target
    voluptuous.Range = _Range
    voluptuous.In = _In

    sys.modules.update(
        {
            "bleak": bleak,
            "bleak.backends": backends,
            "bleak.backends.characteristic": characteristic,
            "bleak.backends.device": device,
            "bleak_retry_connector": connector,
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.bluetooth": bluetooth,
            "homeassistant.components.bluetooth.match": bluetooth_match,
            "homeassistant.components.diagnostics": diagnostics,
            "homeassistant.components.sensor": sensor,
            "homeassistant.components.binary_sensor": binary_sensor,
            "homeassistant.components.switch": switch,
            "homeassistant.components.button": button,
            "homeassistant.components.select": select,
            "homeassistant.components.number": number,
            "homeassistant.config_entries": config_entries,
            "homeassistant.data_entry_flow": data_entry_flow,
            "homeassistant.const": const,
            "homeassistant.core": core,
            "homeassistant.exceptions": exceptions,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entity_platform": entity_platform,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.update_coordinator": update_coordinator,
            "homeassistant.helpers.issue_registry": issue_registry,
            "homeassistant.helpers.event": event,
            "homeassistant.util": util,
            "homeassistant.util.dt": util_dt,
            "homeassistant.util.location": util_location,
            "homeassistant.util.async_": util_async,
            "homeassistant.util.json": util_json,
            "homeassistant.util.yaml": util_yaml,
            "homeassistant.util.logging": util_logging,
            "homeassistant.util.ulid": util_ulid,
            "homeassistant.util.uuid": util_uuid,
            "homeassistant.util.event_type": util_event_type,
            "homeassistant.util.signal_type": util_signal_type,
            "homeassistant.util.unit_system": util_unit_system,
            "homeassistant.runner": runner,
            "voluptuous": voluptuous,
        }
    )
