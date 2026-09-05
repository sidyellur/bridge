"""Human-facing CLI diagnostics: ``bridge text``, ``bridge call``,
``bridge transcript``.

These connect to the router for inspection and one-off messages. Outbound
``call``/``text`` require a managed source session (``BRIDGE_SESSION_ID`` from a
wrapper); the CLI never invents an agent identity to impersonate.
"""

from __future__ import annotations

import json
import os

from .paths import SESSION_ID_ENV, Paths
from .transcript import render_transcript


def _require_source() -> str | None:
    return os.environ.get(SESSION_ID_ENV)


def _connect(session_id: str | None):
    from .router import is_running
    from .router_client import RouterClient

    paths = Paths.resolve()
    if not is_running(paths):
        return None
    return RouterClient.connect(paths, session_id=session_id, role="client")


def cli_text(to: str, message: str) -> int:
    source = _require_source()
    if not source:
        print("bridge text needs a source session; run it inside a `bridge claude`/`bridge codex`")
        return 2
    client = _connect(source)
    if client is None:
        print("bridge router is not running")
        return 1
    try:
        result = client.call("text", {"to": to, "message": message})
        print(json.dumps(result))
        return 0 if result.get("status") != "unreachable" else 1
    finally:
        client.close()


def cli_call(to: str, question: str, *, timeout_s: int = 60) -> int:
    source = _require_source()
    if not source:
        print("bridge call needs a source session; run it inside a `bridge claude`/`bridge codex`")
        return 2
    client = _connect(source)
    if client is None:
        print("bridge router is not running")
        return 1
    try:
        result = client.call(
            "call",
            {"to": to, "question": question, "timeout_s": timeout_s},
            timeout=timeout_s + 5,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "answered" else 1
    finally:
        client.close()


def cli_transcript(*, peer: str | None = None, limit: int = 20) -> int:
    client = _connect(_require_source())
    if client is None:
        print("bridge router is not running")
        return 1
    try:
        payload = client.call("transcript", {"peer": peer, "limit": limit})
        print(render_transcript(payload))
        return 0
    finally:
        client.close()
