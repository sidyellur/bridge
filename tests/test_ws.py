"""Task 1 verify (ws): RFC 6455 client/server handshake and framing codec."""

from __future__ import annotations

import re
import socket
import struct
import threading

import pytest

from bridge.ws import (
    MAX_CONTROL_PAYLOAD,
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WS_GUID,
    WebSocketError,
    accept_key,
    client_handshake,
    recv_message,
    send_frame,
    send_text,
    server_handshake,
)


def _server_thread(sock: socket.socket):
    result: dict = {}

    def run() -> None:
        try:
            result["headers"] = server_handshake(sock)
        except WebSocketError as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def _apply_mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def _recv_exact_raw(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise AssertionError("unexpected EOF while reading a test frame")
        data += chunk
    return data


def _read_raw_frame(sock: socket.socket) -> tuple[int, bool, bool, bytes]:
    header = sock.recv(2)
    byte0, byte1 = header
    fin = bool(byte0 & 0x80)
    opcode = byte0 & 0x0F
    masked = bool(byte1 & 0x80)
    length = byte1 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", sock.recv(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", sock.recv(8))
    mask_key = b""
    if masked:
        mask_key = sock.recv(4)
    payload = b""
    while len(payload) < length:
        payload += sock.recv(length - len(payload))
    if masked:
        payload = _apply_mask(payload, mask_key)
    return opcode, fin, masked, payload


def test_accept_key_matches_the_rfc_vector():
    assert WS_GUID == "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_round_trip_over_a_socketpair():
    client, server = socket.socketpair()
    try:
        thread, result = _server_thread(server)
        client_handshake(client, host="localhost", path="/")
        thread.join(timeout=5)
        assert "error" not in result
        headers = result["headers"]
        assert "sec-websocket-key" in headers
    finally:
        client.close()
        server.close()


def test_server_handshake_requires_only_the_key():
    client, server = socket.socketpair()
    try:
        thread, result = _server_thread(server)
        request = (
            "GET /deep/nested/path HTTP/1.1\r\n"
            "Origin: http://example.com\r\n"
            "Authorization: Bearer secret\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        client.sendall(request.encode())
        thread.join(timeout=5)
        assert "error" not in result
        response = client.recv(4096).decode()
        assert response.startswith("HTTP/1.1 101 Switching Protocols")
        assert "sec-websocket-accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in response
    finally:
        client.close()
        server.close()


def test_server_handshake_without_a_key_raises_and_writes_nothing():
    client, server = socket.socketpair()
    try:
        request = "GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
        client.sendall(request.encode())
        client.shutdown(socket.SHUT_WR)
        with pytest.raises(WebSocketError, match="missing Sec-WebSocket-Key"):
            server_handshake(server)
        server.close()
        assert client.recv(4096) == b""
    finally:
        client.close()
        server.close()


def test_client_handshake_rejects_a_non_101_status():
    client, server = socket.socketpair()
    try:
        captured: dict = {}

        def respond() -> None:
            captured["request"] = server.recv(4096)
            server.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

        thread = threading.Thread(target=respond)
        thread.start()
        with pytest.raises(WebSocketError, match="websocket handshake failed"):
            client_handshake(client)
        thread.join(timeout=5)

        match = re.search(rb"Sec-WebSocket-Key: (\S+)\r\n", captured["request"])
        assert match is not None
        key = match.group(1).decode()
        expected = (
            f"GET / HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        ).encode()
        assert captured["request"] == expected
    finally:
        client.close()
        server.close()


def test_client_handshake_rejects_a_wrong_accept_digest():
    client, server = socket.socketpair()
    try:
        def respond() -> None:
            server.recv(4096)
            server.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"connection: Upgrade\r\n"
                b"upgrade: websocket\r\n"
                b"sec-websocket-accept: not-the-right-digest\r\n"
                b"\r\n"
            )

        thread = threading.Thread(target=respond)
        thread.start()
        with pytest.raises(WebSocketError, match="bad Sec-WebSocket-Accept"):
            client_handshake(client)
        thread.join(timeout=5)
    finally:
        client.close()
        server.close()


def test_client_handshake_does_not_consume_bytes_past_the_header_terminator():
    client, server = socket.socketpair()
    try:
        def respond() -> None:
            request = server.recv(4096)
            match = re.search(rb"Sec-WebSocket-Key: (\S+)\r\n", request)
            key = match.group(1).decode()
            response = (
                f"HTTP/1.1 101 Switching Protocols\r\n"
                f"connection: Upgrade\r\n"
                f"upgrade: websocket\r\n"
                f"sec-websocket-accept: {accept_key(key)}\r\n"
                f"\r\n"
            ).encode()
            frame = bytes([0x81, 0x05]) + b"hello"
            server.sendall(response + frame)

        thread = threading.Thread(target=respond)
        thread.start()
        client_handshake(client)
        thread.join(timeout=5)

        assert recv_message(client) == b"hello"
    finally:
        client.close()
        server.close()


def test_server_handshake_does_not_consume_bytes_past_the_header_terminator():
    client, server = socket.socketpair()
    try:
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        ).encode()

        frame_payload = b"pipelined"
        mask_key = b"\x01\x02\x03\x04"
        masked_payload = _apply_mask(frame_payload, mask_key)
        frame = bytes([0x81, 0x80 | len(frame_payload)]) + mask_key + masked_payload

        client.sendall(request + frame)

        headers = server_handshake(server)
        assert headers["sec-websocket-key"] == key

        assert recv_message(server, require_mask=True) == frame_payload
    finally:
        client.close()
        server.close()


