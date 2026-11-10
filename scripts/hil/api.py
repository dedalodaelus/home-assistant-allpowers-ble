"""Bounded, authenticated, read-only Home Assistant API operations.

This module never exposes URLs, API responses or transport exceptions in errors:
all can contain network topology or personally identifying information.
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any
from urllib import error, parse, request

MAX_JSON_BYTES = 16 * 1024 * 1024
API_TIMEOUT = 12.0
_CONTROL_DOMAINS = {
    "ac_output": "switch",
    "dc_output": "switch",
    "light": "switch",
    "eco_mode": "switch",
    "car_charger": "switch",
    "work_mode": "select",
    "eco_timeout": "select",
}
_CONTROL_OPTIONS = {
    "work_mode": {"mute", "standard", "fast"},
    "eco_timeout": {"one_hour", "two_hours", "four_hours", "six_hours"},
}


class HAAPIError(Exception):
    """Safe message about a Home Assistant API operation."""


def normalize_url(url: str, *, allow_insecure_http: bool = False) -> str:
    """Accept only a bare HA origin and keep TLS verification enabled."""
    try:
        parts = parse.urlsplit(url.strip())
        port = parts.port  # validates the port without exposing the input
    except ValueError as ex:
        raise HAAPIError("Invalid Home Assistant URL or port") from ex

    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or (port is not None and port <= 0)
    ):
        raise HAAPIError(
            "Provide only the Home Assistant origin, without credentials or paths"
        )

    hostname = parts.hostname.casefold()
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme == "http" and not (loopback or allow_insecure_http):
        raise HAAPIError(
            "HTTP to a remote host exposes your token; use HTTPS or "
            "explicitly pass --allow-insecure-http on a trusted network"
        )
    # urlsplit() normalizes the scheme but not the original input string. A
    # mixed-case HTTPS:// origin must never become an unencrypted WS URL.
    return parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _json_object(payload: Any, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HAAPIError(f"Unexpected {context} response type")
    return payload


def _load_json(raw: bytes | str, context: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as ex:
        raise HAAPIError(f"Invalid JSON from {context}") from ex


class HAClient:
    """Session-free REST reader and short-lived WebSocket reader."""

    def __init__(self, origin: str, token: str, *, allow_insecure_http: bool = False):
        self.origin = normalize_url(origin, allow_insecure_http=allow_insecure_http)
        if not token or not token.strip():
            raise HAAPIError("An access token is required")
        self._token = token.strip()

    def rest(self, path: str) -> Any:
        """Perform a bounded GET for a fixed API path, with no redirect following."""
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise HAAPIError("Invalid API path")
        req = request.Request(
            self.origin + path,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        # Do not inherit http_proxy/https_proxy/ALL_PROXY from the runner's
        # environment. Those variables can route a loopback HTTP request
        # (including its Authorization header) to an unexpected proxy.
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirectHandler())
        try:
            with opener.open(req, timeout=API_TIMEOUT) as response:
                if response.status != 200:
                    raise HAAPIError(f"Home Assistant returned HTTP {response.status}")
                content = response.read(MAX_JSON_BYTES + 1)
        except error.HTTPError as ex:
            if ex.code in {401, 403}:
                raise HAAPIError(
                    "Home Assistant token is invalid or lacks permission"
                ) from ex
            raise HAAPIError(
                f"Home Assistant REST request failed (HTTP {ex.code})"
            ) from ex
        except (error.URLError, TimeoutError, socket.timeout, OSError) as ex:
            raise HAAPIError(
                "Home Assistant REST connection failed or timed out"
            ) from ex
        if len(content) > MAX_JSON_BYTES:
            raise HAAPIError("Home Assistant REST response exceeds size limit")
        return _load_json(content, "REST API")

    def config(self) -> dict[str, Any]:
        """Fetch core configuration without retaining private location fields."""
        return _json_object(self.rest("/api/config"), "configuration")

    def entity_state(self, entity_id: str) -> dict[str, Any] | None:
        """Read exactly one entity and discard all unrelated state attributes."""
        if re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id) is None:
            raise HAAPIError("Invalid entity identifier returned by Home Assistant")
        try:
            data = self.rest("/api/states/" + entity_id)
        except HAAPIError as ex:
            if "HTTP 404" in str(ex):
                return None
            raise
        return _json_object(data, "entity state")

    def control_state(self, key: str, entity_id: str) -> dict[str, Any]:
        """Read one discovered integration control and return curated state only."""
        domain = _CONTROL_DOMAINS.get(key)
        if (
            domain is None
            or not isinstance(entity_id, str)
            or not entity_id.startswith(domain + ".")
        ):
            raise HAAPIError("Invalid discovered control entity")
        payload = self.entity_state(entity_id)
        if payload is None:
            return {"available": False, "state": None, "options": []}
        state = payload.get("state")
        if domain == "switch":
            available = isinstance(state, str) and state in {"on", "off"}
            return {
                "available": available,
                "state": state if available else None,
                "options": [],
            }
        attributes = payload.get("attributes")
        options = attributes.get("options") if isinstance(attributes, dict) else None
        allowed = _CONTROL_OPTIONS[key]
        safe_options = (
            sorted(option for option in options if option in allowed)
            if isinstance(options, list)
            and all(isinstance(option, str) for option in options)
            else []
        )
        available = isinstance(state, str) and state in safe_options
        return {
            "available": available,
            "state": state if available else None,
            "options": safe_options,
        }

    def call_control(self, key: str, entity_id: str, value: str) -> None:
        """Call one allowlisted switch/select service on one registry-resolved ID."""
        domain = _CONTROL_DOMAINS.get(key)
        if (
            domain is None
            or not isinstance(entity_id, str)
            or re.fullmatch(rf"{domain}\.[a-z0-9_]+", entity_id) is None
        ):
            raise HAAPIError("Invalid discovered control entity")
        if not isinstance(value, str):
            raise HAAPIError("Invalid control target value")
        if domain == "switch":
            if value not in {"on", "off"}:
                raise HAAPIError("Invalid switch target state")
            service = "turn_on" if value == "on" else "turn_off"
            command: dict[str, Any] = {
                "type": "call_service",
                "domain": "switch",
                "service": service,
                "target": {"entity_id": entity_id},
            }
        else:
            if value not in _CONTROL_OPTIONS[key]:
                raise HAAPIError("Invalid select target option")
            command = {
                "type": "call_service",
                "domain": "select",
                "service": "select_option",
                "service_data": {"option": value},
                "target": {"entity_id": entity_id},
            }
        result = self.websocket([command])[0]
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            raise HAAPIError("Home Assistant did not acknowledge the service call")

    def hil_capabilities(self, entry_id: str) -> dict[str, Any]:
        """Fetch and strictly normalize the entry-scoped HIL capability snapshot."""
        if (
            not isinstance(entry_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry_id) is None
        ):
            raise HAAPIError("Invalid selected integration entry")
        result = self.websocket(
            [{"type": "allpowers_ble/hil/capabilities", "entry_id": entry_id}]
        )[0]
        classifications = {
            "verified",
            "experimental_read_only",
            "rejected",
            "identity_conflict",
        }
        required_booleans = (
            "verified",
            "identity_conflicted",
            "connected",
            "status_fresh",
            "settings_fresh",
            "pending_transaction",
        )
        if (
            not isinstance(result, dict)
            or type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1
            or result.get("model") is None
            or not isinstance(result.get("profile"), str)
            or re.fullmatch(r"[a-z0-9.-]{1,80}", result["profile"]) is None
            or not isinstance(result.get("classification"), str)
            or result["classification"] not in classifications
            or any(type(result.get(key)) is not bool for key in required_booleans)
        ):
            raise HAAPIError("Malformed HIL capability snapshot")
        model = result["model"]
        if not isinstance(model, str) or model not in {
            "R600",
            "VOLIX P1800",
            "S500",
            "S700",
            "BLE power station",
        }:
            raise HAAPIError("Malformed HIL model identity")
        capabilities = result.get("capabilities")
        capability_keys = {
            "read_telemetry",
            "write_output_controls",
            "write_settings_controls",
            "write_settings_keepalive",
            "car_charger_enabled",
        }
        if (
            not isinstance(capabilities, dict)
            or set(capabilities) != capability_keys
            or any(type(value) is not bool for value in capabilities.values())
        ):
            raise HAAPIError("Malformed HIL capability flags")
        normalized: dict[str, Any] = {
            key: result[key]
            for key in (
                "schema_version",
                "model",
                "profile",
                "classification",
                "verified",
                "identity_conflicted",
                "connected",
                "status_fresh",
                "settings_fresh",
                "pending_transaction",
            )
        }
        for key in ("hardware_revision", "firmware_revision"):
            value = result.get(key)
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,3}", value) is None
            ):
                raise HAAPIError("Malformed HIL version metadata")
            normalized[key] = value
        for key in ("raw_hardware_revision", "raw_firmware_revision"):
            value = result.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= 255):
                raise HAAPIError("Malformed HIL raw version metadata")
            normalized[key] = value
        generation = result.get("session_generation")
        if generation is not None and (type(generation) is not int or generation < 1):
            raise HAAPIError("Malformed HIL session generation")
        normalized["session_generation"] = generation
        normalized["capabilities"] = dict(capabilities)
        return normalized

    def websocket(
        self, commands: list[dict[str, Any]], *, scanner_snapshot: bool = False
    ) -> list[Any]:
        """Send read-only WS commands, optionally capturing one scanner event.

        The scanner subscription is short-lived and admin-restricted by HA.
        Closing the socket unregisters it. Never retain its raw identifiers.
        """
        if scanner_snapshot and commands != [
            {"type": "bluetooth/subscribe_scanner_details"}
        ]:
            raise HAAPIError("Unsupported scanner subscription request")
        try:
            from websockets.exceptions import WebSocketException
            from websockets.sync.client import connect
        except ImportError as ex:
            raise HAAPIError(
                "Missing HIL dependency; install requirements_hil.txt"
            ) from ex

        origin = parse.urlsplit(self.origin)
        scheme = "wss" if origin.scheme == "https" else "ws"
        ws_url = f"{scheme}://{origin.netloc}/api/websocket"
        try:
            with connect(
                ws_url,
                open_timeout=API_TIMEOUT,
                close_timeout=2,
                max_size=MAX_JSON_BYTES,
                proxy=None,
            ) as ws:
                hello = _json_object(
                    _load_json(ws.recv(timeout=API_TIMEOUT), "WebSocket"),
                    "handshake",
                )
                if hello.get("type") != "auth_required":
                    raise HAAPIError("Unexpected Home Assistant WebSocket handshake")
                ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = _json_object(
                    _load_json(ws.recv(timeout=API_TIMEOUT), "WebSocket"),
                    "authentication",
                )
                if auth.get("type") != "auth_ok":
                    raise HAAPIError("Home Assistant WebSocket authentication failed")

                results: list[Any] = []
                for request_id, command in enumerate(commands, start=1):
                    ws.send(json.dumps({"id": request_id, **command}))
                    reply = _json_object(
                        _load_json(ws.recv(timeout=API_TIMEOUT), "WebSocket"), "command"
                    )
                    if reply.get("type") != "result" or reply.get("id") != request_id:
                        raise HAAPIError("Unexpected WebSocket reply order or type")
                    if reply.get("success") is not True:
                        code = (
                            reply["error"].get("code")
                            if isinstance(reply.get("error"), dict)
                            else None
                        )
                        safe_code = (
                            code
                            if isinstance(code, str)
                            and re.fullmatch(r"[a-z_]{1,40}", code)
                            else "unsupported_or_denied"
                        )
                        raise HAAPIError(
                            f"Home Assistant WebSocket command rejected ({safe_code})"
                        )
                    results.append(reply.get("result"))
                if scanner_snapshot:
                    # HA sends the subscription acknowledgement followed by an
                    # initial scanner-details event, but sends no event when
                    # no scanners exist. Never wait indefinitely for it.
                    try:
                        event = _json_object(
                            _load_json(ws.recv(timeout=1.5), "scanner event"),
                            "scanner event",
                        )
                    except TimeoutError:
                        return [None]
                    if event.get("type") != "event" or event.get("id") != 1:
                        raise HAAPIError("Unexpected Bluetooth scanner event")
                    return [event.get("event")]
                return results
        except HAAPIError:
            raise
        except (WebSocketException, OSError, TimeoutError, ValueError, EOFError) as ex:
            raise HAAPIError(
                "Home Assistant WebSocket connection failed or timed out"
            ) from ex

    def scanner_candidates(self) -> dict[str, int] | None:
        """Read an optional, privacy-safe scanner inventory (not route proof).

        Home Assistant 2026.10.4 exposes an admin-only scanner subscription.
        Its raw records include BLE addresses and device names; return counts
        only. A remote, connectable scanner is *not* proof of an ESPHome proxy,
        nor evidence of which connection route was used for our device.
        """
        try:
            events = self.websocket(
                [{"type": "bluetooth/subscribe_scanner_details"}],
                scanner_snapshot=True,
            )
        except HAAPIError:
            # Missing integration / permission / unsupported command is not a
            # failure of the ordinary, unprivileged HA preflight.
            return None
        event = events[0] if events else None
        if not isinstance(event, dict) or not isinstance(event.get("add"), list):
            return None
        records = event["add"]
        if len(records) > 128 or any(
            not isinstance(row, dict)
            or not isinstance(row.get("connectable"), bool)
            or not isinstance(row.get("scanner_type"), str)
            for row in records
        ):
            return None
        return {
            "total_scanners": len(records),
            "connectable_scanners": sum(bool(row["connectable"]) for row in records),
            "remote_scanners": sum(row["scanner_type"] == "remote" for row in records),
            "connectable_remote_scanners": sum(
                row["scanner_type"] == "remote" and row["connectable"]
                for row in records
            ),
        }

    def integration_build_fingerprint(self) -> str | None:
        """Read the optional administrator-only integration fingerprint."""
        try:
            result = self.websocket([{"type": "allpowers_ble/hil/build"}])[0]
        except HAAPIError:
            return None
        if (
            not isinstance(result, dict)
            or set(result) != {"schema_version", "fingerprint"}
            or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1
            or not isinstance(result.get("fingerprint"), str)
            or re.fullmatch(r"[a-f0-9]{64}", result["fingerprint"]) is None
        ):
            return None
        return result["fingerprint"]

    def discovery(
        self,
    ) -> tuple[list[Any], list[Any], dict[str, Any] | None, list[Any] | None]:
        """Retrieve registries and optional version/config-entry metadata.

        Optional calls are isolated so permissions or unsupported extensions do
        not discard a successful read of the core registries.
        """
        entities, devices = self.websocket(
            [
                {"type": "config/entity_registry/list"},
                {"type": "config/device_registry/list"},
            ]
        )
        if not isinstance(entities, list) or not isinstance(devices, list):
            raise HAAPIError("Unexpected Home Assistant registry response")
        manifest: dict[str, Any] | None = None
        entries: list[Any] | None = None
        try:
            response = self.websocket(
                [{"type": "manifest/get", "integration": "allpowers_ble"}]
            )[0]
            if isinstance(response, dict):
                manifest = response
        except HAAPIError:
            pass
        try:
            response = self.websocket(
                [{"type": "config_entries/get", "domain": "allpowers_ble"}]
            )[0]
            if isinstance(response, list):
                entries = response
        except HAAPIError:
            pass
        return entities, devices, manifest, entries


class _NoRedirectHandler(request.HTTPRedirectHandler):
    """Never send a bearer token to a redirect destination."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None
