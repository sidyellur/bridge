"""SQLite-backed state: session registry, call state machine, delivery queue,
transcript, and rate counters.

The router daemon is the single writer. WAL mode keeps read-only CLI inspection
non-blocking. Every timestamp comes from an injected ``now`` callable so tests
run on a frozen clock. Migrations are applied idempotently on open and the
current schema version is stored in the ``meta`` table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .paths import Paths

SCHEMA_VERSION = 1

# Session lifecycle states.
STATE_STARTING = "starting"
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_WAITING = "waiting"
STATE_OFFLINE = "offline"
SESSION_STATES = {STATE_STARTING, STATE_IDLE, STATE_WORKING, STATE_WAITING, STATE_OFFLINE}

# Call statuses.
CALL_QUEUED = "queued"
CALL_DELIVERED = "delivered"
CALL_ANSWERING = "answering"
CALL_ANSWERED = "answered"
CALL_TIMEOUT = "timeout"
CALL_UNREACHABLE = "unreachable"
CALL_BLOCKED = "blocked"

# Message/delivery statuses.
MSG_QUEUED = "queued"
MSG_DELIVERED = "delivered"
MSG_PROCESSED = "processed"
MSG_EXPIRED = "expired"
MSG_UNREACHABLE = "unreachable"

# Message kinds.
KIND_TEXT = "text"
KIND_CALL = "call"
KIND_CALL_RESULT = "call_result"


@dataclass
class Session:
    id: str
    family: str
    pid: int | None
    cwd: str
    state: str
    vendor_session_id: str | None
    reachable: bool
    is_managed: bool
    last_user_message: str
    started_at: float
    last_active: float


@dataclass
class Call:
    call_id: str
    from_id: str
    to_id: str
    question: str
    kind: str
    status: str
    answer: str
    blocked: list[str]
    deadline: float
    created_at: float
    delivered_at: float | None
    answered_at: float | None
    target_state_on_delivery: str | None


@dataclass
class QueuedMessage:
    queue_id: int
    target_id: str
    message_id: str
    seq: int
    kind: str
    body: dict[str, Any]
    status: str
    call_id: str | None
    enqueued_at: float
    delivered_at: float | None


@dataclass
class TranscriptEntry:
    ts: float
    from_id: str | None
    to_id: str | None
    kind: str
    status: str
    gist: str
    call_id: str | None


_MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        family TEXT NOT NULL,
        pid INTEGER,
        cwd TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'starting',
        vendor_session_id TEXT,
        reachable INTEGER NOT NULL DEFAULT 0,
        is_managed INTEGER NOT NULL DEFAULT 1,
        last_user_message TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL,
        last_active REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS calls (
        call_id TEXT PRIMARY KEY,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        question TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'call',
        status TEXT NOT NULL,
        answer TEXT NOT NULL DEFAULT '',
        blocked TEXT NOT NULL DEFAULT '[]',
        deadline REAL NOT NULL,
        created_at REAL NOT NULL,
        delivered_at REAL,
        answered_at REAL,
        target_state_on_delivery TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        message_id TEXT PRIMARY KEY,
        from_id TEXT,
        to_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '{}',
        call_id TEXT,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS queue (
        queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        enqueued_at REAL NOT NULL,
        delivered_at REAL,
        acked_at REAL,
        UNIQUE(message_id)
    );
    CREATE INDEX IF NOT EXISTS idx_queue_target ON queue(target_id, status, seq);

    CREATE TABLE IF NOT EXISTS transcript (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        from_id TEXT,
        to_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        gist TEXT NOT NULL DEFAULT '',
        body TEXT,
        call_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_transcript_ts ON transcript(ts);

    CREATE TABLE IF NOT EXISTS rate_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_rate_pair ON rate_events(from_id, to_id, ts);
    """,
]


