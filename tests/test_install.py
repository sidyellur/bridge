"""Task 10 verify (install/uninstall): registration, guidance, permissions,
idempotency, preservation of unrelated settings, obsolete-config migration, and
reversible uninstall that keeps transcripts.
"""

from __future__ import annotations

import json
import stat

from bridge.install import (
    claude_settings_path,
    codex_config_path,
    install,
    uninstall,
)


def _install(paths, fake_user_home, **kw):
    return install(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
        **kw,
    )


def test_install_registers_both_families(paths, fake_user_home):
    _install(paths, fake_user_home)
    settings = json.loads(claude_settings_path(fake_user_home / ".claude").read_text())
    assert settings["mcpServers"]["bridge"]["args"] == ["serve", "--family", "claude"]
    codex_cfg = codex_config_path(fake_user_home / ".codex").read_text()
    assert "[mcp_servers.bridge]" in codex_cfg
    assert 'args = ["serve", "--family", "codex"]' in codex_cfg


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
        claude_settings_path(fake_user_home / ".claude"),
        codex_config_path(fake_user_home / ".codex"),
        fake_user_home / ".claude" / "CLAUDE.md",
        fake_user_home / ".codex" / "AGENTS.md",
    ]
    first = {f: f.read_bytes() for f in files}
    _install(paths, fake_user_home)
    for f in files:
        assert f.read_bytes() == first[f], f"{f} changed on second install"


def test_install_preserves_unrelated_settings(paths, fake_user_home):
    settings = claude_settings_path(fake_user_home / ".claude")
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    _install(paths, fake_user_home)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "bridge" in data["mcpServers"]


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
    assert not claude_settings_path(fake_user_home / ".claude").exists()
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
    settings = json.loads(claude_settings_path(fake_user_home / ".claude").read_text())
    assert "bridge" not in settings.get("mcpServers", {})
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
    settings = claude_settings_path(fake_user_home / ".claude")
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"theme": "dark"}))
    _install(paths, fake_user_home)
    uninstall(
        paths=paths,
        claude_home=fake_user_home / ".claude",
        codex_home=fake_user_home / ".codex",
    )
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
