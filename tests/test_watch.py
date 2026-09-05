"""Issue #5 part B verify (``bridge watch``): the pure frame renderer, the
injected-I/O loop, and read-only access while the router is live.
"""

from __future__ import annotations

import io

from bridge.cli import build_parser
from bridge.store import (
    CALL_DELIVERED,
    KIND_TEXT,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_WORKING,
    Store,
)
from bridge.watch import build_frame, open_read_only, run_watch

from .fakes.clock import FrozenClock
from .fakes.router_peer import RunningRouter


def _seed(store: Store, clock: FrozenClock) -> None:
    """A store with sessions in several states, a queue, a call, and history."""
    store.upsert_session("sess-idle", "claude", cwd="/w/one", state=STATE_IDLE, reachable=True)
    store.upsert_session("sess-busy", "codex", cwd="/w/two", state=STATE_WORKING, reachable=True)
    store.upsert_session("sess-gone", "claude", cwd="/w/three", state=STATE_OFFLINE)
    store.upsert_session("sess-wild", "codex", state=STATE_IDLE, is_managed=False)

    store.create_message("m1", "sess-busy", KIND_TEXT, {"event": {}}, from_id="sess-idle")
    store.create_message("m2", "sess-busy", KIND_TEXT, {"event": {}}, from_id="sess-idle")
    store.enqueue("sess-busy", "m1")
    store.enqueue("sess-busy", "m2")

    store.create_call("c1", "sess-idle", "sess-busy", "why the refactor?", clock.now() + 60)
    store.set_call_status("c1", CALL_DELIVERED, target_state_on_delivery=STATE_IDLE)

    store.record_event(
        "call", "queued", from_id="sess-idle", to_id="sess-busy", gist="why?", call_id="c1"
    )
    clock.advance(1)
    store.record_event("text", "delivered", from_id="sess-idle", to_id="sess-busy", gist="fyi")


# --- build_frame -----------------------------------------------------------


def test_build_frame_renders_every_section(store: Store, clock: FrozenClock):
    _seed(store, clock)
    frame = build_frame(store)

    assert "SESSIONS (4)" in frame
    # roster: id, family, state, reachability
    assert "sess-idle" in frame and "claude" in frame and STATE_IDLE in frame
    assert "sess-busy" in frame and "codex" in frame and STATE_WORKING in frame
    assert "sess-gone" in frame and STATE_OFFLINE in frame
    assert "/w/one" in frame
    # unmanaged sessions are shown but flagged
    assert "! sess-wild" in frame

    # queue depth is per target: 2 pending for sess-busy, 0 for the rest
    busy_row = next(ln for ln in frame.splitlines() if ln.strip().startswith("sess-busy"))
    assert busy_row.split()[4] == "2"
    idle_row = next(ln for ln in frame.splitlines() if ln.strip().startswith("sess-idle"))
    assert idle_row.split()[4] == "0"

    # active inbound call
    assert "ACTIVE CALLS (1)" in frame
    assert "c1" in frame and CALL_DELIVERED in frame
    assert "sess-idle -> sess-busy" in frame
    assert "why the refactor?" in frame

    # transcript tail, oldest first
    assert "TRANSCRIPT (last 2)" in frame
    body = frame.split("TRANSCRIPT")[1]
    assert body.index("why?") < body.index("fyi")


def test_build_frame_on_empty_store(store: Store):
    frame = build_frame(store)
    assert "SESSIONS (0)" in frame
    assert "(no sessions)" in frame
    assert "(no calls in flight)" in frame
    assert "(no activity)" in frame


def test_build_frame_honors_limit(store: Store, clock: FrozenClock):
    for i in range(8):
        store.record_event("text", "queued", from_id="a", to_id="b", gist=f"msg-{i}")
        clock.advance(1)
    frame = build_frame(store, limit=3)
    assert "TRANSCRIPT (last 3)" in frame
    assert "msg-7" in frame and "msg-0" not in frame


