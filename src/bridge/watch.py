"""``bridge watch`` - a read-only live view of the router.

The router daemon is the single writer for ``bridge.db``; WAL mode is what makes
a concurrent reader safe (design spec section 4). This view therefore opens the
same database with ``Store.open(paths, read_only=True)``: it never contends with
the daemon, and it renders whether or not the daemon is running.

:func:`build_frame` is a pure renderer over a store - it performs no I/O beyond
the store reads, so tests can assert on its exact text. :func:`run_watch` is a
small curses-free loop that clears the screen and redraws the frame every
interval. Its stdin, stdout, and sleep are all injected, so tests never touch a
real terminal and never sleep.
"""

from __future__ import annotations

import select
import sys
import time
from collections.abc import Callable
from typing import IO, Any

from .paths import Paths
from .store import Call, Session, Store
from .transcript import render_transcript

# Home the cursor, then clear the whole screen. Cheaper and far smaller than a
# curses dependency, and it degrades to harmless noise on a dumb terminal.
CLEAR_SCREEN = "\x1b[H\x1b[2J"

QUIT_KEYS = {"q", "quit"}

HEADER = "bridge watch - read-only view of the router (press q then Enter to quit)"


# --- rendering (pure) ------------------------------------------------------


def build_frame(store: Store, *, limit: int = 10) -> str:
    """Render one complete frame from a store snapshot.

    Sections: the session roster with per-target queue depth, the calls
    currently in flight, and the tail of the transcript.
    """
    sessions = store.list_sessions(include_unmanaged=True)
    depths = store.queue_depths()
    calls = store.active_calls()
    entries = store.recent(limit=limit)

    lines: list[str] = [HEADER, ""]
    lines += _sessions_block(sessions, depths)
    lines.append("")
    lines += _calls_block(calls)
    lines.append("")
    lines += _transcript_block(entries)
    return "\n".join(lines)


def _sessions_block(sessions: list[Session], depths: dict[str, int]) -> list[str]:
    lines = [f"SESSIONS ({len(sessions)})"]
    if not sessions:
        lines.append("  (no sessions)")
        return lines
    lines.append(f"  {'ID':<38} {'FAMILY':<7} {'STATE':<8} {'REACH':<5} {'QUEUE':<5} CWD")
    for s in sessions:
        reach = "yes" if s.reachable else "no"
        marker = " " if s.is_managed else "!"
        depth = depths.get(s.id, 0)
        lines.append(
            f"{marker} {s.id:<38} {s.family:<7} {s.state:<8} {reach:<5} {depth:<5} {s.cwd}"
        )
    return lines


def _calls_block(calls: list[Call]) -> list[str]:
    lines = [f"ACTIVE CALLS ({len(calls)})"]
    if not calls:
        lines.append("  (no calls in flight)")
        return lines
    lines.append(f"  {'CALL':<38} {'STATUS':<10} {'FROM -> TO':<30} QUESTION")
    for c in calls:
        pair = f"{c.from_id} -> {c.to_id}"
        lines.append(f"  {c.call_id:<38} {c.status:<10} {pair:<30} {c.question}")
    return lines


def _transcript_block(entries: list[Any]) -> list[str]:
    # ``recent`` returns newest-first; a tailing view reads oldest-first.
    payload = {
        "entries": [
            {
                "ts": e.ts,
                "from": e.from_id,
                "to": e.to_id,
                "kind": e.kind,
                "status": e.status,
                "gist": e.gist,
                "call_id": e.call_id,
            }
            for e in reversed(entries)
        ]
    }
    lines = [f"TRANSCRIPT (last {len(entries)})"]
    lines += [f"  {line}" for line in render_transcript(payload).splitlines()]
    return lines


# --- the loop --------------------------------------------------------------


def open_read_only(paths: Paths) -> Store:
    """Open the router database read-only.

    If the router has never run there is no database yet; an empty one is
    created once so ``bridge watch`` still renders (there is no daemon to
    contend with in that case, by definition).
    """
    paths.ensure()
    if not paths.db.exists():
        Store.open(paths).close()
    return Store.open(paths, read_only=True)


def run_watch(
    paths: Paths,
    *,
    interval_s: float = 0.5,
    once: bool = False,
    limit: int = 10,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Clear and redraw :func:`build_frame` every ``interval_s`` seconds.

    Returns 0 when the viewer quits (``q``) or stdin reaches EOF, or after a
    single frame when ``once`` is set.
    """
    stream_in = sys.stdin if stdin is None else stdin
    stream_out = sys.stdout if stdout is None else stdout
    store = open_read_only(paths)
    try:
        while True:
            stream_out.write(CLEAR_SCREEN)
            stream_out.write(build_frame(store, limit=limit))
            stream_out.write("\n")
            stream_out.flush()
            if once:
                return 0
            if _wait_for_quit(stream_in, interval_s, sleep):
                return 0
    finally:
        store.close()


def _wait_for_quit(stream_in: IO[str], interval_s: float, sleep: Callable[[float], None]) -> bool:
    """Wait up to ``interval_s`` for input. True means "stop watching"."""
    fileno = _fileno(stream_in)
    if fileno is None:
        # An injected (or captured) stream has no selectable descriptor: pace
        # the loop with the injected sleep and read straight through.
        sleep(interval_s)
        line = stream_in.readline()
    else:
        ready, _, _ = select.select([fileno], [], [], interval_s)
        if not ready:
            return False
        line = stream_in.readline()
    if line == "":  # EOF
        return True
    return line.strip().lower() in QUIT_KEYS


def _fileno(stream: IO[str]) -> int | None:
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None
