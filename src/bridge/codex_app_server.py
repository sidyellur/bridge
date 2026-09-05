"""Codex App Server client and process management.

Bridge runs one App Server per wrapped Codex session and connects to it as a
client: it performs the initialize handshake, checks the protocol version,
binds to the exact thread the remote TUI displays (from ``thread/started``),
subscribes to runtime/turn events, and starts turns while the thread is idle.
V1 never issues ``turn/steer``.

The pinned contract lives in ``tests/fixtures/codex_protocol/v1.json``; the
constants here must match it (a test asserts they do).
"""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .mcp import RpcEndpoint

PROTOCOL_VERSION = "codex-app-server/1"
SUPPORTED_VERSIONS = ("codex-app-server/1",)
FORBIDDEN_METHODS = ("turn/steer",)

STATUS_IDLE = "idle"
STATUS_WORKING = "working"

# Pinned launch command for the App Server Bridge spawns per wrapped Codex
# session. Mirrored verbatim as "launch_argv" in
# tests/fixtures/codex_protocol/v1.json so the two cannot drift; a test
# asserts they match.
LAUNCH_ARGV: tuple[str, ...] = ("{binary}", "app-server", "--listen", "unix://{socket_path}")


def build_launch_argv(binary: str, socket_path: object) -> list[str]:
    """Fill :data:`LAUNCH_ARGV` for a concrete binary and socket path."""
    return [tok.format(binary=binary, socket_path=socket_path) for tok in LAUNCH_ARGV]


class UnsupportedCodexVersion(Exception):
    pass


class CodexAppServerStartError(Exception):
    """The App Server process could not be started or never bound its socket."""


class CodexAppServerClient:
    def __init__(
        self,
        sock: socket.socket,
        *,
        on_thread_bound: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_turn_completed: Callable[[str, str], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._on_disconnect = on_disconnect
        self.rpc = RpcEndpoint(sock, name="codex-app-server", on_close=self._on_rpc_closed)
        self.thread_id: str | None = None
        self.status: str = STATUS_IDLE
        self.server_info: dict[str, Any] = {}
        self._on_thread_bound = on_thread_bound
        self._on_status = on_status
        self._on_turn_completed = on_turn_completed
        self._delta: dict[str, str] = {}
        self._final: dict[str, str] = {}
        self._register()

    def _on_rpc_closed(self) -> None:
        if self._on_disconnect is not None:
            self._on_disconnect()

    def _register(self) -> None:
        self.rpc.notification("thread/started", self._on_thread)
        self.rpc.notification("thread/resume", self._on_thread)
        self.rpc.notification("runtime/status", self._on_runtime)
        self.rpc.notification("turn/started", self._on_turn_started)
        self.rpc.notification("item/agent_message_delta", self._on_agent_delta)
        self.rpc.notification("item/agent_message", self._on_agent_message)
        self.rpc.notification("turn/completed", self._on_completed)

    def start(self) -> CodexAppServerClient:
        self.rpc.start()
        return self

    # --- handshake ----------------------------------------------------------
    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.rpc.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientInfo": client_info or {"name": "bridge", "version": "0.1.0"},
            },
        )
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise UnsupportedCodexVersion(
                f"Codex App Server speaks {version!r}; Bridge supports {SUPPORTED_VERSIONS}"
            )
        self.server_info = result.get("serverInfo", {})
        self.rpc.notify("initialized", {})
        return result

    def start_turn(self, text: str) -> str:
        if self.thread_id is None:
            raise RuntimeError("no bound Codex thread yet")
        result = self.rpc.request(
            "turn/start",
            {"thread_id": self.thread_id, "input": [{"type": "text", "text": text}]},
        )
        return result.get("turn_id", "")

    # --- notification handlers ---------------------------------------------
    def _on_thread(self, params: dict[str, Any]) -> None:
        self.thread_id = params.get("thread_id")
        if self._on_thread_bound and self.thread_id:
            self._on_thread_bound(self.thread_id)

    def _on_runtime(self, params: dict[str, Any]) -> None:
        self.status = params.get("status", STATUS_IDLE)
        if self._on_status:
            self._on_status(self.status)

    def _on_turn_started(self, params: dict[str, Any]) -> None:
        self.status = STATUS_WORKING
        if self._on_status:
            self._on_status(self.status)

    def _on_agent_delta(self, params: dict[str, Any]) -> None:
        turn_id = params.get("turn_id", "")
        self._delta[turn_id] = self._delta.get(turn_id, "") + str(params.get("delta", ""))

    def _on_agent_message(self, params: dict[str, Any]) -> None:
        self._final[params.get("turn_id", "")] = str(params.get("text", ""))

    def _on_completed(self, params: dict[str, Any]) -> None:
        turn_id = params.get("turn_id", "")
        final = self._final.pop(turn_id, None)
        if final is None:
            final = self._delta.pop(turn_id, "")
        else:
            self._delta.pop(turn_id, None)
        self.status = STATUS_IDLE
        if self._on_status:
            self._on_status(STATUS_IDLE)
        if self._on_turn_completed:
            self._on_turn_completed(turn_id, final)

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
