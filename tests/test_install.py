"""Task 10 verify (install/uninstall): registration, guidance, permissions,
idempotency, preservation of unrelated settings, obsolete-config migration, and
reversible uninstall that keeps transcripts.
"""

from __future__ import annotations

import json
import stat

import pytest

from bridge.claude_probe import ChannelMode, ChannelSupport
from bridge.install import (
    InstallError,
    claude_legacy_settings_path,
    claude_mcp_config_path,
    codex_config_path,
    default_bridge_executable,
    install,
    uninstall,
)

DEV_CHANNEL_ARGS = ["--dangerously-load-development-channels", "server:bridge"]


def _fake_probe(
    support: ChannelSupport = ChannelSupport.UNSUPPORTED,
    version: str = "",
    args: list[str] | None = None,
):
    """A hermetic stand-in for claude_probe.detect_channel_mode. Tests that
    don't care about channel detection get an UNSUPPORTED probe by default so
    no real ``claude`` binary is ever invoked and no channel args file is
    written, matching this suite's pre-detection behavior."""
    mode = ChannelMode(support, version, list(args or []))
    return lambda: mode


def _fake_bridge_executable(fake_user_home) -> str:
    """A real, executable stand-in for the installed ``bridge`` command, under
    tmp_path so no test ever registers the maintainer's real binary."""
    exe = fake_user_home / "bin" / "bridge"
    if not exe.exists():
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    return str(exe)


def _install(paths, fake_user_home, **kw):
    kw.setdefault("probe", _fake_probe())
    kw.setdefault("bridge_executable", _fake_bridge_executable(fake_user_home))
    return install(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        **kw,
    )


def test_install_registers_both_families(paths, fake_user_home):
    exe = _fake_bridge_executable(fake_user_home)
    _install(paths, fake_user_home)
    config = json.loads(claude_mcp_config_path(fake_user_home / ".claude").read_text())
    assert config["mcpServers"]["bridge"] == {
        "type": "stdio",
        "command": exe,
        "args": ["serve", "--family", "claude"],
    }
    codex_cfg = codex_config_path(fake_user_home / ".codex").read_text()
    assert "[mcp_servers.bridge]" in codex_cfg
    assert 'args = ["serve", "--family", "codex"]' in codex_cfg


def test_claude_registration_lands_in_dot_claude_json(paths, fake_user_home):
    """Claude Code reads user-scope MCP servers from ~/.claude.json, not from
    ~/.claude/settings.json."""
    assert claude_mcp_config_path(fake_user_home / ".claude") == fake_user_home / ".claude.json"
    _install(paths, fake_user_home)
    assert (fake_user_home / ".claude.json").exists()
    assert not claude_legacy_settings_path(fake_user_home / ".claude").exists()


def test_codex_block_uses_the_resolved_command(paths, fake_user_home):
    exe = _fake_bridge_executable(fake_user_home)
    _install(paths, fake_user_home)
    codex_cfg = codex_config_path(fake_user_home / ".codex").read_text()
    assert f"command = {json.dumps(exe)}" in codex_cfg


def test_install_writes_guidance_blocks(paths, fake_user_home):
    _install(paths, fake_user_home)
    claude_md = (fake_user_home / ".claude" / "CLAUDE.md").read_text()
    agents_md = (fake_user_home / ".codex" / "AGENTS.md").read_text()
    for md in (claude_md, agents_md):
        assert "BEGIN bridge coordination guidance" in md
        assert "Use Bridge to coordinate, never to retrieve" in md


def test_token_permissions(paths, fake_user_home):
    _install(paths, fake_user_home)
    assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.home.stat().st_mode) == 0o700


def test_install_is_byte_identical_on_second_run(paths, fake_user_home):
    _install(paths, fake_user_home)
    files = [
        claude_mcp_config_path(fake_user_home / ".claude"),
        codex_config_path(fake_user_home / ".codex"),
        fake_user_home / ".claude" / "CLAUDE.md",
        fake_user_home / ".codex" / "AGENTS.md",
    ]
    first = {f: f.read_bytes() for f in files}
    _install(paths, fake_user_home)
    for f in files:
        assert f.read_bytes() == first[f], f"{f} changed on second install"


