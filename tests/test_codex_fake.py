"""The fake Codex App Server is the pinned 0.151.0 contract in executable form.

Every assertion here is a frame the live probe recorded (see
``.superpowers/codex-contract-2026-09-07.md``); Task 4's client is written
against this fake, so a drift here is a drift in the contract.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import socket
import time
from pathlib import Path

import pytest

from bridge import ws
from bridge.ws import accept_key

from .fakes import codex_app_server as fake_module
from .fakes.codex_app_server import FakeCodexAppServer

FIXTURE = Path(__file__).parent / "fixtures" / "codex_protocol" / "codex-0.151.0.json"
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))

DRAIN = 0.15


class _Peer:
    """A raw WebSocket client speaking to the fake: masked text frames, one JSON
    message per frame, exactly like Bridge's real client will."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.sock.settimeout(3.0)
        ws.client_handshake(self.sock)
        self.seen: list[dict] = []
        self._id = 0

    def send(self, obj: dict) -> None:
        ws.send_text(self.sock, json.dumps(obj).encode("utf-8"), mask=True)

    def recv(self, timeout: float = 3.0) -> dict:
        self.sock.settimeout(timeout)
        payload = ws.recv_message(self.sock)
        if payload is None:
            raise EOFError("connection closed by the fake")
        return json.loads(payload)

    def drain(self, seconds: float = DRAIN) -> list[dict]:
        out: list[dict] = []
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return out
            self.sock.settimeout(remaining)
            try:
                payload = ws.recv_message(self.sock)
            except (TimeoutError, OSError):
                return out
            if payload is None:
                return out
            out.append(json.loads(payload))

    def call(self, method: str, params: dict | None = None, *, timeout: float = 3.0) -> dict:
        self._id += 1
        req_id = self._id
        message: dict = {"id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            msg = self.recv(max(0.01, deadline - time.monotonic()))
            if msg.get("id") == req_id:
                return msg
            self.seen.append(msg)


def _peer(sock: socket.socket) -> _Peer:
    return _Peer(sock)


@pytest.fixture
def fake_pair():
    made: list[tuple[FakeCodexAppServer, socket.socket]] = []

    def make(**kwargs) -> tuple[FakeCodexAppServer, _Peer]:
        client_sock, server_sock = socket.socketpair()
        fake = FakeCodexAppServer(server_sock, **kwargs)
        made.append((fake, client_sock))
        return fake, _peer(client_sock)

    yield make
    for fake, sock in made:
        fake.close()
        sock.close()


def _initialize(peer: _Peer) -> dict:
    response = peer.call("initialize", {"clientInfo": {"name": "bridge", "version": "0.1.0"}})
    peer.send({"method": "initialized"})
    return response


# --- the pin ---------------------------------------------------------------


def test_fixture_pins_the_version_and_the_websocket_transport():
    assert CONTRACT["codex_version"] == "0.151.0"
    transport = CONTRACT["transport"]
    assert transport["framing"] == "websocket"
    assert transport["client_frames_must_be_masked"] is True
    assert transport["messages_per_frame"] == 1
    assert transport["jsonrpc_field_present"] is False
    assert CONTRACT["launch_argv"] == [
        "{binary}",
        "app-server",
        "--listen",
        "unix://{socket_path}",
    ]


# --- transport -------------------------------------------------------------


def test_fake_completes_the_websocket_handshake():
    client_sock, server_sock = socket.socketpair()
    fake = FakeCodexAppServer(server_sock)
    try:
        client_sock.settimeout(3.0)
        key = base64.b64encode(os.urandom(16)).decode()
        client_sock.sendall(
            (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode()
        )
        raw = bytearray()
        while not bytes(raw).endswith(b"\r\n\r\n"):
            raw += client_sock.recv(1)
        text = bytes(raw).decode()
        assert "101 Switching Protocols" in text.splitlines()[0]
        assert f"sec-websocket-accept: {accept_key(key)}" in text
    finally:
        fake.close()
        client_sock.close()


def test_every_server_message_omits_jsonrpc_and_notifications_carry_emitted_at_ms(fake_pair):
    fake, peer = fake_pair(agent_message="hello there", resume_result="ok")
    peer.send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "bridge"}}})
    peer.send({"method": "initialized"})
    messages = peer.drain()
    peer.send({"id": 2, "method": "thread/resume", "params": {"threadId": fake.thread_id}})
    peer.send(
        {
            "id": 3,
            "method": "turn/start",
            "params": {"threadId": fake.thread_id, "input": [{"type": "text", "text": "hi"}]},
        }
    )
    messages += peer.drain()

    notifications = [m for m in messages if "method" in m]
    assert notifications, messages
    for message in messages:
        assert "jsonrpc" not in message, message
    for notification in notifications:
        assert isinstance(notification["emittedAtMs"], int), notification


