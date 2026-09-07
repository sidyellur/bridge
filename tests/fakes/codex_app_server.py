"""A fake ``codex app-server`` speaking the pinned 0.151.0 wire contract.

Frames are verbatim from the 2026-09-07 live probe recorded in
``.superpowers/codex-contract-2026-09-07.md``: WebSocket text frames over the
socket, camelCase JSON-RPC with no ``jsonrpc`` field, a top-level ``emittedAtMs``
on every notification, ``thread/started``/``thread/status/changed`` broadcast to
everyone while ``turn/*``/``item/*`` reach subscribers only, and
``thread/resume`` as the (frequently broken) subscribe verb. No real model.

Every wire name comes from :mod:`bridge.codex_app_server` — the client module
is the single source of truth, and ``test_codex_client`` pins both it and this
fake against ``tests/fixtures/codex_protocol/codex-0.151.0.json``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from bridge.codex_app_server import (
    FORBIDDEN_METHODS,
    ITEM_AGENT_MESSAGE,
    M_INITIALIZE,
    M_THREAD_LIST,
    M_THREAD_READ,
    M_THREAD_RESUME,
    M_THREAD_UNSUBSCRIBE,
    M_TURN_START,
    N_INITIALIZED,
    N_ITEM_COMPLETED,
    N_ITEM_DELTA,
    N_ITEM_STARTED,
    N_THREAD_STARTED,
    N_THREAD_STATUS,
    N_TURN_COMPLETED,
    N_TURN_STARTED,
    PINNED_CODEX_VERSION,
    THREAD_STATUS_ACTIVE,
    THREAD_STATUS_IDLE,
)
from bridge.mcp import INVALID_REQUEST, METHOD_NOT_FOUND, Framing, JsonRpcError, RpcEndpoint

_LIST_TURNS_UNSUPPORTED = "list_turns is not supported yet"
_NO_ROLLOUT = "no rollout found for thread id {thread_id}"


class FakeCodexAppServer:
    def __init__(
        self,
        sock,
        *,
        codex_version: str = PINNED_CODEX_VERSION,
        thread_id: str = "thread-abc",
        cwd: str = "/tmp/peer",
        agent_message: str = "",
        auto_complete: bool = True,
        resume_result: str = "ok",
        eager_thread: bool = True,
        turn_start_error: JsonRpcError | None = None,
        thread_list_error: JsonRpcError | None = None,
        defer_turn_status: bool = False,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.codex_version = codex_version
        self.thread_id = thread_id
        self.cwd = cwd
        self.agent_message = agent_message
        self.auto_complete = auto_complete
        self.resume_result = resume_result
        self.eager_thread = eager_thread
        # Error knobs: a real server refuses `turn/start` for an unknown thread
        # (a restarted TUI) and can refuse `thread/loaded/list` outright. Set to
        # a JsonRpcError to answer that method with it instead of succeeding.
        self.turn_start_error = turn_start_error
        self.thread_list_error = thread_list_error
        # This fake answers `turn/start` *after* its own `active` broadcast,
        # which the live server does not: it responds first and broadcasts a
        # moment later. Set this to leave the `active` frame to the test, so a
        # client that leaned on the broadcast to know it is busy is visible.
        self.defer_turn_status = defer_turn_status
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

        self.initialize_params: dict | None = None
        self.threads: list[dict] = []
        self.subscribed: set[str] = set()
        self.turns: list[dict] = []
        self.forbidden_calls: list[dict] = []
        self.parse_errors: list[bytes] = []
        self._turn_n = 0
        self._last_turn_id: str | None = None
        # `threads` and the turn counter are written from the reader thread and
        # read (or written, via start_thread) from the test's thread.
        self._lock = threading.RLock()

        self.rpc = RpcEndpoint(
            sock,
            name="fake-codex-app-server",
            framing=Framing.WS_SERVER,
            include_jsonrpc=False,
            on_parse_error=self._on_parse_error,
            on_close=self._on_close,
        )
        self.rpc.method(M_INITIALIZE, self._initialize)
        self.rpc.notification(N_INITIALIZED, self._on_initialized)
        self.rpc.method(M_THREAD_LIST, self._thread_loaded_list)
        self.rpc.method(M_THREAD_READ, self._thread_read)
        self.rpc.method(M_THREAD_RESUME, self._thread_resume)
        self.rpc.method(M_THREAD_UNSUBSCRIBE, self._thread_unsubscribe)
        self.rpc.method(M_TURN_START, self._turn_start)
        for method in FORBIDDEN_METHODS:
            self.rpc.method(method, lambda params, m=method: self._forbidden(m, params))
        self.rpc.start()

    # --- outbound -----------------------------------------------------------
    def _notify(self, method: str, params: dict) -> None:
        self.rpc.notify(method, params, extra={"emittedAtMs": self._now_ms()})

    def _notify_subscribed(self, method: str, params: dict) -> None:
        if self.thread_id in self.subscribed:
            self._notify(method, params)

    # --- initialize ---------------------------------------------------------
    def _initialize(self, params: dict) -> dict:
        if self.initialize_params is not None:
            raise JsonRpcError(INVALID_REQUEST, "Already initialized")
        self.initialize_params = params
        client = (params.get("clientInfo") or {}).get("name", "client")
        version = (params.get("clientInfo") or {}).get("version", "0.1.0")
        return {
            "userAgent": (
                f"{client}/{self.codex_version} (Fake OS 1.0; arm64) "
                f"fake-term/0 ({client}; {version})"
            ),
            "codexHome": "/fake/.codex",
            "platformFamily": "unix",
            "platformOs": "macos",
        }

    def _require_initialized(self) -> None:
        if self.initialize_params is None:
            raise JsonRpcError(INVALID_REQUEST, "Not initialized")

    def _on_initialized(self, _params: dict) -> None:
        if self.eager_thread:
            self.start_thread(self.thread_id, self.cwd)

    # --- threads ------------------------------------------------------------
    def _thread_obj(self, thread_id: str, cwd: str, status: dict) -> dict:
        seconds = self._now_ms() // 1000
        return {
            "id": thread_id,
            "sessionId": thread_id,
            "status": status,
            "cwd": cwd,
            "createdAt": seconds,
            "updatedAt": seconds,
            "source": "vscode",
            "threadSource": "user",
            "cliVersion": self.codex_version,
            "name": None,
            "preview": "",
            "historyMode": "paginated",
            "path": f"/fake/rollout-{thread_id}.jsonl",
            "turns": [],
        }

    def start_thread(self, thread_id: str, cwd: str) -> dict:
        thread = self._thread_obj(thread_id, cwd, {"type": THREAD_STATUS_IDLE})
        with self._lock:
            self.threads.append(thread)
        self._notify(N_THREAD_STARTED, {"thread": thread})
        self._notify(
            N_THREAD_STATUS, {"threadId": thread_id, "status": {"type": THREAD_STATUS_IDLE}}
        )
        return thread

    def _find_thread(self, thread_id: str) -> dict:
        with self._lock:
            for thread in self.threads:
                if thread["id"] == thread_id:
                    return thread
        # Unprobed guess: the live server's error for an unknown thread/read id
        # was never recorded, so this reuses thread/resume's message.
        raise JsonRpcError(INVALID_REQUEST, _NO_ROLLOUT.format(thread_id=thread_id))

    def _thread_loaded_list(self, _params: dict) -> dict:
        self._require_initialized()
        if self.thread_list_error is not None:
            raise self.thread_list_error
        with self._lock:
            return {"data": [t["id"] for t in self.threads], "nextCursor": None}

    def _thread_read(self, params: dict) -> dict:
        self._require_initialized()
        if params.get("includeTurns"):
            raise JsonRpcError(METHOD_NOT_FOUND, _LIST_TURNS_UNSUPPORTED)
        return {"thread": self._find_thread(params["threadId"])}

    def _thread_resume(self, params: dict) -> dict:
        self._require_initialized()
        thread_id = params["threadId"]
        # The failure branches must not subscribe: 0.151.0 does leave the
        # connection subscribed after the excludeTurns error, but that is a bug
        # (report §3), not contract Bridge may lean on.
        if self.resume_result == "unsupported":
            raise JsonRpcError(METHOD_NOT_FOUND, _LIST_TURNS_UNSUPPORTED)
        if self.resume_result == "no_rollout":
            raise JsonRpcError(INVALID_REQUEST, _NO_ROLLOUT.format(thread_id=thread_id))
        thread = self._find_thread(thread_id)
        self.subscribed.add(thread_id)
        return {"thread": thread}

    def _thread_unsubscribe(self, params: dict) -> dict:
        self._require_initialized()
        thread_id = params["threadId"]
        was = thread_id in self.subscribed
        self.subscribed.discard(thread_id)
        return {"status": "unsubscribed" if was else "notSubscribed"}

    # --- turns --------------------------------------------------------------
    def _turn_obj(self, turn_id: str, status: str) -> dict:
        return {
            "id": turn_id,
            "items": [],
            "itemsView": "notLoaded",
            "status": status,
            "error": None,
            "startedAt": None,
            "completedAt": None,
            "durationMs": None,
        }

    def _turn_start(self, params: dict) -> dict:
        self._require_initialized()
        # Recorded before the refusal branch: `turns` is every `turn/start` the
        # client sent, which is what a test about refused turns has to count.
        with self._lock:
            self.turns.append(params)
        if self.turn_start_error is not None:
            raise self.turn_start_error
        thread_id = params.get("threadId", self.thread_id)
        with self._lock:
            self._turn_n += 1
            turn_id = f"turn-{self._turn_n}"
            self._last_turn_id = turn_id

        if not self.defer_turn_status:
            self.emit_status(THREAD_STATUS_ACTIVE, [])
        self._notify_subscribed(
            N_TURN_STARTED,
            {"threadId": thread_id, "turn": self._turn_obj(turn_id, "inProgress")},
        )
        user_item = {
            "type": "userMessage",
            "id": f"user_{turn_id}",
            "content": params.get("input", []),
            "clientId": None,
        }
        self._notify_subscribed(
            N_ITEM_STARTED,
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": user_item,
                "startedAtMs": self._now_ms(),
            },
        )
        self._notify_subscribed(
            N_ITEM_COMPLETED,
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": user_item,
                "completedAtMs": self._now_ms(),
            },
        )
        if self.auto_complete:
            self.complete_turn(turn_id)
        return {"turn": self._turn_obj(turn_id, "inProgress")}

    def complete_turn(
        self,
        turn_id: str | None = None,
        agent_message: str | None = None,
        status: str = "completed",
    ) -> None:
        turn_id = turn_id or self._last_turn_id or "turn-0"
        message = self.agent_message if agent_message is None else agent_message
        item_id = f"msg_{turn_id}"
        self._notify_subscribed(
            N_ITEM_STARTED,
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "item": {"type": ITEM_AGENT_MESSAGE, "id": item_id, "text": ""},
                "startedAtMs": self._now_ms(),
            },
        )
        if message:
            words = message.split(" ")
            for i, word in enumerate(words):
                delta = word if i == len(words) - 1 else word + " "
                self._notify_subscribed(
                    N_ITEM_DELTA,
                    {
                        "threadId": self.thread_id,
                        "turnId": turn_id,
                        "itemId": item_id,
                        "delta": delta,
                    },
                )
        self._notify_subscribed(
            N_ITEM_COMPLETED,
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "item": {
                    "type": ITEM_AGENT_MESSAGE,
                    "id": item_id,
                    "text": message,
                    "phase": "final",
                    "delivery": None,
                    "memoryCitation": None,
                },
                "completedAtMs": self._now_ms(),
            },
        )
        self.emit_status(THREAD_STATUS_IDLE)
        self._notify_subscribed(
            N_TURN_COMPLETED,
            {"threadId": self.thread_id, "turn": self._turn_obj(turn_id, status)},
        )

    def emit_status(self, type_: str, active_flags: list[str] | None = None) -> None:
        status: dict = {"type": type_}
        if active_flags is not None:
            status["activeFlags"] = active_flags
        self._notify(N_THREAD_STATUS, {"threadId": self.thread_id, "status": status})

    # --- traps --------------------------------------------------------------
    def _forbidden(self, method: str, params: dict) -> dict:
        self.forbidden_calls.append({"method": method, "params": params})
        return {}

    def _on_parse_error(self, raw: bytes) -> None:
        # Two JSON objects in one frame: the real server logs a deserialize
        # error and the connection is dead.
        self.parse_errors.append(raw)
        self.rpc.close()

    def _on_close(self) -> None:
        # An unmasked client frame tears the reader down; the real server then
        # closes the socket with no close frame.
        self.rpc.close()

    def close(self) -> None:
        self.rpc.close()
