"""Transcript retention: keeping Bridge's history bounded.

The audit transcript (§10) and the rate counters record every enqueue,
delivery, reply, timeout, rejection, and disconnect, and nothing ever removed
them: a long-lived install grew its SQLite file forever. This module adds the
sweep that trims them, plus the ``prune`` router op and the
``bridge transcript --prune`` front end.

Two rules keep the sweep safe:

* Only *history* is cut. Unresolved calls and queued/delivered queue entries
  are live coordination state and survive regardless of age -- see
  :meth:`bridge.store.Store.prune`.
* The daemon stays the single writer, so the CLI asks it to prune rather than
  opening the database itself. The one documented exception is a stopped
  router: with no daemon there is no writer to contend with, so ``--prune``
  opens the store read-write directly instead of failing.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from .paths import SESSION_ID_ENV, Paths

if TYPE_CHECKING:
    from .router import Router

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    "": 1.0,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}

#: Order used when rendering the per-table counts.
_TABLES = ("transcript", "rate_events", "calls", "messages", "queue")


def parse_duration(value: str | float | int) -> float:
    """Parse ``7d`` / ``12h`` / ``30m`` / ``45s`` / ``604800`` into seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        match = _DURATION_RE.match(str(value))
        if not match:
            raise ValueError(
                f"could not read duration {value!r}; use e.g. 7d, 12h, 30m, 45s, or plain seconds"
            )
        seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if seconds < 0:
        raise ValueError("duration must not be negative")
    return seconds


def format_duration(seconds: float) -> str:
    for unit, size in (("d", 86400.0), ("h", 3600.0), ("m", 60.0)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def render_prune(result: dict[str, Any]) -> str:
    counts = result.get("pruned", {})
    older = result.get("older_than_s")
    head = "pruned rows older than " + (
        format_duration(float(older)) if older is not None else "the retention window"
    )
    body = ", ".join(f"{table}={int(counts.get(table, 0))}" for table in _TABLES)
    return f"{head}: {body}"


# --- router op --------------------------------------------------------------


def do_prune(router: Router, args: dict[str, Any]) -> dict[str, Any]:
    from .router import RouterError

    raw = args.get("older_than_s")
    if raw in (None, ""):
        older_than_s = float(router.config.retention_s)
    else:
        try:
            older_than_s = parse_duration(raw)
        except ValueError as exc:
            raise RouterError("bad_request", str(exc)) from exc

    counts = router.prune(older_than_s)
    # A human-initiated deletion is worth an audit line; the hourly background
    # sweep in ``tick`` deliberately records nothing so it cannot spam history.
    router.store.record_event(
        "retention",
        "pruned",
        gist=", ".join(f"{table}={counts.get(table, 0)}" for table in _TABLES),
    )
    return {"pruned": counts, "older_than_s": older_than_s}


def _register() -> None:
    from .router import register_simple_op

    register_simple_op("prune", do_prune)


_register()


# --- CLI --------------------------------------------------------------------


def cli_prune(older_than: str | None = None) -> int:
    """``bridge transcript --prune [--older-than <duration>]``."""
    from .router import RouterConfig, is_running
    from .router_client import RouterClient, RouterClientError

    paths = Paths.resolve()
    try:
        older_than_s = parse_duration(older_than) if older_than else None
    except ValueError as exc:
        print(str(exc))
        return 2

    if is_running(paths):
        try:
            client = RouterClient.connect(
                paths, session_id=os.environ.get(SESSION_ID_ENV), role="client"
            )
        except RouterClientError as exc:
            print(f"could not reach the router: {exc.message}")
            return 1
        try:
            result = client.call("prune", {"older_than_s": older_than_s})
        except RouterClientError as exc:
            print(f"prune failed: {exc.message}")
            return 1
        finally:
            client.close()
        print(render_prune(result))
        return 0

    # Documented exception to "the daemon is the single writer": there is no
    # daemon, so nothing can race us. Open the store read-write and prune here.
    from .store import Store

    if older_than_s is None:
        older_than_s = float(RouterConfig().retention_s)
    store = Store.open(paths)
    try:
        counts = store.prune(older_than_s)
    finally:
        store.close()
    print(render_prune({"pruned": counts, "older_than_s": older_than_s}))
    print("(router not running: pruned the database directly)")
    return 0


__all__ = [
    "cli_prune",
    "do_prune",
    "format_duration",
    "parse_duration",
    "render_prune",
]
