"""Task 2 verify: the package installs, the CLI reports its version, and the
hermetic fixtures (temp home, frozen clock, seeded ids, fake executables,
network guard) behave.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

import bridge
from bridge.cli import main
from bridge.lab.capture import CAPTURE_ENV, CAPTURE_FULL_ENV
from bridge.paths import Paths
from bridge.protocol import FrameBuffer, decode_frame, encode_frame

from .fakes.clock import FrozenClock
from .fakes.executables import make_capture_exe, prepend_path, read_captures
from .fakes.ids import SeededIds


def test_version_constant():
    assert bridge.__version__ == "0.1.0"
    assert bridge.PROTOCOL_VERSION == 1


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_cli_no_args_prints_help(capsys):
    assert main([]) == 0
    assert "bridge" in capsys.readouterr().out


def test_paths_resolution_precedence(tmp_path: Path):
    explicit = Paths.resolve(home=tmp_path / "explicit")
    assert explicit.home == tmp_path / "explicit"

    env = {"BRIDGE_HOME": str(tmp_path / "fromenv")}
    assert Paths.resolve(env=env).home == tmp_path / "fromenv"

    env2 = {"HOME": str(tmp_path / "userhome")}
    assert Paths.resolve(env=env2).home == tmp_path / "userhome" / ".bridge"


def test_default_paths_resolution_never_reaches_the_real_bridge_home(tmp_path: Path):
    """The suite must not be able to touch the user's install even when a code
    path forgets to inject ``paths`` (the autouse BRIDGE_HOME fixture)."""
    resolved = Paths.resolve()
    assert tmp_path in resolved.home.parents
    assert resolved.home != Path.home() / ".bridge"


def test_lab_capture_env_is_fenced_out_of_every_test(tmp_path: Path):
    """Proof of the ``BRIDGE_LAB_CAPTURE`` fence (the autouse
    ``_no_real_bridge_home`` fixture in ``tests/conftest.py``): whatever a
    developer's outer shell has exported, neither variable is visible from
    inside a test. This is the direct guard against the real incident -- a
    shell with wire capture exported for a live ``bridge lab`` run had a test
    suite pass through it and append ~2,500 test frames into the live
    capture's ``wire.jsonl`` -- because ``RpcEndpoint``/``RouterServer`` read
    ``os.environ`` directly (see ``bridge/mcp.py``, ``bridge/router.py``), not
    an injected value, so nothing short of clearing the process environment
    itself closes the gap."""
    assert os.environ.get(CAPTURE_ENV) is None
    assert os.environ.get(CAPTURE_FULL_ENV) is None


def test_paths_ensure_permissions(paths: Paths):
    assert paths.home.is_dir()
    assert paths.sessions_dir.is_dir()
    assert paths.logs_dir.is_dir()
    mode = stat.S_IMODE(paths.home.stat().st_mode)
    assert mode == 0o700


def test_frozen_clock(clock: FrozenClock):
    t0 = clock.now()
    clock.advance(5)
    assert clock.now() == t0 + 5


def test_seeded_ids_are_deterministic():
    a = SeededIds()
    b = SeededIds()
    assert a.new() == b.new()
    assert a.new() != a.new()


def test_frame_roundtrip():
    obj = {"t": "req", "id": 1, "op": "hello", "args": {"x": [1, 2, 3]}}
    buf = FrameBuffer()
    buf.feed(encode_frame(obj))
    frames = buf.frames()
    assert frames == [obj]
    assert decode_frame(encode_frame(obj)[4:]) == obj


def test_fake_executable_captures_argv_and_env(tmp_path: Path):
    capture = tmp_path / "cap.jsonl"
    bindir = tmp_path / "bin"
    make_capture_exe(bindir, "claude", capture)
    env = prepend_path(bindir)
    env["BRIDGE_SESSION_ID"] = "sess-123"
    subprocess.run(["claude", "--foo", "bar"], env=env, check=True)
    records = read_captures(capture)
    assert records[0]["argv"] == ["--foo", "bar"]
    assert records[0]["env"]["BRIDGE_SESSION_ID"] == "sess-123"


def test_network_guard_blocks_internet():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access blocked"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


def test_unix_sockets_are_allowed(tmp_path: Path):
    sock_path = tmp_path / "u.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cli.connect(str(sock_path))  # must not raise
    cli.close()
    srv.close()


def test_no_real_vendor_binary_on_test_path():
    # Sanity: the hermetic suite must never see a real vendor CLI unless a
    # fake was explicitly installed onto PATH by a test.
    assert os.environ.get("BRIDGE_ALLOW_REAL_VENDOR") is None
