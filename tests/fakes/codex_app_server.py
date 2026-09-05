"""A fake Codex App Server matching the pinned v1 contract. Records turns, emits
lifecycle notifications, and flags any forbidden ``turn/steer``. No real model.
"""

from __future__ import annotations

from bridge.mcp import RpcEndpoint


class FakeCodexAppServer:
    def __init__(
        self,
        sock,
        *,
        version: str = "codex-app-server/1",
        thread_id: str = "thread-abc",
        agent_message: str = "",
        auto_complete: bool = True,
    ) -> None:
        self.rpc = RpcEndpoint(sock, name="fake-codex-app-server")
        self.version = version
        self.thread_id = thread_id
        self.agent_message = agent_message
        self.auto_complete = auto_complete
        self.turns: list[dict] = []
        self.forbidden_calls: list[dict] = []
        self._turn_n = 0
        self._last_turn_id: str | None = None
        self.rpc.method("initialize", self._initialize)
        self.rpc.notification("initialized", self._on_initialized)
        self.rpc.method("turn/start", self._turn_start)
        self.rpc.method("turn/steer", self._forbidden)
        self.rpc.start()

    def _initialize(self, _params: dict) -> dict:
        return {
            "protocolVersion": self.version,
            "serverInfo": {"name": "fake-codex", "version": "0"},
            "capabilities": {},
        }

    def _on_initialized(self, _params: dict) -> None:
        self.rpc.notify("thread/started", {"thread_id": self.thread_id})
        self.rpc.notify("runtime/status", {"status": "idle"})

    def _turn_start(self, params: dict) -> dict:
        self._turn_n += 1
        turn_id = f"turn-{self._turn_n}"
        self._last_turn_id = turn_id
        self.turns.append(params)
        self.rpc.notify("turn/started", {"turn_id": turn_id, "thread_id": self.thread_id})
        if self.auto_complete:
            self.complete_turn(turn_id)
        return {"turn_id": turn_id}

    def complete_turn(self, turn_id: str | None = None, agent_message: str | None = None) -> None:
        turn_id = turn_id or self._last_turn_id or "turn-0"
        message = self.agent_message if agent_message is None else agent_message
        if message:
            self.rpc.notify("item/agent_message", {"turn_id": turn_id, "text": message})
        self.rpc.notify("turn/completed", {"turn_id": turn_id, "thread_id": self.thread_id})

    def _forbidden(self, params: dict) -> dict:
        self.forbidden_calls.append(params)
        return {}

    def emit_status(self, status: str) -> None:
        self.rpc.notify("runtime/status", {"status": status})

    def close(self) -> None:
        self.rpc.close()