def test_install_writes_channel_args_for_development(paths, fake_user_home):
    probe = _fake_probe(ChannelSupport.DEVELOPMENT, "2.0.0", DEV_CHANNEL_ARGS)
    report = _install(paths, fake_user_home, probe=probe)
    args_file = paths.home / "claude_channel_args.json"
    assert json.loads(args_file.read_text()) == DEV_CHANNEL_ARGS
    assert report.channel_mode == "development"
    assert (
        "Claude Channel mode: development (research preview, claude 2.0.0): launching with "
        "--dangerously-load-development-channels server:bridge. Claude asks once at startup "
        "to confirm the development channel; organization policy may still block inbound "
        "delivery (bridge doctor reports this as a warning)."
    ) in report.notes


def test_install_writes_channel_args_for_plugin(paths, fake_user_home):
    probe = _fake_probe(
        ChannelSupport.PLUGIN, "3.0.0", ["--channels", "plugin:bridge@bridge-marketplace"]
    )
    report = _install(paths, fake_user_home, probe=probe)
    args_file = paths.home / "claude_channel_args.json"
    assert json.loads(args_file.read_text()) == ["--channels", "plugin:bridge@bridge-marketplace"]
    assert report.channel_mode == "plugin"
    assert any("plugin" in n.lower() for n in report.notes)


def test_install_writes_nothing_for_unsupported_channel(paths, fake_user_home):
    probe = _fake_probe(ChannelSupport.UNSUPPORTED, "0.9.0")
    report = _install(paths, fake_user_home, probe=probe)
    args_file = paths.home / "claude_channel_args.json"
    assert not args_file.exists()
    assert report.channel_mode == "unsupported"
    assert any("inbound-unreachable" in n for n in report.notes)


def test_install_unsupported_note_quotes_the_probe_detail(paths, fake_user_home):
    mode = ChannelMode(
        ChannelSupport.UNSUPPORTED,
        "2.1.233",
        [],
        "claude 2.1.233 predates Claude Code Channels (needs >= 2.1.234)",
    )
    report = _install(paths, fake_user_home, probe=lambda: mode)
    assert (
        "Claude channel unsupported (claude 2.1.233 predates Claude Code Channels "
        "(needs >= 2.1.234)): this session will be inbound-unreachable. "
        "Outbound Bridge tools still work."
    ) in report.notes


def test_install_channel_args_byte_identical_on_second_run(paths, fake_user_home):
    probe = _fake_probe(
        ChannelSupport.PLUGIN, "3.0.0", ["--channels", "plugin:bridge@bridge-marketplace"]
    )
    _install(paths, fake_user_home, probe=probe)
    args_file = paths.home / "claude_channel_args.json"
    first = args_file.read_bytes()
    _install(paths, fake_user_home, probe=probe)
    assert args_file.read_bytes() == first


def test_dry_run_does_not_write_channel_args(paths, fake_user_home):
    probe = _fake_probe(ChannelSupport.DEVELOPMENT, "2.0.0", DEV_CHANNEL_ARGS)
    report = _install(paths, fake_user_home, probe=probe, dry_run=True)
    assert not (paths.home / "claude_channel_args.json").exists()
    assert report.channel_mode == "development"


def test_install_preserves_unrelated_claude_json(paths, fake_user_home):
    config = claude_mcp_config_path(fake_user_home / ".claude")
    config.write_text(
        json.dumps({"numStartups": 3, "mcpServers": {"tether": {"command": "tether"}}})
    )
    _install(paths, fake_user_home)
    data = json.loads(config.read_text())
    assert data["numStartups"] == 3
    assert data["mcpServers"]["tether"] == {"command": "tether"}
    assert "bridge" in data["mcpServers"]


