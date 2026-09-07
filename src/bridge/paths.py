"""Filesystem layout for Bridge state.

Every path used by Bridge is derived from a single injectable :class:`Paths`
object rooted at ``BRIDGE_HOME`` (default ``~/.bridge``). Tests construct a
``Paths`` under a temporary directory so no default suite touches real user
state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRIDGE_HOME_ENV = "BRIDGE_HOME"
ROUTER_SOCKET_ENV = "BRIDGE_ROUTER_SOCKET"
ROUTER_TOKEN_ENV = "BRIDGE_ROUTER_TOKEN_PATH"
SESSION_ID_ENV = "BRIDGE_SESSION_ID"


@dataclass(frozen=True)
class Paths:
    """Resolved locations for Bridge's on-disk state."""

    home: Path

    @classmethod
    def resolve(
        cls,
        env: dict[str, str] | None = None,
        home: str | os.PathLike[str] | None = None,
    ) -> Paths:
        """Resolve paths from an explicit ``home``, then ``BRIDGE_HOME``, then ``~/.bridge``."""
        if home is not None:
            root = Path(home)
        else:
            environ = os.environ if env is None else env
            raw = environ.get(BRIDGE_HOME_ENV)
            root = Path(raw) if raw else Path(environ.get("HOME", str(Path.home()))) / ".bridge"
        return cls(home=root.expanduser())

    # --- top-level artifacts ------------------------------------------------
    @property
    def db(self) -> Path:
        return self.home / "bridge.db"

    @property
    def socket(self) -> Path:
        return self.home / "router.sock"

    @property
    def token(self) -> Path:
        return self.home / "router.token"

    @property
    def pidfile(self) -> Path:
        return self.home / "router.pid"

    @property
    def contacts(self) -> Path:
        """User-owned alias file (``name -> session id``). Optional; post-v1."""
        return self.home / "contacts.json"

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    # --- per-session --------------------------------------------------------
    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def session_meta(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def codex_socket(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "codex.sock"

    def merge_session_meta(self, session_id: str, updates: Mapping[str, Any]) -> None:
        """Best-effort top-level merge into ``session.json``. Never raises -- a
        session directory Bridge cannot write to must not break the wrapper or
        the adapter that called this."""
        path = self.session_meta(session_id)
        data: dict[str, Any] = {}
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            data = existing
        data.update(updates)
        try:
            self.ensure_session_dir(session_id)
            # Write-then-rename: a reader (doctor, or the wrapper's own stub) must
            # never observe the truncated window of an in-place rewrite.
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except OSError:
            pass

    # --- construction -------------------------------------------------------
    def ensure(self) -> Paths:
        """Create the home tree with user-only permissions and return self."""
        self.home.mkdir(parents=True, exist_ok=True)
        _chmod(self.home, 0o700)
        for sub in (self.sessions_dir, self.logs_dir):
            sub.mkdir(parents=True, exist_ok=True)
            _chmod(sub, 0o700)
        return self

    def ensure_session_dir(self, session_id: str) -> Path:
        d = self.session_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        _chmod(d, 0o700)
        return d


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except (PermissionError, NotImplementedError):  # pragma: no cover - platform dependent
        pass
