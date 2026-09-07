"""Task 6 verify: the Codex App Server adapter against a fake server.

Proves handshake + version gating, thread binding, idle turn/start delivery of
inbound calls/texts, runtime-status reflection, the final-message fallback
(only when enabled), the MCP-reply path (no fallback needed), and that
turn/steer is never used.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from bridge.adapters.codex import CodexAdapter
from bridge.codex_app_server import (
    FORBIDDEN_METHODS,
    LAUNCH_ARGV,
    SUPPORTED_VERSIONS,
    CodexAppServerClient,
    UnsupportedCodexVersion,
)

from .fakes.codex_app_server import LegacyFakeCodexAppServer
from .fakes.router_peer import RunningRouter

FIXTURE = Path(__file__).parent / "fixtures" / "codex_protocol" / "v1.json"


@pytest.fixture
def codex_factory(paths):
    created: list = []
    rr = RunningRouter(paths).__enter__()

    def make(
        session_id,
        *,
        version="codex-app-server/1",
        agent_message="",
        fallback=False,
        auto_complete=True,
    ):
        client_sock, server_sock = socket.socketpair()
        server = LegacyFakeCodexAppServer(
            server_sock, version=version, agent_message=agent_message, auto_complete=auto_complete
        )
        app = CodexAppServerClient(client_sock).start()
        adapter = CodexAdapter(session_id, app, final_message_fallback=fallback)
        adapter.connect_router(
            lambda on_event: rr.client(session_id=session_id, role="adapter", on_event=on_event)
        )
        created.append((adapter, server))
        return adapter, server

    try:
        yield rr, make
    finally:
        for adapter, server in created:
            try:
                adapter.close()
            except Exception:
                pass
            try:
                server.close()
            except Exception:
                pass
        rr.__exit__(None, None, None)


def _wait_reachable(client, sid, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        roster = client.call("roster", {})
        entry = next((s for s in roster["sessions"] if s["id"] == sid), None)
        if entry and entry["reachable"] and entry["state"] == "idle":
            return True
        time.sleep(0.02)
    return False


def test_pinned_contract_matches_fixture():
    data = json.loads(FIXTURE.read_text())
    assert data["protocol_version"] in SUPPORTED_VERSIONS
    assert tuple(data["supported_versions"]) == SUPPORTED_VERSIONS
    assert tuple(data["forbidden_in_v1"]) == FORBIDDEN_METHODS
    assert tuple(data["launch_argv"]) == LAUNCH_ARGV


def test_unsupported_version_raises():
    client_sock, server_sock = socket.socketpair()
    server = LegacyFakeCodexAppServer(server_sock, version="codex-app-server/999")
    app = CodexAppServerClient(client_sock).start()
    try:
        with pytest.raises(UnsupportedCodexVersion):
            app.initialize()
    finally:
        app.close()
        server.close()


def test_thread_binding_and_reachable(codex_factory):
    rr, make = codex_factory
    adapter, _server = make("codex-1")
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")


def test_inbound_call_starts_turn_with_envelope(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1", agent_message="", fallback=False)
    adapter.start()
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    caller.call("call_async", {"to": "codex-1", "question": "ship it?"})
    deadline = time.time() + 2
    while time.time() < deadline and not server.turns:
        time.sleep(0.02)
    assert server.turns, "adapter did not start a turn"
    text = server.turns[0]["input"][0]["text"]
    assert "[bridge call]" in text and "ship it?" in text
    assert server.forbidden_calls == []  # never steered


def test_final_message_fallback_answers_caller(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1", agent_message="yes, ship it", fallback=True)
    adapter.start()
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    result = caller.call("call", {"to": "codex-1", "question": "ship it?"}, timeout=8)
    assert result["status"] == "answered"
    assert result["answer"] == "yes, ship it"
    assert result["meta"]["answered_by"] == "live-session"
    assert result["meta"]["via"] == "final-message"
    assert server.forbidden_calls == []


def test_mcp_reply_path_wins_over_fallback(codex_factory):
    rr, make = codex_factory
    adapter, server = make(
        "codex-1", agent_message="stale fallback", fallback=True, auto_complete=False
    )
    adapter.start()
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

    deadline = time.time() + 3
    call_id = None
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


def test_app_server_crash_marks_unreachable_and_records_disconnected(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")

    # Simulate a crash: the RPC connection drops out from under the adapter,
    # not a deliberate adapter.close() — never a headless answer.
    server.close()

    deadline = time.time() + 3
    entry = None
    while time.time() < deadline:
        roster = ctrl.call("roster", {})
        entry = next(s for s in roster["sessions"] if s["id"] == "codex-1")
        if entry["reachable"] is False:
            break
        time.sleep(0.02)
    assert entry is not None and entry["reachable"] is False

    tx = ctrl.call("transcript", {"limit": 20})
    assert any(
        e["kind"] == "session" and e["status"] == "disconnected" and e["to"] == "codex-1"
        for e in tx["entries"]
    )


def test_reconnect_recovers_after_transient_disconnect(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")

    spawned: list[LegacyFakeCodexAppServer] = []

    def reconnect():
        client_sock, server_sock = socket.socketpair()
        fresh = LegacyFakeCodexAppServer(server_sock)
        spawned.append(fresh)
        return CodexAppServerClient(client_sock).start()

    adapter._reconnect = reconnect
    adapter._reconnect_sleep = lambda _s: None  # no real waiting in tests
    adapter.start()
    ctrl = rr.client(session_id="ctrl")
    assert _wait_reachable(ctrl, "codex-1")

    server.close()  # transient crash

    deadline = time.time() + 3
    while time.time() < deadline and not spawned:
        time.sleep(0.02)
    assert spawned, "adapter never attempted to reconnect"

    assert _wait_reachable(ctrl, "codex-1", timeout=3.0)
    for s in spawned:
        s.close()


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

    deadline = time.time() + 3
    while time.time() < deadline and attempts["n"] < 3:
        time.sleep(0.02)
    assert attempts["n"] == 3
    roster = ctrl.call("roster", {})
    entry = next(s for s in roster["sessions"] if s["id"] == "codex-1")
    assert entry["reachable"] is False


def test_working_status_holds_delivery(codex_factory):
    rr, make = codex_factory
    adapter, server = make("codex-1")
    adapter.start()
    caller = rr.client(session_id="claude-1")
    assert _wait_reachable(caller, "codex-1")

    server.emit_status("working")
    deadline = time.time() + 1
    while time.time() < deadline:
        roster = caller.call("roster", {})
        entry = next(s for s in roster["sessions"] if s["id"] == "codex-1")
        if entry["state"] == "working":
            break
        time.sleep(0.02)
    before = len(server.turns)
    caller.call("text", {"to": "codex-1", "message": "fyi"})
    time.sleep(0.3)
    assert len(server.turns) == before  # held while working
    assert server.forbidden_calls == []
