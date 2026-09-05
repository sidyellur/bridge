"""The Bridge router: a single local daemon owning the session registry, call
state machine, delivery queues, transcript, and local authentication.

Design notes
------------
* The socket server runs a single-threaded ``selectors`` event loop. Every
  outbound frame is queued on its connection and flushed by the loop, so the
  router can push a delivery to one connection while handling a request on
  another without threads.
* Synchronous ``call`` is *deferred*: the router does not answer the caller's
  request immediately. It records the caller's connection + request id and
  only sends the response frame once ``reply`` arrives or the deadline passes.
  This is what lets a caller's single MCP request block on a live answer.
* All coordination logic lives on :class:`Router` and is transport-agnostic; it
  talks to connections through a :class:`Notifier`. Unit tests inject a
  recording notifier; the socket server injects one backed by live sockets.
"""

from __future__ import annotations

import os
import secrets
import selectors
import signal
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import PROTOCOL_VERSION
from .paths import Paths
from .protocol import (
    FrameBuffer,
    Hello,
    ProtocolError,
    encode_frame,
    err_response,
    event_frame,
    ok_response,
)
from .store import (
    STATE_OFFLINE,
    STATE_STARTING,
    Store,
)

# Selector key marking the stop() wake-up socket (see RouterServer.serve_forever).
_WAKE = object()


class RouterError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RouterConfig:
    idle_grace_s: float = 30.0
    rate_cap: int = 10
    rate_window_s: float = 3600.0
    queue_cap: int = 20
    timeout_cap_s: int = 60
    hop_budget: int = 1
    # Transcript/rate retention. Rows older than this are dropped by the
    # daemon's periodic prune (at most once per ``prune_interval_s``).
    retention_s: float = 30 * 24 * 3600.0
    prune_interval_s: float = 3600.0


class Notifier(Protocol):
    """How the router reaches connected sessions and resolves waiters."""

    def connected(self, session_id: str) -> bool: ...

    def deliver(self, session_id: str, event: dict[str, Any]) -> bool: ...

    def resolve_sync(self, waiter: Any, result: dict[str, Any]) -> None: ...


@dataclass
class RecordingNotifier:
    """In-memory notifier for unit tests. Tracks which sessions are connected,
    records delivered events, and records resolved sync waiters."""

    connected_ids: set[str] = field(default_factory=set)
    delivered: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    resolved: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)

    def connected(self, session_id: str) -> bool:
        return session_id in self.connected_ids

    def deliver(self, session_id: str, event: dict[str, Any]) -> bool:
        if session_id not in self.connected_ids:
            return False
        self.delivered.append((session_id, event))
        return True

    def resolve_sync(self, waiter: Any, result: dict[str, Any]) -> None:
        self.resolved.append((waiter, result))


