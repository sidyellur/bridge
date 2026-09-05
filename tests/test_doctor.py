"""Task 10 verify (doctor): healthy after install, flags missing registration,
bad permissions, obsolete hooks, and runs a token-free loopback probe.
"""

from __future__ import annotations

import json

from bridge.claude_probe import ChannelMode, ChannelSupport
from bridge.doctor import FAIL, OK, WARN, doctor
from bridge.install import install

from .fakes.router_peer import RunningRouter

# A hermetic default: PLUGIN/OK keeps `report.ok` true for tests that aren't
# specifically exercising channel-mode detection, and never invokes a real
# `claude` binary.
_DEFAULT_PROBE = lambda: ChannelMode(  # noqa: E731
    ChannelSupport.PLUGIN, "1.0.0-test", ["--channels", "plugin:bridge@test-marketplace"]
)


def _install(paths, fake_user_home, probe=None):
    install(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        probe=probe or _DEFAULT_PROBE,
    )


def _doctor(paths, fake_user_home, probe=None, **kw):
    return doctor(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        check_binaries=False,
        probe=probe or _DEFAULT_PROBE,
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


def test_channel_mode_ok_for_plugin(paths, fake_user_home):
    probe = lambda: ChannelMode(  # noqa: E731
        ChannelSupport.PLUGIN, "3.0.0", ["--channels", "plugin:bridge@test-marketplace"]
    )
    _install(paths, fake_user_home, probe=probe)
    report = _doctor(paths, fake_user_home, probe=probe)
    assert _status(report, "Claude Channel mode") == OK
    assert report.ok


def test_channel_mode_warns_for_development(paths, fake_user_home):
    probe = lambda: ChannelMode(  # noqa: E731
        ChannelSupport.DEVELOPMENT, "2.0.0", ["--channels", "dev:bridge"]
    )
    _install(paths, fake_user_home, probe=probe)
    report = _doctor(paths, fake_user_home, probe=probe)
    assert _status(report, "Claude Channel mode") == WARN
    assert report.ok  # WARN alone does not fail the overall report


def test_channel_mode_fails_when_unsupported(paths, fake_user_home):
    probe = lambda: ChannelMode(ChannelSupport.UNSUPPORTED, "0.9.0")  # noqa: E731
    _install(paths, fake_user_home, probe=probe)
    report = _doctor(paths, fake_user_home, probe=probe)
    assert _status(report, "Claude Channel mode") == FAIL
    assert not report.ok


def test_policy_check_ok_when_no_sessions(paths, fake_user_home):
    _install(paths, fake_user_home)
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude channel policy") == OK


def test_policy_error_surfaced_from_session_meta(paths, fake_user_home):
    _install(paths, fake_user_home)
    paths.ensure_session_dir("claude-1")
    paths.session_meta("claude-1").write_text(
        json.dumps(
            {
                "policy_error": "organization policy blocks the claude/channel capability",
                "channel_enabled": False,
            }
        )
    )
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude channel policy") == WARN
    detail = next(c.detail for c in report.checks if c.name == "Claude channel policy")
    assert "organization policy" in detail
    assert "claude-1" in detail


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