def test_install_refuses_to_overwrite_unparsable_claude_json(paths, fake_user_home):
    config = claude_mcp_config_path(fake_user_home / ".claude")
    config.write_text('{"numStartups": 3,')  # truncated: the user's real state
    before = config.read_bytes()
    with pytest.raises(InstallError) as excinfo:
        _install(paths, fake_user_home)
    assert str(config) in str(excinfo.value)
    assert config.read_bytes() == before


def test_install_publishes_claude_json_atomically(paths, fake_user_home):
    """The user's Claude Code state is never truncated in place: the new content
    is written to a sibling temp file and renamed over the original."""
    config = claude_mcp_config_path(fake_user_home / ".claude")
    config.write_text(json.dumps({"numStartups": 3}))
    _install(paths, fake_user_home)
    leftovers = [p.name for p in config.parent.iterdir() if p.name.startswith(".claude.json.")]
    assert leftovers == []
    data = json.loads(config.read_text())
    assert data["numStartups"] == 3
    assert "bridge" in data["mcpServers"]


def test_uninstall_publishes_claude_json_atomically(paths, fake_user_home):
    config = claude_mcp_config_path(fake_user_home / ".claude")
    config.write_text(json.dumps({"numStartups": 3}))
    _install(paths, fake_user_home)
    uninstall(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
    )
    leftovers = [p.name for p in config.parent.iterdir() if p.name.startswith(".claude.json.")]
    assert leftovers == []
    data = json.loads(config.read_text())
    assert data["numStartups"] == 3
    assert "bridge" not in data.get("mcpServers", {})


def test_install_refuses_a_claude_json_that_is_not_an_object(paths, fake_user_home):
    config = claude_mcp_config_path(fake_user_home / ".claude")
    config.write_text("[1, 2, 3]")
    before = config.read_bytes()
    with pytest.raises(InstallError):
        _install(paths, fake_user_home)
    assert config.read_bytes() == before


def test_install_migrates_a_legacy_settings_registration(paths, fake_user_home):
    legacy = claude_legacy_settings_path(fake_user_home / ".claude")
    legacy.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"bridge": {"command": "bridge"}}})
    )
    report = _install(paths, fake_user_home)
    data = json.loads(legacy.read_text())
    assert data["theme"] == "dark"
    assert "bridge" not in data.get("mcpServers", {})
    assert legacy in report.removed
    assert any("legacy registration in settings.json removed" in n for n in report.notes)


def test_install_leaves_an_unrelated_settings_json_alone(paths, fake_user_home):
    legacy = claude_legacy_settings_path(fake_user_home / ".claude")
    legacy.write_text(json.dumps({"theme": "dark"}))
    before = legacy.read_bytes()
    report = _install(paths, fake_user_home)
    assert legacy.read_bytes() == before
    assert legacy not in report.removed


def test_install_notes_a_bare_command_that_is_not_on_path(paths, fake_user_home):
    report = _install(paths, fake_user_home, bridge_executable="bridge", which=lambda _: None)
    assert any("bridge is not on PATH" in n for n in report.notes)


def test_install_is_silent_when_the_bare_command_resolves(paths, fake_user_home):
    report = _install(
        paths, fake_user_home, bridge_executable="bridge", which=lambda _: "/usr/local/bin/bridge"
    )
    assert not any("not on PATH" in n for n in report.notes)


def test_cli_install_reports_an_unparsable_claude_json_without_a_traceback(
    monkeypatch, tmp_path, capsys
):
    from bridge.install import cli_install

    home = tmp_path / "cli-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text('{"numStartups": 3,')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRIDGE_HOME", str(home / ".bridge"))
    monkeypatch.setattr("bridge.install.detect_channel_mode", _fake_probe())

    assert cli_install() == 1
    err = capsys.readouterr().err
    assert err.startswith("bridge install: ")
    assert "not valid JSON" in err
    assert (home / ".claude.json").read_text() == '{"numStartups": 3,'


