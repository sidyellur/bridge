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
import time
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
CODEX_FINAL_FALLBACK_ENV = "BRIDGE_CODEX_FINAL_FALLBACK"
CODEX_APP_SERVER_TIMEOUT_S = 10.0

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


def codex_final_message_fallback_enabled(env: dict[str, str] | None = None) -> bool:
    """Experiment F gate for the Codex final-agent-message reply fallback.
    Off by default; a deployment enables it only once the experiment verdict
    validates the correlation (see design spec §7, §12)."""
    env = os.environ if env is None else env
    return env.get(CODEX_FINAL_FALLBACK_ENV, "").strip().lower() in ("1", "true", "yes")


def describe_channel_mode(channel_args: Sequence[str]) -> str:
    """One-line human summary of the Claude Channel launch mode implied by
    ``channel_args`` (the contents of ``claude_channel_args.json``, per
    :class:`bridge.claude_probe.ChannelMode`). Classifies from the args
    themselves so the wrapper never re-probes the vendor binary on every
    ``bridge claude`` launch."""
    if not channel_args:
        return "Claude Channel mode: unsupported; this session is inbound-unreachable"
    from .claude_probe import DEV_ARG_MARKER, PLUGIN_ARG_MARKER

    joined = " ".join(channel_args)
    if PLUGIN_ARG_MARKER in joined:
        return "Claude Channel mode: plugin"
    if DEV_ARG_MARKER in joined:
        return (
            "Claude Channel mode: development (research preview; organization "
            "policy may block inbound delivery)"
        )
    return "Claude Channel mode: custom"


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
    codex_app_server_timeout: float = CODEX_APP_SERVER_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    codex_final_message_fallback: bool | None = None,
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
        return _run_codex_wrapper(
            user_args,
            paths=paths,
            base_env=base_env,
            new_id=new_id,
            spawn=spawn,
            connect=connect,
            forward_signals=forward_signals,
            print_address=print_address,
            app_server_timeout=codex_app_server_timeout,
            sleep=sleep,
            now=now,
            final_message_fallback=(
                codex_final_message_fallback
                if codex_final_message_fallback is not None
                else codex_final_message_fallback_enabled(base_env)
            ),
        )
    else:
        raise ValueError(f"unknown family {family!r}")

    child_env = build_identity_env(paths, session_id, base_env)

    client = connect(paths, session_id=session_id, role="client")
    registry = Registry.for_client(client)
    registry.register_start(session_id, family, cwd=os.getcwd(), pid=None)

    if print_address:
        print(f"[bridge] session address: {session_id}", file=sys.stderr)
        if family == "claude":
            print(f"[bridge] {describe_channel_mode(channel_args)}", file=sys.stderr)

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


# --- codex: App Server owner + adapter + remote TUI -------------------------


def _run_codex_wrapper(
    user_args: Sequence[str],
    *,
    paths: Paths,
    base_env: dict[str, str],
    new_id: Callable[[], str],
    spawn: Callable[..., subprocess.Popen],
    connect: Callable[..., object],
    forward_signals: bool,
    print_address: bool,
    app_server_timeout: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    final_message_fallback: bool,
) -> LaunchResult:
    """One Bridge-managed App Server per wrapped Codex session (spec §4, §5):
    spawn the App Server, wait for its socket, connect the adapter so the
    session is registered/reachable, *then* attach the remote TUI. Tears down
    in reverse order on exit, reaping both children."""
    from .adapters.codex import CodexAdapter
    from .codex_app_server import (
        CodexAppServerClient,
        CodexAppServerProcess,
        connect_app_server_socket,
    )

    session_id = new_id()
    paths.ensure_session_dir(session_id)
    binary = resolve_binary("codex", base_env)
    socket_path = paths.codex_socket(session_id)
    argv = build_codex_argv(binary, socket_path, user_args)
    child_env = build_identity_env(paths, session_id, base_env)

    app_server = CodexAppServerProcess(socket_path, binary=binary)
    app_server.start(env=child_env, spawn=spawn)
    try:
        app_server.wait_for_socket(app_server_timeout, sleep=sleep, now=now)
    except Exception:
        app_server.stop()
        raise

    def _reconnect() -> CodexAppServerClient:
        # A short per-attempt timeout: a dead App Server refuses the
        # connection immediately (ECONNREFUSED), so this bounds how long one
        # of CodexAdapter's own backed-off attempts can take, not whether a
        # live server gets time to answer.
        sock = connect_app_server_socket(socket_path, timeout=0.5)
        return CodexAppServerClient(sock).start()

    def _print_disconnect_diagnostic() -> None:
        print(
            f"[bridge] codex app-server for session {session_id} disconnected; "
            "session marked unreachable",
            file=sys.stderr,
        )

    adapter = None
    try:
        app_client = _reconnect()
        adapter = CodexAdapter(
            session_id,
            app_client,
            final_message_fallback=final_message_fallback,
            cwd=os.getcwd(),
            reconnect=_reconnect,
            on_app_server_disconnect=_print_disconnect_diagnostic,
            reconnect_sleep=sleep,
        )
        adapter.connect_router(
            lambda on_event: connect(
                paths, session_id=session_id, role="adapter", on_event=on_event
            )
        )
        adapter.start()
    except Exception:
        try:
            (adapter or app_client).close()
        except Exception:  # noqa: BLE001
            pass
        app_server.stop()
        raise

    if print_address:
        print(f"[bridge] session address: {session_id}", file=sys.stderr)

    proc = spawn(argv, env=child_env)
    if getattr(proc, "pid", None) is not None:
        try:
            adapter.router.call("update_state", {"session_id": session_id, "pid": proc.pid})
        except Exception:  # noqa: BLE001
            pass

    try:
        if forward_signals:
            with SignalForwarder(proc):
                returncode = proc.wait()
        else:
            returncode = proc.wait()
    finally:
        try:
            adapter.router.call("deregister", {"session_id": session_id})
        except Exception:  # noqa: BLE001
            pass
        adapter.close()
        app_server.stop()

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
