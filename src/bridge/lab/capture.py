"""Opt-in wire capture for ``bridge lab``.

Setting ``BRIDGE_LAB_CAPTURE=<dir>`` makes every :class:`~bridge.mcp.RpcEndpoint`
(MCP / Codex App Server JSON-RPC, both directions) and every
:class:`~bridge.router.RouterServer` frame append a JSON line to
``<dir>/wire.jsonl``. That file is the raw evidence an experiment verdict cites.

Capture is *strictly* opt-in: with the variable unset the hook is ``None``, no
writer is constructed, and nothing is written or opened. Message bodies are
redacted by default -- the fields Bridge treats as content (``text``,
``message``, ``question``, ``answer``) are replaced with
``<redacted:N chars>`` so a capture can be pasted into a public experiment log.
``BRIDGE_LAB_CAPTURE_FULL=1`` (``bridge lab prepare --full``) keeps the bodies.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

CAPTURE_ENV = "BRIDGE_LAB_CAPTURE"
CAPTURE_FULL_ENV = "BRIDGE_LAB_CAPTURE_FULL"
CAPTURE_FILENAME = "wire.jsonl"

#: Body-bearing keys. Their string values never reach a capture file unless the
#: operator explicitly asked for ``--full``.
REDACTED_KEYS = ("text", "message", "question", "answer")

#: ``on_frame(source, direction, obj)``. ``direction`` is ``"in"`` or ``"out"``
#: from the perspective of the component that owns the hook.
FrameHook = Callable[[str, str, Mapping[str, Any]], None]

DIRECTION_IN = "in"
DIRECTION_OUT = "out"


def redacted(value: str) -> str:
    return f"<redacted:{len(value)} chars>"


def redact(obj: Any) -> Any:
    """Recursively replace body strings, preserving every routing field."""
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in REDACTED_KEYS and isinstance(value, str):
                out[key] = redacted(value)
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


class CaptureWriter:
    """Append-only JSONL writer with an :attr:`on_frame` hook.

    Each record is written with its own short-lived ``open(..., "a")`` so the
    lab never leaves a file handle dangling in a vendor process (the hermetic
    suite runs with ``filterwarnings = error``) and so several processes -- the
    router daemon, the Claude Channel adapter, the Codex App Server client --
    can share one capture file.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        full: bool = False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.full = full
        self._now = now
        self._lock = threading.Lock()

    def on_frame(self, source: str, direction: str, obj: Mapping[str, Any]) -> None:
        record = {
            "ts": round(self._now(), 6),
            "source": source or "?",
            "direction": direction,
            "frame": dict(obj) if self.full else redact(obj),
        }
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                # A capture is diagnostic scaffolding; it must never take down a
                # live session because a directory went away mid-experiment.
                pass


def capture_dir(env: Mapping[str, str] | None = None) -> Path | None:
    environ = os.environ if env is None else env
    raw = (environ.get(CAPTURE_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def capture_full(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return (environ.get(CAPTURE_FULL_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def hook_from_env(env: Mapping[str, str] | None = None) -> FrameHook | None:
    """The hook :class:`RpcEndpoint` / :class:`RouterServer` install when the
    operator opted in. ``None`` -- and therefore zero work -- when unset."""
    directory = capture_dir(env)
    if directory is None:
        return None
    return CaptureWriter(directory / CAPTURE_FILENAME, full=capture_full(env)).on_frame


# --- reading ---------------------------------------------------------------


def parse_records(lines: Iterable[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_capture(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return parse_records(p.read_text(encoding="utf-8").splitlines())


def frame_method(record: Mapping[str, Any]) -> str | None:
    frame = record.get("frame")
    if isinstance(frame, Mapping):
        method = frame.get("method")
        if isinstance(method, str):
            return method
    return None


def frame_params(record: Mapping[str, Any]) -> dict[str, Any]:
    frame = record.get("frame")
    if isinstance(frame, Mapping):
        params = frame.get("params")
        if isinstance(params, Mapping):
            return dict(params)
    return {}


class CaptureTail:
    """Reads only the records appended since the last :meth:`mark`."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._offset = 0

    def exists(self) -> bool:
        return self.path.exists()

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def mark(self) -> None:
        self._offset = len(self._lines())

    def read_new(self) -> list[dict[str, Any]]:
        lines = self._lines()
        fresh = lines[self._offset :]
        self._offset = len(lines)
        return parse_records(fresh)

    def all_records(self) -> list[dict[str, Any]]:
        return parse_records(self._lines())


__all__ = [
    "CAPTURE_ENV",
    "CAPTURE_FILENAME",
    "CAPTURE_FULL_ENV",
    "DIRECTION_IN",
    "DIRECTION_OUT",
    "REDACTED_KEYS",
    "CaptureTail",
    "CaptureWriter",
    "FrameHook",
    "capture_dir",
    "capture_full",
    "frame_method",
    "frame_params",
    "hook_from_env",
    "parse_records",
    "read_capture",
    "redact",
    "redacted",
]
