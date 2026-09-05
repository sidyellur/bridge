"""Post-v1 C (transcript retention/pruning): what the sweep removes, what it
must never remove, the once-per-interval tick, the ``prune`` router op, and the
CLI's duration parsing.

Everything here runs on the injected frozen clock -- no sleeps.
"""

from __future__ import annotations

import pytest

from bridge.cli import main
from bridge.paths import Paths
from bridge.retention import format_duration, parse_duration, render_prune
from bridge.router import Router, RouterConfig, RouterError
from bridge.store import (
    CALL_ANSWERED,
    CALL_BLOCKED,
    CALL_DELIVERED,
    CALL_QUEUED,
    CALL_TIMEOUT,
    CALL_UNREACHABLE,
    KIND_CALL,
    KIND_TEXT,
    MSG_DELIVERED,
    MSG_PROCESSED,
    MSG_QUEUED,
    STATE_IDLE,
    Store,
)

from .fakes.clock import FrozenClock
from .fakes.router_peer import RunningRouter

DAY = 86400.0


@pytest.fixture
def cli_env(paths, monkeypatch):
    monkeypatch.setenv("BRIDGE_HOME", str(paths.home))
    monkeypatch.delenv("BRIDGE_SESSION_ID", raising=False)
    return paths


def _resolved_call(store: Store, call_id: str, status: str, *, acked: bool = True) -> None:
    """A finished call plus the message/queue rows a real call leaves behind."""
    store.create_call(call_id, "a", "b", "q", deadline=store._now() + 60, status=CALL_QUEUED)
    store.create_message(call_id, "b", KIND_CALL, {"call_id": call_id}, call_id=call_id)
    store.enqueue("b", call_id)
    store.set_queue_status(call_id, MSG_PROCESSED if acked else MSG_QUEUED)
    store.set_call_status(call_id, status)


def _counts(store: Store) -> dict[str, int]:
    return {
        table: store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("transcript", "rate_events", "calls", "messages", "queue")
    }


# --- Store.prune ------------------------------------------------------------


def test_prune_cuts_transcript_and_rate_events_by_age(store: Store, clock: FrozenClock):
    store.record_event(KIND_TEXT, "queued", from_id="a", to_id="b", gist="old")
    store.record_rate("a", "b")
    clock.advance(10 * DAY)
    store.record_event(KIND_TEXT, "queued", from_id="a", to_id="b", gist="new")
    store.record_rate("a", "b")

    counts = store.prune(5 * DAY)
    assert counts["transcript"] == 1
    assert counts["rate_events"] == 1
    remaining = store.recent(limit=10)
    assert [e.gist for e in remaining] == ["new"]
    assert store.count_rate("a", "b", 3600) == 1


def test_prune_keeps_rows_newer_than_the_cutoff(store: Store, clock: FrozenClock):
    store.record_event(KIND_TEXT, "queued", gist="keep")
    clock.advance(DAY)
    assert store.prune(30 * DAY) == {
        "transcript": 0,
        "rate_events": 0,
        "calls": 0,
        "messages": 0,
        "queue": 0,
    }
    assert len(store.recent(limit=10)) == 1


def test_prune_boundary_is_strictly_older(store: Store, clock: FrozenClock):
    """A row exactly at the cutoff is not yet older than the window."""
    store.record_event(KIND_TEXT, "queued", gist="edge")
    clock.advance(30 * DAY)
    assert store.prune(30 * DAY)["transcript"] == 0
    clock.advance(0.001)
    assert store.prune(30 * DAY)["transcript"] == 1


@pytest.mark.parametrize("status", [CALL_ANSWERED, CALL_BLOCKED, CALL_TIMEOUT, CALL_UNREACHABLE])
def test_prune_removes_resolved_calls_with_their_rows(
    store: Store, clock: FrozenClock, status: str
):
    _resolved_call(store, "c1", status)
    clock.advance(31 * DAY)
    counts = store.prune(30 * DAY)
    assert counts["calls"] == 1
    assert counts["messages"] == 1
    assert counts["queue"] == 1
    assert store.get_call("c1") is None
    assert _counts(store)["queue"] == 0


@pytest.mark.parametrize("status", [CALL_QUEUED, CALL_DELIVERED, "answering"])
def test_prune_never_touches_unresolved_calls(store: Store, clock: FrozenClock, status: str):
    _resolved_call(store, "c1", status, acked=False)
    clock.advance(365 * DAY)
    counts = store.prune(DAY)
    assert counts["calls"] == 0
    assert store.get_call("c1") is not None
    assert _counts(store)["queue"] == 1


