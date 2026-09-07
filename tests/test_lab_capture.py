"""Wire capture for ``bridge lab``: redaction by default, ``--full`` verbatim,
the ``RpcEndpoint`` / ``RouterServer`` hooks, and -- the important one -- that
an unset ``BRIDGE_LAB_CAPTURE`` costs nothing and writes nothing.
"""

from __future__ import annotations

import json
import socket

from bridge.lab import capture
from bridge.mcp import RpcEndpoint

from .fakes.claude_host import FakeClaudeHost
from .fakes.router_peer import RunningRouter


def test_redaction_replaces_only_body_fields():
    frame = {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {
            "kind": "call",
            "call_id": "c-1",
            "from": "codex-1 (refactor)",
            "text": "hello world",
            "question": "why?",
            "answer": "because",
            "message": "hi",
        },
    }
    out = capture.redact(frame)
    params = out["params"]
    assert params["kind"] == "call"
    assert params["call_id"] == "c-1"
    assert params["from"] == "codex-1 (refactor)"
    assert params["text"] == "<redacted:11 chars>"
    assert params["question"] == "<redacted:4 chars>"
    assert params["answer"] == "<redacted:7 chars>"
    assert params["message"] == "<redacted:2 chars>"


def test_channel_notification_content_is_redacted_unless_full(tmp_path):
    frame = {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {
            "content": "[bridge call]\nquestion: why?",
            "meta": {"kind": "call", "call_id": "c-1", "from": "codex-1 (refactor)"},
        },
    }
    path = tmp_path / "wire.jsonl"
    capture.CaptureWriter(path).on_frame("adapter", "out", frame)
    capture.CaptureWriter(path, full=True).on_frame("adapter", "out", frame)
    records = capture.read_capture(path)
    assert capture.frame_params(records[0])["content"] == "<redacted:28 chars>"
    assert capture.frame_params(records[0])["meta"] == frame["params"]["meta"]
    assert capture.frame_params(records[1])["content"] == frame["params"]["content"]


def test_redaction_recurses_into_lists():
    frame = {"params": {"input": [{"type": "text", "text": "abcde"}]}}
    assert capture.redact(frame)["params"]["input"][0]["text"] == "<redacted:5 chars>"
    assert capture.redact(frame)["params"]["input"][0]["type"] == "text"


def test_writer_appends_jsonl_and_full_keeps_bodies(tmp_path):
    path = tmp_path / "wire.jsonl"
    capture.CaptureWriter(path).on_frame("a", "out", {"params": {"text": "secret"}})
    capture.CaptureWriter(path, full=True).on_frame("b", "in", {"params": {"text": "secret"}})
    records = capture.read_capture(path)
    assert [r["source"] for r in records] == ["a", "b"]
    assert [r["direction"] for r in records] == ["out", "in"]
    assert records[0]["frame"]["params"]["text"] == "<redacted:6 chars>"
    assert records[1]["frame"]["params"]["text"] == "secret"


def test_hook_from_env_is_none_when_unset(tmp_path):
    assert capture.hook_from_env({}) is None
    assert capture.hook_from_env({capture.CAPTURE_ENV: "  "}) is None
    hook = capture.hook_from_env({capture.CAPTURE_ENV: str(tmp_path)})
    assert hook is not None
    hook("src", "out", {"a": 1})
    assert (tmp_path / capture.CAPTURE_FILENAME).exists()


def test_capture_full_env_toggles(tmp_path):
    env = {capture.CAPTURE_ENV: str(tmp_path), capture.CAPTURE_FULL_ENV: "1"}
    capture.hook_from_env(env)("s", "out", {"params": {"text": "keepme"}})
    records = capture.read_capture(tmp_path / capture.CAPTURE_FILENAME)
    assert records[0]["frame"]["params"]["text"] == "keepme"


def test_rpc_endpoint_hook_records_both_directions(tmp_path):
    hook = capture.hook_from_env({capture.CAPTURE_ENV: str(tmp_path)})
    a_sock, b_sock = socket.socketpair()
    a = RpcEndpoint(a_sock, name="side-a", on_frame=hook)
    b = RpcEndpoint(b_sock, name="side-b")
    b.method("ping", lambda _p: {"pong": True})
    a.start()
    b.start()
    try:
        assert a.request("ping", {"text": "hello"}, timeout=3.0) == {"pong": True}
    finally:
        a.close()
        b.close()

    records = capture.read_capture(tmp_path / capture.CAPTURE_FILENAME)
    assert {r["direction"] for r in records} == {"in", "out"}
    outbound = [r for r in records if r["direction"] == "out"]
    assert capture.frame_method(outbound[0]) == "ping"
    assert capture.frame_params(outbound[0])["text"] == "<redacted:5 chars>"
    assert all(r["source"] == "side-a" for r in records)


def test_router_server_hook_records_wire_frames(paths, tmp_path, monkeypatch):
    monkeypatch.setenv(capture.CAPTURE_ENV, str(tmp_path))
    with RunningRouter(paths) as rr:
        rr.client(session_id="s1").call(
            "register_session", {"session_id": "s1", "family": "claude", "state": "idle"}
        )
    records = capture.read_capture(tmp_path / capture.CAPTURE_FILENAME)
    ops = [r["frame"].get("op") for r in records if r["direction"] == "in"]
    assert "hello" in ops
    assert "register_session" in ops
    assert all(r["source"] == "router" for r in records)


def test_capture_unset_costs_nothing_and_writes_nothing(paths, tmp_path, monkeypatch):
    """The opt-in must be a true opt-in: no hook, no file, no directory."""
    monkeypatch.delenv(capture.CAPTURE_ENV, raising=False)
    monkeypatch.delenv(capture.CAPTURE_FULL_ENV, raising=False)
    quiet = tmp_path / "should-stay-empty"
    quiet.mkdir()

    host_sock, peer_sock = socket.socketpair()
    endpoint = RpcEndpoint(peer_sock, name="quiet")
    assert endpoint._on_frame is None

    with RunningRouter(paths) as rr:
        assert rr.server._on_frame is None
        host = FakeClaudeHost(host_sock)
        endpoint.method("ping", lambda _p: {})
        endpoint.start()
        try:
            rr.client(session_id="s1").call(
                "register_session", {"session_id": "s1", "family": "claude", "state": "idle"}
            )
        finally:
            host.close()
            endpoint.close()

    assert list(quiet.iterdir()) == []
    assert not (tmp_path / capture.CAPTURE_FILENAME).exists()


def test_capture_tail_reads_only_new_records(tmp_path):
    path = tmp_path / "wire.jsonl"
    writer = capture.CaptureWriter(path)
    writer.on_frame("s", "out", {"n": 1})
    tail = capture.CaptureTail(path)
    tail.mark()
    assert tail.read_new() == []
    writer.on_frame("s", "out", {"n": 2})
    fresh = tail.read_new()
    assert [r["frame"]["n"] for r in fresh] == [2]
    assert [r["frame"]["n"] for r in tail.all_records()] == [1, 2]


def test_parse_records_skips_partial_lines(tmp_path):
    path = tmp_path / "wire.jsonl"
    path.write_text(json.dumps({"frame": {}}) + "\n{not json\n\n", encoding="utf-8")
    assert len(capture.read_capture(path)) == 1
