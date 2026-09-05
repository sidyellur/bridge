"""Typed client for the router socket.

Used by the CLI, the MCP server, the Claude Channel adapter, and the wrappers.
A background reader thread demultiplexes frames: responses are matched to
pending requests by id; server-pushed events are handed to an optional
``on_event`` callback (adapters) or buffered.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from .paths import Paths
from .protocol import (
    ConnectionClosed,
    FrameBuffer,
    encode_frame,
    hello_args,
    request,
)
from .router import RouterConfig, read_token

# The router caps a synchronous call at ``RouterConfig.timeout_cap_s`` and then
# answers it itself with a ``timeout`` result. A client that gave up at exactly
# the cap would race that answer, so it waits a fixed slack longer and lets the
# router's own verdict arrive. Derived from the cap rather than restated next to
# it, so the two can never drift apart.
CALL_TIMEOUT_SLACK_S = 5.0
DEFAULT_CALL_TIMEOUT_S = RouterConfig().timeout_cap_s + CALL_TIMEOUT_SLACK_S


class RouterClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: tuple[str, str] | None = None


class RouterClient:
    def __init__(
        self,
        sock: socket.socket,
        *,
        session_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._sock = sock
        self.session_id = session_id
        self._on_event = on_event
        self._buf = FrameBuffer()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._events_cv = threading.Condition()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # --- construction -------------------------------------------------------
    @classmethod
    def connect(
        cls,
        paths: Paths,
        *,
        session_id: str | None = None,
        token: str | None = None,
        role: str = "client",
        on_event: Callable[[dict[str, Any]], None] | None = None,
        timeout: float = 5.0,
    ) -> RouterClient:
        token = token or read_token(paths)
        sock = _connect_socket(paths.socket, timeout)
        client = cls(sock, session_id=session_id, on_event=on_event)
        try:
            client._hello(token, session_id, role)
        except Exception:
            client.close()
            raise
        return client

    def _hello(self, token: str, session_id: str | None, role: str) -> None:
        self.call("hello", hello_args(token, session_id, role))

    # --- request/response ---------------------------------------------------
    def call(
        self,
        op: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> Any:
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        pending = _Pending()
        with self._pending_lock:
            self._pending[req_id] = pending
        try:
            self._sock.sendall(encode_frame(request(req_id, op, args or {})))
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise RouterClientError("disconnected", str(exc)) from exc
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise RouterClientError("timeout", f"no response to {op!r} in {timeout}s")
        if pending.error is not None:
            raise RouterClientError(*pending.error)
        return pending.result

    def subscribe(self, session_id: str | None = None) -> Any:
        return self.call("subscribe", {"session_id": session_id or self.session_id})

    # --- events -------------------------------------------------------------
    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        with self._events_cv:
            if not self._events:
                self._events_cv.wait(timeout)
            if self._events:
                return self._events.pop(0)
            return None

    def drain_events(self) -> list[dict[str, Any]]:
        with self._events_cv:
            out = self._events[:]
            self._events.clear()
            return out

    # --- internals ----------------------------------------------------------
    def _read_loop(self) -> None:
        try:
            while not self._closed:
                data = self._sock.recv(65536)
                if not data:
                    break
                self._buf.feed(data)
                for frame in self._buf.frames():
                    self._route(frame)
        except (OSError, ConnectionClosed):
            pass
        finally:
            self._fail_pending()

    def _route(self, frame: dict[str, Any]) -> None:
        t = frame.get("t")
        if t == "resp":
            req_id = frame.get("id")
            with self._pending_lock:
                pending = self._pending.pop(req_id, None)
            if pending is None:
                return
            if frame.get("ok"):
                pending.result = frame.get("result")
            else:
                err = frame.get("error") or {}
                pending.error = (err.get("code", "error"), err.get("message", ""))
            pending.event.set()
        elif t == "event":
            event = frame.get("event") or {}
            if self._on_event is not None:
                self._on_event(event)
            else:
                with self._events_cv:
                    self._events.append(event)
                    self._events_cv.notify_all()

    def _fail_pending(self) -> None:
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for p in pendings:
            p.error = ("disconnected", "router connection closed")
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


def _connect_socket(path, timeout: float) -> socket.socket:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_err = exc
            sock.close()
            time.sleep(0.02)
    raise RouterClientError("disconnected", f"could not connect to router socket: {last_err}")
