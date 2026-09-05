"""Task 9 verify (MCP server): six tools, schemas, anti-trigger sentence,
caller identity from the connection (not arguments), and router errors surfaced
as tool errors.
"""

from __future__ import annotations

import socket

from bridge.server import BridgeMCPServer
from bridge.tools import ANTI_RETRIEVAL, tool_names

from .fakes.claude_host import FakeClaudeHost
from .fakes.router_peer import RunningRouter


def _make_server(rr, session_id):
    client_sock, server_sock = socket.socketpair()
    router = rr.client(session_id=session_id, role="client")
    server = BridgeMCPServer(session_id, server_sock, router).start()
    host = FakeClaudeHost(client_sock)  # a generic MCP client
    return server, host


def test_initialize_and_instructions(paths):
    with RunningRouter(paths) as rr:
        server, host = _make_server(rr, "codex-1")
        result = host.initialize()
        assert result["capabilities"]["tools"] == {}
        assert ANTI_RETRIEVAL in result["instructions"]
        host.close()
        server.close()


def test_tools_list_reports_six_tools(paths):
    with RunningRouter(paths) as rr:
        server, host = _make_server(rr, "codex-1")
        host.initialize()
        tools = host.list_tools()["tools"]
        assert {t["name"] for t in tools} == set(tool_names())
        assert len(tools) == 6
        host.close()
        server.close()


def test_public_tool_schemas_and_anti_retrieval(paths):
    with RunningRouter(paths) as rr:
        server, host = _make_server(rr, "codex-1")
        host.initialize()
        tools = {t["name"]: t for t in host.list_tools()["tools"]}
        assert tools["call"]["inputSchema"]["required"] == ["to", "question"]
        assert tools["call"]["inputSchema"]["properties"]["timeout_s"]["maximum"] == 60
        for name in ("roster", "call", "call_async", "text", "transcript"):
            assert ANTI_RETRIEVAL in tools[name]["description"]
        host.close()
        server.close()


def test_caller_identity_from_connection_not_arguments(paths):
    with RunningRouter(paths) as rr:
        server, host = _make_server(rr, "codex-1")
        host.initialize()
        # target managed but not connected -> text is unreachable but still
        # records the source, which must be the connection's session id.
        rr.client(session_id="ctrl").call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        host.call_tool("text", {"to": "b", "message": "hi", "from": "evil", "caller": "evil"})
        tx = rr.client().call("transcript", {"limit": 20})
        text_entries = [e for e in tx["entries"] if e["kind"] == "text"]
        assert text_entries and all(e["from"] == "codex-1" for e in text_entries)
        host.close()
        server.close()


def test_router_error_surfaces_as_tool_error(paths):
    with RunningRouter(paths) as rr:
        server, host = _make_server(rr, "codex-1")
        host.initialize()
        out = host.call_tool("call", {"to": "codex-1", "question": "self?"})  # self-call
        assert out["isError"] is True
        assert out["structuredContent"]["error"] == "self_call"
        host.close()
        server.close()


def test_disconnected_router_surfaces_error(paths):
    with RunningRouter(paths) as rr:
        server, host = _make_server(rr, "codex-1")
        host.initialize()
        server.router.close()  # sever the router connection
        out = host.call_tool("roster", {})
        assert out["isError"] is True
        assert out["structuredContent"]["error"] in ("disconnected", "timeout")
        host.close()
        server.close()
