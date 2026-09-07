"""RFC 6455 WebSocket client and server codec over an already-connected socket.

``codex app-server --listen unix://PATH`` turned out to speak WebSocket over its
Unix socket rather than newline-delimited JSON, so Bridge needs its own codec —
stdlib only, no threads, no asyncio, no third-party dependency.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_VERSION = "13"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_CONTROL_PAYLOAD = 125

_CONTROL_OPCODES = frozenset({OP_CLOSE, OP_PING, OP_PONG})
_RESERVED_OPCODES = frozenset({0x3, 0x4, 0x5, 0x6, 0x7, 0xB, 0xC, 0xD, 0xE, 0xF})


class WebSocketError(Exception):
    pass


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def client_handshake(sock: socket.socket, *, host: str = "localhost", path: str = "/") -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: {WS_VERSION}\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode("utf-8"))
    status_line, headers = _read_headers(sock)
    if "101" not in status_line:
        raise WebSocketError(f"websocket handshake failed: {status_line!r}")
    if headers.get("sec-websocket-accept") != accept_key(key):
        raise WebSocketError("bad Sec-WebSocket-Accept")


def server_handshake(sock: socket.socket) -> dict[str, str]:
    _request_line, headers = _read_headers(sock)
    key = headers.get("sec-websocket-key")
    if key is None:
        raise WebSocketError("missing Sec-WebSocket-Key")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "connection: Upgrade\r\n"
        "upgrade: websocket\r\n"
        f"sec-websocket-accept: {accept_key(key)}\r\n"
        "\r\n"
    )
    sock.sendall(response.encode("utf-8"))
    return headers


def _read_headers(sock: socket.socket) -> tuple[str, dict[str, str]]:
    # One byte at a time so the socket is left positioned exactly after the
    # terminator — a bulk recv() would swallow bytes belonging to the first
    # frame the peer pipelined right after the handshake.
    data = bytearray()
    while not data.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    lines = bytes(data).decode("utf-8", errors="replace").split("\r\n")
    first_line = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return first_line, headers


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return bytes(chunks)


def _mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def send_frame(
    sock: socket.socket, opcode: int, payload: bytes = b"", *, mask: bool, fin: bool = True
) -> None:
    byte0 = (0x80 if fin else 0x00) | opcode
    mask_bit = 0x80 if mask else 0x00
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", byte0, mask_bit | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", byte0, mask_bit | 126, length)
    else:
        header = struct.pack("!BBQ", byte0, mask_bit | 127, length)
    if mask:
        mask_key = os.urandom(4)
        sock.sendall(header + mask_key + _mask(payload, mask_key))
    else:
        sock.sendall(header + payload)


def send_text(sock: socket.socket, payload: bytes, *, mask: bool) -> None:
    """One text frame carrying exactly `payload` — never appends a newline and
    never coalesces or splits messages; a frame is one message at this layer."""
    send_frame(sock, OP_TEXT, payload, mask=mask)


def _recv_frame(sock: socket.socket) -> tuple[int, bool, bool, bytes] | None:
    header = _recv_exact(sock, 2)
    if header is None:
        return None
    byte0, byte1 = header
    fin = bool(byte0 & 0x80)
    opcode = byte0 & 0x0F
    masked = bool(byte1 & 0x80)
    length = byte1 & 0x7F
    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        (length,) = struct.unpack("!H", ext)
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        (length,) = struct.unpack("!Q", ext)
    mask_key = b""
    if masked:
        mask_key = _recv_exact(sock, 4)
        if mask_key is None:
            return None
    payload = _recv_exact(sock, length)
    if payload is None:
        return None
    if masked:
        payload = _mask(payload, mask_key)
    return opcode, fin, masked, payload


def recv_message(sock: socket.socket, *, require_mask: bool = False) -> bytes | None:
    message_opcode: int | None = None
    buffer = bytearray()
    while True:
        frame = _recv_frame(sock)
        if frame is None:
            return None
        opcode, fin, masked, payload = frame

        if require_mask and not masked:
            raise WebSocketError("unmasked client frame")
        if opcode in _RESERVED_OPCODES:
            raise WebSocketError(f"reserved opcode: {opcode:#x}")
        if opcode in _CONTROL_OPCODES:
            if not fin:
                raise WebSocketError("fragmented control frame")
            if len(payload) > MAX_CONTROL_PAYLOAD:
                raise WebSocketError("oversized control frame")

        if opcode == OP_CLOSE:
            return None
        if opcode == OP_PING:
            send_frame(sock, OP_PONG, payload, mask=not require_mask)
            continue
        if opcode == OP_PONG:
            continue

        if opcode == OP_CONT:
            if message_opcode is None:
                raise WebSocketError("continuation frame with no preceding start frame")
            buffer.extend(payload)
        else:
            if message_opcode is not None:
                raise WebSocketError("data frame while a fragmented message is in progress")
            message_opcode = opcode
            buffer = bytearray(payload)

        if fin:
            return bytes(buffer)
