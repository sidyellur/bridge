"""A fake ``codex`` executable written to disk as a standalone script.

Real ``codex`` is invoked two different ways by the wrapper:

- ``codex app-server --listen unix://PATH`` — Bridge's managed App Server.
  This fake binds ``PATH`` as a real Unix socket and then hands the accepted
  connection to ``tests.fakes.codex_app_server.FakeCodexAppServer`` until it
  receives SIGTERM, then exits cleanly. It is still a genuine subprocess, but
  it no longer re-implements the protocol (or RFC 6455) a second time inside
  a string template — it imports the one fake the rest of the suite uses.
- ``codex --remote unix://PATH ...`` — the TUI. This fake captures argv/env
  like ``tests/fakes/executables.py::make_capture_exe``, then blocks until a
  release file appears (so tests can observe "the TUI is running" before
  letting it exit) and exits with a configurable code.

Written as a real file (mode 0755) so it runs as a genuine subprocess. The
App Server half puts the repo on ``sys.path`` and imports the shared fake;
the TUI half stays dependency-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_TEMPLATE = """#!{python}
import json
import os
import socket
import sys
import time

CODEX_VERSION = {codex_version!r}
REPO_PATHS = {repo_paths!r}
THREAD_ID = "thread-fake"
AGENT_MESSAGE = {agent_message!r}
AUTO_COMPLETE = {auto_complete!r}
CAPTURE = {capture!r}
RELEASE_FILE = {release_file!r}
TUI_EXIT_CODE = {tui_exit_code!r}
WATCH_KEYS = {watch_keys!r}
APP_SERVER_CAPTURE = {app_server_capture!r}


def _app_server(argv):
    # `argv` may also carry `-c key=value` override pairs (Bridge's
    # `mcp_servers.bridge.env.*` identity overrides); scanning for the exact
    # token "--listen" rather than a fixed position keeps this immune to
    # those, however many are appended.
    if APP_SERVER_CAPTURE:
        with open(APP_SERVER_CAPTURE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"app_server_argv": argv}}) + "\\n")
    listen = None
    for i, a in enumerate(argv):
        if a == "--listen" and i + 1 < len(argv):
            listen = argv[i + 1]
    assert listen and listen.startswith("unix://"), f"expected --listen unix://PATH, got {{argv!r}}"
    path = listen[len("unix://") :]
    try:
        os.unlink(path)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    srv.settimeout(0.2)
    # Sibling pidfile so a test can simulate a hard crash with a real signal
    # instead of only a graceful SIGTERM.
    with open(path + ".pid", "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    stop = {{"flag": False}}

    def handle_term(_signum, _frame):
        stop["flag"] = True

    import signal

    signal.signal(signal.SIGTERM, handle_term)

    conn = None
    while not stop["flag"] and conn is None:
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            continue
        except OSError:
            break

    if conn is not None:
        sys.path[:0] = REPO_PATHS
        from tests.fakes.codex_app_server import FakeCodexAppServer

        fake = FakeCodexAppServer(
            conn,
            codex_version=CODEX_VERSION,
            thread_id=THREAD_ID,
            cwd=os.getcwd(),
            agent_message=AGENT_MESSAGE,
            auto_complete=AUTO_COMPLETE,
        )
        while not stop["flag"]:
            time.sleep(0.05)
        fake.close()
    try:
        srv.close()
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass
    sys.exit(0)


def _tui(argv):
    remote_socket_path = None
    for a in argv:
        if a.startswith("unix://"):
            remote_socket_path = a[len("unix://") :]
            break
    rec = {{
        "name": "codex",
        "argv": argv,
        "env": {{k: os.environ.get(k) for k in WATCH_KEYS}},
        "cwd": os.getcwd(),
        "ts": time.time(),
        "remote_socket_exists": (
            os.path.exists(remote_socket_path) if remote_socket_path else False
        ),
    }}
    with open(CAPTURE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\\n")
    if RELEASE_FILE:
        while not os.path.exists(RELEASE_FILE):
            time.sleep(0.02)
    sys.exit(TUI_EXIT_CODE)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "app-server":
        _app_server(argv)
    else:
        _tui(argv)


if __name__ == "__main__":
    main()
"""

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PATHS = [str(REPO_ROOT), str(REPO_ROOT / "src")]

DEFAULT_WATCH_KEYS = [
    "BRIDGE_SESSION_ID",
    "BRIDGE_ROUTER_SOCKET",
    "BRIDGE_ROUTER_TOKEN_PATH",
    "BRIDGE_HOME",
]


def make_fake_codex_exe(
    directory: Path,
    *,
    capture: Path,
    release_file: Path | None = None,
    tui_exit_code: int = 0,
    agent_message: str = "",
    auto_complete: bool = True,
    watch_keys: list[str] | None = None,
    codex_version: str = "0.151.0",
    app_server_capture: Path | None = None,
) -> Path:
    """Write a dual-mode fake ``codex`` executable to ``directory/codex``.

    ``app-server --listen unix://PATH`` binds a real socket and serves
    ``FakeCodexAppServer`` on it until SIGTERM; any other invocation is treated
    as the TUI, which captures argv/env to ``capture`` and blocks on ``release_file``
    (if given) before exiting with ``tui_exit_code``. ``codex_version`` lets a
    test simulate an App Server too old for the pinned contract.

    ``app_server_capture``, if given, gets one JSON line per App Server launch
    -- ``{{"app_server_argv": argv}}`` -- recorded before anything else runs, so
    a test can assert on the ``-c mcp_servers.bridge.env.*`` overrides Bridge
    appends to the launch line without disturbing the TUI-only ``capture``
    file the rest of the suite already relies on.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "codex"
    script = _SCRIPT_TEMPLATE.format(
        python=sys.executable,
        agent_message=agent_message,
        auto_complete=auto_complete,
        capture=str(capture),
        release_file=str(release_file) if release_file is not None else None,
        tui_exit_code=tui_exit_code,
        watch_keys=watch_keys or DEFAULT_WATCH_KEYS,
        codex_version=codex_version,
        repo_paths=REPO_PATHS,
        app_server_capture=str(app_server_capture) if app_server_capture is not None else None,
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path
