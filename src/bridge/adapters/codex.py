"""Codex App Server adapter.

Wires a router subscription to a :class:`CodexAppServerClient`: router events
become idle ``turn/start`` deliveries in the exact bound thread, runtime/turn
events are reflected back into the session's Bridge state, and — only when
enabled by Experiment F — a turn that completes without an MCP ``reply`` yields
its final agent message as a clearly-labeled fallback answer.
"""

from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable
from typing import Any

from ..codex_app_server import STATUS_IDLE, CodexAppServerClient


class CodexAdapter:
    def __init__(
        self,
        session_id: str,
        app_client: CodexAppServerClient,
        *,
        final_message_fallback: bool = False,
        cwd: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.app = app_client
        self.fallback = final_message_fallback
        self.cwd = cwd or os.getcwd()
        self.router: Any = None
        self._active_call: str | None = None
        # A single worker thread runs all router-bound calls issued from App
        # Server callbacks, so the App Server reader thread never blocks waiting
        # on a router response (which could need that same reader thread).
        self._tasks: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker_running = True
        # App Server callbacks
        self.app._on_thread_bound = self._on_thread_bound
        self.app._on_status = self._on_status
        self.app._on_turn_completed = self._on_turn_completed

    def connect_router(self, connect: Callable[[Callable[[dict[str, Any]], None]], Any]) -> None:
        self.router = connect(self._on_router_event)

    def start(self) -> CodexAdapter:
        self._worker.start()
        self.app.initialize()  # raises UnsupportedCodexVersion on drift
        self.router.call(
            "register_session",
            {
                "session_id": self.session_id,
                "family": "codex",
                "cwd": self.cwd,
                "state": "starting",
                "is_managed": True,
            },
        )
        if self.app.thread_id:
            self.router.call(
                "update_state",
                {"session_id": self.session_id, "vendor_session_id": self.app.thread_id},
            )
        self.router.subscribe(self.session_id)
        self.router.call(
            "update_state",
            {"session_id": self.session_id, "state": self.app.status or STATUS_IDLE},
        )
        return self

    # --- worker -------------------------------------------------------------
    def _run_worker(self) -> None:
        while self._worker_running:
            task = self._tasks.get()
            if task is None:
                return
            try:
                task()
            except Exception:  # noqa: BLE001
                pass

    def _submit(self, fn: Callable[[], None]) -> None:
        self._tasks.put(fn)

    # --- App Server callbacks (App reader thread) ---------------------------
    def _on_thread_bound(self, thread_id: str) -> None:
        self._submit(
            lambda: self.router.call(
                "update_state",
                {"session_id": self.session_id, "vendor_session_id": thread_id},
            )
        )

    def _on_status(self, status: str) -> None:
        state = "idle" if status == STATUS_IDLE else "working"
        self._submit(
            lambda: self.router.call(
                "update_state", {"session_id": self.session_id, "state": state}
            )
        )

    def _on_turn_completed(self, _turn_id: str, final_message: str) -> None:
        call_id = self._active_call
        self._active_call = None
        if call_id and self.fallback and final_message:
            self._submit(lambda: self._fallback_reply(call_id, final_message))

    def _fallback_reply(self, call_id: str, message: str) -> None:
        from ..router_client import RouterClientError

        try:
            self.router.call(
                "reply", {"call_id": call_id, "answer": message, "via": "final-message"}
            )
        except RouterClientError:
            # The agent already answered via the MCP reply tool; nothing to do.
            pass

    # --- router event -> turn/start (RouterClient reader thread) ------------
    def _on_router_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        text = event.get("text", "")
        if kind == "call":
            self._active_call = event.get("call_id")
        self.app.start_turn(text)
        if kind in ("text", "call_result") and event.get("message_id"):
            mid = event["message_id"]
            self._submit(lambda: self._ack(mid))

    def _ack(self, message_id: str) -> None:
        from ..router_client import RouterClientError

        try:
            self.router.call("ack", {"message_id": message_id})
        except RouterClientError:
            pass

    def close(self) -> None:
        self._worker_running = False
        self._tasks.put(None)
        self.app.close()
        if self.router is not None:
            try:
                self.router.close()
            except Exception:  # noqa: BLE001
                pass


def run(paths=None):  # pragma: no cover - integration entry
    """Real entry: wire a managed App Server + adapter for a wrapped Codex."""
    from ..paths import SESSION_ID_ENV, Paths

    paths = paths or Paths.resolve()
    session_id = os.environ.get(SESSION_ID_ENV)
    if not session_id:
        return 2
    # Real socket wiring to the managed App Server is established by the wrapper;
    # this stub documents the entry point exercised by live tests.
    raise NotImplementedError("live Codex App Server wiring is exercised by -m live tests")
