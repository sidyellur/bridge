"""Roster: the contacts app for managed sessions.

The router builds the roster payload; this module renders it for humans and
provides the ``bridge roster`` CLI command.
"""

from __future__ import annotations

import json
from typing import Any

from .paths import SESSION_ID_ENV, Paths


def render_roster(payload: dict[str, Any]) -> str:
    sessions = payload.get("sessions", [])
    warnings = payload.get("warnings", [])
    if not sessions:
        lines = ["(no sessions)"]
    else:
        lines = []
        header = f"{'ID':<38} {'ALIAS':<12} {'FAMILY':<7} {'STATE':<8} {'REACH':<5} CWD"
        lines.append(header)
        for s in sessions:
            reach = "yes" if s.get("reachable") else "no"
            marker = "*" if s.get("is_self") else " "
            alias = s.get("alias") or "-"
            lines.append(
                f"{marker}{s['id']:<37} {alias:<12} {s['family']:<7} {s['state']:<8} {reach:<5}"
                f" {s.get('cwd', '')}"
            )
            preview = (s.get("last_user_message") or "").strip()
            if preview:
                lines.append(f"    last: {preview}")
    for w in warnings:
        lines.append(f"! {w}")
    return "\n".join(lines)


def fetch_roster(paths: Paths | None = None, *, include_unmanaged: bool = False) -> dict[str, Any]:
    import os

    from .router_client import RouterClient

    paths = paths or Paths.resolve()
    session_id = os.environ.get(SESSION_ID_ENV)
    client = RouterClient.connect(paths, session_id=session_id, role="client")
    try:
        return client.call("roster", {"include_unmanaged": include_unmanaged})
    finally:
        client.close()


def cli_roster(*, include_unmanaged: bool = False, as_json: bool = False) -> int:
    from .router import is_running
    from .router_client import RouterClientError

    paths = Paths.resolve()
    if not is_running(paths):
        print(
            "bridge router is not running; start a session with `bridge claude` or `bridge codex`"
        )
        return 1
    try:
        payload = fetch_roster(paths, include_unmanaged=include_unmanaged)
    except RouterClientError as exc:
        print(f"could not reach the router: {exc.message}")
        return 1
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_roster(payload))
    return 0
