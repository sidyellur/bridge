"""Bridge MCP tool server (stdio).

Codex launches this as its normal MCP server; it exposes the six coordination
tools plus the protocol-facing ``reply`` and identifies the caller solely from
the inherited ``BRIDGE_SESSION_ID`` (never from tool arguments). Claude does not
run this — it gets the identical handlers from its Channel adapter — so no two
servers register duplicate tool names for the same session.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

from . import __version__
from .mcp import INVALID_PARAMS, JsonRpcError, RpcEndpoint
from .paths import SESSION_ID_ENV, Paths
from .tools import ANTI_RETRIEVAL, all_tools, dispatch_tool

MCP_PROTOCOL_VERSION = "2024-11-05"

INSTRUCTIONS = (
    "Bridge coordinates live agent sessions. Tools: roster, call, call_async, "
    "text, transcript, and reply (only while handling an inbound call). " + ANTI_RETRIEVAL
)


class BridgeMCPServer:
    def __init__(self, session_id: str, host_sock: socket.socket, router: Any) -> None:
        self.session_id = session_id
        self.router = router
        self.rpc = RpcEndpoint(host_sock, name="bridge-mcp")
        self._register()

    def _register(self) -> None:
        self.rpc.method("initialize", self._initialize)
        self.rpc.notification("notifications/initialized", lambda _p: None)
        self.rpc.method("tools/list", self._tools_list)
        self.rpc.method("tools/call", self._tools_call)
        self.rpc.method("ping", lambda _p: {})

    def start(self) -> BridgeMCPServer:
        self.rpc.start()
        return self

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": params.get("protocolVersion", MCP_PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bridge", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": all_tools()}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires a name")
        from .router_client import RouterClientError

        try:
            result = dispatch_tool(self.router, name, arguments)
        except KeyError:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool {name!r}") from None
        except RouterClientError as exc:
            payload = {"error": exc.code, "message": exc.message}
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "structuredContent": result,
        }

    def close(self) -> None:
        self.rpc.close()
        try:
            self.router.close()
        except Exception:  # noqa: BLE001
            pass


def _stdio_socket() -> socket.socket:  # pragma: no cover - real stdio wiring
    import threading

    parent, child = socket.socketpair()

    def pump_in() -> None:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            parent.sendall(data)

    def pump_out() -> None:
        while True:
            data = parent.recv(65536)
            if not data:
                break
            os.write(1, data)

    threading.Thread(target=pump_in, daemon=True).start()
    threading.Thread(target=pump_out, daemon=True).start()
    return child


def cli_serve(*, family: str = "codex", paths: Paths | None = None) -> int:  # pragma: no cover
    paths = paths or Paths.resolve()
    if family == "claude":
        from .adapters.claude import run as run_claude

        return run_claude(paths)

    session_id = os.environ.get(SESSION_ID_ENV)
    if not session_id:
        print("BRIDGE_SESSION_ID is not set; launch via `bridge codex`")
        return 2

    from .router import ensure_running
    from .router_client import RouterClient

    ensure_running(paths)
    router = RouterClient.connect(paths, session_id=session_id, role="client")
    server = BridgeMCPServer(session_id, _stdio_socket(), router).start()
    server.rpc._thread.join()
    return 0
