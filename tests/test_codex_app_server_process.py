"""Task 6 verify (process management): ``CodexAppServerProcess`` against a
fake ``Popen``-like object — no real ``codex`` binary, no real subprocess.
Covers the pinned launch argv, a bounded/injectable socket wait (including
fast-failing when the child already exited), and terminate->kill escalation
with reaping on ``stop()``.
"""

from __future__ import annotations

import subprocess

import pytest

from bridge.codex_app_server import (
    LAUNCH_ARGV,
    MCP_ENV_KEYS,
    CodexAppServerProcess,
    CodexAppServerStartError,
    build_launch_argv,
)
from bridge.paths import (
    BRIDGE_HOME_ENV,
    ROUTER_SOCKET_ENV,
    ROUTER_TOKEN_ENV,
    SESSION_ID_ENV,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakePopen:
    def __init__(self, argv, env=None, *, exit_after: int | None = None) -> None:
        self.argv = argv
        self.env = env
        self.pid = 4242
        self._exit_after = exit_after
        self._poll_calls = 0
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []
        self._wait_raises_timeout = False

    def poll(self):
        self._poll_calls += 1
        if self._exit_after is not None and self._poll_calls >= self._exit_after:
            self.returncode = 17
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._wait_raises_timeout and not self.killed:
            self._wait_raises_timeout = False
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_build_launch_argv_fills_template():
    argv = build_launch_argv("/usr/local/bin/codex", "/tmp/x/codex.sock")
    assert argv == ["/usr/local/bin/codex", "app-server", "--listen", "unix:///tmp/x/codex.sock"]
    assert argv[0] != LAUNCH_ARGV[0]  # the template itself is unfilled


def test_build_launch_argv_without_mcp_env_is_byte_identical():
    # No mcp_env at all, and an explicit empty mapping, must both leave argv
    # exactly as it is today -- no `-c` tokens appended.
    base = ["codex", "app-server", "--listen", "unix:///tmp/x/codex.sock"]
    assert build_launch_argv("codex", "/tmp/x/codex.sock") == base
    assert build_launch_argv("codex", "/tmp/x/codex.sock", mcp_env=None) == base
    assert build_launch_argv("codex", "/tmp/x/codex.sock", mcp_env={}) == base


def test_build_launch_argv_appends_mcp_env_overrides_in_fixed_key_order():
    mcp_env = {
        # Given out of order to prove the output order is MCP_ENV_KEYS, not
        # insertion order.
        BRIDGE_HOME_ENV: "/home/u/.bridge",
        ROUTER_TOKEN_ENV: "/home/u/.bridge/router.token",
        SESSION_ID_ENV: "s-1",
        ROUTER_SOCKET_ENV: "/home/u/.bridge/router.sock",
    }
    argv = build_launch_argv("codex", "/tmp/x/codex.sock", mcp_env=mcp_env)
    assert argv == [
        "codex",
        "app-server",
        "--listen",
        "unix:///tmp/x/codex.sock",
        "-c",
        'mcp_servers.bridge.env.BRIDGE_SESSION_ID="s-1"',
        "-c",
        'mcp_servers.bridge.env.BRIDGE_ROUTER_SOCKET="/home/u/.bridge/router.sock"',
        "-c",
        'mcp_servers.bridge.env.BRIDGE_ROUTER_TOKEN_PATH="/home/u/.bridge/router.token"',
        "-c",
        'mcp_servers.bridge.env.BRIDGE_HOME="/home/u/.bridge"',
    ]
    assert MCP_ENV_KEYS == (
        SESSION_ID_ENV,
        ROUTER_SOCKET_ENV,
        ROUTER_TOKEN_ENV,
        BRIDGE_HOME_ENV,
    )


def test_build_launch_argv_skips_keys_missing_from_mcp_env():
    argv = build_launch_argv("codex", "/tmp/x/codex.sock", mcp_env={SESSION_ID_ENV: "s-1"})
    assert argv == [
        "codex",
        "app-server",
        "--listen",
        "unix:///tmp/x/codex.sock",
        "-c",
        'mcp_servers.bridge.env.BRIDGE_SESSION_ID="s-1"',
    ]


def test_build_launch_argv_json_escapes_a_quote_in_the_value():
    argv = build_launch_argv(
        "codex", "/tmp/x/codex.sock", mcp_env={SESSION_ID_ENV: 'weird"id'}
    )
    assert argv[-1] == 'mcp_servers.bridge.env.BRIDGE_SESSION_ID="weird\\"id"'


def test_start_spawns_with_pinned_argv_and_env():
    captured = {}

    def spawn(argv, env=None):
        captured["argv"] = argv
        captured["env"] = env
        return FakePopen(argv, env)

    proc = CodexAppServerProcess("/tmp/x/codex.sock", binary="codex")
    p = proc.start(env={"FOO": "bar"}, spawn=spawn)
    assert captured["argv"] == ["codex", "app-server", "--listen", "unix:///tmp/x/codex.sock"]
    assert captured["env"] == {"FOO": "bar"}
    assert p is proc.proc


def test_start_passes_mcp_env_through_to_build_launch_argv():
    captured = {}

    def spawn(argv, env=None):
        captured["argv"] = argv
        return FakePopen(argv, env)

    proc = CodexAppServerProcess("/tmp/x/codex.sock", binary="codex")
    proc.start(env={"FOO": "bar"}, spawn=spawn, mcp_env={SESSION_ID_ENV: "s-1"})
    assert captured["argv"] == [
        "codex",
        "app-server",
        "--listen",
        "unix:///tmp/x/codex.sock",
        "-c",
        'mcp_servers.bridge.env.BRIDGE_SESSION_ID="s-1"',
    ]


def test_wait_for_socket_succeeds_once_it_appears(tmp_path):
    sock = tmp_path / "codex.sock"
    clock = FakeClock()

    def spawn(argv, env=None):
        return FakePopen(argv, env)

    proc = CodexAppServerProcess(sock)
    proc.start(spawn=spawn)

    calls = {"n": 0}

    def sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 3:
            sock.write_text("")
        clock.t += _seconds

    proc.wait_for_socket(timeout=5.0, sleep=sleep, now=clock.now)
    assert sock.exists()
    assert calls["n"] == 3


def test_wait_for_socket_times_out_cleanly(tmp_path):
    sock = tmp_path / "codex.sock"
    clock = FakeClock()

    def spawn(argv, env=None):
        return FakePopen(argv, env)

    proc = CodexAppServerProcess(sock)
    proc.start(spawn=spawn)

    with pytest.raises(CodexAppServerStartError, match="did not bind"):
        proc.wait_for_socket(timeout=1.0, sleep=clock.sleep, now=clock.now)
    assert not sock.exists()


def test_wait_for_socket_fails_fast_when_child_already_exited(tmp_path):
    sock = tmp_path / "codex.sock"
    clock = FakeClock()

    def spawn(argv, env=None):
        return FakePopen(argv, env, exit_after=1)

    proc = CodexAppServerProcess(sock)
    proc.start(spawn=spawn)

    with pytest.raises(CodexAppServerStartError, match="exited with code 17"):
        proc.wait_for_socket(timeout=1000.0, sleep=clock.sleep, now=clock.now)
    # Failed on the very first poll, long before the timeout budget was spent.
    assert clock.t == 0.0


def test_stop_terminates_and_reaps_running_process():
    fake = FakePopen(["codex"])

    def spawn(argv, env=None):
        return fake

    proc = CodexAppServerProcess("/tmp/x/codex.sock")
    proc.start(spawn=spawn)
    rc = proc.stop()
    assert fake.terminated is True
    assert fake.killed is False
    assert rc == 0


def test_stop_escalates_to_kill_when_terminate_does_not_finish():
    fake = FakePopen(["codex"])
    fake._wait_raises_timeout = True

    def spawn(argv, env=None):
        return fake

    proc = CodexAppServerProcess("/tmp/x/codex.sock")
    proc.start(spawn=spawn)
    rc = proc.stop(timeout=0.01)
    assert fake.terminated is True
    assert fake.killed is True
    assert rc == -9
    assert len(fake.wait_calls) == 2  # first wait timed out, second reaped after kill


def test_stop_on_already_exited_process_just_reaps():
    fake = FakePopen(["codex"])
    fake.returncode = 3

    def spawn(argv, env=None):
        return fake

    proc = CodexAppServerProcess("/tmp/x/codex.sock")
    proc.start(spawn=spawn)
    rc = proc.stop()
    assert fake.terminated is False
    assert rc == 3


def test_stop_is_a_noop_before_start():
    proc = CodexAppServerProcess("/tmp/x/codex.sock")
    assert proc.stop() is None
