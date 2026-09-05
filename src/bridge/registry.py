"""Session registry: lifecycle state transitions and stale cleanup.

State is driven by wrapper launch, adapter connections, and vendor turn events
— never by file mtime. A client-side :class:`Registry` reports transitions to
the router; a store-side sweep offlines sessions whose process is gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .store import (
    SESSION_STATES,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_STARTING,
    STATE_WORKING,
    Store,
)

if TYPE_CHECKING:
    from .router_client import RouterClient

# Advisory transition graph (vendor events may move freely between active
# states; this documents the expected flow and rejects nonsense).
_ALLOWED_NEXT: dict[str, set[str]] = {
    STATE_STARTING: {STATE_IDLE, STATE_WORKING, STATE_OFFLINE},
    STATE_IDLE: {"working", "waiting", STATE_OFFLINE, STATE_IDLE},
    STATE_WORKING: {"idle", "waiting", STATE_OFFLINE, STATE_WORKING},
    "waiting": {"idle", "working", STATE_OFFLINE, "waiting"},
    STATE_OFFLINE: {STATE_STARTING, STATE_IDLE, STATE_WORKING, STATE_OFFLINE},
}


def is_valid_transition(current: str, nxt: str) -> bool:
    if nxt not in SESSION_STATES:
        return False
    return nxt in _ALLOWED_NEXT.get(current, SESSION_STATES)


class Registry:
    """Client-side reporter. Talks to the router over a connected client."""

    def __init__(self, client: RouterClient) -> None:
        self._client = client

    @classmethod
    def for_client(cls, client: RouterClient) -> Registry:
        return cls(client)

    def register_start(
        self, session_id: str, family: str, *, cwd: str = "", pid: int | None = None
    ) -> None:
        self._client.call(
            "register_session",
            {
                "session_id": session_id,
                "family": family,
                "cwd": cwd,
                "pid": pid,
                "state": STATE_STARTING,
                "is_managed": True,
            },
        )

    def mark(self, session_id: str, state: str) -> None:
        self._client.call("update_state", {"session_id": session_id, "state": state})

    def set_pid(self, session_id: str, pid: int) -> None:
        self._client.call("update_state", {"session_id": session_id, "pid": pid})

    def set_vendor_session(self, session_id: str, vendor_session_id: str) -> None:
        self._client.call(
            "update_state",
            {"session_id": session_id, "vendor_session_id": vendor_session_id},
        )

    def set_last_user_message(self, session_id: str, message: str) -> None:
        self._client.call("update_state", {"session_id": session_id, "last_user_message": message})

    def mark_offline(self, session_id: str) -> None:
        try:
            self._client.call("deregister", {"session_id": session_id})
        except Exception:
            pass


# --- store-side maintenance ------------------------------------------------


def sweep_stale(store: Store, *, ttl_s: float, now: float) -> list[str]:
    """Offline any managed session that is not reachable and has not been active
    within ``ttl_s``. Returns the ids that were offlined."""
    offlined: list[str] = []
    for s in store.list_sessions(include_unmanaged=False):
        if s.state == STATE_OFFLINE:
            continue
        if not s.reachable and (now - s.last_active) > ttl_s:
            store.mark_offline(s.id)
            offlined.append(s.id)
    return offlined


def register_unmanaged(store: Store, discovered: list[dict]) -> None:
    """Record externally-discovered sessions as unmanaged (never callable)."""
    for d in discovered:
        store.upsert_session(
            d["id"],
            d.get("family", "unknown"),
            cwd=d.get("cwd", ""),
            state=d.get("state", STATE_IDLE),
            vendor_session_id=d.get("vendor_session_id"),
            reachable=False,
            is_managed=False,
        )
