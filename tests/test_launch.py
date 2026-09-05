"""Task 4 verify (launch): pure command/env construction, binary resolution,
signal forwarding, and an integration launch proving BRIDGE_SESSION_ID reaches
the child and the exit code passes through.
"""

from __future__ import annotations

import json
import signal

import pytest

from bridge.launch import (
    SignalForwarder,
    build_claude_argv,
    build_codex_argv,
    build_identity_env,
    describe_channel_mode,
    load_claude_channel_args,
    resolve_binary,
    resolve_claude_session_id,
    run_wrapper,
)
from bridge.paths import (
    BRIDGE_HOME_ENV,
    ROUTER_SOCKET_ENV,
    ROUTER_TOKEN_ENV,
    SESSION_ID_ENV,
)

from .fakes.executables import make_capture_exe, read_captures
from .fakes.router_peer import RunningRouter


def test_identity_env(paths):
    env = build_identity_env(paths, "sess-1", base_env={"PATH": "/bin"})
    assert env[SESSION_ID_ENV] == "sess-1"
    assert env[ROUTER_SOCKET_ENV] == str(paths.socket)
    assert env[ROUTER_TOKEN_ENV] == str(paths.token)
    assert env[BRIDGE_HOME_ENV] == str(paths.home)
    assert env["PATH"] == "/bin"


def test_resolve_claude_session_id_fresh():
    sid, is_resume = resolve_claude_session_id([], lambda: "fresh-uuid")
    assert sid == "fresh-uuid" and is_resume is False


def test_resolve_claude_session_id_explicit():
    sid, is_resume = resolve_claude_session_id(["--session-id", "abc"], lambda: "x")
    assert sid == "abc" and is_resume is False
    sid2, is_resume2 = resolve_claude_session_id(["--resume", "old"], lambda: "x")
    assert sid2 == "old" and is_resume2 is True


def test_build_claude_argv_adds_session_id_when_absent():
    argv = build_claude_argv("claude", "sid", ["--foo"], ["--channels", "bridge"])
    assert argv == ["claude", "--session-id", "sid", "--channels", "bridge", "--foo"]


def test_build_claude_argv_preserves_explicit_session_id():
    argv = build_claude_argv("claude", "sid", ["--session-id", "abc"], [])
    assert argv == ["claude", "--session-id", "abc"]
    assert argv.count("--session-id") == 1


def test_build_claude_argv_resume_does_not_add_session_id():
    argv = build_claude_argv("claude", "sid", ["--resume", "old"], [], is_resume=True)
    assert "--session-id" not in argv


def test_build_codex_argv(paths):
    sock = paths.codex_socket("sess-1")
    argv = build_codex_argv("codex", sock, ["--model", "o3"])
    assert argv == ["codex", "--remote", f"unix://{sock}", "--model", "o3"]


def test_load_claude_channel_args(paths):
    assert load_claude_channel_args(paths) == []
    (paths.home / "claude_channel_args.json").write_text(json.dumps(["--channels", "bridge@dev"]))
    assert load_claude_channel_args(paths) == ["--channels", "bridge@dev"]


def test_describe_channel_mode_unsupported_when_no_args():
    assert "unsupported" in describe_channel_mode([]).lower()
    assert "inbound-unreachable" in describe_channel_mode([])


def test_describe_channel_mode_development():
    line = describe_channel_mode(["--channels", "dev:bridge"])
    assert "development" in line.lower()
    assert "research preview" in line.lower()


def test_describe_channel_mode_plugin():
    line = describe_channel_mode(["--channels", "plugin:bridge@bridge-marketplace"])
    assert "plugin" in line.lower()
    assert "research preview" not in line.lower()


def test_resolve_binary_env_override():
    env = {"BRIDGE_CLAUDE_BIN": "/opt/fake/claude", "PATH": "/nowhere"}
    assert resolve_binary("claude", env) == "/opt/fake/claude"


def test_resolve_binary_missing_raises():
    with pytest.raises(FileNotFoundError):
        resolve_binary("claude", {"PATH": "/nonexistent-dir-xyz"})


def test_signal_forwarder_forwards_and_restores():
    class FakeProc:
        def __init__(self):
            self.signals = []

        def send_signal(self, s):
            self.signals.append(s)

    fp = FakeProc()
    before = signal.getsignal(signal.SIGUSR1)
    fwd = SignalForwarder(fp, signals=[signal.SIGUSR1])
    with fwd:
        assert signal.getsignal(signal.SIGUSR1) is not before
        fwd._forward(signal.SIGUSR1)
    assert fp.signals == [signal.SIGUSR1]
    assert signal.getsignal(signal.SIGUSR1) is before


