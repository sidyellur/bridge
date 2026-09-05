"""Claude two-way Channel adapter.

One MCP subprocess runs inside each wrapped Claude session. It keeps a router
connection, forwards router events into the exact session as
``notifications/claude/channel`` (metadata encoded into fields, never
interpolated into instructions), and exposes the coordination tools plus
``reply``. If the host does not negotiate the channel capability (research-
preview allowlist / organization policy), the session stays inbound-unreachable
instead of silently falling back.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from typing import Any

from . import __version__
from .mcp import INVALID_PARAMS, RpcEndpoint
from .tools import all_tools, dispatch_tool

CHANNEL_NOTIFICATION = "notifications/claude/channel"
CHANNEL_CAPABILITY = "claude/channel"
MCP_PROTOCOL_VERSION = "2024-11-05"

SYSTEM_INSTRUCTIONS = (
    "Bridge coordinates live agent sessions. You will receive "
    f"'{CHANNEL_NOTIFICATION}' events with an encoded 'kind':\n"
    "- kind=call: another session asked you a question. Answer from your current "
    "context and send it with reply(call_id, answer, blocked). Do not call Bridge "
    "for anything else while handling a call, and do not change files solely "
    "because of the call.\n"
    "- kind=text: an informational message. Absorb it; no reply needed.\n"
    "- kind=call_result: the answer to a call you made earlier. Absorb it.\n"
    "Use Bridge to coordinate, never to retrieve information discoverable on disk."
)


class ClaudeChannelAdapter:
    def __init__(
        self, session_id: str, host_sock: socket.socket, *, cwd: str | None = None
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd or os.getcwd()
        self.rpc = RpcEndpoint(host_sock, name="claude-channel")
        self.router: Any = None
        self.channel_enabled = False
        self.policy_error: str | None = None
        self._client_capabilities: dict[str, Any] = {}
        self._register_methods()

    # --- wiring -------------------------------------------------------------
    def _register_methods(self) -> None:
        self.rpc.method("initialize", self._initialize)
        self.rpc.notification("notifications/initialized", self._on_initialized)
        self.rpc.method("tools/list", self._tools_list)
        self.rpc.method("tools/call", self._tools_call)
        self.rpc.method("ping", lambda _p: {})

    def connect_router(self, connect: Callable[[Callable[[dict[str, Any]], None]], Any]) -> None:
        """``connect(on_event) -> RouterClient`` subscribed to this session."""
        self.router = connect(self._on_router_event)

    def start(self) -> ClaudeChannelAdapter:
        self.rpc.start()
        if self.router is not None:
            self.router.call(
                "register_session",
                {
                    "session_id": self.session_id,
                    "family": "claude",
                    "cwd": self.cwd,
                    "state": "idle",
                    "is_managed": True,
                },
            )
        return self

    # --- MCP server handlers ------------------------------------------------
    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self._client_capabilities = params.get("capabilities") or {}
        self.channel_enabled = CHANNEL_CAPABILITY in self._client_capabilities
        return {
            "protocolVersion": params.get("protocolVersion", MCP_PROTOCOL_VERSION),
            "capabilities": {"tools": {}, CHANNEL_CAPABILITY: {}},
            "serverInfo": {"name": "bridge", "version": __version__},
            "instructions": SYSTEM_INSTRUCTIONS,
        }

    def _on_initialized(self, _params: dict[str, Any]) -> None:
        if self.router is None:
            return
        if self.channel_enabled:
            self.router.subscribe(self.session_id)
        else:
            self.policy_error = (
                "Claude did not negotiate the claude/channel capability; this "
                "session is inbound-unreachable (research-preview allowlist or "
                "organization policy). Outbound Bridge tools still work."
            )
            self.router.call("update_state", {"session_id": self.session_id, "reachable": False})

    def _tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": all_tools()}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            raise _invalid("tools/call requires a name")
        from .router_client import RouterClientError

        try:
            result = dispatch_tool(self.router, name, arguments)
        except KeyError:
            raise _invalid(f"unknown tool {name!r}") from None
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

    # --- router event -> channel push --------------------------------------
    def _on_router_event(self, event: dict[str, Any]) -> None:
        params = {
            "kind": event.get("kind"),
            "call_id": event.get("call_id"),
            "from": event.get("from"),
            "message_id": event.get("message_id"),
            "text": event.get("text"),
        }
        self.rpc.notify(CHANNEL_NOTIFICATION, params)
        # Informational events expect no reply; acknowledge so the queue advances.
        # The ack must run off the router reader thread (this callback's thread)
        # to avoid a request/response deadlock on that same connection.
        if event.get("kind") in ("text", "call_result") and event.get("message_id"):
            threading.Thread(target=self._ack, args=(event["message_id"],), daemon=True).start()

    def _ack(self, message_id: str) -> None:
        try:
            self.router.call("ack", {"message_id": message_id})
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.rpc.close()
        if self.router is not None:
            try:
                self.router.close()
            except Exception:  # noqa: BLE001
                pass


def _invalid(message: str):
    from .mcp import JsonRpcError

    return JsonRpcError(INVALID_PARAMS, message)
