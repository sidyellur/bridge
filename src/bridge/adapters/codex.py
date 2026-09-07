"""Codex App Server adapter.

Wires a router subscription to a :class:`CodexAppServerClient`: the adapter
binds the thread the remote TUI already owns, subscribes to that thread,
delivers router events into it as idle ``turn/start`` calls, reflects thread
status back into the session's Bridge state, and records what it knows about
the thread in the session's ``session.json`` for ``bridge doctor``.

An App Server disconnect is not session death. The thread outlives the
connection, so a crash (never a deliberate ``close()``) marks the session
unreachable and records a transcript ``disconnected`` event, then a bounded
backoff reconnect re-binds *the same thread* on the replacement connection —
the Bridge session id, its router subscription, and the vendor thread id all
survive.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from ..codex_app_server import (
    STATUS_IDLE,
    TURN_STATUS_COMPLETED,
    CodexAppServerClient,
    CodexThreadBusy,
)
from ..paths import Paths


class CodexAdapter:
    """One wrapped Codex session's bridge between the router and its App Server.

    Correlating a Bridge call with Codex's final agent message is proven, but
    the ``turn/*``/``item/*`` stream it needs is subscriber-only and 0.151.0
    usually refuses ``thread/resume`` for a live TUI thread. The reply path for
    calls is therefore Codex invoking Bridge's ``reply`` MCP tool; the final
    message is a clearly-labeled fallback, enabled only by Experiment F and
    only ever available when :meth:`CodexAppServerClient.subscribe` succeeded.
    """

    def __init__(
        self,
        session_id: str,
        app_client: CodexAppServerClient,
        *,
        final_message_fallback: bool = False,
        cwd: str | None = None,
        paths: Paths | None = None,
        reconnect: Callable[[], CodexAppServerClient] | None = None,
        on_app_server_disconnect: Callable[[], None] | None = None,
        reconnect_attempts: int = 5,
        reconnect_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session_id = session_id
        self.app = app_client
        self.fallback = final_message_fallback
        self.cwd = cwd or os.getcwd()
        self.paths = paths
        self.router: Any = None
        self._active_call: str | None = None
        self._final: dict[str, str] = {}
        # Builds and starts a replacement CodexAppServerClient bound to the
        # same socket path when the App Server connection drops unexpectedly.
        # None disables reconnect (the session simply stays unreachable).
        self._reconnect = reconnect
        self._on_app_server_disconnect = on_app_server_disconnect
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_sleep = reconnect_sleep
        # A single worker thread runs all router-bound calls issued from App
        # Server callbacks, so the App Server reader thread never blocks waiting
        # on a router response (which could need that same reader thread).
        self._tasks: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker_running = True
        # A callback can fire while start() is still handshaking, and an
        # update_state for a session the router has not registered yet is
        # silently dropped. The worker waits for start() to finish its own
        # router calls, which also keeps two threads off one router socket.
        self._started = threading.Event()
        self._bind_app_callbacks(self.app)

    def _bind_app_callbacks(self, app: CodexAppServerClient) -> None:
        app._on_thread_bound = self._on_thread_bound
        app._on_status = self._on_status
        app._on_agent_message = self._on_agent_message
        app._on_turn_completed = self._on_turn_completed
        app._on_thread_error = self._on_thread_error
        app._on_disconnect = self._on_app_disconnected

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
        # Finding nothing is normal: a TUI thread appears seconds after the TUI
        # attaches, and the `thread/started` broadcast binds it then.
        self.app.bind_thread()
        self.router.subscribe(self.session_id)
        self.router.call(
            "update_state",
            {"session_id": self.session_id, "state": self.app.status or STATUS_IDLE},
        )
        self._write_session_meta()
        self._started.set()
        return self

    # --- worker -------------------------------------------------------------
    def _run_worker(self) -> None:
        self._started.wait(30.0)
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

    def _update_state(self, updates: dict[str, Any]) -> None:
        from ..router_client import RouterClientError

        try:
            self.router.call("update_state", {"session_id": self.session_id, **updates})
        except RouterClientError:
            pass

    def _write_session_meta(self) -> None:
        if self.paths is None:
            return
        meta: dict[str, Any] = {
            "thread_id": self.app.thread_id or "",
            "subscribed": bool(self.app.subscribed),
            "codex_version": self.app.codex_version,
        }
        if self.app.codex_version_warning:
            meta["codex_version_warning"] = self.app.codex_version_warning
        if self.app.last_thread_error:
            meta["last_thread_error"] = self.app.last_thread_error
        self.paths.merge_session_meta(self.session_id, meta)

    # --- App Server callbacks (App reader thread) ---------------------------
    def _on_thread_bound(self, thread_id: str) -> None:
        # The single path for the initial bind and every rebind. It runs on the
        # worker because subscribe() issues a request, which a notification
        # handler on the App Server reader thread could never answer.
        self._submit(lambda: self._bind_thread_task(thread_id))

    def _bind_thread_task(self, thread_id: str) -> None:
        self._update_state({"vendor_session_id": thread_id})
        self.app.subscribe()
        self._write_session_meta()

    def _on_status(self, status: str) -> None:
        # Already a Bridge state: the client maps thread status through
        # THREAD_STATUS_TO_STATE before it calls back.
        self._submit(lambda: self._update_state({"state": status}))

    def _on_thread_error(self, _detail: str) -> None:
        # systemError maps to idle, so only the recorded detail changes here.
        self._submit(self._write_session_meta)

    def _on_agent_message(self, turn_id: str, text: str) -> None:
        self._final[turn_id] = text

    def _on_turn_completed(self, turn_id: str, status: str) -> None:
        call_id = self._active_call
        self._active_call = None
        # Both stores are evicted unconditionally, whichever one answers: a
        # long-lived session must not accumulate every completed turn's text.
        stored = self._final.pop(turn_id, "")
        message = self.app.pop_final_message(turn_id) or stored
        if call_id and self.fallback and status == TURN_STATUS_COMPLETED and message:
            self._submit(lambda: self._fallback_reply(call_id, message))

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
        if self.app.thread_id is None:
            # No thread bound yet. The router does not deliver to a session it
            # has not seen go reachable, so this is a race, not a normal path.
            return
        try:
            self.app.start_turn(text)
        except CodexThreadBusy:
            # The router's pump holds delivery while the session is `working`,
            # so a refusal here is only ever a race with the status we have not
            # reported yet. The message is already marked delivered by
            # `delivery.pump_target` before this handler runs, so withholding
            # the ack leaves it delivered-but-unacked: it comes back only when
            # `redeliver_inflight` replays it on the adapter's next router
            # reconnect. Explicitly re-queuing it instead is a follow-up.
            return
        if kind in ("text", "call_result") and event.get("message_id"):
            mid = event["message_id"]
            self._submit(lambda: self._ack(mid))

    def _ack(self, message_id: str) -> None:
        from ..router_client import RouterClientError

        try:
            self.router.call("ack", {"message_id": message_id})
        except RouterClientError:
            pass

    # --- App Server disconnect + reconnect (App reader thread callback) -----
    def _on_app_disconnected(self) -> None:
        # Never a headless answer: mark unreachable before anything else, and
        # let the wrapper print its own one-line diagnostic if it wants one.
        if self._on_app_server_disconnect is not None:
            try:
                self._on_app_server_disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._submit(self._handle_app_disconnected)

    def _handle_app_disconnected(self) -> None:
        self._update_state({"reachable": False})
        if self._reconnect is not None:
            self._try_reconnect()

    def _try_reconnect(self) -> bool:
        """Bounded-backoff reconnect to the same App Server socket, re-running
        the initialize handshake and the bind on the replacement client. The
        Bridge session id and the router subscription are untouched, and the
        thread outlives the connection, so the same thread id is re-bound —
        ``_on_thread_bound`` then re-subscribes it on this same worker."""
        from ..router_client import RouterClientError

        for attempt in range(self._reconnect_attempts):
            if not self._worker_running:
                return False
            old_app = self.app
            try:
                new_app = self._reconnect()
                self.app = new_app
                self._bind_app_callbacks(self.app)
                self.app.initialize()
                self.app.own_thread_ids = set(old_app.own_thread_ids)
                self.app.bind_thread()
            except Exception:  # noqa: BLE001
                self._reconnect_sleep(min(0.1 * (2**attempt), 2.0))
                continue
            finally:
                # The old (already-dead) connection is only ever discarded
                # here, on the worker thread, once a replacement is in hand —
                # never leave its socket for the garbage collector to close.
                if old_app is not self.app:
                    try:
                        old_app.close()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                if self.app.thread_id:
                    self.router.call(
                        "update_state",
                        {
                            "session_id": self.session_id,
                            "vendor_session_id": self.app.thread_id,
                        },
                    )
                self.router.call(
                    "update_state",
                    {
                        "session_id": self.session_id,
                        "state": self.app.status or STATUS_IDLE,
                        "reachable": True,
                    },
                )
            except RouterClientError:
                self._reconnect_sleep(min(0.1 * (2**attempt), 2.0))
                continue
            self._write_session_meta()
            return True
        return False

    def close(self) -> None:
        self._worker_running = False
        self._started.set()
        self._tasks.put(None)
        self.app.close()
        if self.router is not None:
            try:
                self.router.close()
            except Exception:  # noqa: BLE001
                pass
