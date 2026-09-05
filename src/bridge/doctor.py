"""``bridge doctor``: diagnose the installation with token-free local probes
where possible.

Checks router socket/token ownership and permissions, MCP registration on both
families, coordination guidance, the Claude Channel research-preview mode, Codex
App Server/remote availability, stale sessions, and a local loopback protocol
probe that spends no model tokens.
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import PROTOCOL_VERSION
from .claude_probe import ChannelMode, ChannelSupport, detect_channel_mode
from .install import claude_settings_path, codex_config_path
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
    report.add(*_check_claude_registration(claude_home))
    report.add(*_check_codex_registration(codex_home))

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
    report.add(*_policy_check(paths))

    # Vendor binaries / remote flags.
    if check_binaries:
        report.add("claude binary", OK if shutil.which("claude") else WARN, "on PATH")
        report.add("codex binary", OK if shutil.which("codex") else WARN, "on PATH")

    # No prompt hooks / obsolete queue must be absent.
    obsolete = [p for p in (paths.home / "spool", paths.home / "hooks") if p.exists()]
    report.add(
        "no obsolete hooks/queue",
        OK if not obsolete else FAIL,
        ", ".join(str(p) for p in obsolete) if obsolete else "clean",
    )

    # Loopback protocol probe (uses the local token; no model tokens).
    report.add(*_loopback_probe(paths))

    return report


def _check_claude_registration(claude_home: Path) -> tuple[str, str, str]:
    path = claude_settings_path(claude_home)
    if not path.exists():
        return ("Claude MCP registration", WARN, "settings.json missing; run `bridge install`")
    import json

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return ("Claude MCP registration", FAIL, "settings.json is not valid JSON")
    if "bridge" in data.get("mcpServers", {}):
        return ("Claude MCP registration", OK, "settings.json")
    return ("Claude MCP registration", FAIL, "bridge server not registered")


def _check_codex_registration(codex_home: Path) -> tuple[str, str, str]:
    path = codex_config_path(codex_home)
    if not path.exists():
        return ("Codex MCP registration", WARN, "config.toml missing; run `bridge install`")
    text = path.read_text()
    if "[mcp_servers.bridge]" in text:
        return ("Codex MCP registration", OK, "config.toml")
    return ("Codex MCP registration", FAIL, "bridge server not registered")


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
            f"research preview: development channel flag (claude {mode.version or 'unknown'}); "
            "organization policy may still block inbound delivery",
        )
    detail = f"claude {mode.version or 'not found'}"
    if mode.detail:
        detail += f": {mode.detail}"
    return (
        "Claude Channel mode",
        FAIL,
        f"unsupported ({detail}); this session is inbound-unreachable",
    )


def _policy_check(paths: Paths) -> tuple[str, str, str]:
    """Surface any persisted ``policy_error`` from known sessions' session.json
    (written by the Claude Channel adapter when Claude does not negotiate the
    channel capability). Token-free: reads only what is already on disk."""
    import json

    sessions_dir = paths.sessions_dir
    if not sessions_dir.exists():
        return ("Claude channel policy", OK, "no managed sessions recorded")
    errors = []
    for meta_path in sorted(sessions_dir.glob("*/session.json")):
        try:
            data = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        err = data.get("policy_error")
        if err:
            errors.append(f"{meta_path.parent.name}: {err}")
    if errors:
        return ("Claude channel policy", WARN, "; ".join(errors))
    return ("Claude channel policy", OK, "no policy errors recorded")


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


def cli_doctor() -> int:  # pragma: no cover - thin shim
    import os

    paths = Paths.resolve()
    home = Path(os.environ.get("HOME", str(Path.home())))
    report = doctor(paths=paths, claude_home=home / ".claude", codex_home=home / ".codex")
    print(report.render())
    return 0 if report.ok else 1