class Router:
    def __init__(
        self,
        store: Store,
        paths: Paths,
        *,
        now: Callable[[], float],
        new_id: Callable[[], str],
        notifier: Notifier | None = None,
        config: RouterConfig | None = None,
    ) -> None:
        self.store = store
        self.paths = paths
        self._now = now
        self._new_id = new_id
        self.notifier: Notifier = notifier or RecordingNotifier()
        self.config = config or RouterConfig()
        # call_id -> (waiter, deadline) for synchronous calls awaiting a reply.
        self.pending_sync: dict[str, tuple[Any, float]] = {}
        # Last retention sweep on the injected clock; None means "never".
        self._last_prune: float | None = None

    @classmethod
    def create(
        cls,
        paths: Paths,
        *,
        now: Callable[[], float] = time.time,
        new_id: Callable[[], str] | None = None,
        notifier: Notifier | None = None,
        config: RouterConfig | None = None,
        retain_bodies: bool = False,
    ) -> Router:
        store = Store.open(paths, now=now, retain_bodies=retain_bodies)
        return cls(
            store,
            paths,
            now=now,
            new_id=new_id or (lambda: str(uuid.uuid4())),
            notifier=notifier,
            config=config,
        )

    def close(self) -> None:
        self.store.close()

    # --- authentication -----------------------------------------------------
    def check_hello(self, hello: Hello, expected_token: str) -> None:
        if hello.protocol_version != PROTOCOL_VERSION:
            raise RouterError(
                "version_mismatch",
                f"router speaks protocol {PROTOCOL_VERSION}, client sent {hello.protocol_version}",
            )
        if not secrets.compare_digest(hello.token, expected_token):
            raise RouterError("unauthorized", "invalid router token")

    # --- session registry ---------------------------------------------------
    def register_session(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = require_field(args, "session_id")
        family = require_field(args, "family")
        self.store.upsert_session(
            session_id,
            family,
            pid=args.get("pid"),
            cwd=args.get("cwd", ""),
            state=args.get("state", "starting"),
            vendor_session_id=args.get("vendor_session_id"),
            reachable=bool(args.get("reachable", False)),
            is_managed=bool(args.get("is_managed", True)),
        )
        self.store.record_event("session", "registered", to_id=session_id, gist=family)
        return {"registered": True, "session_id": session_id}

    def update_state(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = require_field(args, "session_id")
        if "state" in args:
            self._transition(session_id, str(args["state"]))
        if "reachable" in args:
            new_reachable = bool(args["reachable"])
            if not new_reachable:
                current = self.store.get_session(session_id)
                if current is not None and current.reachable:
                    # A real transition, e.g. a Codex App Server crash detected
                    # by the adapter — record it exactly like a socket-level
                    # disconnect so the transcript never shows a silent gap.
                    self.store.record_event("session", "disconnected", to_id=session_id)
            self.store.set_reachable(session_id, new_reachable)
        if "vendor_session_id" in args and args["vendor_session_id"]:
            self.store.set_vendor_session(session_id, args["vendor_session_id"])
        if "last_user_message" in args:
            self.store.set_last_user_message(session_id, args["last_user_message"])
        if args.get("pid") is not None:
            self.store.set_pid(session_id, int(args["pid"]))
        # Becoming idle may release a queued delivery.
        self.pump(session_id)
        return {"ok": True}

    def _transition(self, session_id: str, state: str) -> None:
        """Move a session to ``state``, rejecting transitions the registry's
        lifecycle graph does not allow.

        This is the daemon-side enforcement of :func:`registry.is_valid_transition`
        (previously advisory only). An unknown session has no prior state to
        move from, so it is validated against ``starting``. Rejections are
        recorded in the transcript like any other refusal.
        """
        from .registry import is_valid_transition

        current = self.store.get_session(session_id)
        current_state = current.state if current is not None else STATE_STARTING
        if not is_valid_transition(current_state, state):
            self.store.record_event(
                "session",
                "rejected",
                to_id=session_id,
                gist=f"bad transition {current_state} -> {state}",
            )
            raise RouterError(
                "bad_transition",
                f"session {session_id!r} cannot move from {current_state!r} to {state!r}",
            )
        self.store.set_state(session_id, state)

    def heartbeat(self, args: dict[str, Any]) -> dict[str, Any]:
        self.store.touch(require_field(args, "session_id"))
        return {"ok": True}

    def deregister(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = require_field(args, "session_id")
        self.store.mark_offline(session_id)
        self.store.record_event("session", "offline", to_id=session_id)
        return {"ok": True}

    # --- aliases (post-v1, optional) ---------------------------------------
    def resolve_target(self, to: str) -> str:
        """Map a ``to`` field through the user's contacts file.

        A live session id always wins over an alias spelled the same way, and an
        unknown name is returned unchanged so the ``unreachable`` guardrail --
        not this helper -- produces the error. Contacts are re-read per call so
        the daemon holds no alias state of its own.
        """
        from . import contacts

        to = str(to)
        if self.store.get_session(to) is not None:
            return to
        return contacts.resolve(self.paths, to)

    def describe_target(self, to_id: str) -> str:
        """``alias (id)`` when the user has an alias for ``to_id``, else the id."""
        from . import contacts

        return contacts.label(self.paths, to_id)

    def roster(self, args: dict[str, Any]) -> dict[str, Any]:
        from . import contacts

        include_unmanaged = bool(args.get("include_unmanaged", False))
        caller = args.get("caller")
        sessions = self.store.list_sessions(include_unmanaged=include_unmanaged)
        aliases = contacts.alias_index(self.paths)
        warnings: list[str] = []
        out = []
        for s in sessions:
            reachable = s.reachable and self.notifier.connected(s.id)
            if not s.is_managed:
                reachable = False
            out.append(
                {
                    "id": s.id,
                    "alias": aliases.get(s.id),
                    "family": s.family,
                    "state": s.state,
                    "reachable": reachable,
                    "cwd": s.cwd,
                    "last_user_message": s.last_user_message,
                    "last_active": s.last_active,
                    "is_self": s.id == caller,
                }
            )
            if not s.is_managed:
                warnings.append(
                    f"{s.id} is not Bridge-managed; restart it with `bridge {s.family}`"
                    " to make it callable"
                )
        return {"sessions": out, "warnings": warnings}

    def transcript(self, args: dict[str, Any]) -> dict[str, Any]:
        peer = args.get("peer")
        limit = int(args.get("limit", 20))
        entries = self.store.recent(peer=peer, limit=limit)
        return {
            "entries": [
                {
                    "ts": e.ts,
                    "from": e.from_id,
                    "to": e.to_id,
                    "kind": e.kind,
                    "status": e.status,
                    "gist": e.gist,
                    "call_id": e.call_id,
                }
                for e in entries
            ]
        }

    # --- delivery pump (extended by delivery/calls modules) -----------------
    def pump(self, target_id: str) -> None:
        """Attempt to deliver queued items to a now-ready target.

        The base router only delivers when the target is idle and reachable.
        Delivery/call semantics are layered on in :mod:`bridge.delivery` and
        :mod:`bridge.calls`, which import and extend this method's helpers.
        """
        from .delivery import pump_target

        pump_target(self, target_id)

    # --- lifecycle events from the socket layer -----------------------------
    def on_session_connected(self, session_id: str) -> None:
        self.store.set_reachable(session_id, True)
        # Re-queue anything left delivered-but-unacked from a prior connection.
        self.store.redeliver_inflight(session_id)
        self.pump(session_id)

    def on_session_disconnected(self, session_id: str) -> None:
        s = self.store.get_session(session_id)
        if s is not None:
            self.store.set_reachable(session_id, False)
            if s.state != STATE_OFFLINE:
                self.store.set_state(session_id, STATE_OFFLINE)
            self.store.record_event("session", "disconnected", to_id=session_id)

    # --- periodic maintenance ----------------------------------------------
    def tick(self) -> None:
        from .calls import expire_due

        expire_due(self)
        self.maybe_prune()

    def prune(self, older_than_s: float | None = None) -> dict[str, int]:
        """Run a retention sweep now and restart the interval timer."""
        if older_than_s is None:
            older_than_s = self.config.retention_s
        self._last_prune = self._now()
        return self.store.prune(older_than_s)

    def maybe_prune(self) -> dict[str, int] | None:
        """Sweep at most once per ``prune_interval_s``; ``None`` when skipped.

        The first tick always sweeps, so a router that was down for a month
        catches up as soon as it comes back rather than waiting an hour.
        """
        last = self._last_prune
        if last is not None and self._now() - last < self.config.prune_interval_s:
            return None
        return self.prune()

    def has_activity(self) -> bool:
        """True while any managed session is connected or any call is pending."""
        for s in self.store.list_sessions(include_unmanaged=False):
            if self.notifier.connected(s.id):
                return True
        if self.store.calls_awaiting_delivery():
            return True
        return bool(self.store.due_calls())

    # --- dispatch -----------------------------------------------------------
    def dispatch(self, op: str, args: dict[str, Any], *, waiter: Any = None) -> tuple[str, Any]:
        """Handle an op. Returns ('respond', result) or ('defer', None).

        A deferred op (synchronous ``call``) will be answered later via the
        notifier's ``resolve_sync`` with the recorded waiter.
        """
        handler = _DISPATCH.get(op)
        if handler is None:
            raise RouterError("unknown_op", f"unknown op {op!r}")
        return handler(self, args, waiter)

    def _idgen(self) -> str:
        return self._new_id()


def require_field(args: dict[str, Any], key: str) -> Any:
    """Return ``args[key]`` or raise the router's standard bad_request error.

    The single definition for every op handler, here and in the feature modules
    (:mod:`bridge.calls`, :mod:`bridge.delivery`), so "missing required field"
    means exactly one thing on the wire.
    """
    if key not in args or args[key] in (None, ""):
        raise RouterError("bad_request", f"missing required field {key!r}")
    return args[key]


# Dispatch table. Handlers return ('respond', result) or ('defer', None).
# Session/roster/transcript ops are defined here; call/text/reply ops are
# registered by their feature modules on import.
def _wrap_simple(fn: Callable[[Router, dict[str, Any]], dict[str, Any]]):
    def handler(router: Router, args: dict[str, Any], waiter: Any) -> tuple[str, Any]:
        return ("respond", fn(router, args))

    return handler


_DISPATCH: dict[str, Callable[[Router, dict[str, Any], Any], tuple[str, Any]]] = {
    "register_session": _wrap_simple(Router.register_session),
    "update_state": _wrap_simple(Router.update_state),
    "heartbeat": _wrap_simple(Router.heartbeat),
    "deregister": _wrap_simple(Router.deregister),
    "roster": _wrap_simple(Router.roster),
    "transcript": _wrap_simple(Router.transcript),
}


def register_op(
    name: str, handler: Callable[[Router, dict[str, Any], Any], tuple[str, Any]]
) -> None:
    _DISPATCH[name] = handler


def register_simple_op(name: str, fn: Callable[[Router, dict[str, Any]], dict[str, Any]]) -> None:
    _DISPATCH[name] = _wrap_simple(fn)


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------


@dataclass
class _Conn:
    sock: socket.socket
    buf: FrameBuffer = field(default_factory=FrameBuffer)
    outbound: bytearray = field(default_factory=bytearray)
    authed: bool = False
    session_id: str | None = None
    role: str = "client"
    on_frame: Callable[[str, str, dict[str, Any]], None] | None = None

    def queue(self, frame: dict[str, Any]) -> None:
        if self.on_frame is not None:
            self.on_frame("router", "out", frame)
        self.outbound.extend(encode_frame(frame))


class SocketNotifier:
    """Notifier backed by live connections in the event loop."""

    def __init__(self, server: RouterServer) -> None:
        self._server = server

    def connected(self, session_id: str) -> bool:
        return session_id in self._server.session_conns

    def deliver(self, session_id: str, event: dict[str, Any]) -> bool:
        conn = self._server.session_conns.get(session_id)
        if conn is None:
            return False
        conn.queue(event_frame(event))
        self._server.want_write(conn)
        return True

    def resolve_sync(self, waiter: Any, result: dict[str, Any]) -> None:
        conn, req_id = waiter
        if conn.sock.fileno() == -1:
            return
        conn.queue(ok_response(req_id, result))
        self._server.want_write(conn)


class RouterServer:
    def __init__(
        self,
        paths: Paths,
        *,
        now: Callable[[], float] = time.time,
        new_id: Callable[[], str] | None = None,
        config: RouterConfig | None = None,
        token: str | None = None,
        on_frame: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.paths = paths.ensure()
        self.config = config or RouterConfig()
        # Opt-in `bridge lab` wire capture; see :mod:`bridge.lab.capture`.
        if on_frame is None and os.environ.get("BRIDGE_LAB_CAPTURE"):
            from .lab.capture import hook_from_env

            on_frame = hook_from_env()
        self._on_frame = on_frame
        self.token = token or _read_or_create_token(paths)
        self.router = Router.create(
            paths, now=now, new_id=new_id, notifier=None, config=self.config
        )
        self.router.notifier = SocketNotifier(self)
        self._sel = selectors.DefaultSelector()
        self._listener: socket.socket | None = None
        self._conns: dict[int, _Conn] = {}
        self.session_conns: dict[str, _Conn] = {}
        self._running = False
        self._last_active_ts = now()
        self._now = now
        # Self-pipe so stop() wakes the selector immediately instead of waiting
        # out the poll timeout; a stopped router must never answer a late request.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)

    # --- setup --------------------------------------------------------------
    def _bind(self) -> None:
        sock_path = self.paths.socket
        if sock_path.exists():
            sock_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        listener.listen(64)
        listener.setblocking(False)
        self._listener = listener
        self._sel.register(listener, selectors.EVENT_READ, data=None)
        self._sel.register(self._wake_r, selectors.EVENT_READ, data=_WAKE)
        self.paths.pidfile.write_text(str(os.getpid()))

    def serve_forever(self) -> None:
        self._bind()
        self._running = True
        while self._running:
            events = self._sel.select(timeout=1.0)
            if not self._running:
                # stop() raced the poll: do not service (and answer) anything
                # that arrived alongside the wake-up. A stopped router is silent.
                break
            for key, mask in events:
                if key.data is _WAKE:
                    self._drain_wake()
                elif key.data is None:
                    self._accept()
                else:
                    self._service(key.data, mask)
            self.router.tick()
            self._maybe_idle_shutdown()

    def stop(self) -> None:
        self._running = False
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(64):
                pass
        except (BlockingIOError, OSError):
            pass

    def shutdown(self) -> None:
        for conn in list(self._conns.values()):
            self._close_conn(conn)
        for s in (self._wake_r, self._wake_w):
            try:
                self._sel.unregister(s)
            except (KeyError, ValueError):
                pass
            try:
                s.close()
            except OSError:
                pass
        if self._listener is not None:
            self._sel.unregister(self._listener)
            self._listener.close()
        for p in (self.paths.socket, self.paths.pidfile):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self.router.close()

    # --- event loop internals ----------------------------------------------
    def _accept(self) -> None:
        assert self._listener is not None
        sock, _ = self._listener.accept()
        sock.setblocking(False)
        conn = _Conn(sock=sock, on_frame=self._on_frame)
        self._conns[sock.fileno()] = conn
        self._sel.register(sock, selectors.EVENT_READ, data=conn)

    def want_write(self, conn: _Conn) -> None:
        if conn.sock.fileno() == -1:
            return
        self._sel.modify(conn.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, data=conn)

    def _service(self, conn: _Conn, mask: int) -> None:
        if mask & selectors.EVENT_READ:
            self._read(conn)
        if conn.sock.fileno() != -1 and (mask & selectors.EVENT_WRITE):
            self._flush(conn)

    def _read(self, conn: _Conn) -> None:
        try:
            data = conn.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._close_conn(conn)
            return
        if not data:
            self._close_conn(conn)
            return
        try:
            conn.buf.feed(data)
            for frame in conn.buf.frames():
                self._handle_frame(conn, frame)
        except ProtocolError:
            self._close_conn(conn)
            return
        if conn.outbound:
            self._flush(conn)

    def _flush(self, conn: _Conn) -> None:
        if not conn.outbound:
            self._sel.modify(conn.sock, selectors.EVENT_READ, data=conn)
            return
        try:
            sent = conn.sock.send(conn.outbound)
            del conn.outbound[:sent]
        except BlockingIOError:
            return
        except OSError:
            self._close_conn(conn)
            return
        if not conn.outbound:
            self._sel.modify(conn.sock, selectors.EVENT_READ, data=conn)

    def _handle_frame(self, conn: _Conn, frame: dict[str, Any]) -> None:
        if self._on_frame is not None:
            self._on_frame("router", "in", frame)
        if frame.get("t") != "req":
            return
        req_id = frame.get("id")
        op = frame.get("op")
        args = frame.get("args") or {}
        self._mark_active()

        if not conn.authed:
            if op != "hello":
                conn.queue(err_response(req_id, "unauthorized", "hello required first"))
                return
            self._do_hello(conn, req_id, args)
            return

        if op == "hello":
            conn.queue(ok_response(req_id, {"already": True}))
            return
        if op == "subscribe":
            self._do_subscribe(conn, req_id, args)
            return

        # Attribute the caller from the authenticated connection, never args.
        if conn.session_id and op in _CALLER_OPS:
            args = {**args, "caller": conn.session_id}

        try:
            waiter = (conn, req_id)
            outcome, result = self.router.dispatch(op, args, waiter=waiter)
        except RouterError as exc:
            conn.queue(err_response(req_id, exc.code, exc.message))
            return
        except Exception as exc:  # noqa: BLE001 - surface as protocol error, keep loop alive
            conn.queue(err_response(req_id, "internal", str(exc)))
            return
        if outcome == "respond":
            conn.queue(ok_response(req_id, result))
        # 'defer' -> resolved later via SocketNotifier.resolve_sync

    def _do_hello(self, conn: _Conn, req_id: Any, args: dict[str, Any]) -> None:
        hello = Hello.parse(args)
        try:
            self.router.check_hello(hello, self.token)
        except RouterError as exc:
            conn.queue(err_response(req_id, exc.code, exc.message))
            return
        conn.authed = True
        conn.session_id = hello.session_id
        conn.role = hello.role
        conn.queue(ok_response(req_id, {"protocol_version": PROTOCOL_VERSION}))

    def _do_subscribe(self, conn: _Conn, req_id: Any, args: dict[str, Any]) -> None:
        session_id = conn.session_id or args.get("session_id")
        if not session_id:
            conn.queue(err_response(req_id, "bad_request", "subscribe requires a session id"))
            return
        conn.session_id = session_id
        conn.role = "adapter"
        self.session_conns[session_id] = conn
        self.router.on_session_connected(session_id)
        conn.queue(ok_response(req_id, {"subscribed": True, "session_id": session_id}))

    def _close_conn(self, conn: _Conn) -> None:
        fileno = conn.sock.fileno()
        if fileno != -1:
            try:
                self._sel.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
        self._conns.pop(fileno, None)
        if conn.session_id and self.session_conns.get(conn.session_id) is conn:
            del self.session_conns[conn.session_id]
            self.router.on_session_disconnected(conn.session_id)
        try:
            conn.sock.close()
        except OSError:
            pass

    def _mark_active(self) -> None:
        self._last_active_ts = self._now()

    def _maybe_idle_shutdown(self) -> None:
        if self.router.has_activity():
            self._mark_active()
            return
        if self._now() - self._last_active_ts >= self.config.idle_grace_s:
            self._running = False


# ---------------------------------------------------------------------------
# Token helpers and CLI entry
# ---------------------------------------------------------------------------


def _read_or_create_token(paths: Paths) -> str:
    paths.ensure()
    if paths.token.exists():
        return paths.token.read_text().strip()
    token = secrets.token_urlsafe(32)
    paths.token.write_text(token)
    paths.token.chmod(0o600)
    return token


def read_token(paths: Paths) -> str:
    return _read_or_create_token(paths)


def is_running(paths: Paths) -> bool:
    return paths.socket.exists() and _pid_alive(paths)


def _pid_alive(paths: Paths) -> bool:
    if not paths.pidfile.exists():
        return False
    try:
        pid = int(paths.pidfile.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ensure_running(paths: Paths) -> None:
    """Lazily start the router daemon if it is not already serving."""
    if is_running(paths):
        return
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child path exercised via integration
        os.setsid()
        try:
            server = RouterServer(paths)
            _install_signal_handlers(server)
            server.serve_forever()
        finally:
            try:
                server.shutdown()
            except Exception:
                pass
        os._exit(0)
    _await_socket(paths)


def _await_socket(paths: Paths, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if paths.socket.exists():
            return
        time.sleep(0.02)
    raise TimeoutError("router socket did not appear")


def _install_signal_handlers(server: RouterServer) -> None:  # pragma: no cover
    def _stop(_signum, _frame):
        server.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def cli_router(action: str) -> int:  # pragma: no cover - thin CLI shim
    paths = Paths.resolve()
    if action == "run":
        server = RouterServer(paths)
        _install_signal_handlers(server)
        try:
            server.serve_forever()
        finally:
            server.shutdown()
        return 0
    if action == "status":
        print("running" if is_running(paths) else "stopped")
        return 0
    if action == "stop":
        if paths.pidfile.exists():
            try:
                os.kill(int(paths.pidfile.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        return 0
    return 2


# Ops for which the router substitutes the authenticated caller id.
_CALLER_OPS = {"roster", "call", "call_async", "text", "reply", "transcript", "ack"}


# Import feature modules for their side effect of registering ops. Deferred to
# the bottom so RouterError / register_op / Router are already defined.
from . import calls as _calls  # noqa: E402,F401
from . import delivery as _delivery  # noqa: E402,F401
from . import retention as _retention  # noqa: E402,F401
