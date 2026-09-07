"""Codex App Server client and process management.

Bridge runs one ``codex app-server`` per wrapped Codex session and connects to
it over **WebSocket**: RFC 6455 text frames on the ``unix://`` socket, exactly
one JSON-RPC message per frame, no ``jsonrpc`` field on anything the server
sends. There is no protocol version to negotiate anywhere in the protocol; the
``initialize`` result carries a ``userAgent`` whose embedded codex version is
the only version gate (:func:`parse_codex_version`).

Bridge never creates the thread it drives. It binds to the thread the remote
TUI already owns — from the global ``thread/started`` broadcast, or by walking
``thread/loaded/list`` + ``thread/read`` and preferring a ``cwd`` match — and
then tries to subscribe with ``thread/resume``, the only subscribe verb the
protocol has. In 0.151.0 that call fails with ``-32601 "list_turns is not
supported yet"`` for exactly the paginated threads a live TUI owns, so a failed
subscribe is tolerated rather than fatal: the global ``thread/started`` and
``thread/status/changed`` broadcasts still arrive (busy/idle keeps working) and
only the per-thread ``turn/*``/``item/*`` stream is lost.

The pinned contract lives in
``tests/fixtures/codex_protocol/codex-0.151.0.json``. The constants below are
the single source of truth for every wire name: the fake server in
``tests/fakes/codex_app_server.py`` imports them, and a test asserts the
constants and the fixture agree in both directions.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .mcp import INVALID_REQUEST, METHOD_NOT_FOUND, Framing, JsonRpcError, RpcEndpoint

M_INITIALIZE = "initialize"
M_THREAD_LIST = "thread/loaded/list"
M_THREAD_READ = "thread/read"
M_THREAD_RESUME = "thread/resume"
M_THREAD_UNSUBSCRIBE = "thread/unsubscribe"
M_TURN_START = "turn/start"

N_INITIALIZED = "initialized"

N_THREAD_STARTED = "thread/started"
N_THREAD_STATUS = "thread/status/changed"
N_TURN_STARTED = "turn/started"
N_TURN_COMPLETED = "turn/completed"
N_ITEM_STARTED = "item/started"
N_ITEM_DELTA = "item/agentMessage/delta"
N_ITEM_COMPLETED = "item/completed"

CLIENT_REQUESTS = (
    M_INITIALIZE,
    M_THREAD_LIST,
    M_THREAD_READ,
    M_THREAD_RESUME,
    M_THREAD_UNSUBSCRIBE,
    M_TURN_START,
)
CLIENT_NOTIFICATIONS = (N_INITIALIZED,)
SERVER_NOTIFICATIONS = (
    N_THREAD_STARTED,
    N_THREAD_STATUS,
    N_TURN_STARTED,
    N_TURN_COMPLETED,
    N_ITEM_STARTED,
    N_ITEM_DELTA,
    N_ITEM_COMPLETED,
)

FORBIDDEN_METHODS = ("turn/steer", "turn/interrupt", "review/start")

#: Server notifications Bridge asks the App Server not to send it at all.
OPT_OUT_NOTIFICATION_METHODS = (
    "remoteControl/status/changed",
    "fs/changed",
    "account/rateLimits/updated",
    "thread/tokenUsage/updated",
)

MIN_CODEX_VERSION = (0, 151, 0)
PINNED_CODEX_VERSION = "0.151.0"

ITEM_AGENT_MESSAGE = "agentMessage"

THREAD_STATUS_NOT_LOADED = "notLoaded"
THREAD_STATUS_IDLE = "idle"
THREAD_STATUS_ACTIVE = "active"
THREAD_STATUS_SYSTEM_ERROR = "systemError"

STATUS_IDLE = "idle"
STATUS_WORKING = "working"

THREAD_STATUS_TO_STATE = {
    THREAD_STATUS_IDLE: STATUS_IDLE,
    THREAD_STATUS_ACTIVE: STATUS_WORKING,
    THREAD_STATUS_NOT_LOADED: STATUS_IDLE,
    THREAD_STATUS_SYSTEM_ERROR: STATUS_IDLE,
}

SYSTEM_ERROR_MESSAGE = "thread reported systemError"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_codex_version(user_agent: str) -> tuple[int, int, int] | None:
    """The codex version embedded in an ``initialize`` ``userAgent``.

    ``"bridge/0.151.0 (Mac OS 15.5.0; arm64) iTerm.app/3.6.11 (bridge; 0.1.0)"``
    → ``(0, 151, 0)``; ``None`` when that token is not a version triple.

    Only the first whitespace-delimited token after the first ``/`` is read, and
    the triple must start it: a dev build (``"codex/dev (Mac OS 15.5.0; …)"``)
    must not pick up the OS version further along the string.
    """
    _, sep, rest = user_agent.partition("/")
    if not sep:
        return None
    head = rest.split(maxsplit=1)
    if not head:
        return None
    match = _VERSION_RE.match(head[0])
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))

# Pinned launch command for the App Server Bridge spawns per wrapped Codex
# session. Mirrored verbatim as "launch_argv" in
# tests/fixtures/codex_protocol/codex-0.151.0.json so the two cannot drift; a
# test asserts they match.
LAUNCH_ARGV: tuple[str, ...] = ("{binary}", "app-server", "--listen", "unix://{socket_path}")


def build_launch_argv(binary: str, socket_path: object) -> list[str]:
    """Fill :data:`LAUNCH_ARGV` for a concrete binary and socket path."""
    return [tok.format(binary=binary, socket_path=socket_path) for tok in LAUNCH_ARGV]


class UnsupportedCodexVersion(Exception):
    pass


class CodexThreadBusy(Exception):
    """``turn/start`` was refused because Bridge sees the thread as working."""


class CodexAppServerStartError(Exception):
    """The App Server process could not be started or never bound its socket."""


class CodexAppServerClient:
    """A Bridge client for one ``codex app-server`` connection.

    Notification handlers run on :class:`RpcEndpoint`'s reader thread, which is
    also the thread that reads responses — so no handler here may ever issue a
    request, and none may block on state a caller sets after its own request
    returns. ``bind_thread``/``subscribe``/``start_turn`` are the request-issuing
    entry points and belong to the main thread or the adapter's worker.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        cwd: str | None = None,
        client_version: str = "0.1.0",
        on_thread_bound: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_agent_message: Callable[[str, str], None] | None = None,
        on_turn_completed: Callable[[str, str], None] | None = None,
        on_thread_error: Callable[[str], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.cwd = cwd
        self._client_version = client_version
        self._on_disconnect = on_disconnect
        self.rpc = RpcEndpoint(
            sock,
            name="codex-app-server",
            framing=Framing.WS_CLIENT,
            on_close=self._on_rpc_closed,
        )
        self.thread_id: str | None = None
        self.status: str = STATUS_IDLE
        self.subscribed = False
        self.codex_version = ""
        self.codex_version_warning: str | None = None
        self.last_thread_error: str | None = None
        self.own_thread_ids: set[str] = set()
        self.server_info: dict[str, Any] = {}
        self._on_thread_bound = on_thread_bound
        self._on_status = on_status
        self._on_agent_message = on_agent_message
        self._on_turn_completed = on_turn_completed
        self._on_thread_error = on_thread_error
        self._delta: dict[str, str] = {}
        self._items: dict[str, str] = {}
        self._register()

    def _on_rpc_closed(self) -> None:
        if self._on_disconnect is not None:
            self._on_disconnect()

    def _register(self) -> None:
        self.rpc.notification(N_THREAD_STARTED, self._on_thread_started)
        self.rpc.notification(N_THREAD_STATUS, self._on_thread_status_changed)
        self.rpc.notification(N_TURN_STARTED, self._on_turn_started)
        self.rpc.notification(N_ITEM_DELTA, self._on_item_delta)
        self.rpc.notification(N_ITEM_COMPLETED, self._on_item_completed)
        self.rpc.notification(N_TURN_COMPLETED, self._on_turn_completed_notification)

    def start(self) -> CodexAppServerClient:
        self.rpc.start()
        return self

    # --- handshake ----------------------------------------------------------
    def initialize(self) -> dict[str, Any]:
        result = self.rpc.request(
            M_INITIALIZE,
            {
                "clientInfo": {
                    "name": "bridge",
                    "title": "Bridge",
                    "version": self._client_version,
                },
                "capabilities": {
                    "optOutNotificationMethods": list(OPT_OUT_NOTIFICATION_METHODS)
                },
            },
        )
        self.server_info = dict(result or {})
        user_agent = str(self.server_info.get("userAgent", ""))
        version = parse_codex_version(user_agent)
        if version is None:
            raise UnsupportedCodexVersion(
                f"could not parse a codex version from userAgent {user_agent!r}"
            )
        self.codex_version = ".".join(str(part) for part in version)
        if version < MIN_CODEX_VERSION:
            raise UnsupportedCodexVersion(
                f"codex {self.codex_version} predates the pinned App Server contract "
                f"(Bridge needs >= {PINNED_CODEX_VERSION})"
            )
        if version > MIN_CODEX_VERSION:
            # A patch bump must not take a working install down: the wire shapes
            # are stable across them, and a `brew upgrade` is not a protocol
            # break.
            self.codex_version_warning = (
                f"codex {self.codex_version} is newer than the pinned contract "
                f"{PINNED_CODEX_VERSION}; wire shapes are assumed stable"
            )
        self.rpc.notify(N_INITIALIZED, None, omit_empty_params=True)
        return result

    # --- thread binding -----------------------------------------------------
    def bind_thread(self) -> str | None:
        """Find and bind the thread the remote TUI owns. Issues requests, so it
        belongs to the main thread or the adapter's worker — never a handler."""
        data = self.rpc.request(M_THREAD_LIST, {}).get("data") or []
        candidates: list[dict[str, Any]] = []
        for thread_id in data:
            try:
                result = self.rpc.request(
                    M_THREAD_READ, {"threadId": thread_id, "includeTurns": False}
                )
            except JsonRpcError:
                continue
            thread = result.get("thread")
            if isinstance(thread, Mapping) and thread.get("id") not in self.own_thread_ids:
                candidates.append(dict(thread))
        if not candidates:
            return self.thread_id
        same_cwd = [t for t in candidates if t.get("cwd") == self.cwd]
        pool = same_cwd or candidates
        self._bind(max(pool, key=lambda t: int(t.get("createdAt") or 0)))
        return self.thread_id

    def _bind(self, thread: Mapping[str, Any]) -> None:
        thread_id = thread.get("id")
        if not thread_id:
            return
        self.thread_id = str(thread_id)
        self._apply_thread_status(thread.get("status"))
        if self._on_thread_bound is not None:
            self._on_thread_bound(self.thread_id)

    def subscribe(self) -> bool:
        """Subscribe to the bound thread's ``turn/*``/``item/*`` stream.

        0.151.0 answers this with ``-32601 "list_turns is not supported yet"``
        for the live TUI thread, which costs Bridge that stream but not
        busy/idle — ``thread/status/changed`` is a global broadcast (see the
        2026-09-07 two-client spike). A refusal is therefore recorded and
        reported, never raised.
        """
        if not self.thread_id:
            return False
        try:
            self.rpc.request(
                M_THREAD_RESUME, {"threadId": self.thread_id, "excludeTurns": True}
            )
        except JsonRpcError as exc:
            if exc.code in (METHOD_NOT_FOUND, INVALID_REQUEST):
                self.subscribed = False
                self.last_thread_error = exc.message
                return False
            raise
        self.subscribed = True
        return True

    def start_turn(self, text: str) -> str:
        if self.thread_id is None:
            raise RuntimeError("no bound Codex thread yet")
        if self.status == STATUS_WORKING:
            # Bridge gates on its own observed idle rather than trusting the
            # server to queue: `thread/queue/changed` exists but was never probed.
            raise CodexThreadBusy(
                f"thread {self.thread_id} is working; Bridge never queues a second turn"
            )
        result = self.rpc.request(
            M_TURN_START,
            {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]},
        )
        return str((result.get("turn") or {}).get("id") or "")

    def final_message(self, turn_id: str) -> str | None:
        """The completed agent message for ``turn_id``, deltas as the fallback."""
        text = self._items.get(turn_id)
        if text is None:
            text = self._delta.get(turn_id)
        return text or None

    # --- notification handlers (reader thread; never issue a request) -------
    def _on_thread_started(self, params: dict[str, Any]) -> None:
        thread = params.get("thread")
        if not isinstance(thread, Mapping):
            return
        thread_id = thread.get("id")
        if not thread_id or thread_id in self.own_thread_ids:
            return
        # Last wins in our own cwd: the operator restarting the TUI there is
        # rebinding, not opening a second session Bridge should ignore.
        if self.thread_id is None or thread.get("cwd") == self.cwd:
            self.subscribed = False
            self._bind(thread)

    def _on_thread_status_changed(self, params: dict[str, Any]) -> None:
        if params.get("threadId") != self.thread_id:
            return
        self._apply_thread_status(params.get("status"))

    def _apply_thread_status(self, status: Any) -> None:
        if not isinstance(status, Mapping):
            return
        status_type = status.get("type")
        if status_type not in THREAD_STATUS_TO_STATE:
            return
        if status_type == THREAD_STATUS_SYSTEM_ERROR:
            self.last_thread_error = SYSTEM_ERROR_MESSAGE
            if self._on_thread_error is not None:
                self._on_thread_error(SYSTEM_ERROR_MESSAGE)
        self._set_status(THREAD_STATUS_TO_STATE[status_type])

    def _set_status(self, state: str) -> None:
        if state == self.status:
            return
        self.status = state
        if self._on_status is not None:
            self._on_status(state)

    def _on_turn_started(self, params: dict[str, Any]) -> None:
        self._set_status(STATUS_WORKING)

    def _on_item_delta(self, params: dict[str, Any]) -> None:
        turn_id = str(params.get("turnId") or "")
        self._delta[turn_id] = self._delta.get(turn_id, "") + str(params.get("delta", ""))

    def _on_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        if item.get("type") != ITEM_AGENT_MESSAGE:
            return
        turn_id = str(params.get("turnId") or "")
        text = str(item.get("text") or "")
        self._items[turn_id] = text
        if self._on_agent_message is not None:
            self._on_agent_message(turn_id, text)

    def _on_turn_completed_notification(self, params: dict[str, Any]) -> None:
        # Not named `_on_turn_completed`: that attribute is the caller-supplied
        # callback slot, which the adapter rebinds.
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        status = str(turn.get("status") or "")
        delta = self._delta.pop(turn_id, "")
        if delta and turn_id not in self._items:
            self._items[turn_id] = delta
        self._set_status(STATUS_IDLE)
        if self._on_turn_completed is not None:
            self._on_turn_completed(turn_id, status)

    def close(self) -> None:
        self.rpc.close()


