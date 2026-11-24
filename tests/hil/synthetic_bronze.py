"""Stateful HA boundary fixture for synthetic Bronze onboarding coverage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scripts.validate_hil_qualification import STATUS_REQUEST


class SyntheticBronzeHA:
    """Simulate the external HA boundary while retaining production HIL flow."""

    origin = "https://ha.example.test"

    def __init__(self, *, correlated_status: bool = True) -> None:
        self.correlated_status = correlated_status
        self.calls: list[tuple[Any, ...]] = []
        self.state_changing_calls: list[tuple[Any, ...]] = []
        self.capture_id = "synthetic-capture"
        self.entry_id = "synthetic-entry"
        self.capture_active = False
        self.refresh_seen = False
        self.fetch_count = 0
        self.fetch_snapshots: list[dict[str, Any]] = []
        self.stop_count = 0
        self.stop_snapshot: dict[str, Any] | None = None
        self.status_count = 8
        self.trace_utc = datetime.now(UTC).isoformat()

    def config(self) -> dict[str, str]:
        self.calls.append(("read", "config"))
        return {"version": "2026.10.4", "time_zone": "UTC"}

    def discovery(self):
        self.calls.append(("read", "registry"))
        device_id = "synthetic-device"
        entry_id = "synthetic-entry"
        entities = [
            {
                "device_id": device_id,
                "platform": "allpowers_ble",
                "entity_id": "sensor.synthetic_battery",
                "unique_id": "synthetic_battery",
                "disabled_by": None,
            },
            {
                "device_id": device_id,
                "platform": "allpowers_ble",
                "entity_id": "binary_sensor.synthetic_connected",
                "unique_id": "synthetic_connected",
                "disabled_by": None,
            },
            {
                "device_id": device_id,
                "platform": "allpowers_ble",
                "entity_id": "binary_sensor.synthetic_telemetry_available",
                "unique_id": "synthetic_telemetry_available",
                "disabled_by": None,
            },
            {
                "device_id": device_id,
                "platform": "allpowers_ble",
                "entity_id": "sensor.synthetic_hardware_version",
                "unique_id": "synthetic_hardware_version",
                "disabled_by": None,
            },
            {
                "device_id": device_id,
                "platform": "allpowers_ble",
                "entity_id": "sensor.synthetic_firmware_version",
                "unique_id": "synthetic_firmware_version",
                "disabled_by": None,
            },
            {
                "device_id": device_id,
                "platform": "allpowers_ble",
                "entity_id": "button.synthetic_refresh",
                "unique_id": "synthetic_refresh",
                "config_entry_id": entry_id,
                "disabled_by": None,
            },
        ]
        devices = [
            {
                "id": device_id,
                "config_entry_id": entry_id,
                "identifiers": [["allpowers_ble", "synthetic-identifier"]],
                "model": "R600",
                "hw_version": "0.3",
                "sw_version": "0.2",
            }
        ]
        entries = [{"entry_id": entry_id, "domain": "allpowers_ble", "state": "loaded"}]
        return entities, devices, {"version": "0.3.0"}, entries

    def integration_build_fingerprint(self) -> str:
        from scripts.hil.environment import _local_integration_snapshot

        snapshot = _local_integration_snapshot()
        assert snapshot is not None
        return snapshot[0]

    def scanner_candidates(self) -> dict[str, int]:
        self.calls.append(("read", "scanner_inventory"))
        return {"bluetooth": 1}

    def entity_state(self, entity_id: str) -> dict[str, Any]:
        self.calls.append(("read", "entity", entity_id))
        state = {
            "battery": "76",
            "connected": "on",
            "telemetry_available": "on",
            "hardware_version": "0.3",
            "firmware_version": "0.2",
        }
        key = entity_id.removeprefix("sensor.synthetic_").removeprefix(
            "binary_sensor.synthetic_"
        )
        return {
            "state": state[key],
            "last_updated": datetime.now(UTC).isoformat(),
        }

    def hil_capabilities(self, _entry_id: str) -> dict[str, Any]:
        self.calls.append(("read", "hil_capabilities"))
        return {
            "schema_version": 2,
            "model": "R600",
            "profile": "r600-hw-0.3",
            "classification": "verified",
            "verified": True,
            "identity_conflicted": False,
            "hardware_revision": "0.3",
            "firmware_revision": "0.2",
            "raw_hardware_revision": 3,
            "raw_firmware_revision": 2,
            "capabilities": {
                "read_telemetry": True,
                "write_output_controls": False,
                "write_settings_controls": False,
                "write_settings_keepalive": False,
                "car_charger_enabled": False,
            },
            "connected": True,
            "status_fresh": True,
            "settings_fresh": True,
            "status_notification_count": self.status_count,
            "session_generation": 7,
            "pending_transaction": False,
            "features": {
                "telemetry": {
                    "physical_presence": "present",
                    "production_authorized": False,
                    "candidate_hil_eligible": False,
                    "runtime_ready": True,
                }
            },
        }

    def request_read_only_refresh(self, entity_id: str) -> None:
        assert self.capture_active, "Status refresh must run during an active capture"
        assert not self.refresh_seen, "Bronze should request one status refresh"
        self.calls.append(("read_only_refresh", entity_id))
        self.refresh_seen = True
        self.status_count += 1

    def call_control(self, *args: Any, **kwargs: Any) -> None:
        self.state_changing_calls.append(("call_control", args, kwargs))
        raise AssertionError("Bronze must not call a state-changing HA control")

    def experimental_output(self, *args: Any, **kwargs: Any) -> None:
        self.state_changing_calls.append(("experimental_output", args, kwargs))
        raise AssertionError("Bronze must not dispatch an output action")

    def experimental_settings(self, *args: Any, **kwargs: Any) -> None:
        self.state_changing_calls.append(("experimental_settings", args, kwargs))
        raise AssertionError("Bronze must not dispatch a settings action")

    def websocket(self, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        command = commands[0]
        kind = command["type"]
        if kind not in {
            "allpowers_ble/hil/start",
            "allpowers_ble/hil/fetch",
            "allpowers_ble/hil/stop",
        }:
            raise AssertionError(f"Unexpected HIL command during Bronze: {kind}")
        self.calls.append(("websocket", kind))
        if kind.endswith("/start"):
            assert not self.capture_active, "Capture is already active"
            assert command["entry_id"] == self.entry_id
            assert command["scenario_id"] == "read_only_snapshot"
            self.capture_id = "synthetic-capture"
            self.started_run_id = command["run_id"]
            self.capture_active = True
            return [
                {"schema_version": 2, "capture_id": self.capture_id, "ttl_seconds": 10}
            ]
        assert command["entry_id"] == self.entry_id, "Mismatched entry identifier"
        assert command["capture_id"] == self.capture_id, "Mismatched capture identifier"
        assert self.capture_active, "Fetch/stop requires an active capture"
        if kind.endswith("/fetch"):
            assert self.refresh_seen, "Capture must include the status refresh"
            self.fetch_count += 1
            snapshot = self._trace_snapshot(sealed=False)
            assert snapshot["closed"] is False
            assert snapshot["complete"] is False
            assert snapshot["termination_reason"] is None
            assert all(
                event["event"] != "capture_stopped" for event in snapshot["events"]
            )
            self.fetch_snapshots.append(snapshot)
            return [snapshot]

        self.stop_count += 1
        self.capture_active = False
        self.stop_snapshot = self._trace_snapshot(sealed=True)
        return [self.stop_snapshot]

    def _trace_snapshot(self, *, sealed: bool) -> dict[str, Any]:
        run_id = self.started_run_id
        events = self._trace_events(run_id, sealed=sealed)
        frames = self._trace_frames(run_id)
        return {
            "schema_version": 2,
            "run_id": run_id,
            "scenario_id": "read_only_snapshot",
            "capture_id": self.capture_id,
            "requested_route": "auto",
            "observed_route": "active_proxy",
            "route_observations": [
                {
                    "session_generation": 7,
                    "seq": 2,
                    "utc": events[1]["utc"],
                    "elapsed_ms": 20,
                    "observed_route": "active_proxy",
                    "backend_kind": "esphome",
                    "evidence": "esphome_scanner_binding",
                    "proxy_id": "synthetic-proxy",
                }
            ],
            "capture_layer": "gatt_api_not_rf",
            "qualification_allowed": False,
            "private_raw_bytes": True,
            "closed": sealed,
            "expired": False,
            "complete": sealed,
            "termination_reason": "requested_stop" if sealed else None,
            "dropped_events": 0,
            "dropped_frames": 0,
            "event_count": len(events),
            "frame_count": len(frames),
            "has_gatt_frames": bool(frames),
            "events": events,
            "frames": frames,
        }

    def _trace_events(self, run_id: str, *, sealed: bool) -> list[dict[str, Any]]:
        now = self.trace_utc
        rows = [
            (1, "capture_started", "unverified"),
            (2, "route_observed", "active_proxy"),
            (3, "write_attempt", "active_proxy"),
            (4, "write_ok", "active_proxy"),
        ]
        if self.correlated_status:
            rows.append((5, "notification_decoded", "active_proxy"))
        if sealed:
            rows.append(
                (6 if self.correlated_status else 5, "capture_stopped", "active_proxy")
            )
        events = []
        for seq, event, route in rows:
            record = {
                "schema_version": 2,
                "seq": seq,
                "utc": now,
                "elapsed_ms": seq * 10,
                "run_id": run_id,
                "scenario_id": "read_only_snapshot",
                "event": event,
                "session_generation": 7,
                "layer": "gatt_api",
                "observed_route": route,
                "capture_id": self.capture_id,
            }
            if event in {"write_attempt", "write_ok"}:
                record["transaction_id"] = "synthetic-status-transaction"
            if event == "notification_decoded":
                record["decoded_packet_types"] = ["StatusData"]
                record["status_measurement"] = {
                    "battery": 76,
                    "input_power": 100,
                    "output_power": 25,
                    "remaining_time": 90,
                }
            if event == "route_observed":
                record.update(
                    backend_kind="esphome", evidence="esphome_scanner_binding"
                )
            events.append(record)
        return events

    def _trace_frames(self, run_id: str) -> list[dict[str, Any]]:
        common = {
            "schema_version": 2,
            "seq": 3,
            "utc": self._trace_events(run_id, sealed=False)[2]["utc"],
            "elapsed_ms": 30,
            "run_id": run_id,
            "scenario_id": "read_only_snapshot",
            "event": "write_attempt",
            "session_generation": 7,
            "layer": "gatt_api",
            "observed_route": "active_proxy",
            "transaction_id": "synthetic-status-transaction",
        }
        return [
            {
                **common,
                "direction": "tx",
                "command_type": "status_request",
                "frame_validation": "validated_tx",
                "hex": STATUS_REQUEST.hex(),
                "transaction_id": "synthetic-status-transaction",
            }
        ]