def test_run_wrapper_passes_identity_and_exit_code(paths, tmp_path, ids):
    capture = tmp_path / "cap.jsonl"
    bindir = tmp_path / "bin"
    make_capture_exe(bindir, "claude", capture, exit_code=7)

    with RunningRouter(paths) as rr:
        env = {
            "PATH": f"{bindir}",
            "BRIDGE_CLAUDE_BIN": str(bindir / "claude"),
        }
        result = run_wrapper(
            "claude",
            ["--foo", "bar"],
            paths=paths,
            env=env,
            new_id=ids.new,
            ensure_running=lambda p: None,
            connect=lambda paths, session_id, role: rr.client(session_id=session_id, role=role),
            forward_signals=False,
            print_address=False,
        )
        assert result.returncode == 7
        records = read_captures(capture)
        assert records[0]["env"][SESSION_ID_ENV] == result.session_id
        assert records[0]["argv"][:2] == ["--session-id", result.session_id]

        # session was registered then offlined
        roster = rr.client().call("roster", {})
        entry = next(s for s in roster["sessions"] if s["id"] == result.session_id)
        assert entry["state"] == "offline"


def test_run_wrapper_codex_builds_remote_socket(paths, tmp_path, ids):
    capture = tmp_path / "cap.jsonl"
    bindir = tmp_path / "bin"
    make_capture_exe(bindir, "codex", capture)

    with RunningRouter(paths) as rr:
        env = {"PATH": f"{bindir}", "BRIDGE_CODEX_BIN": str(bindir / "codex")}
        result = run_wrapper(
            "codex",
            ["--model", "o3"],
            paths=paths,
            env=env,
            new_id=ids.new,
            ensure_running=lambda p: None,
            connect=lambda paths, session_id, role: rr.client(session_id=session_id, role=role),
            forward_signals=False,
            print_address=False,
        )
        records = read_captures(capture)
        assert records[0]["argv"][0] == "--remote"
        assert records[0]["argv"][1].startswith("unix://")
        assert result.session_id in records[0]["argv"][1]


def test_run_wrapper_claude_prints_channel_mode_line(paths, tmp_path, ids, capsys):
    capture = tmp_path / "cap.jsonl"
    bindir = tmp_path / "bin"
    make_capture_exe(bindir, "claude", capture)
    (paths.home / "claude_channel_args.json").write_text(json.dumps(["--channels", "dev:bridge"]))

    with RunningRouter(paths) as rr:
        env = {"PATH": f"{bindir}", "BRIDGE_CLAUDE_BIN": str(bindir / "claude")}
        run_wrapper(
            "claude",
            [],
            paths=paths,
            env=env,
            new_id=ids.new,
            ensure_running=lambda p: None,
            connect=lambda paths, session_id, role: rr.client(session_id=session_id, role=role),
            forward_signals=False,
            print_address=True,
        )
    err = capsys.readouterr().err
    assert "session address:" in err
    assert "Claude Channel mode: development" in err


def test_run_wrapper_claude_prints_unsupported_channel_line(paths, tmp_path, ids, capsys):
    capture = tmp_path / "cap.jsonl"
    bindir = tmp_path / "bin"
    make_capture_exe(bindir, "claude", capture)
    # No claude_channel_args.json written: unsupported by default.

    with RunningRouter(paths) as rr:
        env = {"PATH": f"{bindir}", "BRIDGE_CLAUDE_BIN": str(bindir / "claude")}
        run_wrapper(
            "claude",
            [],
            paths=paths,
            env=env,
            new_id=ids.new,
            ensure_running=lambda p: None,
            connect=lambda paths, session_id, role: rr.client(session_id=session_id, role=role),
            forward_signals=False,
            print_address=True,
        )
    err = capsys.readouterr().err
    assert "Claude Channel mode: unsupported" in err
    assert "inbound-unreachable" in err


def test_run_wrapper_no_channel_line_when_print_address_false(paths, tmp_path, ids, capsys):
    capture = tmp_path / "cap.jsonl"
    bindir = tmp_path / "bin"
    make_capture_exe(bindir, "claude", capture)
    (paths.home / "claude_channel_args.json").write_text(json.dumps(["--channels", "dev:bridge"]))

    with RunningRouter(paths) as rr:
        env = {"PATH": f"{bindir}", "BRIDGE_CLAUDE_BIN": str(bindir / "claude")}
        run_wrapper(
            "claude",
            [],
            paths=paths,
            env=env,
            new_id=ids.new,
            ensure_running=lambda p: None,
            connect=lambda paths, session_id, role: rr.client(session_id=session_id, role=role),
            forward_signals=False,
            print_address=False,
        )
    err = capsys.readouterr().err
    assert err == ""


def test_run_wrapper_uses_spawn_injection(paths, ids):
    captured = {}

    class FakeProc:
        pid = 4321

        def wait(self):
            return 0

    def fake_spawn(argv, env):
        captured["argv"] = argv
        captured["env"] = env
        return FakeProc()

    with RunningRouter(paths) as rr:
        run_wrapper(
            "claude",
            [],
            paths=paths,
            env={"PATH": "/x", "BRIDGE_CLAUDE_BIN": "/x/claude"},
            new_id=ids.new,
            spawn=fake_spawn,
            ensure_running=lambda p: None,
            connect=lambda paths, session_id, role: rr.client(session_id=session_id, role=role),
            forward_signals=False,
            print_address=False,
        )
    assert captured["argv"][0] == "/x/claude"
    assert captured["env"][SESSION_ID_ENV]
