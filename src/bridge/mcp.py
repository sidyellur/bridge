"""Minimal JSON-RPC 2.0 endpoint over a newline-delimited byte stream.

This is the transport shared by the Claude Channel adapter and the Codex MCP
tool server. MCP's stdio transport is newline-delimited JSON-RPC; an
:class:`RpcEndpoint` can simultaneously answer requests, receive notifications,
send requests, and push notifications, which is what a two-way channel needs.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from typing import Any


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


class RpcEndpoint:
    def __init__(self, sock: socket.socket, *, name: str = "") -> None:
        self._sock = sock
        self._name = name
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

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # --- internals ----------------------------------------------------------
    def _send(self, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._send_lock:
            try:
                self._sock.sendall(data)
            except OSError:
                pass

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                self._buf.extend(chunk)
                while b"\n" in self._buf:
                    line, _, rest = self._buf.partition(b"\n")
                    self._buf = bytearray(rest)
                    line = line.strip()
                    if line:
                        self._handle_line(bytes(line))
        except OSError:
            pass
        finally:
            self._fail_pending()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
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