class Store:
    def __init__(self, conn: sqlite3.Connection, now: Callable[[], float], retain_bodies: bool):
        self._conn = conn
        self._now = now
        self._retain_bodies = retain_bodies
        self._lock = threading.RLock()

    # --- lifecycle ----------------------------------------------------------
    @classmethod
    def open(
        cls,
        paths: Paths,
        now: Callable[[], float] = time.time,
        *,
        retain_bodies: bool = False,
        read_only: bool = False,
    ) -> Store:
        paths.ensure()
        if read_only:
            uri = f"file:{paths.db}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            conn = sqlite3.connect(str(paths.db), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            paths.db.chmod(0o600)
        store = cls(conn, now, retain_bodies)
        if not read_only:
            store._migrate()
        return store

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            current = self._current_version()
            for version in range(current, len(_MIGRATIONS)):
                self._conn.executescript(_MIGRATIONS[version])
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version + 1),),
                )

    def _current_version(self) -> int:
        if not _table_exists(self._conn, "meta"):
            return 0
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._current_version()

    # --- meta ---------------------------------------------------------------
    def set_retain_bodies(self, value: bool) -> None:
        self._retain_bodies = value

    # --- sessions -----------------------------------------------------------
    def upsert_session(
        self,
        session_id: str,
        family: str,
        *,
        pid: int | None = None,
        cwd: str = "",
        state: str = STATE_STARTING,
        vendor_session_id: str | None = None,
        reachable: bool = False,
        is_managed: bool = True,
    ) -> None:
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions
                    (id, family, pid, cwd, state, vendor_session_id, reachable,
                     is_managed, last_user_message, started_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    family=excluded.family,
                    pid=excluded.pid,
                    cwd=excluded.cwd,
                    state=excluded.state,
                    vendor_session_id=COALESCE(
                        excluded.vendor_session_id, sessions.vendor_session_id),
                    reachable=excluded.reachable,
                    is_managed=excluded.is_managed,
                    last_active=excluded.last_active
                """,
                (
                    session_id,
                    family,
                    pid,
                    cwd,
                    state,
                    vendor_session_id,
                    int(reachable),
                    int(is_managed),
                    now,
                    now,
                ),
            )

    def set_state(self, session_id: str, state: str) -> None:
        if state not in SESSION_STATES:
            raise ValueError(f"unknown state {state!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET state=?, last_active=? WHERE id=?",
                (state, self._now(), session_id),
            )

    def set_reachable(self, session_id: str, reachable: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET reachable=?, last_active=? WHERE id=?",
                (int(reachable), self._now(), session_id),
            )

    def set_vendor_session(self, session_id: str, vendor_session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET vendor_session_id=? WHERE id=?",
                (vendor_session_id, session_id),
            )

    def set_last_user_message(self, session_id: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_user_message=?, last_active=? WHERE id=?",
                (message[:120], self._now(), session_id),
            )

    def touch(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_active=? WHERE id=?", (self._now(), session_id)
            )

    def mark_offline(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET state=?, reachable=0, last_active=? WHERE id=?",
                (STATE_OFFLINE, self._now(), session_id),
            )

    def deregister(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, *, include_unmanaged: bool = False) -> list[Session]:
        with self._lock:
            if include_unmanaged:
                rows = self._conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM sessions WHERE is_managed=1 ORDER BY started_at"
                ).fetchall()
        return [_row_to_session(r) for r in rows]

    # --- calls --------------------------------------------------------------
    def create_call(
        self,
        call_id: str,
        from_id: str,
        to_id: str,
        question: str,
        deadline: float,
        *,
        kind: str = KIND_CALL,
        status: str = CALL_QUEUED,
    ) -> Call:
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO calls
                    (call_id, from_id, to_id, question, kind, status, deadline, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (call_id, from_id, to_id, question, kind, status, deadline, now),
            )
        call = self.get_call(call_id)
        assert call is not None
        return call

    def get_call(self, call_id: str) -> Call | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
        return _row_to_call(row) if row else None

    def set_call_status(
        self, call_id: str, status: str, *, target_state_on_delivery: str | None = None
    ) -> None:
        now = self._now()
        with self._lock:
            if status == CALL_DELIVERED:
                self._conn.execute(
                    "UPDATE calls SET status=?, delivered_at=?, target_state_on_delivery=?"
                    " WHERE call_id=?",
                    (status, now, target_state_on_delivery, call_id),
                )
            else:
                self._conn.execute("UPDATE calls SET status=? WHERE call_id=?", (status, call_id))

    def record_answer(self, call_id: str, answer: str, blocked: list[str]) -> None:
        now = self._now()
        status = CALL_BLOCKED if blocked else CALL_ANSWERED
        with self._lock:
            self._conn.execute(
                "UPDATE calls SET status=?, answer=?, blocked=?, answered_at=? WHERE call_id=?",
                (status, answer, json.dumps(blocked), now, call_id),
            )

    def active_inbound_call(self, target_id: str) -> Call | None:
        """The call currently occupying the target (delivered but not resolved)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE to_id=? AND status IN (?, ?)"
                " ORDER BY delivered_at LIMIT 1",
                (target_id, CALL_DELIVERED, CALL_ANSWERING),
            ).fetchone()
        return _row_to_call(row) if row else None

    def calls_awaiting_delivery(self) -> list[Call]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE status=? ORDER BY created_at", (CALL_QUEUED,)
            ).fetchall()
        return [_row_to_call(r) for r in rows]

    def due_calls(self) -> list[Call]:
        """Unresolved calls whose deadline has passed."""
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE status IN (?, ?, ?) AND deadline<=? ORDER BY deadline",
                (CALL_QUEUED, CALL_DELIVERED, CALL_ANSWERING, now),
            ).fetchall()
        return [_row_to_call(r) for r in rows]

    # --- messages & queue ---------------------------------------------------
    def create_message(
        self,
        message_id: str,
        to_id: str,
        kind: str,
        body: dict[str, Any],
        *,
        from_id: str | None = None,
        call_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (message_id, from_id, to_id, kind, body, call_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, from_id, to_id, kind, json.dumps(body), call_id, self._now()),
            )

    def enqueue(self, target_id: str, message_id: str) -> int:
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS nxt FROM queue WHERE target_id=?",
                (target_id,),
            ).fetchone()
            seq = int(row["nxt"])
            cur = self._conn.execute(
                "INSERT INTO queue (target_id, message_id, seq, status, enqueued_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (target_id, message_id, seq, MSG_QUEUED, now),
            )
            return int(cur.lastrowid)

    def queue_depth(self, target_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM queue WHERE target_id=? AND status IN (?, ?)",
                (target_id, MSG_QUEUED, MSG_DELIVERED),
            ).fetchone()
        return int(row["n"])

    def next_queued(self, target_id: str) -> QueuedMessage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT q.*, m.kind AS kind, m.body AS body, m.call_id AS call_id"
                " FROM queue q JOIN messages m ON m.message_id=q.message_id"
                " WHERE q.target_id=? AND q.status=? ORDER BY q.seq LIMIT 1",
                (target_id, MSG_QUEUED),
            ).fetchone()
        return _row_to_queued(row) if row else None

    def pending_for(self, target_id: str) -> list[QueuedMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT q.*, m.kind AS kind, m.body AS body, m.call_id AS call_id"
                " FROM queue q JOIN messages m ON m.message_id=q.message_id"
                " WHERE q.target_id=? AND q.status IN (?, ?) ORDER BY q.seq",
                (target_id, MSG_QUEUED, MSG_DELIVERED),
            ).fetchall()
        return [_row_to_queued(r) for r in rows]

    def get_queued(self, message_id: str) -> QueuedMessage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT q.*, m.kind AS kind, m.body AS body, m.call_id AS call_id"
                " FROM queue q JOIN messages m ON m.message_id=q.message_id"
                " WHERE q.message_id=? LIMIT 1",
                (message_id,),
            ).fetchone()
        return _row_to_queued(row) if row else None

    def set_queue_status(self, message_id: str, status: str) -> None:
        now = self._now()
        with self._lock:
            if status == MSG_DELIVERED:
                self._conn.execute(
                    "UPDATE queue SET status=?, delivered_at=? WHERE message_id=?",
                    (status, now, message_id),
                )
            elif status == MSG_PROCESSED:
                self._conn.execute(
                    "UPDATE queue SET status=?, acked_at=? WHERE message_id=?",
                    (status, now, message_id),
                )
            else:
                self._conn.execute(
                    "UPDATE queue SET status=? WHERE message_id=?", (status, message_id)
                )

    def redeliver_inflight(self, target_id: str) -> None:
        """On reconnect, reset delivered-but-unacked entries back to queued so they
        are re-sent. Stable message ids let the adapter dedupe."""
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET status=?, delivered_at=NULL WHERE target_id=? AND status=?",
                (MSG_QUEUED, target_id, MSG_DELIVERED),
            )

    # --- transcript ---------------------------------------------------------
    def record_event(
        self,
        kind: str,
        status: str,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        gist: str = "",
        body: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> None:
        stored_body = json.dumps(body) if (body is not None and self._retain_bodies) else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO transcript (ts, from_id, to_id, kind, status, gist, body, call_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._now(), from_id, to_id, kind, status, gist[:200], stored_body, call_id),
            )

    def recent(self, peer: str | None = None, limit: int = 20) -> list[TranscriptEntry]:
        with self._lock:
            if peer:
                rows = self._conn.execute(
                    "SELECT * FROM transcript WHERE from_id=? OR to_id=? ORDER BY id DESC LIMIT ?",
                    (peer, peer, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM transcript ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_row_to_transcript(r) for r in rows]

    # --- rate limiting ------------------------------------------------------
    def record_rate(self, from_id: str, to_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_events (from_id, to_id, ts) VALUES (?, ?, ?)",
                (from_id, to_id, self._now()),
            )

    def count_rate(self, from_id: str, to_id: str, window_s: float) -> int:
        since = self._now() - window_s
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM rate_events WHERE from_id=? AND to_id=? AND ts>=?",
                (from_id, to_id, since),
            ).fetchone()
        return int(row["n"])


