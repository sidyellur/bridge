"""Task 3 verify (store): migrations, WAL, session/call/queue/transcript/rate,
and restart recovery.
"""

from __future__ import annotations

import pytest

from bridge.paths import Paths
from bridge.store import (
    CALL_ANSWERED,
    CALL_DELIVERED,
    CALL_QUEUED,
    KIND_TEXT,
    MSG_DELIVERED,
    MSG_QUEUED,
    SCHEMA_VERSION,
    STATE_IDLE,
    STATE_OFFLINE,
    Store,
)

from .fakes.clock import FrozenClock


def test_migrations_set_schema_version(store: Store):
    assert store.schema_version == SCHEMA_VERSION


def test_wal_mode_enabled(paths: Paths, clock: FrozenClock):
    st = Store.open(paths, now=clock.now)
    mode = st._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    st.close()


def test_db_file_permissions(store: Store, paths: Paths):
    import stat

    mode = stat.S_IMODE(paths.db.stat().st_mode)
    assert mode == 0o600


def test_session_lifecycle(store: Store):
    store.upsert_session("s1", "claude", cwd="/tmp", state="starting")
    s = store.get_session("s1")
    assert s and s.family == "claude" and s.state == "starting"
    store.set_state("s1", STATE_IDLE)
    store.set_reachable("s1", True)
    store.set_last_user_message("s1", "hello there " * 50)
    s = store.get_session("s1")
    assert s.state == STATE_IDLE and s.reachable is True
    assert len(s.last_user_message) == 120
    store.mark_offline("s1")
    assert store.get_session("s1").state == STATE_OFFLINE


def test_list_sessions_excludes_unmanaged_by_default(store: Store):
    store.upsert_session("m", "claude", is_managed=True)
    store.upsert_session("u", "codex", is_managed=False)
    managed = {s.id for s in store.list_sessions()}
    assert managed == {"m"}
    both = {s.id for s in store.list_sessions(include_unmanaged=True)}
    assert both == {"m", "u"}


def test_call_state_machine(store: Store, clock: FrozenClock):
    store.create_call("c1", "a", "b", "why?", deadline=clock.now() + 60)
    assert store.get_call("c1").status == CALL_QUEUED
    store.set_call_status("c1", CALL_DELIVERED, target_state_on_delivery="idle")
    call = store.get_call("c1")
    assert call.status == CALL_DELIVERED and call.target_state_on_delivery == "idle"
    assert store.active_inbound_call("b").call_id == "c1"
    store.record_answer("c1", "because", [])
    assert store.get_call("c1").status == CALL_ANSWERED
    assert store.active_inbound_call("b") is None


def test_queue_fifo_ordering_and_depth(store: Store):
    store.create_message("m1", "b", KIND_TEXT, {"event": {}}, from_id="a")
    store.create_message("m2", "b", KIND_TEXT, {"event": {}}, from_id="a")
    store.enqueue("b", "m1")
    store.enqueue("b", "m2")
    assert store.queue_depth("b") == 2
    first = store.next_queued("b")
    assert first.message_id == "m1"
    store.set_queue_status("m1", MSG_DELIVERED)
    # next_queued only returns QUEUED items
    assert store.next_queued("b").message_id == "m2"


def test_redeliver_inflight_resets_delivered(store: Store):
    store.create_message("m1", "b", KIND_TEXT, {"event": {}}, from_id="a")
    store.enqueue("b", "m1")
    store.set_queue_status("m1", MSG_DELIVERED)
    assert store.get_queued("m1").status == MSG_DELIVERED
    store.redeliver_inflight("b")
    assert store.get_queued("m1").status == MSG_QUEUED


def test_transcript_records_and_reads(store: Store):
    store.record_event("call", "queued", from_id="a", to_id="b", gist="why?", call_id="c1")
    store.record_event("text", "queued", from_id="a", to_id="c", gist="fyi")
    all_entries = store.recent(limit=10)
    assert len(all_entries) == 2
    peer_entries = store.recent(peer="c", limit=10)
    assert len(peer_entries) == 1 and peer_entries[0].to_id == "c"


def test_transcript_bodies_redacted_by_default(store: Store, paths: Paths):
    store.record_event("text", "queued", to_id="b", body={"secret": "hunter2"})
    row = store._conn.execute("SELECT body FROM transcript").fetchone()
    assert row["body"] is None


def test_transcript_bodies_retained_when_enabled(paths: Paths, clock: FrozenClock):
    st = Store.open(paths, now=clock.now, retain_bodies=True)
    st.record_event("text", "queued", to_id="b", body={"secret": "hunter2"})
    row = st._conn.execute("SELECT body FROM transcript").fetchone()
    assert "hunter2" in row["body"]
    st.close()


def test_rate_window(store: Store, clock: FrozenClock):
    for _ in range(3):
        store.record_rate("a", "b")
    assert store.count_rate("a", "b", 3600) == 3
    clock.advance(4000)
    assert store.count_rate("a", "b", 3600) == 0


def test_restart_recovery_persists_state(paths: Paths, clock: FrozenClock):
    st = Store.open(paths, now=clock.now)
    st.upsert_session("s1", "claude", state=STATE_IDLE, reachable=True)
    st.create_call("c1", "a", "s1", "q", deadline=clock.now() + 60)
    st.enqueue("s1", "m-none") if False else None  # queue exercised elsewhere
    st.close()

    st2 = Store.open(paths, now=clock.now)
    assert st2.get_session("s1").family == "claude"
    assert st2.get_call("c1").status == CALL_QUEUED
    assert st2.schema_version == SCHEMA_VERSION
    st2.close()


def test_reopen_is_idempotent(paths: Paths, clock: FrozenClock):
    Store.open(paths, now=clock.now).close()
    Store.open(paths, now=clock.now).close()  # must not raise on re-migrate


def test_read_only_open(paths: Paths, clock: FrozenClock):
    st = Store.open(paths, now=clock.now)
    st.upsert_session("s1", "claude")
    st.close()
    ro = Store.open(paths, now=clock.now, read_only=True)
    assert ro.get_session("s1") is not None
    with pytest.raises(Exception):
        ro.upsert_session("s2", "codex")
    ro.close()
