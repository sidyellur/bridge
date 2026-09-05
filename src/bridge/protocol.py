"""Wire protocol for the Bridge router socket.

Frames are length-prefixed JSON: a 4-byte big-endian unsigned length followed by
that many bytes of UTF-8 JSON. Three frame shapes travel over a connection:

* request  ``{"t": "req",  "id": <int>, "op": <str>, "args": {...}}``
* response ``{"t": "resp", "id": <int>, "ok": <bool>, "result"|"error": ...}``
* event    ``{"t": "event", "event": {...}}``  (server-initiated push)

The first request on every connection must be ``hello`` carrying the protocol
version and the bearer token; the router rejects a wrong token or an
unsupported version before any other op is honored.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from typing import Any

from . import PROTOCOL_VERSION

LEN_PREFIX = struct.Struct(">I")
MAX_FRAME_BYTES = 1 << 20  # 1 MiB hard ceiling on a single frame
MAX_MESSAGE_CHARS = 16_000  # per-field text limit for call/text bodies


class ProtocolError(Exception):
    """Raised for framing, version, or authentication faults."""


class FrameTooLarge(ProtocolError):
    pass


class ConnectionClosed(ProtocolError):
    pass


# --- framing ---------------------------------------------------------------

def encode_frame(obj: Any) -> bytes:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"frame of {len(body)} bytes exceeds {MAX_FRAME_BYTES}")
    return LEN_PREFIX.pack(len(body)) + body


def decode_frame(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"))


class FrameBuffer:
    """Incremental parser turning a byte stream into decoded frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def frames(self) -> list[Any]:
        out: list[Any] = []
        while True:
            if len(self._buf) < LEN_PREFIX.size:
                break
            (length,) = LEN_PREFIX.unpack_from(self._buf, 0)
            if length > MAX_FRAME_BYTES:
                raise FrameTooLarge(f"declared frame length {length} exceeds {MAX_FRAME_BYTES}")
            if len(self._buf) < LEN_PREFIX.size + length:
                break
            start = LEN_PREFIX.size
            body = bytes(self._buf[start : start + length])
            del self._buf[: start + length]
            out.append(decode_frame(body))
        return out


# --- blocking socket helpers ----------------------------------------------

def send_frame(sock: socket.socket, obj: Any) -> None:
    sock.sendall(encode_frame(obj))


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            raise ConnectionClosed("peer closed connection")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(sock: socket.socket) -> Any:
    header = _recv_exactly(sock, LEN_PREFIX.size)
    (length,) = LEN_PREFIX.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"declared frame length {length} exceeds {MAX_FRAME_BYTES}")
    return decode_frame(_recv_exactly(sock, length))


# --- message constructors --------------------------------------------------

def request(req_id: int, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"t": "req", "id": req_id, "op": op, "args": args or {}}


def ok_response(req_id: int, result: Any) -> dict[str, Any]:
    return {"t": "resp", "id": req_id, "ok": True, "result": result}


def err_response(req_id: int, code: str, message: str) -> dict[str, Any]:
    return {"t": "resp", "id": req_id, "ok": False, "error": {"code": code, "message": message}}


def event_frame(event: dict[str, Any]) -> dict[str, Any]:
    return {"t": "event", "event": event}


def hello_args(token: str, session_id: str | None = None, role: str = "client") -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "token": token,
        "session_id": session_id,
        "role": role,
    }


@dataclass
class Hello:
    protocol_version: int
    token: str
    session_id: str | None
    role: str

    @classmethod
    def parse(cls, args: dict[str, Any]) -> Hello:
        return cls(
            protocol_version=int(args.get("protocol_version", 0)),
            token=str(args.get("token", "")),
            session_id=args.get("session_id"),
            role=str(args.get("role", "client")),
        )
