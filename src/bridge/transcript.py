"""Transcript helpers: deterministic gists and human rendering.

Every enqueue, delivery, processing, reply, timeout, rejection, and disconnect
is recorded by the store with a truncated gist. Full message bodies are kept
only when the user opts in (``retain_bodies``); by default they are redacted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

GIST_MAX = 120


def gist(text: str) -> str:
    """A deterministic, single-line, length-capped summary of a message body."""
    return text.strip().replace("\n", " ")[:GIST_MAX]


def render_transcript(payload: dict[str, Any]) -> str:
    entries = payload.get("entries", [])
    if not entries:
        return "(no activity)"
    lines = []
    for e in entries:
        ts = _fmt_ts(e.get("ts"))
        frm = e.get("from") or "-"
        to = e.get("to") or "-"
        line = f"{ts}  {e.get('kind', ''):<12} {e.get('status', ''):<10} {frm} -> {to}"
        if e.get("gist"):
            line += f"  {e['gist']}"
        lines.append(line)
    return "\n".join(lines)


def _fmt_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--:--:--"