# --- initialize ------------------------------------------------------------


def test_initialize_returns_the_four_fields_and_a_parseable_user_agent(fake_pair):
    fake, peer = fake_pair()
    params = {
        "clientInfo": {"name": "bridge", "title": "Bridge", "version": "0.1.0"},
        "capabilities": {"optOutNotificationMethods": ["remoteControl/status/changed"]},
    }
    result = peer.call("initialize", params)["result"]

    assert set(result) == set(CONTRACT["client_requests"]["initialize"]["result"])
    assert result["userAgent"].split("/", 1)[1].split(" ", 1)[0] == "0.151.0"
    assert fake.initialize_params == params


def test_second_initialize_is_already_initialized_minus_32600(fake_pair):
    _fake, peer = fake_pair()
    peer.call("initialize", {"clientInfo": {"name": "bridge"}})
    error = peer.call("initialize", {"clientInfo": {"name": "bridge"}})["error"]
    assert error == {"code": -32600, "message": "Already initialized"}


def test_request_before_initialize_is_not_initialized(fake_pair):
    _fake, peer = fake_pair()
    error = peer.call("thread/loaded/list", {})["error"]
    assert error == {"code": -32600, "message": "Not initialized"}


# --- threads ---------------------------------------------------------------


def test_initialized_broadcasts_thread_started_and_an_idle_status(fake_pair):
    fake, peer = fake_pair()
    _initialize(peer)
    messages = peer.drain()

    started = next(m for m in messages if m["method"] == "thread/started")
    assert started["params"]["thread"]["id"] == fake.thread_id
    assert started["params"]["thread"]["cwd"] == fake.cwd
    assert started["params"]["thread"]["status"] == {"type": "idle"}
    for field in CONTRACT["types"]["Thread.required_subset"]:
        assert field in started["params"]["thread"]

    status = next(m for m in messages if m["method"] == "thread/status/changed")
    assert status["params"] == {"threadId": fake.thread_id, "status": {"type": "idle"}}


def test_thread_loaded_list_and_thread_read_expose_cwd_and_status(fake_pair):
    fake, peer = fake_pair()
    _initialize(peer)

    listed = peer.call("thread/loaded/list", {})["result"]
    assert listed == {"data": [fake.thread_id], "nextCursor": None}

    thread = peer.call("thread/read", {"threadId": fake.thread_id, "includeTurns": False})[
        "result"
    ]["thread"]
    assert thread["cwd"] == fake.cwd
    assert thread["status"] == {"type": "idle"}
    assert thread["cliVersion"] == "0.151.0"


def test_thread_read_with_include_turns_is_known_broken(fake_pair):
    fake, peer = fake_pair()
    _initialize(peer)
    error = peer.call("thread/read", {"threadId": fake.thread_id, "includeTurns": True})["error"]
    assert error == {"code": -32601, "message": "list_turns is not supported yet"}
    assert "list_turns is not supported yet" in CONTRACT["known_broken_0_151_0"]["thread/read"]


