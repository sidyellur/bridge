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
