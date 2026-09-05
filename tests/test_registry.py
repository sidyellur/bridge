"""Task 4 verify (registry): state transitions, stale sweep, unmanaged records.

Issue #5 part D adds the router-side enforcement of ``is_valid_transition``.
"""

from __future__ import annotations

import pytest

from bridge.registry import (
    Registry,
    is_valid_transition,
    register_unmanaged,
    sweep_stale,
)
from bridge.router import RouterError
from bridge.store import (
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_STARTING,
    STATE_WORKING,
    Store,
)

from .fakes.clock import FrozenClock
from .fakes.router_peer import RunningRouter


def test_valid_transitions():
    assert is_valid_transition(STATE_STARTING, STATE_IDLE)
    assert is_valid_transition(STATE_IDLE, STATE_WORKING)
    assert is_valid_transition(STATE_WORKING, "waiting")
    assert is_valid_transition("waiting", STATE_IDLE)
    assert is_valid_transition(STATE_IDLE, STATE_OFFLINE)
    assert not is_valid_transition(STATE_IDLE, "bogus")


def test_router_enforces_a_valid_transition(router_core):
    """A legal move is applied and answered normally."""
    r = router_core
    r.store.upsert_session("s1", "claude", state=STATE_STARTING)
    outcome, _ = r.dispatch("update_state", {"session_id": "s1", "state": STATE_WORKING})
    assert outcome == "respond"
    assert r.store.get_session("s1").state == STATE_WORKING


def test_router_rejects_an_invalid_transition(router_core):
    """A state that is not a session state is refused, not written."""
    r = router_core
    r.store.upsert_session("s1", "claude", state=STATE_IDLE)
    with pytest.raises(RouterError) as exc:
        r.dispatch("update_state", {"session_id": "s1", "state": "compacting"})
    assert exc.value.code == "bad_transition"
    assert r.store.get_session("s1").state == STATE_IDLE  # untouched
    rejected = [e for e in r.store.recent(limit=10) if e.status == "rejected"]
    assert rejected and "compacting" in rejected[0].gist


def test_registry_reports_transitions(paths):
    with RunningRouter(paths) as rr:
        client = rr.client(session_id="s1")
        reg = Registry.for_client(client)
        reg.register_start("s1", "claude", cwd="/w", pid=111)
        reg.mark("s1", STATE_IDLE)
        reg.mark("s1", STATE_WORKING)
        reg.set_last_user_message("s1", "do the thing")

        roster = client.call("roster", {})
        entry = next(s for s in roster["sessions"] if s["id"] == "s1")
        assert entry["state"] == STATE_WORKING
        assert entry["last_user_message"] == "do the thing"

        reg.mark_offline("s1")
        roster = client.call("roster", {})
        entry = next(s for s in roster["sessions"] if s["id"] == "s1")
        assert entry["state"] == STATE_OFFLINE


def test_sweep_stale_offlines_dead_sessions(paths):
    clock = FrozenClock()
    store = Store.open(paths, now=clock.now)
    store.upsert_session("live", "claude", state=STATE_IDLE, reachable=True)
    store.upsert_session("dead", "codex", state=STATE_IDLE, reachable=False)
    clock.advance(1000)
    offlined = sweep_stale(store, ttl_s=300, now=clock.now())
    assert offlined == ["dead"]
    assert store.get_session("dead").state == STATE_OFFLINE
    assert store.get_session("live").state == STATE_IDLE  # reachable, spared
    store.close()


def test_register_unmanaged(paths):
    store = Store.open(paths)
    register_unmanaged(store, [{"id": "u1", "family": "codex", "cwd": "/tmp"}])
    s = store.get_session("u1")
    assert s.is_managed is False and s.reachable is False
    # excluded from default listing
    assert store.list_sessions() == []
    store.close()
