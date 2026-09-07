"""Task 2 verify: pluggable JSONL/WebSocket framing on RpcEndpoint."""

from __future__ import annotations

import json
import socket
import threading

from bridge.mcp import Framing, JsonRpcError, RpcEndpoint
from bridge.ws import recv_message, server_handshake


def test_jsonl_request_bytes_are_unchanged():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        thread = threading.Thread(target=endpoint.request, args=("m",), kwargs={"timeout": 5.0})
        thread.start()
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        assert data == b'{"jsonrpc":"2.0","id":1,"method":"m","params":{}}\n'
        peer.sendall(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        thread.join(timeout=5)
    finally:
        endpoint.close()
        peer.close()


def test_jsonl_notify_bytes_are_unchanged():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        endpoint.notify("n", {"a": 1})
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        assert data == b'{"jsonrpc":"2.0","method":"n","params":{"a":1}}\n'
    finally:
        endpoint.close()
        peer.close()


def test_jsonl_round_trip_request_response_and_notification():
    left_sock, right_sock = socket.socketpair()
    try:
        left = RpcEndpoint(left_sock, name="left").start()
        right = RpcEndpoint(right_sock, name="right")
        right.method("ping", lambda params: {"pong": params.get("n")})
        received = []
        right.notification("evt", lambda params: received.append(params))
        right.start()

        result = left.request("ping", {"n": 42}, timeout=5.0)
        assert result == {"pong": 42}

        left.notify("evt", {"x": 1})
        for _ in range(200):
            if received:
                break
            threading.Event().wait(0.01)
        assert received == [{"x": 1}]
    finally:
        left.close()
        right.close()


def test_ws_client_to_ws_server_round_trip():
    client_sock, server_sock = socket.socketpair()
    try:
        server = RpcEndpoint(server_sock, name="server", framing=Framing.WS_SERVER)
        server.method("ping", lambda params: {"pong": params.get("n")})
        received_on_server = []
        server.notification("evt-to-server", lambda params: received_on_server.append(params))
        server.start()

        client = RpcEndpoint(client_sock, name="client", framing=Framing.WS_CLIENT)
        received_on_client = []
        client.notification("evt-to-client", lambda params: received_on_client.append(params))
        client.start()

        assert client.request("ping", {"n": 7}, timeout=5.0) == {"pong": 7}

        client.notify("evt-to-server", {"a": 1})
        server.notify("evt-to-client", {"b": 2})

        for _ in range(200):
            if received_on_server and received_on_client:
                break
            threading.Event().wait(0.01)
        assert received_on_server == [{"a": 1}]
        assert received_on_client == [{"b": 2}]
    finally:
        client.close()
        server.close()


def test_ws_send_is_one_message_per_frame_with_no_newline():
    client_sock, peer = socket.socketpair()
    try:
        thread = threading.Thread(target=server_handshake, args=(peer,))
        thread.start()

        endpoint = RpcEndpoint(client_sock, name="client", framing=Framing.WS_CLIENT).start()
        thread.join(timeout=5)

        endpoint.notify("n", {"a": 1})

        payload = recv_message(peer, require_mask=True)
        assert payload is not None
        assert b"\n" not in payload
        obj = json.loads(payload)
        assert obj == {"jsonrpc": "2.0", "method": "n", "params": {"a": 1}}
    finally:
        endpoint.close()
        peer.close()


def test_ws_server_framing_handshakes_on_the_reader_thread():
    client_sock, server_sock = socket.socketpair()
    try:
        server = RpcEndpoint(server_sock, name="server", framing=Framing.WS_SERVER)
        server.method("ping", lambda params: {"pong": True})
        server.start()

        client = RpcEndpoint(client_sock, name="client", framing=Framing.WS_CLIENT).start()
        assert client.request("ping", timeout=5.0) == {"pong": True}
    finally:
        client.close()
        server.close()


def test_response_without_jsonrpc_is_delivered_to_the_pending_request():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        thread_result = {}

        def run():
            thread_result["value"] = endpoint.request("m", timeout=5.0)

        thread = threading.Thread(target=run)
        thread.start()
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        peer.sendall(b'{"id":1,"result":{"ok":true}}\n')
        thread.join(timeout=5)
        assert thread_result["value"] == {"ok": True}
    finally:
        endpoint.close()
        peer.close()


def test_error_response_without_jsonrpc_raises_jsonrpcerror():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        errors = {}

        def run():
            try:
                endpoint.request("m", timeout=5.0)
            except JsonRpcError as exc:
                errors["exc"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        peer.sendall(b'{"id":1,"error":{"code":-32600,"message":"Not initialized"}}\n')
        thread.join(timeout=5)
        assert errors["exc"].code == -32600
    finally:
        endpoint.close()
        peer.close()


def test_notification_with_extra_envelope_keys_is_dispatched():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        received = []
        endpoint.notification("x", lambda params: received.append(params))
        peer.sendall(b'{"method":"x","params":{},"emittedAtMs":1788754571273}\n')
        for _ in range(200):
            if received:
                break
            threading.Event().wait(0.01)
        assert received == [{}]
    finally:
        endpoint.close()
        peer.close()


def test_include_jsonrpc_false_omits_the_field():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a", include_jsonrpc=False).start()
        endpoint.notify("n", {"a": 1})
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        obj = json.loads(data)
        assert "jsonrpc" not in obj

        thread = threading.Thread(target=endpoint.request, args=("m",), kwargs={"timeout": 5.0})
        thread.start()
        data2 = b""
        while not data2.endswith(b"\n"):
            data2 += peer.recv(4096)
        obj2 = json.loads(data2)
        assert "jsonrpc" not in obj2
        peer.sendall(b'{"id":1,"result":{}}\n')
        thread.join(timeout=5)
    finally:
        endpoint.close()
        peer.close()


def test_notify_extra_merges_envelope_keys():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        endpoint.notify("n", {"a": 1}, extra={"emittedAtMs": 7})
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        obj = json.loads(data)
        assert obj == {"jsonrpc": "2.0", "method": "n", "params": {"a": 1}, "emittedAtMs": 7}
    finally:
        endpoint.close()
        peer.close()


def test_notify_omit_empty_params_sends_no_params_key():
    client, peer = socket.socketpair()
    try:
        endpoint = RpcEndpoint(client, name="a").start()
        endpoint.notify("initialized", omit_empty_params=True)
        data = b""
        while not data.endswith(b"\n"):
            data += peer.recv(4096)
        assert data == b'{"jsonrpc":"2.0","method":"initialized"}\n'
    finally:
        endpoint.close()
        peer.close()


def test_on_parse_error_receives_the_raw_payload_and_default_is_silent():
    client, peer = socket.socketpair()
    try:
        errors = []
        endpoint = RpcEndpoint(
            client, name="a", on_parse_error=lambda payload: errors.append(payload)
        ).start()
        peer.sendall(b"not json\n")
        peer.sendall(b'{"method":"ping","params":{}}\n')
        received = []
        endpoint.notification("ping", lambda params: received.append(params))
        for _ in range(200):
            if received:
                break
            threading.Event().wait(0.01)
        assert errors == [b"not json"]
        assert received == [{}]
    finally:
        endpoint.close()
        peer.close()

    client2, peer2 = socket.socketpair()
    try:
        default_endpoint = RpcEndpoint(client2, name="b").start()
        peer2.sendall(b"not json\n")
        peer2.sendall(b'{"method":"ping","params":{}}\n')
        received2 = []
        default_endpoint.notification("ping", lambda params: received2.append(params))
        for _ in range(200):
            if received2:
                break
            threading.Event().wait(0.01)
        assert received2 == [{}]
    finally:
        default_endpoint.close()
        peer2.close()


def test_ws_peer_close_fails_pending_requests_and_calls_on_close():
    client_sock, server_sock = socket.socketpair()
    try:
        close_calls = []
        server = RpcEndpoint(
            server_sock,
            name="server",
            framing=Framing.WS_SERVER,
            on_close=lambda: close_calls.append(1),
        )
        server.start()

        from bridge import ws

        ws.client_handshake(client_sock)

        errors = {}

        def run():
            try:
                server.request("m", timeout=5.0)
            except JsonRpcError as exc:
                errors["exc"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        client_sock.close()
        thread.join(timeout=5)

        assert errors["exc"].code == -32603
        assert errors["exc"].message == "connection closed"
        for _ in range(200):
            if close_calls:
                break
            threading.Event().wait(0.01)
        assert close_calls == [1]
    finally:
        server.close()
