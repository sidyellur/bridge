"""Task 4 verify (registry): state transitions, stale sweep, unmanaged records."""

from __future__ import annotations

from bridge.registry import (
    Registry,
    is_valid_transition,
    register_unmanaged,
    sweep_stale,
)
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
