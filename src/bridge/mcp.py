"""Minimal JSON-RPC 2.0 endpoint over newline-JSON or RFC 6455 text frames.

This is the transport shared by the Claude Channel adapter, the Bridge MCP
server, and the router protocol. An :class:`RpcEndpoint` can simultaneously
answer requests, receive notifications, send requests, and push notifications,
which is what a two-way channel needs. ``Framing.JSONL`` (newline-delimited
JSON) is the default and is what MCP stdio and Bridge's router protocol use;
``Framing.WS_CLIENT``/``Framing.WS_SERVER`` speak RFC 6455 text frames instead,
for ``codex app-server --listen unix://``.
"""

from __future__ import annotations

import enum
import json
import os
import socket
import threading
from collections.abc import Callable, Mapping
from typing import Any


class Framing(enum.Enum):
    JSONL = "jsonl"
    WS_CLIENT = "ws-client"
    WS_SERVER = "ws-server"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# JSON-RPC + MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: JsonRpcError | None = None


MethodHandler = Callable[[dict[str, Any]], Any]
NotificationHandler = Callable[[dict[str, Any]], None]
#: ``on_frame(source, direction, obj)`` -- see :mod:`bridge.lab.capture`.
FrameHook = Callable[[str, str, Mapping[str, Any]], None]


class RpcEndpoint:
    def __init__(
        self,
        sock: socket.socket,
        *,
        name: str = "",
        on_close: Callable[[], None] | None = None,
        on_frame: FrameHook | None = None,
        framing: Framing = Framing.JSONL,
        include_jsonrpc: bool = True,
        on_parse_error: Callable[[bytes], None] | None = None,
    ) -> None:
        self._sock = sock
        self._name = name
        self._on_close = on_close
        self._framing = framing
        self._include_jsonrpc = include_jsonrpc
        self._on_parse_error = on_parse_error
        # Opt-in `bridge lab` wire capture. Unset -> a single dict lookup and no
        # hook, so the endpoint pays nothing and writes nothing.
        if on_frame is None and os.environ.get("BRIDGE_LAB_CAPTURE"):
            from .lab.capture import hook_from_env

            on_frame = hook_from_env()
        self._on_frame = on_frame
        self._buf = bytearray()
        self._methods: dict[str, MethodHandler] = {}
        self._notifications: dict[str, NotificationHandler] = {}
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._id = 0
        self._id_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name=f"rpc-{name}")

    def start(self) -> RpcEndpoint:
        if self._framing is Framing.WS_CLIENT:
            # Synchronous so a handshake failure raises to the caller instead of
            # surfacing only as a silent reader-thread close.
            from . import ws

            ws.client_handshake(self._sock)
        self._thread.start()
        return self

    # --- registration -------------------------------------------------------
    def method(self, name: str, handler: MethodHandler) -> None:
        self._methods[name] = handler

    def notification(self, name: str, handler: NotificationHandler) -> None:
        self._notifications[name] = handler

    # --- outbound -----------------------------------------------------------
    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10.0
    ) -> Any:
        with self._id_lock:
            self._id += 1
            req_id = self._id
        pending = _Pending()
        with self._pending_lock:
            self._pending[req_id] = pending
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise JsonRpcError(INTERNAL_ERROR, f"timeout waiting for {method!r}")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        extra: Mapping[str, Any] | None = None,
        omit_empty_params: bool = False,
    ) -> None:
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not (omit_empty_params and not params):
            envelope["params"] = params or {}
        envelope.update(extra or {})
        self._send(envelope)

    # --- internals ----------------------------------------------------------
    def _send(self, obj: dict[str, Any]) -> None:
        if not self._include_jsonrpc:
            obj = {k: v for k, v in obj.items() if k != "jsonrpc"}
        if self._on_frame is not None:
            self._on_frame(self._name, "out", obj)
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        with self._send_lock:
            try:
                if self._framing is Framing.JSONL:
                    self._sock.sendall(data + b"\n")
                else:
                    from . import ws

                    ws.send_text(self._sock, data, mask=self._framing is Framing.WS_CLIENT)
            except OSError:
                pass

    def _read_loop(self) -> None:
        exc_types: tuple[type[BaseException], ...] = (OSError,)
        if self._framing is not Framing.JSONL:
            from . import ws

            exc_types = (OSError, ws.WebSocketError)
        unexpected = False
        try:
            if self._framing is Framing.WS_SERVER:
                # Runs here, not in start(), so a server endpoint can be
                # constructed and start()ed before its peer connects, instead of
                # blocking the caller's constructor on the handshake; inside the
                # try so a handshake failure gets the same teardown as any other
                # protocol violation, instead of an unhandled traceback.
                ws.server_handshake(self._sock)
            if self._framing is Framing.JSONL:
                while not self._closed:
                    chunk = self._sock.recv(65536)
                    if not chunk:
                        unexpected = not self._closed
                        break
                    self._buf.extend(chunk)
                    while b"\n" in self._buf:
                        line, _, rest = self._buf.partition(b"\n")
                        self._buf = bytearray(rest)
                        line = line.strip()
                        if line:
                            self._handle_line(bytes(line))
            else:
                while not self._closed:
                    payload = ws.recv_message(
                        self._sock, require_mask=self._framing is Framing.WS_SERVER
                    )
                    if payload is None:
                        unexpected = not self._closed
                        break
                    self._handle_line(payload)
        except exc_types:
            unexpected = not self._closed
        finally:
            self._fail_pending()
            # Only signal callers when the peer went away unexpectedly (e.g. the
            # App Server crashed) — not when we tore the connection down
            # ourselves via close().
            if unexpected and self._on_close is not None:
                self._on_close()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            if self._on_parse_error is not None:
                self._on_parse_error(line)
            return
        if self._on_frame is not None:
            self._on_frame(self._name, "in", msg)
        # Dispatch keys only on "method"/"id", so a response missing "jsonrpc"
        # and a notification carrying extra keys (e.g. "emittedAtMs") are
        # handled by construction.
        if "method" in msg and "id" in msg:
            self._handle_request(msg)
        elif "method" in msg:
            self._handle_notification(msg)
        elif "id" in msg:
            self._handle_response(msg)

    def _handle_request(self, msg: dict[str, Any]) -> None:
        req_id = msg["id"]
        method = msg["method"]
        params = msg.get("params") or {}
        handler = self._methods.get(method)
        if handler is None:
            self._send(_error(req_id, METHOD_NOT_FOUND, f"method {method!r} not found"))
            return
        try:
            result = handler(params)
        except JsonRpcError as exc:
            self._send(_error(req_id, exc.code, exc.message, exc.data))
            return
        except Exception as exc:  # noqa: BLE001
            self._send(_error(req_id, INTERNAL_ERROR, str(exc)))
            return
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        handler = self._notifications.get(msg["method"])
        if handler is not None:
            try:
                handler(msg.get("params") or {})
            except Exception:  # noqa: BLE001 - notifications never error back
                pass

    def _handle_response(self, msg: dict[str, Any]) -> None:
        with self._pending_lock:
            pending = self._pending.pop(msg["id"], None)
        if pending is None:
            return
        if "error" in msg:
            err = msg["error"]
            pending.error = JsonRpcError(err.get("code", INTERNAL_ERROR), err.get("message", ""))
        else:
            pending.result = msg.get("result")
        pending.event.set()

    def _fail_pending(self) -> None:
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for p in pendings:
            p.error = JsonRpcError(INTERNAL_ERROR, "connection closed")
            p.event.set()

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}
