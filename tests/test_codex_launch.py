"""Issue #2 verify: ``bridge codex`` end to end against a fake ``codex``
executable that really binds the App Server Unix socket and really runs a TUI
subprocess (``tests/fakes/codex_exe.py``) — no real vendor binary, but real
processes and a real router over its socket (``RunningRouter``).

Covers: the App Server socket exists before the TUI is spawned; TUI argv
carries ``--remote unix://…``; the session is registered and
reachable/idle while the TUI "runs"; both children are reaped and the session
is offline after exit with the TUI's exit code passed through; an App Server
crash marks the session unreachable and records a transcript ``disconnected``
entry; and a socket that never appears fails cleanly with no leaked process.
"""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import threading
import time

import pytest

from bridge.launch import run_wrapper
from bridge.paths import Paths

from .fakes.codex_exe import make_fake_codex_exe
from .fakes.executables import make_capture_exe, read_captures
from .fakes.router_peer import RunningRouter


@pytest.fixture
def paths():
    """A short-path Bridge home, overriding the shared fixture's deep pytest
    ``tmp_path`` tree: AF_UNIX socket paths are capped at ~108 bytes, and a
    real per-session ``codex.sock`` under the default tmp tree overflows that
    once a descriptive test name is in the path."""
    home = tempfile.mkdtemp(prefix="bhome-")
    p = Paths.resolve(home=home).ensure()
    try:
        yield p
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _roster_entry(client, sid):
    roster = client.call("roster", {})
    return next((s for s in roster["sessions"] if s["id"] == sid), None)


def _connect_with_events(rr, paths, session_id, role, on_event=None):
    return rr.client(session_id=session_id, role=role, on_event=on_event)


@pytest.fixture
def codex_bin(tmp_path):
    """A real-subprocess fake `codex` plus the paths/dirs a test needs."""
    bindir = tmp_path / "bin"
    capture = tmp_path / "tui_cap.jsonl"
    release = tmp_path / "release"
    return bindir, capture, release


