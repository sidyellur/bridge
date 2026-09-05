"""Shared fixtures. Every path is rooted in a pytest temp dir; the default suite
uses no network, no real vendor binaries, and no model tokens.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from bridge.paths import Paths

from .fakes.clock import FrozenClock
from .fakes.ids import SeededIds


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def ids() -> SeededIds:
    return SeededIds()


@pytest.fixture
def bridge_home(tmp_path: Path) -> Path:
    home = tmp_path / "bridge-home"
    return home


@pytest.fixture
def paths(bridge_home: Path) -> Paths:
    return Paths.resolve(home=bridge_home).ensure()


@pytest.fixture
def fake_user_home(tmp_path: Path) -> Path:
    """A fake ``$HOME`` for installer/config tests, with vendor config dirs."""
    home = tmp_path / "user-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    return home


@pytest.fixture
def store(paths: Paths, clock: FrozenClock):
    from bridge.store import Store

    st = Store.open(paths, now=clock.now)
    try:
        yield st
    finally:
        st.close()


@pytest.fixture
def router_core(paths: Paths, clock: FrozenClock, ids: SeededIds):
    """In-process router core (no socket) for unit tests of coordination logic."""
    from bridge.router import Router

    r = Router.create(paths, now=clock.now, new_id=ids.new)
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def running_router(paths: Paths, clock: FrozenClock, ids: SeededIds) -> Iterator:
    """A real router serving on its Unix socket in a background thread."""
    from .fakes.router_peer import RunningRouter

    with RunningRouter(paths, now=clock.now, new_id=ids.new) as rr:
        yield rr


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to reach a non-loopback AF_INET/AF_INET6 address."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _blocked(addr):
        if isinstance(addr, tuple) and addr:
            host = addr[0]
            if host not in ("127.0.0.1", "::1", "localhost", ""):
                raise RuntimeError(f"network access blocked in tests: {addr!r}")

    def connect(self, addr):  # type: ignore[no-untyped-def]
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _blocked(addr)
        return real_connect(self, addr)

    def connect_ex(self, addr):  # type: ignore[no-untyped-def]
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _blocked(addr)
        return real_connect_ex(self, addr)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
