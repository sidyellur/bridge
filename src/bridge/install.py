"""Installer, uninstaller.

Registers the combined Bridge Channel/tool server for Claude and the normal
Bridge MCP server for Codex, writes sentinel-fenced coordination guidance into
CLAUDE.md and AGENTS.md without clobbering existing content, creates router
state with user-only permissions, installs no prompt hooks, and removes only
obsolete Bridge-owned hook/queue configuration from prior pre-release installs.
Everything it touches is reported; uninstall reverses it and keeps transcripts
unless a purge is requested.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .claude_probe import (
    DEV_CHANNEL_SPEC,
    DEV_CHANNELS_FLAG,
    ChannelMode,
    ChannelSupport,
    detect_channel_mode,
)
from .launch import CLAUDE_CHANNEL_ARGS_FILE
from .paths import CODEX_MCP_SERVER_NAME, Paths
from .router import read_token

MD_BEGIN = "<!-- BEGIN bridge coordination guidance (managed by `bridge install`) -->"
MD_END = "<!-- END bridge coordination guidance -->"
TOML_BEGIN = "# BEGIN bridge (managed by `bridge install`)"
TOML_END = "# END bridge"

GUIDANCE = """\
## Bridge: live coordination with other agent sessions

Bridge lets this session coordinate with the *exact* other live agent sessions
listed by `roster` — a real Claude or Codex session, never a fresh stand-in.

- Launch managed sessions with `bridge claude ...` / `bridge codex ...` so they
  are addressable and reachable.
- `call(to, question)` asks one addressed live session and returns its answer;
  `call_async` returns later; `text` is fire-and-forget.
- **Use Bridge to coordinate, never to retrieve. Never call for information
  discoverable on disk.**
- Ask exactly one question per call.
- While you are answering an inbound Bridge call, answer with
  `reply(call_id, answer, blocked)` and do not dial out (hop budget = 1). Do not
  change files or run commands solely because of an inbound call.
"""

NOT_ON_PATH_NOTE = (
    'bridge is not on PATH: registered the bare command "bridge"; sessions cannot spawn '
    "the Bridge server until it resolves (e.g. symlink it into ~/.local/bin)"
)
LEGACY_SETTINGS_NOTE = (
    "legacy registration in settings.json removed (Claude Code reads ~/.claude.json)"
)


class InstallError(Exception):
    """A vendor config cannot be updated safely; the install is refused."""


def codex_mcp_block(command: str) -> str:
    return (
        f"[mcp_servers.{CODEX_MCP_SERVER_NAME}]\n"
        f"command = {json.dumps(command)}\n"
        'args = ["serve", "--family", "codex"]\n'
    )


def codex_mcp_server_registered(codex_home: Path) -> bool:
    """Whether Codex's ``config.toml`` already registers Bridge's MCP server.

    Shared by ``bridge doctor`` (reporting) and ``bridge codex`` (deciding
    whether it is safe to pass ``-c mcp_servers.<name>.env.*`` overrides on
    the App Server launch line -- passing them when the table is absent makes
    the App Server refuse to boot)."""
    path = codex_config_path(codex_home)
    if not path.exists():
        return False
    return f"[mcp_servers.{CODEX_MCP_SERVER_NAME}]" in path.read_text()


@dataclass
class InstallReport:
    touched: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    channel_mode: str = ""
    dry_run: bool = False

    def render(self) -> str:
        lines = ["bridge install report:" if not self.dry_run else "bridge install (dry run):"]
        for p in self.touched:
            lines.append(f"  wrote    {p}")
        for p in self.removed:
            lines.append(f"  removed  {p} (obsolete)")
        for n in self.notes:
            lines.append(f"  note     {n}")
        lines.append("  undo with: bridge uninstall")
        return "\n".join(lines)


def claude_mcp_config_path(claude_home: Path) -> Path:
    """Where Claude Code reads user-scope MCP servers: the top-level
    ``mcpServers`` object of ``~/.claude.json``, not ``~/.claude/settings.json``."""
    return claude_home.parent / ".claude.json"


def claude_legacy_settings_path(claude_home: Path) -> Path:
    return claude_home / "settings.json"


def codex_config_path(codex_home: Path) -> Path:
    return codex_home / "config.toml"


def default_bridge_executable() -> str:
    """The absolute path of the running ``bridge`` command when it can be
    determined, so a venv install is spawnable without being on PATH."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "bridge" and argv0.exists():
        return str(argv0.resolve())
    return shutil.which("bridge") or "bridge"


