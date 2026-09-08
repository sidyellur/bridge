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

import json
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
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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


def test_app_server_is_launched_with_mcp_env_overrides(paths, tmp_path, ids):
    """Codex scrubs the environment it hands to the MCP servers it starts
    from ``~/.codex/config.toml``, so the App Server line must carry the
    session identity itself as ``-c mcp_servers.bridge.env.*`` overrides --
    otherwise `bridge serve --family codex` never sees `BRIDGE_SESSION_ID`
    and exits (see src/bridge/codex_app_server.py::MCP_ENV_KEYS). This only
    happens when Codex's own config already registers Bridge's MCP server;
    see the sibling ``..._when_server_not_registered`` test below."""
    bindir = tmp_path / "bin"
    capture = tmp_path / "tui_cap.jsonl"
    release = tmp_path / "release"
    app_server_capture = tmp_path / "app_server_cap.jsonl"
    make_fake_codex_exe(
        bindir,
        capture=capture,
        release_file=release,
        app_server_capture=app_server_capture,
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.bridge]\ncommand = "bridge"\nargs = ["serve", "--family", "codex"]\n'
    )

    with RunningRouter(paths) as rr:
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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
                codex_home=codex_home,
            )

        t = threading.Thread(target=go)
        t.start()
        try:
            assert _wait_for(
                lambda: app_server_capture.exists() and read_captures(app_server_capture)
            ), "app-server argv was never recorded"
            assert _wait_for(lambda: capture.exists() and read_captures(capture))
        finally:
            release.write_text("go")
            t.join(timeout=10)

        assert result_box["result"].returncode == 0
        session_id = result_box["result"].session_id
        argv = read_captures(app_server_capture)[0]["app_server_argv"]
        assert argv[:3] == [
            "app-server",
            "--listen",
            f"unix://{paths.codex_socket(session_id)}",
        ]
        assert argv[3:] == [
            "-c",
            f"mcp_servers.bridge.env.BRIDGE_SESSION_ID={json.dumps(session_id)}",
            "-c",
            f"mcp_servers.bridge.env.BRIDGE_ROUTER_SOCKET={json.dumps(str(paths.socket))}",
            "-c",
            f"mcp_servers.bridge.env.BRIDGE_ROUTER_TOKEN_PATH={json.dumps(str(paths.token))}",
            "-c",
            f"mcp_servers.bridge.env.BRIDGE_HOME={json.dumps(str(paths.home))}",
        ]


def test_app_server_launched_without_mcp_env_overrides_when_server_not_registered(
    paths, tmp_path, ids, capsys
):
    """Passing ``-c mcp_servers.bridge.env.*`` overrides for a server Codex's
    own config.toml never registers makes the App Server refuse to boot
    entirely (``invalid transport in mcp_servers.bridge``) -- so a user who
    never ran ``bridge install`` for Codex must still get a working wrapper,
    just without Bridge's MCP tools, instead of ``bridge codex`` dying in
    ``wait_for_socket``."""
    bindir = tmp_path / "bin"
    capture = tmp_path / "tui_cap.jsonl"
    release = tmp_path / "release"
    app_server_capture = tmp_path / "app_server_cap.jsonl"
    make_fake_codex_exe(
        bindir,
        capture=capture,
        release_file=release,
        app_server_capture=app_server_capture,
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("# no bridge mcp server registered here\n")

    with RunningRouter(paths) as rr:
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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
                codex_home=codex_home,
            )

        t = threading.Thread(target=go)
        t.start()
        try:
            assert _wait_for(
                lambda: app_server_capture.exists() and read_captures(app_server_capture)
            ), "app-server argv was never recorded"
            assert _wait_for(lambda: capture.exists() and read_captures(capture))
        finally:
            release.write_text("go")
            t.join(timeout=10)

        assert result_box["result"].returncode == 0
        argv = read_captures(app_server_capture)[0]["app_server_argv"]
        session_id = result_box["result"].session_id
        assert argv == [
            "app-server",
            "--listen",
            f"unix://{paths.codex_socket(session_id)}",
        ]
        err = capsys.readouterr().err
        assert (
            f"[bridge] Codex MCP server not registered in {codex_home / 'config.toml'}; "
            "run bridge install for Bridge tools inside Codex" in err
        )


def test_session_registered_reachable_and_idle_while_tui_runs(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=0)

    with RunningRouter(paths) as rr:
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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


def test_session_meta_is_written_for_a_wrapped_codex_session(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=0)

    with RunningRouter(paths) as rr:
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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
            session_id = next(
                s["id"] for s in ctrl.call("roster", {})["sessions"] if s["family"] == "codex"
            )

            def meta():
                try:
                    return json.loads(paths.session_meta(session_id).read_text())
                except (OSError, json.JSONDecodeError):
                    return {}

            assert _wait_for(lambda: meta().get("thread_id") == "thread-fake")
            assert meta()["family"] == "codex"
            assert meta()["subscribed"] is True
            assert meta()["codex_version"] == "0.151.0"
        finally:
            release.write_text("go")
            t.join(timeout=15)


def test_tui_exit_code_passes_through_and_both_children_reaped(paths, codex_bin, ids):
    bindir, capture, release = codex_bin
    make_fake_codex_exe(bindir, capture=capture, release_file=release, tui_exit_code=9)

    with RunningRouter(paths) as rr:
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
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
        codex_version="0.150.0",
    )

    with RunningRouter(paths) as rr:
        env = {
            "PATH": str(bindir),
            "BRIDGE_CODEX_BIN": str(bindir / "codex"),
            # No codex_home is passed explicitly by most of these tests; point
            # the wrapper's default `$HOME/.codex` resolution at a directory
            # that can never exist, so it can never pick up mcp_env overrides
            # (or lack thereof) from the real developer machine's ~/.codex.
            "HOME": str(bindir.parent / "no-such-home"),
        }
        with pytest.raises(Exception, match="predates the pinned App Server contract"):
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


def test_reconnect_factory_closes_the_socket_when_the_client_cannot_start(tmp_path, monkeypatch):
    """`CodexAdapter` retries this factory on a backoff, so a client that fails
    to start (a handshake the App Server never completes) must not leave its
    connected socket for the garbage collector: five attempts would leak five
    fds against an App Server that is up but wedged."""
    import socket as socket_mod

    from bridge import codex_app_server, launch

    sockets: list = []

    class FailingClient:
        def __init__(self, sock, **_kwargs) -> None:
            sockets.append(sock)

        def start(self):
            raise OSError("handshake never completed")

    monkeypatch.setattr(codex_app_server, "CodexAppServerClient", FailingClient)

    socket_path = tmp_path / "app.sock"
    listener = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    try:
        with pytest.raises(OSError, match="handshake never completed"):
            launch.connect_codex_client(socket_path)
    finally:
        listener.close()

    assert len(sockets) == 1
    assert sockets[0].fileno() == -1, "the connected socket was left open"
