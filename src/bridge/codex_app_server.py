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
from collections.abc import Callable
from typing import Any

from .mcp import RpcEndpoint

PROTOCOL_VERSION = "codex-app-server/1"
SUPPORTED_VERSIONS = ("codex-app-server/1",)
FORBIDDEN_METHODS = ("turn/steer",)

STATUS_IDLE = "idle"
STATUS_WORKING = "working"


class UnsupportedCodexVersion(Exception):
    pass


class CodexAppServerClient:
    def __init__(
        self,
        sock: socket.socket,
        *,
        on_thread_bound: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_turn_completed: Callable[[str, str], None] | None = None,
    ) -> None:
        self.rpc = RpcEndpoint(sock, name="codex-app-server")
        self.thread_id: str | None = None
        self.status: str = STATUS_IDLE
        self.server_info: dict[str, Any] = {}
        self._on_thread_bound = on_thread_bound
        self._on_status = on_status
        self._on_turn_completed = on_turn_completed
        self._delta: dict[str, str] = {}
        self._final: dict[str, str] = {}
        self._register()

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


class CodexAppServerProcess:  # pragma: no cover - real subprocess wiring
    """Spawns and reaps a real ``codex app-server`` bound to a Unix socket."""

    def __init__(self, socket_path, *, binary: str = "codex") -> None:
        self.socket_path = socket_path
        self.binary = binary
        self.proc = None

    def start(self, env: dict[str, str] | None = None):
        import subprocess

        self.proc = subprocess.Popen(
            [self.binary, "app-server", "--listen", f"unix://{self.socket_path}"], env=env
        )
        return self.proc

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
