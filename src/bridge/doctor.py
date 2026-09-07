"""``bridge doctor``: diagnose the installation with token-free local probes
where possible.

Checks router socket/token ownership and permissions, MCP registration on both
families, coordination guidance, the Claude Channel research-preview mode, Codex
App Server/remote availability, stale sessions, and a local loopback protocol
probe that spends no model tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import PROTOCOL_VERSION
from .claude_probe import (
    DEV_CHANNEL_SPEC,
    DEV_CHANNELS_FLAG,
    ChannelMode,
    ChannelSupport,
    detect_channel_mode,
)
from .install import (
    claude_legacy_settings_path,
    claude_mcp_config_path,
    codex_config_path,
)
from .paths import Paths

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)

    def render(self) -> str:
        symbol = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]"}
        lines = [
            f"{symbol.get(c.status, '[?]')} {c.name}" + (f": {c.detail}" if c.detail else "")
            for c in self.checks
        ]
        lines.append("")
        lines.append("doctor: healthy" if self.ok else "doctor: problems found")
        return "\n".join(lines)


def doctor(
    *,
    paths: Paths,
    claude_home: Path,
    codex_home: Path,
    check_binaries: bool = True,
    probe: Callable[[], ChannelMode] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> DoctorReport:
    report = DoctorReport()

    # Router home + token permissions (token-free).
    if paths.home.exists():
        mode = stat.S_IMODE(paths.home.stat().st_mode)
        report.add("router home permissions", OK if mode == 0o700 else WARN, oct(mode))
    else:
        report.add("router home", WARN, "not created yet; run `bridge install`")

    if paths.token.exists():
        mode = stat.S_IMODE(paths.token.stat().st_mode)
        report.add("router token permissions", OK if mode == 0o600 else FAIL, oct(mode))
    else:
        report.add("router token", WARN, "absent; created on first launch or install")

    if paths.socket.exists():
        mode = stat.S_IMODE(paths.socket.stat().st_mode)
        report.add("router socket permissions", OK if mode == 0o600 else FAIL, oct(mode))
    else:
        report.add("router socket", OK, "not listening (router idle)")

    # MCP registration.
    report.add(*_check_claude_registration(claude_home, which=which))
    legacy = _check_legacy_claude_registration(claude_home)
    if legacy is not None:
        report.add(*legacy)
    report.add(*_check_codex_registration(codex_home, which=which))

    # Coordination guidance.
    report.add(
        "Claude guidance",
        OK if _has_guidance(claude_home / "CLAUDE.md") else WARN,
        "CLAUDE.md",
    )
    report.add(
        "Codex guidance",
        OK if _has_guidance(codex_home / "AGENTS.md") else WARN,
        "AGENTS.md",
    )

    # Channel mode: detect real support instead of assuming a hard-coded mode.
    report.add(*_channel_mode_check(probe))
    report.add(*_handshake_check(paths))

    # Vendor binaries / remote flags.
    if check_binaries:
        report.add("claude binary", OK if which("claude") else WARN, "on PATH")
        report.add("codex binary", OK if which("codex") else WARN, "on PATH")

    # No prompt hooks / obsolete queue must be absent.
    obsolete = [p for p in (paths.home / "spool", paths.home / "hooks") if p.exists()]
    report.add(
        "no obsolete hooks/queue",
        OK if not obsolete else FAIL,
        ", ".join(str(p) for p in obsolete) if obsolete else "clean",
    )

    # Loopback protocol probe (uses the local token; no model tokens).
    report.add(*_loopback_probe(paths))

    # Per-session Codex App Server liveness (token-free): any managed session
    # the registry still considers live should have a listening App Server
    # socket. A dead socket behind a "live" session means bridge codex was
    # killed uncleanly and the state is stale.
    report.add(*_codex_app_server_liveness(paths))

    return report


def _unresolvable_command(command: str, which: Callable[[str], str | None]) -> str | None:
    """Why the registered command cannot be spawned, or ``None`` if it can."""
    if not command:
        return "no command registered"
    path = Path(command)
    if path.is_absolute():
        if path.exists() and os.access(path, os.X_OK):
            return None
        return f"registered command {command} does not exist or is not executable"
    if which(command) is None:
        return f"registered command `{command}` is not on PATH"
    return None


def _check_claude_registration(
    claude_home: Path, which: Callable[[str], str | None] = shutil.which
) -> tuple[str, str, str]:
    path = claude_mcp_config_path(claude_home)
    if not path.exists():
        return ("Claude MCP registration", WARN, "~/.claude.json missing; run `bridge install`")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return ("Claude MCP registration", FAIL, "~/.claude.json is not valid JSON")
    if not isinstance(data, dict):
        return ("Claude MCP registration", FAIL, "~/.claude.json is not valid JSON")
    entry = data.get("mcpServers", {}).get("bridge")
    if not isinstance(entry, dict):
        return (
            "Claude MCP registration",
            FAIL,
            "bridge server not registered in ~/.claude.json",
        )
    command = entry.get("command", "")
    problem = _unresolvable_command(command, which)
    if problem:
        return ("Claude MCP registration", FAIL, problem)
    return ("Claude MCP registration", OK, f"~/.claude.json ({command})")


def _check_legacy_claude_registration(claude_home: Path) -> tuple[str, str, str] | None:
    path = claude_legacy_settings_path(claude_home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "bridge" not in data.get("mcpServers", {}):
        return None
    return (
        "Claude MCP registration (legacy)",
        WARN,
        "stale entry in settings.json; run `bridge install`",
    )


def _check_codex_registration(
    codex_home: Path, which: Callable[[str], str | None] = shutil.which
) -> tuple[str, str, str]:
    path = codex_config_path(codex_home)
    if not path.exists():
        return ("Codex MCP registration", WARN, "config.toml missing; run `bridge install`")
    text = path.read_text()
    if "[mcp_servers.bridge]" not in text:
        return ("Codex MCP registration", FAIL, "bridge server not registered")
    command = _codex_registered_command(text)
    problem = _unresolvable_command(command, which)
    if problem:
        return ("Codex MCP registration", FAIL, problem)
    return ("Codex MCP registration", OK, f"config.toml ({command})")


def _codex_registered_command(text: str) -> str:
    """The ``command = "..."`` value inside the bridge TOML table, or ``""``."""
    _, _, rest = text.partition("[mcp_servers.bridge]")
    for line in rest.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if stripped.split("=", 1)[0].strip() == "command":
            _, _, raw = stripped.partition("=")
            try:
                value = json.loads(raw.strip())
            except json.JSONDecodeError:
                return ""
            return value if isinstance(value, str) else ""
    return ""


def _channel_mode_check(probe: Callable[[], ChannelMode] | None) -> tuple[str, str, str]:
    mode = (probe or detect_channel_mode)()
    if mode.support is ChannelSupport.PLUGIN:
        return (
            "Claude Channel mode",
            OK,
            f"plugin channel active (claude {mode.version or 'unknown'})",
        )
    if mode.support is ChannelSupport.DEVELOPMENT:
        return (
            "Claude Channel mode",
            WARN,
            f"research preview: {DEV_CHANNELS_FLAG} {DEV_CHANNEL_SPEC} "
            f"(claude {mode.version or 'unknown'}); organization policy may still "
            "block inbound delivery",
        )
    detail = f"claude {mode.version or 'not found'}"
    if mode.detail:
        detail += f": {mode.detail}"
    return (
        "Claude Channel mode",
        FAIL,
        f"unsupported ({detail}); this session is inbound-unreachable",
    )


def _handshake_check(paths: Paths) -> tuple[str, str, str]:
    """Report which live Claude sessions recorded an initialize handshake in
    their session.json. Claude Code drops the events of a channel it never loaded
    without telling the server, so a missing handshake is the only local signal
    that Bridge was not registered. Token-free: reads only what is on disk."""
    name = "Claude channel handshake"
    sessions_dir = paths.sessions_dir
    # Session dirs outlive their sessions, so only sessions the store still
    # considers live can say anything about the current install.
    live = _live_claude_session_ids(paths)
    if not sessions_dir.exists() or not live:
        return (name, OK, "no managed sessions recorded")
    clients: list[str] = []
    missing: list[str] = []
    for meta_path in sorted(sessions_dir.glob("*/session.json")):
        if meta_path.parent.name not in live:
            continue
        try:
            data = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        family = data.get("family")
        if family is not None and family != "claude":
            continue
        handshake = data.get("handshake")
        if isinstance(handshake, dict):
            info = handshake.get("client_info")
            info = info if isinstance(info, dict) else {}
            clients.append(f"{info.get('name') or 'unknown'} {info.get('version') or 'unknown'}")
        else:
            missing.append(meta_path.parent.name)
    if missing:
        return (name, WARN, _missing_handshake_detail(missing))
    if not clients:
        return (name, OK, "no managed sessions recorded")
    return (name, OK, f"{len(clients)} session(s) completed initialize ({', '.join(clients)})")


def _missing_handshake_detail(missing: list[str], limit: int = 3) -> str:
    listed = ", ".join(missing[:limit])
    if len(missing) > limit:
        listed += f" (+{len(missing) - limit} more)"
    subject = f"session {listed} has" if len(missing) == 1 else f"sessions {listed} have"
    return (
        f"{subject} no recorded initialize handshake; Claude may not have loaded "
        "the Bridge server (check the startup channels notice)"
    )


def _live_claude_session_ids(paths: Paths) -> set[str]:
    """Managed Claude sessions the store does not consider offline."""
    if not paths.db.exists():
        return set()

    from .store import STATE_OFFLINE, Store

    try:
        store = Store.open(paths, read_only=True)
    except Exception:  # noqa: BLE001 - a corrupt/locked db is reported by the liveness row
        return set()
    try:
        return {
            s.id
            for s in store.list_sessions(include_unmanaged=False)
            if s.family == "claude" and s.state != STATE_OFFLINE
        }
    finally:
        store.close()


def _has_guidance(path: Path) -> bool:
    return path.exists() and "BEGIN bridge coordination guidance" in path.read_text()


def _loopback_probe(paths: Paths) -> tuple[str, str, str]:
    from .router import is_running

    if not is_running(paths):
        return ("loopback protocol probe", OK, "router idle; nothing to probe")
    from .router_client import RouterClient, RouterClientError

    try:
        client = RouterClient.connect(paths, role="client", timeout=2.0)
    except RouterClientError as exc:
        return ("loopback protocol probe", FAIL, exc.message)
    try:
        client.call("roster", {})
        return ("loopback protocol probe", OK, f"protocol v{PROTOCOL_VERSION}")
    except RouterClientError as exc:
        return ("loopback protocol probe", FAIL, exc.message)
    finally:
        client.close()


def _codex_app_server_liveness(paths: Paths) -> tuple[str, str, str]:
    if not paths.db.exists():
        return ("codex app-server liveness", OK, "no sessions recorded yet")

    from .store import STATE_OFFLINE, Store

    try:
        store = Store.open(paths, read_only=True)
    except Exception as exc:  # noqa: BLE001 - a corrupt/locked db is a warning, not a crash
        return ("codex app-server liveness", WARN, f"could not read session store: {exc}")
    try:
        sessions = [
            s
            for s in store.list_sessions(include_unmanaged=False)
            if s.family == "codex" and s.state != STATE_OFFLINE
        ]
    finally:
        store.close()

    if not sessions:
        return ("codex app-server liveness", OK, "no live-managed Codex sessions")

    dead = [s.id for s in sessions if not _codex_socket_is_live(paths.codex_socket(s.id))]
    if dead:
        return (
            "codex app-server liveness",
            FAIL,
            f"dead App Server socket for session(s): {', '.join(dead)}",
        )
    return ("codex app-server liveness", OK, f"{len(sessions)} live session(s) checked")


def _codex_socket_is_live(socket_path: Path, *, timeout: float = 1.0) -> bool:
    """Connect-and-close probe: no data is sent, no token is needed."""
    import socket

    if not socket_path.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def cli_doctor() -> int:  # pragma: no cover - thin shim
    import os

    paths = Paths.resolve()
    home = Path(os.environ.get("HOME", str(Path.home())))
    report = doctor(paths=paths, claude_home=home / ".claude", codex_home=home / ".codex")
    print(report.render())
    return 0 if report.ok else 1