def test_client_frames_are_masked_and_server_frames_are_not():
    client, server = socket.socketpair()
    try:
        send_text(client, b"hello", mask=True)
        opcode, fin, masked, payload = _read_raw_frame(server)
        assert masked
        assert opcode == OP_TEXT
        assert fin
        assert payload == b"hello"

        send_text(server, b"world", mask=False)
        opcode, fin, masked, payload = _read_raw_frame(client)
        assert not masked
        assert payload == b"world"
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("size", [5, 200, 70_000])
def test_round_trip_across_all_three_length_forms(size: int):
    payload = bytes(i % 256 for i in range(size))
    client, server = socket.socketpair()
    try:
        received: dict = {}

        def send_from_client() -> None:
            send_text(client, payload, mask=True)

        thread = threading.Thread(target=send_from_client)
        thread.start()
        received["from_client"] = recv_message(server, require_mask=True)
        thread.join(timeout=5)
        assert received["from_client"] == payload

        def send_from_server() -> None:
            send_text(server, payload, mask=False)

        thread = threading.Thread(target=send_from_server)
        thread.start()
        received["from_server"] = recv_message(client)
        thread.join(timeout=5)
        assert received["from_server"] == payload
    finally:
        client.close()
        server.close()


def test_length_forms_encode_the_correct_field_width():
    client, server = socket.socketpair()
    try:
        payload_200 = b"x" * 200
        send_frame(client, OP_TEXT, payload_200, mask=True)
        header = _recv_exact_raw(server, 2)
        assert header[1] & 0x7F == 126
        (length,) = struct.unpack("!H", _recv_exact_raw(server, 2))
        assert length == 200
        mask_key = _recv_exact_raw(server, 4)
        payload = _recv_exact_raw(server, 200)
        assert _apply_mask(payload, mask_key) == payload_200

        payload_big = b"y" * 70_000

        def send_big() -> None:
            send_frame(client, OP_TEXT, payload_big, mask=True)

        thread = threading.Thread(target=send_big)
        thread.start()
        header = _recv_exact_raw(server, 2)
        assert header[1] & 0x7F == 127
        (length,) = struct.unpack("!Q", _recv_exact_raw(server, 8))
        assert length == 70_000
        mask_key = _recv_exact_raw(server, 4)
        payload = _recv_exact_raw(server, 70_000)
        thread.join(timeout=5)
        assert _apply_mask(payload, mask_key) == payload_big
    finally:
        client.close()
        server.close()