def test_thread_resume_ok_subscribes_and_unsubscribe_reports_it(fake_pair):
    fake, peer = fake_pair(resume_result="ok")
    _initialize(peer)

    unsubscribed = peer.call("thread/unsubscribe", {"threadId": fake.thread_id})["result"]
    assert unsubscribed == {"status": "notSubscribed"}

    resumed = peer.call("thread/resume", {"threadId": fake.thread_id, "excludeTurns": True})
    assert resumed["result"]["thread"]["id"] == fake.thread_id
    assert fake.subscribed == {fake.thread_id}

    assert peer.call("thread/unsubscribe", {"threadId": fake.thread_id})["result"] == {
        "status": "unsubscribed"
    }
    assert peer.call("thread/unsubscribe", {"threadId": fake.thread_id})["result"] == {
        "status": "notSubscribed"
    }


def test_thread_resume_unsupported_returns_32601_and_does_not_subscribe(fake_pair):
    fake, peer = fake_pair(resume_result="unsupported")
    _initialize(peer)

    error = peer.call("thread/resume", {"threadId": fake.thread_id, "excludeTurns": True})["error"]
    assert error == {"code": -32601, "message": "list_turns is not supported yet"}
    assert fake.subscribed == set()
    assert peer.call("thread/unsubscribe", {"threadId": fake.thread_id})["result"] == {
        "status": "notSubscribed"
    }


def test_thread_resume_no_rollout_returns_32600(fake_pair):
    fake, peer = fake_pair(resume_result="no_rollout")
    _initialize(peer)

    error = peer.call("thread/resume", {"threadId": fake.thread_id, "excludeTurns": True})["error"]
    assert error == {
        "code": -32600,
        "message": f"no rollout found for thread id {fake.thread_id}",
    }
    assert fake.subscribed == set()


def test_start_thread_broadcasts_a_second_thread_started(fake_pair):
    fake, peer = fake_pair()
    _initialize(peer)
    peer.drain()

    fake.start_thread("thread-tui", "/tmp/peer-b")
    messages = peer.drain()

    started = [m for m in messages if m["method"] == "thread/started"]
    assert len(started) == 1
    assert started[0]["params"]["thread"]["id"] == "thread-tui"
    assert started[0]["params"]["thread"]["cwd"] == "/tmp/peer-b"
    assert peer.call("thread/loaded/list", {})["result"]["data"] == [fake.thread_id, "thread-tui"]


# --- turns -----------------------------------------------------------------


def test_turn_and_item_notifications_reach_only_a_subscribed_peer(fake_pair):
    fake, peer = fake_pair(resume_result="unsupported", agent_message="hello there")
    _initialize(peer)
    peer.drain()
    peer.call("thread/resume", {"threadId": fake.thread_id, "excludeTurns": True})
    peer.call(
        "turn/start",
        {"threadId": fake.thread_id, "input": [{"type": "text", "text": "hi"}]},
    )
    unsubscribed_methods = [m["method"] for m in peer.drain() + peer.seen if "method" in m]
    assert unsubscribed_methods == ["thread/status/changed", "thread/status/changed"]

    fake2, peer2 = fake_pair(resume_result="ok", agent_message="hello there")
    _initialize(peer2)
    peer2.drain()
    peer2.call("thread/resume", {"threadId": fake2.thread_id, "excludeTurns": True})
    peer2.call(
        "turn/start",
        {"threadId": fake2.thread_id, "input": [{"type": "text", "text": "hi"}]},
    )
    messages = peer2.seen + peer2.drain()
    methods = [m["method"] for m in messages if "method" in m]
    assert methods == [
        "thread/status/changed",
        "turn/started",
        "item/started",
        "item/started",
        "item/agentMessage/delta",
        "item/agentMessage/delta",
        "item/completed",
        "turn/completed",
        "thread/status/changed",
    ]

    by_method = {}
    for message in messages:
        by_method.setdefault(message["method"], []).append(message["params"])

    turn_id = by_method["turn/started"][0]["turn"]["id"]
    assert by_method["turn/started"][0]["turn"]["status"] == "inProgress"
    assert by_method["turn/started"][0]["threadId"] == fake2.thread_id

    user_item, agent_item = by_method["item/started"]
    assert user_item["item"]["type"] == "userMessage"
    assert user_item["item"]["content"] == [{"type": "text", "text": "hi"}]
    assert user_item["turnId"] == turn_id
    assert isinstance(user_item["startedAtMs"], int)
    assert agent_item["item"]["type"] == "agentMessage"

    deltas = [p["delta"] for p in by_method["item/agentMessage/delta"]]
    assert "".join(deltas) == "hello there"
    assert by_method["item/agentMessage/delta"][0]["itemId"] == agent_item["item"]["id"]

    completed = by_method["item/completed"][0]
    assert completed["item"] == {
        "type": "agentMessage",
        "id": agent_item["item"]["id"],
        "text": "hello there",
        "phase": "final",
        "delivery": None,
        "memoryCitation": None,
    }
    assert isinstance(completed["completedAtMs"], int)

    assert by_method["turn/completed"][0]["turn"]["id"] == turn_id
    assert by_method["turn/completed"][0]["turn"]["status"] == "completed"
    assert by_method["thread/status/changed"][0]["status"] == {"type": "active", "activeFlags": []}
    assert by_method["thread/status/changed"][-1]["status"] == {"type": "idle"}