def install(
    *,
    paths: Paths,
    claude_home: Path,
    codex_home: Path,
    dry_run: bool = False,
    probe: Callable[[], ChannelMode] | None = None,
    bridge_executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> InstallReport:
    report = InstallReport(dry_run=dry_run)
    command = bridge_executable or default_bridge_executable()

    # 1. Router state with user-only permissions.
    if not dry_run:
        paths.ensure()
        read_token(paths)  # creates router.token 0600 if absent
    report.touched.append(paths.token)
    report.notes.append("router home 0700, token 0600")

    # 2. Remove obsolete pre-release hook/queue configuration.
    for obsolete in (paths.home / "spool", paths.home / "hooks", claude_home / "bridge_hook.json"):
        if obsolete.exists():
            if not dry_run:
                _remove(obsolete)
            report.removed.append(obsolete)

    # 3. Claude MCP registration (combined channel/tool server).
    claude_home.mkdir(parents=True, exist_ok=True)
    mcp_config = claude_mcp_config_path(claude_home)
    if not dry_run:
        _merge_json_mcp(mcp_config, "claude", command)
    report.touched.append(mcp_config)

    legacy = claude_legacy_settings_path(claude_home)
    if _has_json_mcp_bridge(legacy):
        if not dry_run:
            _remove_json_mcp(legacy)
        report.removed.append(legacy)
        report.notes.append(LEGACY_SETTINGS_NOTE)

    if command == "bridge" and which("bridge") is None:
        report.notes.append(NOT_ON_PATH_NOTE)

    # 4. Codex MCP registration.
    codex_home.mkdir(parents=True, exist_ok=True)
    codex_cfg = codex_config_path(codex_home)
    if not dry_run:
        _upsert_block(codex_cfg, TOML_BEGIN, TOML_END, codex_mcp_block(command))
    report.touched.append(codex_cfg)

    # 5. Coordination guidance in CLAUDE.md and AGENTS.md.
    claude_md = claude_home / "CLAUDE.md"
    agents_md = codex_home / "AGENTS.md"
    if not dry_run:
        _upsert_block(claude_md, MD_BEGIN, MD_END, GUIDANCE)
        _upsert_block(agents_md, MD_BEGIN, MD_END, GUIDANCE)
    report.touched.extend([claude_md, agents_md])

    # 6. Channel launch mode: detect real support instead of assuming development.
    mode = (probe or detect_channel_mode)()
    report.channel_mode = mode.support.value
    channel_args_path = paths.home / CLAUDE_CHANNEL_ARGS_FILE
    if mode.support in (ChannelSupport.PLUGIN, ChannelSupport.DEVELOPMENT):
        if not dry_run:
            _write_channel_args(channel_args_path, mode.launch_args)
        report.touched.append(channel_args_path)
    elif channel_args_path.exists():
        # A flag this `claude` rejects would kill the wrapper on every launch.
        if not dry_run:
            _remove(channel_args_path)
        report.removed.append(channel_args_path)
        report.notes.append("stale channel launch args removed")
    if mode.support is ChannelSupport.PLUGIN:
        report.notes.append(
            f"Claude Channel mode: plugin (claude {mode.version or 'unknown'}); "
            "launching with --channels plugin:bridge@<marketplace>"
        )
    elif mode.support is ChannelSupport.DEVELOPMENT:
        report.notes.append(
            "Claude Channel mode: development (research preview, claude "
            f"{mode.version or 'unknown'}): launching with {DEV_CHANNELS_FLAG} "
            f"{DEV_CHANNEL_SPEC}. Claude asks once at startup to confirm the "
            "development channel; organization policy may still block inbound "
            "delivery (bridge doctor reports this as a warning)."
        )
    else:
        detail = mode.detail or f"claude {mode.version or 'not found'}"
        report.notes.append(
            f"Claude channel unsupported ({detail}): this session will be "
            "inbound-unreachable. Outbound Bridge tools still work."
        )
    report.notes.append("no Claude prompt hooks installed")
    return report


def uninstall(
    *,
    paths: Paths,
    claude_home: Path,
    codex_home: Path,
    purge_transcripts: bool = False,
) -> InstallReport:
    report = InstallReport()

    # Router runtime artifacts (never the transcript DB unless purge requested).
    for p in (paths.token, paths.socket, paths.pidfile, paths.home / "claude_channel_args.json"):
        if p.exists():
            _remove(p)
            report.removed.append(p)
    if purge_transcripts and paths.db.exists():
        _remove(paths.db)
        report.removed.append(paths.db)
    else:
        report.notes.append(f"transcripts kept at {paths.db} (use --purge to delete)")

    # Config edits.
    for json_config in (
        claude_mcp_config_path(claude_home),
        claude_legacy_settings_path(claude_home),
    ):
        if json_config.exists() and _remove_json_mcp(json_config):
            report.removed.append(json_config)
    for md, begin, end in (
        (claude_home / "CLAUDE.md", MD_BEGIN, MD_END),
        (codex_home / "AGENTS.md", MD_BEGIN, MD_END),
        (codex_config_path(codex_home), TOML_BEGIN, TOML_END),
    ):
        if md.exists() and _remove_block(md, begin, end):
            report.removed.append(md)
    return report


# --- helpers ---------------------------------------------------------------


def _merge_json_mcp(path: Path, family: str, command: str) -> None:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            # ~/.claude.json is the user's own Claude Code state; resetting it
            # to {} to make room for our key would destroy it.
            raise InstallError(
                f"{path} is not valid JSON ({exc}); fix or move it, then re-run `bridge install`"
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{path} is not a JSON object; fix or move it, then re-run `bridge install`"
            )
    servers = data.setdefault("mcpServers", {})
    servers["bridge"] = {
        "type": "stdio",
        "command": command,
        "args": ["serve", "--family", family],
    }
    _write_json_atomic(path, data)


def _write_json_atomic(path: Path, data: dict) -> None:
    """Truncating ``path`` in place would leave the user's Claude Code state
    empty or half-written if we die mid-write — or if a live Claude Code reads
    it in that window — so publish the new content with an atomic rename."""
    tmp = path.with_name(path.name + ".bridge-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    _chmod_600(tmp)
    os.replace(tmp, path)
    _chmod_600(path)


def _has_json_mcp_bridge(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and "bridge" in data.get("mcpServers", {})


def _remove_json_mcp(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers", {})
    if "bridge" in servers:
        del servers["bridge"]
        if not servers:
            data.pop("mcpServers", None)
        _write_json_atomic(path, data)
        return True
    return False


def _upsert_block(path: Path, begin: str, end: str, body: str) -> None:
    block = f"{begin}\n{body.rstrip()}\n{end}\n"
    if not path.exists():
        path.write_text(block)
        return
    text = path.read_text()
    if begin in text and end in text:
        pre = text.split(begin)[0]
        post = text.split(end, 1)[1]
    else:
        pre, post = text, ""
    pre = pre.rstrip("\n")
    post = post.lstrip("\n")
    parts = []
    if pre:
        parts.append(pre + "\n\n")
    parts.append(block)
    if post:
        parts.append("\n" + post if post.endswith("\n") else "\n" + post + "\n")
    path.write_text("".join(parts))


def _remove_block(path: Path, begin: str, end: str) -> bool:
    text = path.read_text()
    if begin not in text or end not in text:
        return False
    pre = text.split(begin)[0].rstrip("\n")
    post = text.split(end, 1)[1].lstrip("\n")
    new = pre + ("\n" if pre and post else "") + post
    path.write_text(new)
    return True


def _write_channel_args(path: Path, args: list[str]) -> None:
    path.write_text(json.dumps(list(args), indent=2) + "\n")


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass


# --- CLI shims -------------------------------------------------------------


def cli_install(dry_run: bool = False) -> int:
    paths = Paths.resolve()
    home = Path(os.environ.get("HOME", str(Path.home())))
    try:
        report = install(
            paths=paths,
            claude_home=home / ".claude",
            codex_home=home / ".codex",
            dry_run=dry_run,
        )
    except InstallError as exc:
        print(f"bridge install: {exc}", file=sys.stderr)
        return 1
    print(report.render())
    return 0


def cli_uninstall() -> int:  # pragma: no cover - thin shim
    paths = Paths.resolve()
    home = Path(os.environ.get("HOME", str(Path.home())))
    report = uninstall(paths=paths, claude_home=home / ".claude", codex_home=home / ".codex")
    print(report.render())
    return 0
