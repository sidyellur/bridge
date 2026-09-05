"""Task 3 verify (protocol): framing, size limits, and message constructors."""

from __future__ import annotations

import socket

import pytest

from bridge.protocol import (
    MAX_FRAME_BYTES,
    FrameBuffer,
    FrameTooLarge,
    Hello,
    encode_frame,
    err_response,
    event_frame,
    hello_args,
    ok_response,
    recv_frame,
    request,
    send_frame,
)


def test_partial_frames_are_buffered():
    buf = FrameBuffer()
    payload = encode_frame({"a": 1})
    buf.feed(payload[:2])
    assert buf.frames() == []
    buf.feed(payload[2:])
    assert buf.frames() == [{"a": 1}]


def test_multiple_frames_in_one_feed():
    buf = FrameBuffer()
    buf.feed(encode_frame({"a": 1}) + encode_frame({"b": 2}))
    assert buf.frames() == [{"a": 1}, {"b": 2}]


def test_oversize_frame_rejected_on_encode():
    with pytest.raises(FrameTooLarge):
        encode_frame({"x": "z" * (MAX_FRAME_BYTES + 1)})


def test_oversize_declared_length_rejected():
    buf = FrameBuffer()
    buf.feed((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
    with pytest.raises(FrameTooLarge):
        buf.frames()


def test_message_constructors():
    assert request(1, "hello", {"x": 1}) == {"t": "req", "id": 1, "op": "hello", "args": {"x": 1}}
    assert ok_response(2, {"y": 2}) == {"t": "resp", "id": 2, "ok": True, "result": {"y": 2}}
    err = err_response(3, "bad", "nope")
    assert err["ok"] is False and err["error"] == {"code": "bad", "message": "nope"}
    assert event_frame({"k": "v"}) == {"t": "event", "event": {"k": "v"}}


def test_hello_parse():
    h = Hello.parse(hello_args("tok", "sess-1", "adapter"))
    assert h.token == "tok"
    assert h.session_id == "sess-1"
    assert h.role == "adapter"
    assert h.protocol_version == 1


def test_blocking_socket_roundtrip(tmp_path):
    a, b = socket.socketpair()
    try:
        send_frame(a, {"hello": "world"})
        assert recv_frame(b) == {"hello": "world"}
    finally:
        a.close()
        b.close()