@pytest.mark.parametrize("queue_status", [MSG_QUEUED, MSG_DELIVERED])
def test_prune_never_touches_undelivered_queue_entries(
    store: Store, clock: FrozenClock, queue_status: str
):
    """A resolved call still owing its target an event keeps all of its rows."""
    _resolved_call(store, "c1", CALL_ANSWERED, acked=False)
    store.set_queue_status("c1", queue_status)
    clock.advance(365 * DAY)
    counts = store.prune(DAY)
    assert counts == {
        "transcript": 0,
        "rate_events": 0,
        "calls": 0,
        "messages": 0,
        "queue": 0,
    }
    assert store.get_call("c1") is not None
    assert store.get_queued("c1") is not None


def test_prune_leaves_recent_resolved_calls_alone(store: Store, clock: FrozenClock):
    _resolved_call(store, "c1", CALL_ANSWERED)
    clock.advance(DAY)
    assert store.prune(30 * DAY)["calls"] == 0
    assert store.get_call("c1") is not None


def test_prune_leaves_sessions_alone(store: Store, clock: FrozenClock):
    store.upsert_session("s1", "claude", state=STATE_IDLE)
    clock.advance(365 * DAY)
    store.prune(DAY)
    assert store.get_session("s1") is not None


def test_prune_is_idempotent(store: Store, clock: FrozenClock):
    store.record_event(KIND_TEXT, "queued", gist="old")
    _resolved_call(store, "c1", CALL_ANSWERED)
    clock.advance(31 * DAY)
    first = store.prune(30 * DAY)
    assert first["transcript"] == 1 and first["calls"] == 1
    second = store.prune(30 * DAY)
    assert second == {
        "transcript": 0,
        "rate_events": 0,
        "calls": 0,
        "messages": 0,
        "queue": 0,
    }


def test_prune_handles_many_calls(store: Store, clock: FrozenClock):
    """More calls than one SQLite parameter batch, to exercise chunking."""
    for i in range(900):
        _resolved_call(store, f"c{i}", CALL_ANSWERED)
    clock.advance(31 * DAY)
    counts = store.prune(30 * DAY)
    assert counts["calls"] == 900
    assert _counts(store)["messages"] == 0


# --- router tick ------------------------------------------------------------


def test_tick_prunes_at_most_once_per_interval(paths: Paths, clock: FrozenClock, ids):
    # A retention window shorter than the sweep interval lets a row be
    # over-age while the sweep is still on cooldown -- the case that proves
    # tick() is rate-limited rather than pruning on every pass.
    r = Router.create(
        paths,
        now=clock.now,
        new_id=ids.new,
        config=RouterConfig(retention_s=60.0, prune_interval_s=7 * DAY),
    )
    try:
        r.store.record_event(KIND_TEXT, "queued", gist="first")
        clock.advance(120)
        r.tick()  # the first tick always sweeps and starts the timer
        assert r.store.recent(limit=10) == []

        r.store.record_event(KIND_TEXT, "queued", gist="second")
        clock.advance(120)  # past retention, but well inside the interval
        r.tick()
        assert [e.gist for e in r.store.recent(limit=10)] == ["second"]

        clock.advance(7 * DAY)  # interval elapsed -> the next tick sweeps
        r.tick()
        assert r.store.recent(limit=10) == []
    finally:
        r.close()


def test_maybe_prune_returns_none_inside_the_interval(paths: Paths, clock: FrozenClock, ids):
    r = Router.create(
        paths, now=clock.now, new_id=ids.new, config=RouterConfig(prune_interval_s=3600.0)
    )
    try:
        assert r.maybe_prune() is not None  # first sweep
        assert r.maybe_prune() is None
        clock.advance(3599)
        assert r.maybe_prune() is None
        clock.advance(2)
        assert r.maybe_prune() is not None
    finally:
        r.close()


def test_tick_prune_does_not_disturb_live_calls(router_core, clock: FrozenClock):
    r = router_core
    r.config = RouterConfig(retention_s=DAY, prune_interval_s=3600.0)
    r.notifier.connected_ids.add("b")
    r.store.upsert_session("b", "claude", state=STATE_IDLE, reachable=True)
    r.dispatch("call", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    call_id = r.notifier.delivered[0][1]["call_id"]

    r.tick()
    assert r.store.get_call(call_id) is not None
    out, _ = r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "ok"})
    assert out == "respond"