class CodexAppServerProcess:
    """Spawns, waits on, and reaps a ``codex app-server`` bound to a Unix socket.

    Command construction is pinned by :data:`LAUNCH_ARGV`; ``start``/
    ``wait_for_socket``/``stop`` take injectable ``spawn``/``sleep``/``now`` so
    they are unit-testable against a fake ``Popen``-like object and never touch
    a real ``codex`` binary in tests.
    """

    def __init__(self, socket_path, *, binary: str = "codex") -> None:
        self.socket_path = socket_path
        self.binary = binary
        self.proc: subprocess.Popen | None = None

    def start(
        self,
        env: dict[str, str] | None = None,
        *,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> subprocess.Popen:
        argv = build_launch_argv(self.binary, self.socket_path)
        self.proc = spawn(argv, env=env)
        return self.proc

    def wait_for_socket(
        self,
        timeout: float = 10.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        """Block until ``socket_path`` exists, bounded by ``timeout``.

        Raises :class:`CodexAppServerStartError` immediately if the process
        already exited (never waiting out the full timeout for a dead child),
        and after ``timeout`` seconds if the socket never appears.
        """
        path = Path(self.socket_path)
        deadline = now() + timeout
        while True:
            if path.exists():
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise CodexAppServerStartError(
                    f"codex app-server exited with code {self.proc.returncode} "
                    f"before binding {path}"
                )
            if now() >= deadline:
                raise CodexAppServerStartError(
                    f"codex app-server did not bind {path} within {timeout}s"
                )
            sleep(0.02)

    def stop(self, *, timeout: float = 5.0) -> int | None:
        """Terminate, escalate to kill on timeout, and reap. Idempotent."""
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
        else:
            self.proc.wait()
        return self.proc.returncode


def connect_app_server_socket(socket_path, *, timeout: float = 5.0) -> socket.socket:
    """Connect a client socket to a just-started App Server, with a short
    bounded retry for the narrow race between the socket file appearing and
    the server being ready to ``accept``."""
    deadline = time.time() + timeout
    last_err: OSError | None = None
    while time.time() < deadline:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(socket_path))
            return sock
        except OSError as exc:
            last_err = exc
            sock.close()
            time.sleep(0.02)
    raise CodexAppServerStartError(f"could not connect to codex app-server socket: {last_err}")