def test_default_bridge_executable_prefers_argv0(monkeypatch, tmp_path):
    exe = tmp_path / "venv" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(exe), "install"])
    assert default_bridge_executable() == str(exe.resolve())


def test_default_bridge_executable_falls_back_to_which(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", [str(tmp_path / "pytest"), "install"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/bridge")
    assert default_bridge_executable() == "/usr/local/bin/bridge"


def test_default_bridge_executable_falls_back_to_the_bare_name(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", [str(tmp_path / "pytest"), "install"])
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert default_bridge_executable() == "bridge"


def test_install_preserves_unrelated_markdown(paths, fake_user_home):
    claude_md = fake_user_home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    claude_md.write_text("# My rules\nAlways be kind.\n")
    _install(paths, fake_user_home)
    text = claude_md.read_text()
    assert "Always be kind." in text
    assert "BEGIN bridge coordination guidance" in text


def test_install_migrates_obsolete_hooks(paths, fake_user_home):
    paths.ensure()
    (paths.home / "spool").mkdir()
    (paths.home / "hooks").mkdir()
    (fake_user_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_user_home / ".claude" / "bridge_hook.json").write_text("{}")
    report = _install(paths, fake_user_home)
    assert not (paths.home / "spool").exists()
    assert not (paths.home / "hooks").exists()
    assert not (fake_user_home / ".claude" / "bridge_hook.json").exists()
    assert any("bridge_hook.json" in str(p) for p in report.removed)


def test_dry_run_touches_nothing(paths, fake_user_home):
    report = _install(paths, fake_user_home, dry_run=True)
    assert not claude_mcp_config_path(fake_user_home / ".claude").exists()
    assert report.touched  # still reports what it would do


def test_uninstall_keeps_transcripts_and_removes_registration(paths, fake_user_home):
    _install(paths, fake_user_home)
    # simulate an existing transcript db
    paths.db.write_bytes(b"sqlite")
    uninstall(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
    )
    assert paths.db.exists()  # transcripts kept
    assert not paths.token.exists()
    config = json.loads(claude_mcp_config_path(fake_user_home / ".claude").read_text())
    assert "bridge" not in config.get("mcpServers", {})
    codex_cfg = codex_config_path(fake_user_home / ".codex").read_text()
    assert "[mcp_servers.bridge]" not in codex_cfg
    claude_md = (fake_user_home / ".claude" / "CLAUDE.md").read_text()
    assert "BEGIN bridge coordination guidance" not in claude_md


def test_uninstall_purge_deletes_transcripts(paths, fake_user_home):
    _install(paths, fake_user_home)
    paths.db.write_bytes(b"sqlite")
    uninstall(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        purge_transcripts=True,
    )
    assert not paths.db.exists()


def test_uninstall_preserves_unrelated_after_reinstall_cycle(paths, fake_user_home):
    config = claude_mcp_config_path(fake_user_home / ".claude")
    config.write_text(json.dumps({"numStartups": 3}))
    _install(paths, fake_user_home)
    uninstall(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
    )
    data = json.loads(config.read_text())
    assert data["numStartups"] == 3


def test_uninstall_removes_the_entry_from_both_claude_files(paths, fake_user_home):
    legacy = claude_legacy_settings_path(fake_user_home / ".claude")
    _install(paths, fake_user_home)
    # A legacy entry written back after install (e.g. by an older bridge).
    legacy.write_text(json.dumps({"theme": "dark", "mcpServers": {"bridge": {"command": "x"}}}))
    report = uninstall(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
    )
    config = json.loads(claude_mcp_config_path(fake_user_home / ".claude").read_text())
    assert "bridge" not in config.get("mcpServers", {})
    assert "bridge" not in json.loads(legacy.read_text()).get("mcpServers", {})
    assert claude_mcp_config_path(fake_user_home / ".claude") in report.removed
    assert legacy in report.removed