# --- prune op ---------------------------------------------------------------


def test_prune_op_uses_configured_retention(router_core, clock: FrozenClock):
    r = router_core
    r.config = RouterConfig(retention_s=DAY)
    r.store.record_event(KIND_TEXT, "queued", gist="old")
    clock.advance(2 * DAY)
    _, result = r.dispatch("prune", {})
    assert result["older_than_s"] == DAY
    assert result["pruned"]["transcript"] == 1
    # The explicit sweep leaves its own audit line behind.
    assert [e.status for e in r.store.recent(limit=5)] == ["pruned"]


def test_prune_op_accepts_a_duration_string(router_core, clock: FrozenClock):
    r = router_core
    r.store.record_event(KIND_TEXT, "queued", gist="old")
    clock.advance(8 * DAY)
    r.store.record_event(KIND_TEXT, "queued", gist="new")
    _, result = r.dispatch("prune", {"older_than_s": "7d"})
    assert result["older_than_s"] == 7 * DAY
    assert result["pruned"]["transcript"] == 1


def test_prune_op_rejects_a_bad_duration(router_core):
    with pytest.raises(RouterError) as exc:
        router_core.dispatch("prune", {"older_than_s": "next tuesday"})
    assert exc.value.code == "bad_request"


def test_prune_op_resets_the_tick_interval(router_core, clock: FrozenClock):
    r = router_core
    r.dispatch("prune", {})
    assert r.maybe_prune() is None


def test_prune_op_over_the_socket(paths: Paths, clock: FrozenClock, ids):
    with RunningRouter(paths, now=clock.now, new_id=ids.new) as rr:
        client = rr.client(session_id="s1")
        result = client.call("prune", {"older_than_s": "1d"})
        assert result["older_than_s"] == DAY
        assert set(result["pruned"]) == {
            "transcript",
            "rate_events",
            "calls",
            "messages",
            "queue",
        }


# --- duration parsing / rendering -------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("7d", 7 * DAY),
        ("12h", 43200.0),
        ("30m", 1800.0),
        ("45s", 45.0),
        ("2w", 14 * DAY),
        ("3600", 3600.0),
        ("1.5h", 5400.0),
        (" 7D ", 7 * DAY),
        (900, 900.0),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "7 days", "d7", "-5", "-1d", "1x"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_format_duration():
    assert format_duration(30 * DAY) == "30d"
    assert format_duration(3600) == "1h"
    assert format_duration(1800) == "30m"
    assert format_duration(45) == "45s"


def test_render_prune_lists_every_table():
    out = render_prune({"pruned": {"transcript": 3}, "older_than_s": 7 * DAY})
    assert "7d" in out
    assert "transcript=3" in out
    assert "rate_events=0" in out


# --- CLI --------------------------------------------------------------------


def test_cli_prune_through_the_router(cli_env, capsys, clock: FrozenClock, ids):
    with RunningRouter(cli_env, now=clock.now, new_id=ids.new) as rr:
        rr.client(session_id="s1").call(
            "register_session", {"session_id": "s1", "family": "claude", "state": "idle"}
        )
        # Stay well inside the router's idle grace: advance seconds, not days.
        clock.advance(2)
        assert main(["transcript", "--prune", "--older-than", "1s"]) == 0
        out = capsys.readouterr().out
        assert "transcript=1" in out
        assert "router not running" not in out


def test_cli_prune_without_a_router_opens_the_store_directly(cli_env, capsys):
    """Documented exception: no daemon means no second writer to contend with."""
    store = Store.open(cli_env)
    store.record_event(KIND_TEXT, "queued", gist="ancient")
    store._conn.execute("UPDATE transcript SET ts = 0")
    store.close()

    assert main(["transcript", "--prune", "--older-than", "1d"]) == 0
    out = capsys.readouterr().out
    assert "transcript=1" in out
    assert "router not running" in out

    reopened = Store.open(cli_env)
    assert reopened.recent(limit=5) == []
    reopened.close()


def test_cli_prune_rejects_a_bad_duration(cli_env, capsys):
    assert main(["transcript", "--prune", "--older-than", "whenever"]) == 2
    assert "could not read duration" in capsys.readouterr().out


def test_cli_transcript_without_prune_is_unchanged(cli_env, capsys):
    assert main(["transcript"]) == 1
    assert "not running" in capsys.readouterr().out
