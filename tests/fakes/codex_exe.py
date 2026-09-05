"""A fake ``codex`` executable written to disk as a standalone script.

Real ``codex`` is invoked two different ways by the wrapper:

- ``codex app-server --listen unix://PATH`` — Bridge's managed App Server.
  This fake binds ``PATH`` as a real Unix socket and serves the same minimal
  initialize/thread/turn contract as ``tests/fakes/codex_app_server.py``
  (inlined here, not imported, since this runs as its own subprocess) until it
  receives SIGTERM, then exits cleanly.
- ``codex --remote unix://PATH ...`` — the TUI. This fake captures argv/env
  like ``tests/fakes/executables.py::make_capture_exe``, then blocks until a
  release file appears (so tests can observe "the TUI is running" before
  letting it exit) and exits with a configurable code.

Written as a real file (mode 0755) so it runs as a genuine subprocess; it must
not import anything from the ``bridge`` or ``tests`` packages at runtime.
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

PROTOCOL_VERSION = {protocol_version!r}
THREAD_ID = "thread-fake"
AGENT_MESSAGE = {agent_message!r}
AUTO_COMPLETE = {auto_complete!r}
CAPTURE = {capture!r}
RELEASE_FILE = {release_file!r}
TUI_EXIT_CODE = {tui_exit_code!r}
WATCH_KEYS = {watch_keys!r}


def _app_server(argv):
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
        conn.settimeout(0.2)
        buf = b""
        turn_n = 0

        def send(obj):
            try:
                conn.sendall((json.dumps(obj) + "\\n").encode("utf-8"))
            except OSError:
                pass

        while not stop["flag"]:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\\n" in buf:
                line, _, buf = buf.partition(b"\\n")
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    send(
                        {{
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "result": {{
                                "protocolVersion": PROTOCOL_VERSION,
                                "serverInfo": {{"name": "fake-codex-exe", "version": "0"}},
                                "capabilities": {{}},
                            }},
                        }}
                    )
                elif method == "initialized":
                    send(
                        {{
                            "jsonrpc": "2.0",
                            "method": "thread/started",
                            "params": {{"thread_id": THREAD_ID}},
                        }}
                    )
                    send(
                        {{
                            "jsonrpc": "2.0",
                            "method": "runtime/status",
                            "params": {{"status": "idle"}},
                        }}
                    )
                elif method == "turn/start":
                    turn_n += 1
                    turn_id = f"turn-{{turn_n}}"
                    send(
                        {{
                            "jsonrpc": "2.0",
                            "method": "turn/started",
                            "params": {{"turn_id": turn_id, "thread_id": THREAD_ID}},
                        }}
                    )
                    if AUTO_COMPLETE:
                        if AGENT_MESSAGE:
                            send(
                                {{
                                    "jsonrpc": "2.0",
                                    "method": "item/agent_message",
                                    "params": {{"turn_id": turn_id, "text": AGENT_MESSAGE}},
                                }}
                            )
                        send(
                            {{
                                "jsonrpc": "2.0",
                                "method": "turn/completed",
                                "params": {{"turn_id": turn_id, "thread_id": THREAD_ID}},
                            }}
                        )
                    send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"turn_id": turn_id}}}})
                elif method == "turn/steer":
                    # Forbidden in v1; answer so a caller does not hang, but
                    # the real assertion lives in the adapter's own tests.
                    send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{}}}})
        try:
            conn.close()
        except OSError:
            pass
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
    protocol_version: str = "codex-app-server/1",
) -> Path:
    """Write a dual-mode fake ``codex`` executable to ``directory/codex``.

    ``app-server --listen unix://PATH`` binds a real socket and serves the
    pinned protocol until SIGTERM; any other invocation is treated as the TUI,
    which captures its argv/env to ``capture`` and blocks on ``release_file``
    (if given) before exiting with ``tui_exit_code``. ``protocol_version`` lets
    a test simulate an unsupported/drifted App Server.
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
        protocol_version=protocol_version,
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path