def test_utf8_payload_round_trips():
    text = "café 日本"  # "é" and "日"
    payload = text.encode("utf-8")
    client, server = socket.socketpair()
    try:
        send_text(client, payload, mask=True)
        received = recv_message(server, require_mask=True)
        assert received == payload
        assert received.decode("utf-8") == text
    finally:
        client.close()
        server.close()


def test_continuation_frames_are_reassembled():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_TEXT, b"hello ", mask=True, fin=False)
        send_frame(client, OP_CONT, b"cruel ", mask=True, fin=False)
        send_frame(client, OP_CONT, b"world", mask=True, fin=True)
        assert recv_message(server, require_mask=True) == b"hello cruel world"
    finally:
        client.close()
        server.close()


def test_data_frame_during_fragmented_message_is_rejected():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_TEXT, b"start", mask=True, fin=False)
        send_frame(client, OP_TEXT, b"newmsg", mask=True, fin=True)
        with pytest.raises(
            WebSocketError, match="data frame while a fragmented message is in progress"
        ):
            recv_message(server, require_mask=True)
    finally:
        client.close()
        server.close()


def test_ping_is_answered_with_a_pong_and_reading_continues():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_PING, b"hi", mask=True)
        send_text(client, b"still here", mask=True)

        assert recv_message(server, require_mask=True) == b"still here"

        opcode, fin, masked, payload = _read_raw_frame(client)
        assert opcode == OP_PONG
        assert fin
        assert not masked
        assert payload == b"hi"
    finally:
        client.close()
        server.close()


def test_pong_frames_are_ignored():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_PONG, b"unsolicited", mask=True)
        send_text(client, b"payload", mask=True)
        assert recv_message(server, require_mask=True) == b"payload"
    finally:
        client.close()
        server.close()


def test_close_frame_returns_none():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_CLOSE, struct.pack("!H", 1000) + b"bye", mask=True)
        assert recv_message(server, require_mask=True) is None
    finally:
        client.close()
        server.close()


def test_eof_returns_none():
    client, server = socket.socketpair()
    try:
        client.close()
        assert recv_message(server) is None
    finally:
        server.close()


def test_server_role_rejects_an_unmasked_client_frame():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_TEXT, b"hello", mask=False)
        with pytest.raises(WebSocketError, match="unmasked client frame"):
            recv_message(server, require_mask=True)
    finally:
        client.close()
        server.close()


def test_client_role_accepts_unmasked_server_frames():
    client, server = socket.socketpair()
    try:
        send_frame(server, OP_TEXT, b"from server", mask=False)
        assert recv_message(client) == b"from server"
    finally:
        client.close()
        server.close()


def test_fragmented_control_frame_is_rejected():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_PING, b"hi", mask=True, fin=False)
        with pytest.raises(WebSocketError):
            recv_message(server, require_mask=True)
    finally:
        client.close()
        server.close()


def test_oversized_control_frame_is_rejected():
    client, server = socket.socketpair()
    try:
        send_frame(client, OP_PING, b"x" * (MAX_CONTROL_PAYLOAD + 1), mask=True)
        with pytest.raises(WebSocketError):
            recv_message(server, require_mask=True)
    finally:
        client.close()
        server.close()


def test_reserved_opcode_is_rejected():
    client, server = socket.socketpair()
    try:
        send_frame(client, 0x3, b"", mask=True)
        with pytest.raises(WebSocketError):
            recv_message(server, require_mask=True)
    finally:
        client.close()
        server.close()


def test_send_text_never_appends_a_newline():
    payload = b'{"a": 1}\n{"b": 2}'
    client, server = socket.socketpair()
    try:
        send_text(client, payload, mask=True)
        received = recv_message(server, require_mask=True)
        assert received == payload
        assert received.count(b"\n") == 1
    finally:
        client.close()
        server.close()


def test_binary_opcode_constant_is_distinct():
    assert OP_BINARY == 0x2
