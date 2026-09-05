"""Task 10 verify (doctor): healthy after install, flags missing registration,
bad permissions, obsolete hooks, and runs a token-free loopback probe.
"""

from __future__ import annotations

from bridge.doctor import FAIL, OK, WARN, doctor
from bridge.install import install

from .fakes.router_peer import RunningRouter


def _install(paths, fake_user_home):
    install(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
    )


def _doctor(paths, fake_user_home, **kw):
    return doctor(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        check_binaries=False,
        **kw,
    )


def _status(report, name):
    return next(c.status for c in report.checks if c.name == name)


def test_healthy_after_install(paths, fake_user_home):
    _install(paths, fake_user_home)
    report = _doctor(paths, fake_user_home)
    assert report.ok
    assert _status(report, "router token permissions") == OK
    assert _status(report, "Claude MCP registration") == OK
    assert _status(report, "Codex MCP registration") == OK


def test_missing_registration_flags_fail(paths, fake_user_home):
    (fake_user_home / ".claude" / "settings.json").write_text("{}")
    (fake_user_home / ".codex" / "config.toml").write_text("# empty\n")
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == FAIL
    assert _status(report, "Codex MCP registration") == FAIL
    assert not report.ok


def test_channel_mode_is_warned(paths, fake_user_home):
    _install(paths, fake_user_home)
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude Channel mode") == WARN


def test_bad_token_permissions_fail(paths, fake_user_home):
    _install(paths, fake_user_home)
    paths.token.chmod(0o644)
    report = _doctor(paths, fake_user_home)
    assert _status(report, "router token permissions") == FAIL
    assert not report.ok


def test_obsolete_hooks_flagged(paths, fake_user_home):
    _install(paths, fake_user_home)
    (paths.home / "spool").mkdir()
    report = _doctor(paths, fake_user_home)
    assert _status(report, "no obsolete hooks/queue") == FAIL


def test_loopback_probe_ok_with_running_router(paths, fake_user_home):
    _install(paths, fake_user_home)
    with RunningRouter(paths):
        report = _doctor(paths, fake_user_home)
        assert _status(report, "loopback protocol probe") == OK


def test_loopback_probe_ok_when_idle(paths, fake_user_home):
    _install(paths, fake_user_home)
    report = _doctor(paths, fake_user_home)
    assert _status(report, "loopback protocol probe") == OK  # idle: nothing to probe


def test_codex_liveness_ok_with_no_sessions(paths, fake_user_home):
    _install(paths, fake_user_home)
    report = _doctor(paths, fake_user_home)
    assert _status(report, "codex app-server liveness") == OK


def test_codex_liveness_ok_when_socket_is_listening(paths, fake_user_home):
    import socket

    from bridge.store import Store

    _install(paths, fake_user_home)
    session_id = "codex-live-1"
    store = Store.open(paths)
    store.upsert_session(session_id, "codex", state="idle", is_managed=True, reachable=True)
    store.close()

    sock_path = paths.ensure_session_dir(session_id) / "codex.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        srv.listen(1)
        report = _doctor(paths, fake_user_home)
        assert _status(report, "codex app-server liveness") == OK
        assert report.ok
    finally:
        srv.close()


def test_codex_liveness_fails_for_dead_socket_on_live_session(paths, fake_user_home):
    from bridge.store import Store

    _install(paths, fake_user_home)
    session_id = "codex-dead-1"
    store = Store.open(paths)
    store.upsert_session(session_id, "codex", state="working", is_managed=True, reachable=True)
    store.close()
    paths.ensure_session_dir(session_id)  # socket file never created: a crash

    report = _doctor(paths, fake_user_home)
    assert _status(report, "codex app-server liveness") == FAIL
    assert not report.ok


def test_codex_liveness_ignores_offline_sessions(paths, fake_user_home):
    from bridge.store import Store

    _install(paths, fake_user_home)
    session_id = "codex-offline-1"
    store = Store.open(paths)
    store.upsert_session(session_id, "codex", state="offline", is_managed=True, reachable=False)
    store.close()
    # No socket exists at all for this session; it must not be checked.

    report = _doctor(paths, fake_user_home)
    assert _status(report, "codex app-server liveness") == OK
    assert report.ok