def test_build_frame_only_shows_calls_in_flight(store: Store, clock: FrozenClock):
    store.create_call("c1", "a", "b", "q", clock.now() + 60)
    store.record_answer("c1", "done", [])
    assert "ACTIVE CALLS (0)" in build_frame(store)


# --- run_watch -------------------------------------------------------------


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_run_watch_once_renders_exactly_one_frame(paths, clock):
    store = Store.open(paths, now=clock.now)
    _seed(store, clock)
    store.close()

    out = io.StringIO()
    sleeps = _Sleeps()
    rc = run_watch(
        paths, once=True, stdin=io.StringIO(""), stdout=out, sleep=sleeps, interval_s=0.01
    )
    assert rc == 0
    text = out.getvalue()
    assert text.count("SESSIONS (4)") == 1
    assert text.startswith("\x1b[H\x1b[2J")
    assert sleeps.calls == []  # `--once` never waits


def test_run_watch_quits_on_q(paths, clock):
    Store.open(paths, now=clock.now).close()
    out = io.StringIO()
    sleeps = _Sleeps()
    rc = run_watch(paths, stdin=io.StringIO("\nq\n"), stdout=out, sleep=sleeps, interval_s=0.25)
    assert rc == 0
    # blank line -> redraw, then 'q' -> quit: exactly two frames, two waits
    assert out.getvalue().count("SESSIONS") == 2
    assert sleeps.calls == [0.25, 0.25]


def test_run_watch_quits_on_eof(paths, clock):
    Store.open(paths, now=clock.now).close()
    out = io.StringIO()
    rc = run_watch(paths, stdin=io.StringIO(""), stdout=out, sleep=_Sleeps(), interval_s=0.01)
    assert rc == 0
    assert out.getvalue().count("SESSIONS") == 1


def test_run_watch_works_without_a_router_ever_running(paths, clock):
    """No daemon, no database yet: the view still renders an empty frame."""
    assert not paths.db.exists()
    out = io.StringIO()
    assert run_watch(paths, once=True, stdin=io.StringIO(""), stdout=out, sleep=_Sleeps()) == 0
    assert "(no sessions)" in out.getvalue()


def test_watch_reads_while_router_is_live(paths, clock, ids):
    """The read-only open must not contend with the daemon's writes."""
    with RunningRouter(paths, now=clock.now, new_id=ids.new) as rr:
        client = rr.client(session_id="ctrl")
        client.call(
            "register_session",
            {"session_id": "live-1", "family": "claude", "state": STATE_IDLE, "cwd": "/w"},
        )
        out = io.StringIO()
        assert run_watch(paths, once=True, stdin=io.StringIO(""), stdout=out, sleep=_Sleeps()) == 0
        frame = out.getvalue()
        assert "live-1" in frame
        assert "SESSIONS (1)" in frame

        # the daemon keeps writing happily while the reader is open
        store = open_read_only(paths)
        try:
            client.call(
                "register_session",
                {"session_id": "live-2", "family": "codex", "state": STATE_IDLE},
            )
            assert "live-2" in build_frame(store)
        finally:
            store.close()


def test_read_only_store_rejects_writes(paths, clock):
    Store.open(paths, now=clock.now).close()
    store = open_read_only(paths)
    try:
        try:
            store.upsert_session("nope", "claude")
        except Exception as exc:  # sqlite3.OperationalError: readonly database
            assert "readonly" in str(exc).lower()
        else:
            raise AssertionError("read-only store accepted a write")
    finally:
        store.close()


# --- CLI wiring ------------------------------------------------------------


def test_cli_parses_watch_flags():
    args = build_parser().parse_args(["watch", "--interval", "2.5", "--once"])
    assert args.command == "watch" and args.interval == 2.5 and args.once is True
    defaults = build_parser().parse_args(["watch"])
    assert defaults.interval == 0.5 and defaults.once is False


def test_cli_watch_command_renders(paths, monkeypatch, capsys):
    from bridge.cli import main

    monkeypatch.setenv("BRIDGE_HOME", str(paths.home))
    Store.open(paths).close()
    assert main(["watch", "--once"]) == 0
    assert "SESSIONS" in capsys.readouterr().out
