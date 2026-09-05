"""Task 3 verify (router): auth, version negotiation, idempotent connect,
socket/token permissions, reachability lifecycle, restart recovery, and idle
shutdown.
"""

from __future__ import annotations

import socket
import stat
import threading
import time

import pytest

from bridge.protocol import hello_args, recv_frame, request, send_frame
from bridge.router import RouterConfig, RouterServer, is_running
from bridge.router_client import RouterClient, RouterClientError

from .fakes.router_peer import RunningRouter


def test_socket_and_token_permissions(running_router: RunningRouter):
    paths = running_router.paths
    assert stat.S_IMODE(paths.socket.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600


def test_hello_required_before_other_ops(running_router: RunningRouter):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(running_router.paths.socket))
    send_frame(sock, request(1, "roster", {}))
    resp = recv_frame(sock)
    assert resp["ok"] is False and resp["error"]["code"] == "unauthorized"
    sock.close()


def test_wrong_token_rejected(running_router: RunningRouter):
    with pytest.raises(RouterClientError) as exc:
        RouterClient.connect(running_router.paths, token="wrong-token")
    assert exc.value.code == "unauthorized"


def test_version_mismatch_rejected(running_router: RunningRouter):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(running_router.paths.socket))
    args = hello_args(running_router.token)
    args["protocol_version"] = 999
    send_frame(sock, request(1, "hello", args))
    resp = recv_frame(sock)
    assert resp["ok"] is False and resp["error"]["code"] == "version_mismatch"
    sock.close()


def test_idempotent_second_connection(running_router: RunningRouter):
    c1 = running_router.client(session_id="a")
    c2 = running_router.client(session_id="b")
    # both authenticate and can call roster
    assert "sessions" in c1.call("roster", {})
    assert "sessions" in c2.call("roster", {})


def test_register_and_roster_roundtrip(running_router: RunningRouter):
    c = running_router.client(session_id="a")
    c.call("register_session", {"session_id": "a", "family": "claude", "cwd": "/w"})
    roster = c.call("roster", {})
    ids = {s["id"] for s in roster["sessions"]}
    assert "a" in ids
    entry = next(s for s in roster["sessions"] if s["id"] == "a")
    assert entry["is_self"] is True  # caller substituted from the connection


def test_subscribe_sets_reachable_and_disconnect_offline(running_router: RunningRouter):
    ctrl = running_router.client(session_id="ctrl")
    ctrl.call("register_session", {"session_id": "t", "family": "claude", "state": "idle"})

    adapter = running_router.adapter("t")
    adapter.client.call(
        "register_session", {"session_id": "t", "family": "claude", "state": "idle"}
    )
    adapter.subscribe()

    roster = ctrl.call("roster", {})
    entry = next(s for s in roster["sessions"] if s["id"] == "t")
    assert entry["reachable"] is True

    adapter.client.close()
    # allow the server to notice the disconnect
    time.sleep(0.2)
    roster = ctrl.call("roster", {})
    entry = next(s for s in roster["sessions"] if s["id"] == "t")
    assert entry["reachable"] is False


def test_unmanaged_session_warned_and_unreachable(running_router: RunningRouter):
    c = running_router.client(session_id="a")
    c.call(
        "register_session",
        {"session_id": "u", "family": "codex", "is_managed": False, "state": "idle"},
    )
    roster = c.call("roster", {"include_unmanaged": True})
    entry = next(s for s in roster["sessions"] if s["id"] == "u")
    assert entry["reachable"] is False
    assert any("not Bridge-managed" in w for w in roster["warnings"])


def test_restart_recovery(paths, clock):
    # First server: register a session and record state.
    with RunningRouter(paths, new_id=lambda: "fixed") as rr:
        c = rr.client(session_id="a")
        c.call("register_session", {"session_id": "a", "family": "claude", "state": "idle"})
        c.close()
    # Second server on the same paths recovers persisted registry.
    with RunningRouter(paths) as rr2:
        c2 = rr2.client(session_id="b")
        roster = c2.call("roster", {})
        ids = {s["id"] for s in roster["sessions"]}
        assert "a" in ids
        # a is no longer connected after restart, so it is not reachable
        entry = next(s for s in roster["sessions"] if s["id"] == "a")
        assert entry["reachable"] is False


def test_idle_shutdown():
    import tempfile
    from pathlib import Path

    from bridge.paths import Paths

    tmp = Path(tempfile.mkdtemp())
    paths = Paths.resolve(home=tmp).ensure()
    server = RouterServer(paths, config=RouterConfig(idle_grace_s=0.2))
    thread = threading.Thread(
        target=lambda: (server.serve_forever(), server.shutdown()), daemon=True
    )
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not paths.socket.exists():
        time.sleep(0.01)
    assert paths.socket.exists()
    # No client ever connects; the server should shut itself down.
    thread.join(timeout=3.0)
    assert not thread.is_alive()


def test_ensure_running_lazy_start(paths):
    from bridge.router import cli_router, ensure_running, read_token

    assert not is_running(paths)
    ensure_running(paths)
    try:
        assert is_running(paths)
        token = read_token(paths)
        c = RouterClient.connect(paths, token=token, session_id="x")
        assert "sessions" in c.call("roster", {})
        c.close()
    finally:
        cli_router("stop")
        deadline = time.time() + 3
        while time.time() < deadline and is_running(paths):
            time.sleep(0.05)


def test_internal_error_does_not_kill_loop(running_router: RunningRouter):
    c = running_router.client(session_id="a")
    with pytest.raises(RouterClientError):
        c.call("register_session", {"family": "claude"})  # missing session_id
    # loop still alive, subsequent op works
    assert "sessions" in c.call("roster", {})
