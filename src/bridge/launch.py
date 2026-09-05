"""Bridge-managed session wrappers: ``bridge claude`` and ``bridge codex``.

The wrappers supply identity at launch (a Bridge session UUID plus router
address in the environment) instead of inferring it from process tables or
mutable vendor artifacts. Command/env construction is pure and unit-tested;
the runner adds lazy router start, registration, signal forwarding, and
exit-code passthrough.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .paths import (
    BRIDGE_HOME_ENV,
    ROUTER_SOCKET_ENV,
    ROUTER_TOKEN_ENV,
    SESSION_ID_ENV,
    Paths,
)

CLAUDE_BIN_ENV = "BRIDGE_CLAUDE_BIN"
CODEX_BIN_ENV = "BRIDGE_CODEX_BIN"
CLAUDE_CHANNEL_ARGS_FILE = "claude_channel_args.json"

FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH, signal.SIGHUP)


# --- identity + command construction (pure) --------------------------------

def build_identity_env(
    paths: Paths, session_id: str, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env[SESSION_ID_ENV] = session_id
    env[ROUTER_SOCKET_ENV] = str(paths.socket)
    env[ROUTER_TOKEN_ENV] = str(paths.token)
    env[BRIDGE_HOME_ENV] = str(paths.home)
    return env


def resolve_claude_session_id(
    user_args: Sequence[str], new_id: Callable[[], str]
) -> tuple[str, bool]:
    """Return (session_id, is_resume). An explicit --session-id or --resume is
    preserved; otherwise a fresh Bridge UUID is minted."""
    for flag in ("--session-id", "--resume"):
        val = _flag_value(user_args, flag)
        if val is not None:
            return val, flag == "--resume"
    return new_id(), False


def build_claude_argv(
    binary: str,
    session_id: str,
    user_args: Sequence[str],
    channel_args: Sequence[str],
    *,
    is_resume: bool = False,
) -> list[str]:
    argv = [binary]
    has_session_flag = _has_flag(user_args, "--session-id") or _has_flag(user_args, "--resume")
    if not has_session_flag and not is_resume:
        argv += ["--session-id", session_id]
    argv += list(channel_args)
    argv += list(user_args)
    return argv


def build_codex_argv(binary: str, socket_path, user_args: Sequence[str]) -> list[str]:
    return [binary, "--remote", f"unix://{socket_path}", *user_args]


def load_claude_channel_args(paths: Paths) -> list[str]:
    """Optional channel launch args written by the installer once Experiment E
    pins the research-preview development flag. Absent by default."""
    f = paths.home / CLAUDE_CHANNEL_ARGS_FILE
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def resolve_binary(family: str, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    override = env.get(CLAUDE_BIN_ENV if family == "claude" else CODEX_BIN_ENV)
    if override:
        return override
    found = shutil.which(family, path=env.get("PATH"))
    if not found:
        raise FileNotFoundError(f"could not find the {family!r} executable on PATH")
    return found


# --- signal forwarding -----------------------------------------------------

@dataclass
class SignalForwarder:
    """Forwards a set of signals to a child process while installed."""

    proc: object  # anything with send_signal
    signals: Sequence[int] = FORWARDED_SIGNALS

    def _forward(self, signum, _frame=None) -> None:
        try:
            self.proc.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass

    def __enter__(self) -> SignalForwarder:
        self._old: dict[int, object] = {}
        for sig in self.signals:
            try:
                self._old[sig] = signal.signal(sig, self._forward)
            except (ValueError, OSError):  # pragma: no cover - non-main-thread
                pass
        return self

    def __exit__(self, *exc) -> None:
        for sig, handler in self._old.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass


# --- runner ----------------------------------------------------------------

@dataclass
class LaunchResult:
    session_id: str
    argv: list[str]
    returncode: int


def run_wrapper(
    family: str,
    user_args: Sequence[str],
    *,
    paths: Paths | None = None,
    env: dict[str, str] | None = None,
    new_id: Callable[[], str] | None = None,
    spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    ensure_running: Callable[[Paths], None] | None = None,
    connect: Callable[..., object] | None = None,
    forward_signals: bool = True,
    print_address: bool = True,
) -> LaunchResult:
    from .registry import Registry

    paths = (paths or Paths.resolve()).ensure()
    base_env = os.environ if env is None else env
    new_id = new_id or (lambda: str(uuid.uuid4()))

    if ensure_running is None:
        from .router import ensure_running as _er

        ensure_running = _er
    ensure_running(paths)

    if connect is None:
        from .router_client import RouterClient

        connect = RouterClient.connect

    if family == "claude":
        session_id, is_resume = resolve_claude_session_id(user_args, new_id)
        binary = resolve_binary("claude", base_env)
        channel_args = load_claude_channel_args(paths)
        argv = build_claude_argv(binary, session_id, user_args, channel_args, is_resume=is_resume)
    elif family == "codex":
        session_id = new_id()
        paths.ensure_session_dir(session_id)
        binary = resolve_binary("codex", base_env)
        argv = build_codex_argv(binary, paths.codex_socket(session_id), user_args)
    else:
        raise ValueError(f"unknown family {family!r}")

    child_env = build_identity_env(paths, session_id, base_env)

    client = connect(paths, session_id=session_id, role="client")
    registry = Registry.for_client(client)
    registry.register_start(session_id, family, cwd=os.getcwd(), pid=None)

    if print_address:
        print(f"[bridge] session address: {session_id}", file=sys.stderr)

    proc = spawn(argv, env=child_env)
    registry.mark(session_id, "idle")
    if getattr(proc, "pid", None) is not None:
        registry.set_pid(session_id, proc.pid)

    try:
        if forward_signals:
            with SignalForwarder(proc):
                returncode = proc.wait()
        else:
            returncode = proc.wait()
    finally:
        registry.mark_offline(session_id)
        _safe_close(client)

    return LaunchResult(session_id=session_id, argv=argv, returncode=returncode or 0)


def cli_launch_claude(args: Sequence[str]) -> int:  # pragma: no cover - thin shim
    return run_wrapper("claude", list(args)).returncode


def cli_launch_codex(args: Sequence[str]) -> int:  # pragma: no cover - thin shim
    return run_wrapper("codex", list(args)).returncode


# --- helpers ---------------------------------------------------------------

def _has_flag(args: Sequence[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in args)


def _flag_value(args: Sequence[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _safe_close(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