# --- row helpers -----------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        family=row["family"],
        pid=row["pid"],
        cwd=row["cwd"],
        state=row["state"],
        vendor_session_id=row["vendor_session_id"],
        reachable=bool(row["reachable"]),
        is_managed=bool(row["is_managed"]),
        last_user_message=row["last_user_message"],
        started_at=row["started_at"],
        last_active=row["last_active"],
    )


def _row_to_call(row: sqlite3.Row) -> Call:
    return Call(
        call_id=row["call_id"],
        from_id=row["from_id"],
        to_id=row["to_id"],
        question=row["question"],
        kind=row["kind"],
        status=row["status"],
        answer=row["answer"],
        blocked=json.loads(row["blocked"]),
        deadline=row["deadline"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
        answered_at=row["answered_at"],
        target_state_on_delivery=row["target_state_on_delivery"],
    )


def _row_to_queued(row: sqlite3.Row) -> QueuedMessage:
    return QueuedMessage(
        queue_id=row["queue_id"],
        target_id=row["target_id"],
        message_id=row["message_id"],
        seq=row["seq"],
        kind=row["kind"],
        body=json.loads(row["body"]),
        status=row["status"],
        call_id=row["call_id"],
        enqueued_at=row["enqueued_at"],
        delivered_at=row["delivered_at"],
    )


def _row_to_transcript(row: sqlite3.Row) -> TranscriptEntry:
    return TranscriptEntry(
        ts=row["ts"],
        from_id=row["from_id"],
        to_id=row["to_id"],
        kind=row["kind"],
        status=row["status"],
        gist=row["gist"],
        call_id=row["call_id"],
    )


def iter_json_list(value: Iterable[Any]) -> list[Any]:  # small convenience for callers
    return list(value)