def test_turn_start_result_is_a_turn_object_with_an_id(fake_pair):
    fake, peer = fake_pair(resume_result="ok", auto_complete=False)
    _initialize(peer)
    peer.drain()
    peer.call("thread/resume", {"threadId": fake.thread_id, "excludeTurns": True})

    params = {"threadId": fake.thread_id, "input": [{"type": "text", "text": "hi"}]}
    result = peer.call("turn/start", params)["result"]

    assert set(result) == set(CONTRACT["client_requests"]["turn/start"]["result"])
    assert result["turn"] == {
        "id": "turn-1",
        "items": [],
        "itemsView": "notLoaded",
        "status": "inProgress",
        "error": None,
        "startedAt": None,
        "completedAt": None,
        "durationMs": None,
    }
    assert fake.turns == [params]


# --- the two traps ---------------------------------------------------------


def test_an_unmasked_client_frame_closes_the_connection(fake_pair):
    _fake, peer = fake_pair()
    ws.send_frame(peer.sock, ws.OP_TEXT, b'{"id":1,"method":"initialize"}', mask=False)
    with pytest.raises(EOFError):
        peer.recv()


def test_two_json_objects_in_one_frame_close_the_connection(fake_pair):
    fake, peer = fake_pair()
    payload = (
        b'{"id":1,"method":"initialize","params":{}}\n{"id":2,"method":"initialize","params":{}}'
    )
    ws.send_text(peer.sock, payload, mask=True)
    with pytest.raises(EOFError):
        peer.recv()
    assert fake.parse_errors == [payload]


# --- the fake never invents wire names -------------------------------------


def test_fake_only_speaks_methods_and_notifications_that_are_in_the_fixture(fake_pair):
    fake, _peer_obj = fake_pair()
    source = inspect.getsource(fake_module)
    emitted = {
        getattr(fake_module, name)
        for name in re.findall(r"_notify(?:_subscribed)?\(\s*([A-Z][A-Z0-9_]*)", source)
    }
    assert emitted, "no notification constants found in the fake"

    allowed_requests = set(CONTRACT["client_requests"]) | set(CONTRACT["forbidden_methods"])
    assert set(fake.rpc._methods) <= allowed_requests
    assert set(fake.rpc._notifications) <= set(CONTRACT["client_notifications"])
    assert emitted <= set(CONTRACT["server_notifications"])
    assert set(fake_module._FORBIDDEN) == set(CONTRACT["forbidden_methods"])
