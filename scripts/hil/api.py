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
    return url.strip().rstrip("/")


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
        opener = request.build_opener(_NoRedirectHandler())
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

    def websocket(self, commands: list[dict[str, Any]]) -> list[Any]:
        """Send fixed read-only commands over one authenticated WS connection."""
        try:
            from websockets.exceptions import WebSocketException
            from websockets.sync.client import connect
        except ImportError as ex:
            raise HAAPIError(
                "Missing HIL dependency; install requirements_hil.txt"
            ) from ex

        scheme = "wss" if self.origin.startswith("https:") else "ws"
        ws_url = f"{scheme}://{parse.urlsplit(self.origin).netloc}/api/websocket"
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
                return results
        except HAAPIError:
            raise
        except (WebSocketException, OSError, TimeoutError, ValueError, EOFError) as ex:
            raise HAAPIError(
                "Home Assistant WebSocket connection failed or timed out"
            ) from ex

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
