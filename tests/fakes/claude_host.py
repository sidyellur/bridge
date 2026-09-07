"""A fake Claude MCP host: performs initialize, receives channel notifications,
lists/calls tools, and can auto-reply to inbound calls. No real model.
"""

from __future__ import annotations

import threading

from bridge.mcp import RpcEndpoint


class FakeClaudeHost:
    def __init__(self, sock, *, auto_reply: str | None = None) -> None:
        self.rpc = RpcEndpoint(sock, name="fake-claude-host")
        self.auto_reply = auto_reply
        self.channel_events: list[dict] = []
        self._cv = threading.Condition()
        self.rpc.notification("notifications/claude/channel", self._on_channel)
        self.rpc.start()

    def initialize(self) -> dict:
        return self.rpc.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "fake-claude", "version": "0"},
            },
        )

    def initialized(self) -> None:
        self.rpc.notify("notifications/initialized", {})

    def list_tools(self) -> dict:
        return self.rpc.request("tools/list", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.rpc.request("tools/call", {"name": name, "arguments": arguments})

    def _on_channel(self, params: dict) -> None:
        with self._cv:
            self.channel_events.append(params)
            self._cv.notify_all()
        meta = params.get("meta") or {}
        if self.auto_reply is not None and meta.get("kind") == "call":
            # Reply off the reader thread: call_tool waits for a response that this
            # same thread must read, so a synchronous reply here would deadlock.
            threading.Thread(
                target=self.call_tool,
                args=("reply", {"call_id": meta["call_id"], "answer": self.auto_reply}),
                daemon=True,
            ).start()

    def wait_for_events(self, n: int, timeout: float = 3.0) -> bool:
        import time

        deadline = time.time() + timeout
        with self._cv:
            while len(self.channel_events) < n:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            return True

    def close(self) -> None:
        self.rpc.close()
