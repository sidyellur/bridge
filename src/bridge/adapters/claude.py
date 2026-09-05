"""Entry point that runs the Claude Channel adapter over real stdio.

Launched by Claude as an MCP subprocess; it inherits ``BRIDGE_SESSION_ID`` and
the router address from the ``bridge claude`` wrapper.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

from ..claude_channel import ClaudeChannelAdapter
from ..paths import SESSION_ID_ENV, Paths


def _stdio_socketpair() -> socket.socket:  # pragma: no cover - real stdio wiring
    """Bridge process stdio to a socket the RpcEndpoint can use.

    A background pump copies stdin -> socket and socket -> stdout so the endpoint
    can use recv/sendall uniformly.
    """
    parent, child = socket.socketpair()

    def pump_stdin() -> None:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            parent.sendall(data)

    def pump_stdout() -> None:
        while True:
            data = parent.recv(65536)
            if not data:
                break
            os.write(1, data)

    threading.Thread(target=pump_stdin, daemon=True).start()
    threading.Thread(target=pump_stdout, daemon=True).start()
    return child


def run(paths: Paths | None = None) -> int:  # pragma: no cover - integration entry
    paths = paths or Paths.resolve()
    session_id = os.environ.get(SESSION_ID_ENV)
    if not session_id:
        print("BRIDGE_SESSION_ID is not set; launch via `bridge claude`", file=sys.stderr)
        return 2

    from ..router_client import RouterClient

    host_sock = _stdio_socketpair()
    adapter = ClaudeChannelAdapter(session_id, host_sock, paths=paths)

    def connect(on_event):
        return RouterClient.connect(paths, session_id=session_id, role="adapter", on_event=on_event)

    adapter.connect_router(connect)
    adapter.start()
    # Block on the reader thread until stdio closes.
    adapter.rpc._thread.join()
    return 0
