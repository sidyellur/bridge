"""Task 5 verify: the Codex App Server adapter against the real-contract fake.

Proves the adapter binds the thread the remote TUI owns (from the global
``thread/started`` broadcast *and* from a ``thread/loaded/list`` walk for a
thread that predates Bridge), subscribes to it on its own worker thread,
records what it knows in the session meta, reflects thread status into Bridge
state, answers a call from the final agent message only when it actually has
the subscriber-only stream, and re-binds the same thread after an App Server
disconnect. No forbidden method is ever used.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from bridge.adapters.codex import CodexAdapter
from bridge.codex_app_server import (
    PINNED_CODEX_VERSION,
    SYSTEM_ERROR_MESSAGE,
    CodexAppServerClient,
)
from bridge.store import Store

from .fakes.codex_app_server import FakeCodexAppServer
from .fakes.router_peer import RunningRouter

CWD = "/tmp/peer"
THREAD = "thread-abc"


@pytest.fixture
def codex_factory(paths):
    created: list = []
    rr = RunningRouter(paths).__enter__()

    def make(
        session_id,
        *,
        agent_message="",
        fallback=False,
        auto_complete=True,
        resume_result="ok",
        codex_version=PINNED_CODEX_VERSION,
        eager_thread=True,
    ):
        client_sock, server_sock = socket.socketpair()
        server = FakeCodexAppServer(
            server_sock,
            codex_version=codex_version,
            cwd=CWD,
            agent_message=agent_message,
            auto_complete=auto_complete,
            resume_result=resume_result,
            eager_thread=eager_thread,
        )
        app = CodexAppServerClient(client_sock, cwd=CWD).start()
        adapter = CodexAdapter(
            session_id, app, final_message_fallback=fallback, cwd=CWD, paths=paths
        )
        adapter.connect_router(
            lambda on_event: rr.client(session_id=session_id, role="adapter", on_event=on_event)
        )
        created.append((adapter, server))
        return adapter, server

    try:
        yield rr, make
    finally:
        for adapter, server in created:
            adapter._reconnect = None
            try:
                adapter.close()
            except Exception:
                pass
            try:
                server.close()
            except Exception:
                pass
        rr.__exit__(None, None, None)


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _entry(client, sid):
    roster = client.call("roster", {})
    return next((s for s in roster["sessions"] if s["id"] == sid), None)


def _wait_reachable(client, sid, timeout=3.0):
    return _wait(
        lambda: (e := _entry(client, sid)) is not None and e["reachable"] and e["state"] == "idle",
        timeout,
    )


def _wait_subscribed(server, thread_id=THREAD, timeout=3.0):
    return _wait(lambda: thread_id in server.subscribed, timeout)


def _meta(paths, sid) -> dict:
    try:
        return json.loads(paths.session_meta(sid).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _vendor_session_id(paths, sid) -> str | None:
    store = Store.open(paths, read_only=True)
    try:
        session = store.get_session(sid)
        return session.vendor_session_id if session is not None else None
    finally:
        store.close()


# --- binding ---------------------------------------------------------------


def test_thread_binding_and_reachable(codex_factory, paths):
    rr, make = codex_factory
    adapter, _server = make("codex-1")
    adapter.start()
    ctrl = rr.client(session_id="ctrl")

    assert _wait_reachable(ctrl, "codex-1")
    assert _wait(lambda: _vendor_session_id(paths, "codex-1") == THREAD)


def test_binds_a_thread_that_existed_before_bridge_connected(codex_factory, paths):
    rr, make = codex_factory
    adapter, server = make("codex-1", eager_thread=False)
    # No `thread/started` is ever broadcast for this thread, so the only route
    # to it is the `thread/loaded/list` + `thread/read` walk in bind_thread().
    server.threads.append(
        {"id": THREAD, "cwd": CWD, "createdAt": 1, "status": {"type": "idle"}}
    )

    adapter.start()

    assert adapter.app.thread_id == THREAD
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")
    assert _wait(lambda: _vendor_session_id(paths, "codex-1") == THREAD)
    assert _wait_subscribed(server)


# --- session meta ----------------------------------------------------------


def test_session_meta_records_thread_id_subscribed_and_codex_version(codex_factory, paths):
    _rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()

    assert _wait_subscribed(server)
    assert _wait(lambda: _meta(paths, "codex-1").get("subscribed") is True)
    meta = _meta(paths, "codex-1")
    assert meta["thread_id"] == THREAD
    assert meta["subscribed"] is True
    assert meta["codex_version"] == "0.151.0"
    assert "codex_version_warning" not in meta


def test_session_meta_records_subscribed_false_when_resume_is_broken(codex_factory, paths):
    _rr, make = codex_factory
    adapter, server = make("codex-1", resume_result="unsupported")
    adapter.start()

    assert _wait(lambda: "last_thread_error" in _meta(paths, "codex-1"))
    meta = _meta(paths, "codex-1")
    assert meta["thread_id"] == THREAD
    assert meta["subscribed"] is False
    assert "list_turns is not supported yet" in meta["last_thread_error"]
    assert server.subscribed == set()


# --- inbound delivery ------------------------------------------------------


def test_inbound_call_starts_turn_with_envelope(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    caller.call("call_async", {"to": "codex-1", "question": "ship it?"})

    assert _wait(lambda: bool(server.turns)), "adapter did not start a turn"
    text = server.turns[0]["input"][0]["text"]
    assert "[bridge call]" in text and "ship it?" in text
    assert server.turns[0]["threadId"] == THREAD
    assert server.forbidden_calls == []  # never steered


def test_working_status_holds_delivery(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    server.emit_status("active")
    assert _wait(lambda: _entry(caller, "codex-1")["state"] == "working")

    before = len(server.turns)
    caller.call("text", {"to": "codex-1", "message": "fyi"})
    time.sleep(0.3)
    assert len(server.turns) == before  # held while working
    assert server.forbidden_calls == []


def test_system_error_status_records_the_error_and_stays_idle(codex_factory, paths):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")

    server.emit_status("systemError")

    assert _wait(lambda: _meta(paths, "codex-1").get("last_thread_error") == SYSTEM_ERROR_MESSAGE)
    assert _entry(ctrl, "codex-1")["state"] == "idle"
    assert _entry(ctrl, "codex-1")["reachable"] is True


# --- the final-message fallback --------------------------------------------


def test_final_message_fallback_answers_caller(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1", agent_message="yes, ship it", fallback=True)
    adapter.start()
    assert _wait_subscribed(server)
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    result = caller.call("call", {"to": "codex-1", "question": "ship it?"}, timeout=8)

    assert result["status"] == "answered"
    assert result["answer"] == "yes, ship it"
    assert result["meta"]["answered_by"] == "live-session"
    assert result["meta"]["via"] == "final-message"
    assert server.forbidden_calls == []


def test_no_fallback_when_not_subscribed(codex_factory):
    rr, make = codex_factory
    adapter, server = make(
        "codex-1", agent_message="yes, ship it", fallback=True, resume_result="unsupported"
    )
    adapter.start()
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    # `turn/*`/`item/*` are subscriber-only, so with a refused subscribe there
    # is no final message to answer from -- the call must time out, never be
    # answered from nothing.
    result = caller.call(
        "call", {"to": "codex-1", "question": "ship it?", "timeout_s": 1}, timeout=10
    )

    assert result["status"] == "timeout"
    assert not result.get("answer")
    assert server.turns, "the turn was still started"
    assert server.forbidden_calls == []


def test_mcp_reply_path_wins_over_fallback(codex_factory):
    rr, make = codex_factory
    adapter, server = make(
        "codex-1", agent_message="stale fallback", fallback=True, auto_complete=False
    )
    adapter.start()
    assert _wait_subscribed(server)
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    mcp = rr.client(session_id="codex-1")
    result_box = {}

    def do_call():
        result_box["result"] = caller.call(
            "call", {"to": "codex-1", "question": "ship it?"}, timeout=8
        )

    t = threading.Thread(target=do_call)
    t.start()

    call_id = None
    deadline = time.time() + 3
    while time.time() < deadline:
        tx = caller.call("transcript", {"limit": 20})
        delivered = [e for e in tx["entries"] if e["kind"] == "call" and e["status"] == "delivered"]
        if delivered:
            call_id = delivered[0]["call_id"]
            break
        time.sleep(0.02)
    assert call_id is not None
    mcp.call("reply", {"call_id": call_id, "answer": "fresh MCP answer"})
    server.complete_turn()  # fallback now fires but is rejected as already answered
    t.join(timeout=8)

    assert result_box["result"]["answer"] == "fresh MCP answer"
    assert result_box["result"]["meta"]["via"] == "tool"


# --- disconnect + reconnect ------------------------------------------------


def test_app_server_crash_marks_unreachable_and_records_disconnected(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")

    # Simulate a crash: the RPC connection drops out from under the adapter,
    # not a deliberate adapter.close() -- never a headless answer.
    server.close()

    assert _wait(lambda: _entry(ctrl, "codex-1")["reachable"] is False)
    tx = ctrl.call("transcript", {"limit": 20})
    assert any(
        e["kind"] == "session" and e["status"] == "disconnected" and e["to"] == "codex-1"
        for e in tx["entries"]
    )


def test_reconnect_rebinds_the_same_thread_and_resubscribes(codex_factory, paths):
    rr, make = codex_factory
    adapter, server = make("codex-1")

    spawned: list[FakeCodexAppServer] = []

    def reconnect():
        client_sock, server_sock = socket.socketpair()
        # The same App Server socket comes back serving the same thread: a
        # client disconnect never closed it.
        fresh = FakeCodexAppServer(server_sock, cwd=CWD)
        spawned.append(fresh)
        return CodexAppServerClient(client_sock, cwd=CWD).start()

    adapter._reconnect = reconnect
    adapter._reconnect_sleep = lambda _s: None  # no real waiting in tests
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")
    assert _wait_subscribed(server)

    server.close()  # transient crash

    assert _wait(lambda: bool(spawned)), "adapter never attempted to reconnect"
    assert _wait_reachable(ctrl, "codex-1")
    assert _wait_subscribed(spawned[0]), "adapter did not re-subscribe the re-bound thread"
    assert adapter.app.thread_id == THREAD
    assert _vendor_session_id(paths, "codex-1") == THREAD
    assert _meta(paths, "codex-1")["thread_id"] == THREAD
    assert _meta(paths, "codex-1")["subscribed"] is True

    adapter._reconnect = None
    for fake in spawned:
        fake.close()


def test_reconnect_gives_up_after_exhausting_attempts(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")

    attempts = {"n": 0}

    def reconnect():
        attempts["n"] += 1
        raise OSError("still dead")

    adapter._reconnect = reconnect
    adapter._reconnect_sleep = lambda _s: None
    adapter._reconnect_attempts = 3
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")

    server.close()

    assert _wait(lambda: attempts["n"] >= 3)
    assert attempts["n"] == 3
    assert _entry(ctrl, "codex-1")["reachable"] is False


def test_a_rebind_to_a_new_tui_thread_updates_the_router_and_meta(codex_factory, paths):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")
    assert _wait_subscribed(server)

    # The operator restarted the TUI in the same cwd: last thread wins.
    server.start_thread("thread-def", CWD)

    assert _wait(lambda: _vendor_session_id(paths, "codex-1") == "thread-def")
    assert _wait(lambda: _meta(paths, "codex-1").get("thread_id") == "thread-def")
    assert _wait_subscribed(server, "thread-def")
    assert server.forbidden_calls == []
