"""Task 10 verify (doctor): healthy after install, flags missing registration,
bad permissions, obsolete hooks, and runs a token-free loopback probe.
"""

from __future__ import annotations

import json

from bridge.claude_probe import ChannelMode, ChannelSupport
from bridge.doctor import FAIL, OK, WARN, doctor
from bridge.install import (
    claude_legacy_settings_path,
    claude_mcp_config_path,
    codex_config_path,
    install,
)

from .fakes.router_peer import RunningRouter

# A hermetic default: PLUGIN/OK keeps `report.ok` true for tests that aren't
# specifically exercising channel-mode detection, and never invokes a real
# `claude` binary.
_DEFAULT_PROBE = lambda: ChannelMode(  # noqa: E731
    ChannelSupport.PLUGIN, "1.0.0-test", ["--channels", "plugin:bridge@test-marketplace"]
)

# Hermetic default for command resolution: nothing is on PATH unless a test
# injects a `which` that says otherwise.
_NOTHING_ON_PATH = lambda _name: None  # noqa: E731


def _fake_bridge_executable(fake_user_home) -> str:
    exe = fake_user_home / "bin" / "bridge"
    if not exe.exists():
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    return str(exe)


def _install(paths, fake_user_home, probe=None, **kw):
    kw.setdefault("bridge_executable", _fake_bridge_executable(fake_user_home))
    install(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        probe=probe or _DEFAULT_PROBE,
        **kw,
    )


def _doctor(paths, fake_user_home, probe=None, which=None, **kw):
    return doctor(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        check_binaries=False,
        probe=probe or _DEFAULT_PROBE,
        which=which or _NOTHING_ON_PATH,
        **kw,
    )


def _status(report, name):
    return next(c.status for c in report.checks if c.name == name)


def _detail(report, name):
    return next(c.detail for c in report.checks if c.name == name)


def _names(report):
    return [c.name for c in report.checks]


def test_healthy_after_install(paths, fake_user_home):
    exe = _fake_bridge_executable(fake_user_home)
    _install(paths, fake_user_home)
    report = _doctor(paths, fake_user_home)
    assert report.ok
    assert _status(report, "router token permissions") == OK
    assert _status(report, "Claude MCP registration") == OK
    assert _detail(report, "Claude MCP registration") == f"~/.claude.json ({exe})"
    assert _status(report, "Codex MCP registration") == OK
    assert exe in _detail(report, "Codex MCP registration")


def test_missing_registration_flags_fail(paths, fake_user_home):
    claude_mcp_config_path(fake_user_home / ".claude").write_text("{}")
    codex_config_path(fake_user_home / ".codex").write_text("# empty\n")
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == FAIL
    assert "~/.claude.json" in _detail(report, "Claude MCP registration")
    assert _status(report, "Codex MCP registration") == FAIL
    assert not report.ok


def test_missing_claude_json_warns(paths, fake_user_home):
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == WARN
    assert _detail(report, "Claude MCP registration") == (
        "~/.claude.json missing; run `bridge install`"
    )


def test_unparsable_claude_json_fails(paths, fake_user_home):
    claude_mcp_config_path(fake_user_home / ".claude").write_text('{"numStartups": 3,')
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == FAIL
    assert _detail(report, "Claude MCP registration") == "~/.claude.json is not valid JSON"


def _register_claude_command(fake_user_home, command: str) -> None:
    claude_mcp_config_path(fake_user_home / ".claude").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bridge": {
                        "type": "stdio",
                        "command": command,
                        "args": ["serve", "--family", "claude"],
                    }
                }
            }
        )
    )


def test_registered_absolute_command_that_is_missing_fails(paths, fake_user_home):
    missing = str(fake_user_home / "bin" / "bridge")
    _register_claude_command(fake_user_home, missing)
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == FAIL
    assert _detail(report, "Claude MCP registration") == (
        f"registered command {missing} does not exist or is not executable"
    )


def test_registered_absolute_command_that_is_not_executable_fails(paths, fake_user_home):
    not_exec = fake_user_home / "bin" / "bridge"
    not_exec.parent.mkdir(parents=True, exist_ok=True)
    not_exec.write_text("#!/bin/sh\n")
    not_exec.chmod(0o644)
    _register_claude_command(fake_user_home, str(not_exec))
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == FAIL
    assert "is not executable" in _detail(report, "Claude MCP registration")


def test_registered_bare_command_off_path_fails(paths, fake_user_home):
    _register_claude_command(fake_user_home, "bridge")
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == FAIL
    assert _detail(report, "Claude MCP registration") == (
        "registered command `bridge` is not on PATH"
    )


def test_registered_bare_command_on_path_is_ok(paths, fake_user_home):
    _register_claude_command(fake_user_home, "bridge")
    report = _doctor(paths, fake_user_home, which=lambda _: "/usr/local/bin/bridge")
    assert _status(report, "Claude MCP registration") == OK
    assert _detail(report, "Claude MCP registration") == "~/.claude.json (bridge)"


def test_stale_legacy_settings_entry_warns(paths, fake_user_home):
    _install(paths, fake_user_home)
    claude_legacy_settings_path(fake_user_home / ".claude").write_text(
        json.dumps({"mcpServers": {"bridge": {"command": "bridge"}}})
    )
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Claude MCP registration") == OK
    assert _status(report, "Claude MCP registration (legacy)") == WARN
    assert _detail(report, "Claude MCP registration (legacy)") == (
        "stale entry in settings.json; run `bridge install`"
    )
    assert report.ok  # WARN alone does not fail the report


def test_no_legacy_row_when_settings_is_clean(paths, fake_user_home):
    _install(paths, fake_user_home)
    claude_legacy_settings_path(fake_user_home / ".claude").write_text(
        json.dumps({"theme": "dark"})
    )
    report = _doctor(paths, fake_user_home)
    assert "Claude MCP registration (legacy)" not in _names(report)


def test_codex_registered_command_that_is_missing_fails(paths, fake_user_home):
    missing = str(fake_user_home / "bin" / "bridge")
    codex_config_path(fake_user_home / ".codex").write_text(
        "[mcp_servers.bridge]\n"
        f"command = {json.dumps(missing)}\n"
        'args = ["serve", "--family", "codex"]\n'
    )
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Codex MCP registration") == FAIL
    assert _detail(report, "Codex MCP registration") == (
        f"registered command {missing} does not exist or is not executable"
    )


def test_codex_registered_bare_command_off_path_fails(paths, fake_user_home):
    codex_config_path(fake_user_home / ".codex").write_text(
        '[mcp_servers.bridge]\ncommand = "bridge"\nargs = ["serve", "--family", "codex"]\n'
    )
    report = _doctor(paths, fake_user_home)
    assert _status(report, "Codex MCP registration") == FAIL
    assert _detail(report, "Codex MCP registration") == (
        "registered command `bridge` is not on PATH"
    )


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