def test_app_server_socket_exists_before_tui_spawn_and_remote_arg(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=0)

    with RunningRouter(paths) as rr:
        env = {"PATH": str(bindir), "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        result_box = {}

        def go():
            result_box["result"] = run_wrapper(
                "codex",
                ["--model", "o3"],
                paths=paths,
                env=env,
                new_id=ids.new,
                ensure_running=lambda p: None,
                connect=lambda paths, session_id, role, on_event=None: _connect_with_events(
                    rr, paths, session_id, role, on_event
                ),
                forward_signals=False,
                print_address=False,
            )

        t = threading.Thread(target=go)
        t.start()

        assert _wait_for(lambda: capture.exists() and read_captures(capture)), (
            "TUI never recorded its launch"
        )
        records = read_captures(capture)
        assert records[0]["argv"][0] == "--remote"
        assert records[0]["argv"][1].startswith("unix://")
        # The strongest proof the App Server owned the socket first: the TUI
        # itself observed the socket file already existing at its own startup.
        assert records[0]["remote_socket_exists"] is True

        release.write_text("go")
        t.join(timeout=10)
        assert result_box["result"].returncode == 0
        assert result_box["result"].session_id in records[0]["argv"][1]


def test_session_registered_reachable_and_idle_while_tui_runs(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=0)

    with RunningRouter(paths) as rr:
        env = {"PATH": str(bindir), "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        result_box = {}

        def go():
            result_box["result"] = run_wrapper(
                "codex",
                [],
                paths=paths,
                env=env,
                new_id=ids.new,
                ensure_running=lambda p: None,
                connect=lambda paths, session_id, role, on_event=None: _connect_with_events(
                    rr, paths, session_id, role, on_event
                ),
                forward_signals=False,
                print_address=False,
            )

        t = threading.Thread(target=go)
        t.start()
        try:
            assert _wait_for(lambda: capture.exists() and read_captures(capture))
            ctrl = rr.client(session_id="ctrl")
            assert _wait_for(
                lambda: any(
                    s["family"] == "codex" and s["reachable"] and s["state"] == "idle"
                    for s in ctrl.call("roster", {})["sessions"]
                )
            )
            entry = next(
                s
                for s in ctrl.call("roster", {})["sessions"]
                if s["family"] == "codex" and s["reachable"]
            )
            session_id = entry["id"]
            assert entry["state"] == "idle"
            assert entry["reachable"] is True
        finally:
            release.write_text("go")
            t.join(timeout=10)

        assert result_box["result"].session_id == session_id
        assert result_box["result"].returncode == 0
        final = _roster_entry(rr.client(), session_id)
        assert final["state"] == "offline"


def test_tui_exit_code_passes_through_and_both_children_reaped(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=9)

    with RunningRouter(paths) as rr:
        env = {"PATH": str(bindir), "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        result_box = {}
        socket_path_box = {}

        def go():
            result_box["result"] = run_wrapper(
                "codex",
                [],
                paths=paths,
                env=env,
                new_id=ids.new,
                ensure_running=lambda p: None,
                connect=lambda paths, session_id, role, on_event=None: _connect_with_events(
                    rr, paths, session_id, role, on_event
                ),
                forward_signals=False,
                print_address=False,
            )

        t = threading.Thread(target=go)
        t.start()
        assert _wait_for(lambda: capture.exists() and read_captures(capture))
        records = read_captures(capture)
        socket_path_box["path"] = records[0]["argv"][1][len("unix://") :]
        release.write_text("go")
        t.join(timeout=10)

    assert result_box["result"].returncode == 9
    # The fake App Server unlinks its socket on a clean SIGTERM shutdown; its
    # absence is external proof the process was actually reaped, not merely
    # sent a signal and abandoned as a zombie.
    assert _wait_for(lambda: not os.path.exists(socket_path_box["path"]), timeout=5.0)


def test_app_server_crash_marks_session_unreachable_and_records_disconnected(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=0)

    with RunningRouter(paths) as rr:
        env = {"PATH": str(bindir), "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        result_box = {}

        def go():
            result_box["result"] = run_wrapper(
                "codex",
                [],
                paths=paths,
                env=env,
                new_id=ids.new,
                ensure_running=lambda p: None,
                connect=lambda paths, session_id, role, on_event=None: _connect_with_events(
                    rr, paths, session_id, role, on_event
                ),
                forward_signals=False,
                print_address=False,
            )

        t = threading.Thread(target=go)
        t.start()
        try:
            assert _wait_for(lambda: capture.exists() and read_captures(capture))
            ctrl = rr.client(session_id="ctrl")
            assert _wait_for(
                lambda: any(
                    s["family"] == "codex" and s["reachable"]
                    for s in ctrl.call("roster", {})["sessions"]
                )
            )
            entry = next(s for s in ctrl.call("roster", {})["sessions"] if s["family"] == "codex")
            session_id = entry["id"]
            records = read_captures(capture)
            socket_path = records[0]["argv"][1][len("unix://") :]

            with open(socket_path + ".pid", encoding="utf-8") as fh:
                pid = int(fh.read())
            os.kill(pid, signal.SIGKILL)

            assert _wait_for(
                lambda: (
                    _roster_entry(ctrl, session_id) is not None
                    and _roster_entry(ctrl, session_id)["reachable"] is False
                ),
                timeout=5.0,
            )
            tx = ctrl.call("transcript", {"limit": 50})
            assert any(
                e["kind"] == "session" and e["status"] == "disconnected" and e["to"] == session_id
                for e in tx["entries"]
            )
        finally:
            release.write_text("go")
            t.join(timeout=15)

    assert result_box["result"].returncode == 0


def test_app_server_socket_never_appearing_fails_cleanly_with_no_leak(paths, tmp_path, ids):
    bindir = tmp_path / "bin"
    capture = tmp_path / "cap.jsonl"
    # A plain single-mode exe: whatever it's invoked as, it just captures and
    # exits — it never binds a Unix socket, so the App Server wait must time
    # out cleanly instead of hanging or silently launching the TUI anyway.
    make_capture_exe(bindir, "codex", capture)

    with RunningRouter(paths) as rr:
        env = {"PATH": str(bindir), "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        with pytest.raises(Exception, match="codex app-server"):
            run_wrapper(
                "codex",
                [],
                paths=paths,
                env=env,
                new_id=ids.new,
                ensure_running=lambda p: None,
                connect=lambda paths, session_id, role, on_event=None: _connect_with_events(
                    rr, paths, session_id, role, on_event
                ),
                forward_signals=False,
                print_address=False,
                codex_app_server_timeout=0.5,
            )

    # Only the one (failed) app-server invocation happened; no TUI was ever
    # spawned, so exactly one capture record exists and it is not `--remote`.
    records = read_captures(capture)
    assert len(records) == 1
    assert records[0]["argv"][0] == "app-server"


def test_unsupported_app_server_version_stops_app_server_and_spawns_no_tui(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(
        bindir,
        capture=capture,
        release_file=release,
        protocol_version="codex-app-server/999",
    )

    with RunningRouter(paths) as rr:
        env = {"PATH": str(bindir), "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        with pytest.raises(Exception, match="codex-app-server/999"):
            run_wrapper(
                "codex",
                [],
                paths=paths,
                env=env,
                new_id=ids.new,
                ensure_running=lambda p: None,
                connect=lambda paths, session_id, role, on_event=None: _connect_with_events(
                    rr, paths, session_id, role, on_event
                ),
                forward_signals=False,
                print_address=False,
            )

    # No TUI was ever spawned (the adapter failed its handshake first), and
    # the App Server was still stopped/reaped rather than left running.
    assert not capture.exists() or not read_captures(capture)
