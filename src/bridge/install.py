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
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .claude_probe import ChannelMode, ChannelSupport, detect_channel_mode
from .launch import CLAUDE_CHANNEL_ARGS_FILE
from .paths import Paths
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

CODEX_MCP_BLOCK = """\
[mcp_servers.bridge]
command = "bridge"
args = ["serve", "--family", "codex"]
"""


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


def claude_settings_path(claude_home: Path) -> Path:
    return claude_home / "settings.json"


def codex_config_path(codex_home: Path) -> Path:
    return codex_home / "config.toml"


def install(
    *,
    paths: Paths,
    claude_home: Path,
    codex_home: Path,
    dry_run: bool = False,
    probe: Callable[[], ChannelMode] | None = None,
) -> InstallReport:
    report = InstallReport(dry_run=dry_run)

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
    settings = claude_settings_path(claude_home)
    if not dry_run:
        _merge_json_mcp(settings, "claude")
    report.touched.append(settings)

    # 4. Codex MCP registration.
    codex_home.mkdir(parents=True, exist_ok=True)
    codex_cfg = codex_config_path(codex_home)
    if not dry_run:
        _upsert_block(codex_cfg, TOML_BEGIN, TOML_END, CODEX_MCP_BLOCK)
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
    if mode.support is ChannelSupport.PLUGIN:
        report.notes.append(
            f"Claude Channel mode: plugin (claude {mode.version or 'unknown'}); "
            "launching with --channels plugin:bridge@<marketplace>"
        )
    elif mode.support is ChannelSupport.DEVELOPMENT:
        report.notes.append(
            "Claude Channel mode: development (research preview) "
            f"(claude {mode.version or 'unknown'}); launching with the development "
            "channel flag. Organization policy may still block inbound delivery; "
            "run `bridge doctor` to check."
        )
    else:
        detail = f" ({mode.detail})" if mode.detail else ""
        report.notes.append(
            "Claude channel unsupported"
            f" (claude {mode.version or 'not found'}){detail}: this session will be "
            "inbound-unreachable until Claude Code Channels are available. "
            "Outbound Bridge tools still work."
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
    settings = claude_settings_path(claude_home)
    if settings.exists() and _remove_json_mcp(settings):
        report.removed.append(settings)
    for md, begin, end in (
        (claude_home / "CLAUDE.md", MD_BEGIN, MD_END),
        (codex_home / "AGENTS.md", MD_BEGIN, MD_END),
        (codex_config_path(codex_home), TOML_BEGIN, TOML_END),
    ):
        if md.exists() and _remove_block(md, begin, end):
            report.removed.append(md)
    return report


# --- helpers ---------------------------------------------------------------


def _merge_json_mcp(path: Path, family: str) -> None:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    servers = data.setdefault("mcpServers", {})
    servers["bridge"] = {"command": "bridge", "args": ["serve", "--family", family]}
    path.write_text(json.dumps(data, indent=2) + "\n")
    _chmod_600(path)


def _remove_json_mcp(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    servers = data.get("mcpServers", {})
    if "bridge" in servers:
        del servers["bridge"]
        if not servers:
            data.pop("mcpServers", None)
        path.write_text(json.dumps(data, indent=2) + "\n")
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
        import shutil

        shutil.rmtree(path)
    else:
        path.unlink()


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass


# --- CLI shims -------------------------------------------------------------


def cli_install(dry_run: bool = False) -> int:  # pragma: no cover - thin shim
    import os

    paths = Paths.resolve()
    claude_home = Path(os.environ.get("HOME", str(Path.home()))) / ".claude"
    codex_home = Path(os.environ.get("HOME", str(Path.home()))) / ".codex"
    report = install(paths=paths, claude_home=claude_home, codex_home=codex_home, dry_run=dry_run)
    print(report.render())
    return 0


def cli_uninstall() -> int:  # pragma: no cover - thin shim
    import os

    paths = Paths.resolve()
    claude_home = Path(os.environ.get("HOME", str(Path.home()))) / ".claude"
    codex_home = Path(os.environ.get("HOME", str(Path.home()))) / ".codex"
    report = uninstall(paths=paths, claude_home=claude_home, codex_home=codex_home)
    print(report.render())
    return 0
