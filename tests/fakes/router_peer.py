"""Run a real RouterServer on its Unix socket in a background thread and hand
tests authenticated clients. This exercises the true protocol/subprocess
boundary without any vendor binary or model call.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from bridge.paths import Paths
from bridge.router import RouterConfig, RouterServer
from bridge.router_client import RouterClient


class RunningRouter:
    def __init__(
        self,
        paths: Paths,
        *,
        now: Callable[[], float] = time.time,
        new_id: Callable[[], str] | None = None,
        config: RouterConfig | None = None,
    ) -> None:
        self.paths = paths
        self.server = RouterServer(paths, now=now, new_id=new_id, config=config)
        self.token = self.server.token
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._clients: list[RouterClient] = []

    def _run(self) -> None:
        try:
            self.server.serve_forever()
        finally:
            self.server.shutdown()

    def __enter__(self) -> RunningRouter:
        self._thread.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and not self.paths.socket.exists():
            time.sleep(0.01)
        if not self.paths.socket.exists():
            raise TimeoutError("router socket did not appear")
        return self

    def client(
        self,
        session_id: str | None = None,
        *,
        role: str = "client",
        on_event: Callable[[dict], None] | None = None,
    ) -> RouterClient:
        c = RouterClient.connect(
            self.paths, session_id=session_id, token=self.token, role=role, on_event=on_event
        )
        self._clients.append(c)
        return c

    def adapter(self, session_id: str) -> RecordingAdapter:
        return RecordingAdapter(self, session_id)

    def __exit__(self, *exc) -> None:
        for c in self._clients:
            try:
                c.close()
            except Exception:
                pass
        self.server.stop()
        self._thread.join(timeout=3.0)


class RecordingAdapter:
    """A client that subscribes as a session and records pushed events."""

    def __init__(self, rr: RunningRouter, session_id: str) -> None:
        self.session_id = session_id
        self.events: list[dict] = []
        self._cv = threading.Condition()
        self.client = rr.client(session_id=session_id, role="adapter", on_event=self._on_event)

    def _on_event(self, event: dict) -> None:
        with self._cv:
            self.events.append(event)
            self._cv.notify_all()

    def subscribe(self) -> None:
        self.client.subscribe(self.session_id)

    def wait_for_event(self, timeout: float = 2.0) -> dict | None:
        with self._cv:
            if not self.events:
                self._cv.wait(timeout)
            return self.events[-1] if self.events else None

    def wait_for_count(self, n: int, timeout: float = 2.0) -> bool:
        deadline = time.time() + timeout
        with self._cv:
            while len(self.events) < n:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            return True

    def reply(self, call_id: str, answer: str, blocked: list[str] | None = None) -> dict:
        return self.client.call(
            "reply", {"call_id": call_id, "answer": answer, "blocked": blocked or []}
        )
