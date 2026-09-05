"""Shared Bridge tool surface: schemas, descriptions, and dispatch.

Both the Claude Channel adapter and the Codex MCP server expose the identical
public tools (``roster``, ``call``, ``call_async``, ``text``, ``transcript``)
plus the protocol-facing ``reply``. Source identity is always taken from the
adapter's inherited ``BRIDGE_SESSION_ID`` and never from tool arguments.
"""

from __future__ import annotations

from typing import Any

# Every public tool description carries this so a model does not misuse Bridge
# as a retrieval tool.
ANTI_RETRIEVAL = (
    "Use Bridge to coordinate, never to retrieve. Never call for information discoverable on disk."
)

_ONE_QUESTION = "Ask exactly one question per call."

PUBLIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "roster",
        "description": ("List the live agent sessions you can reach. " + ANTI_RETRIEVAL),
        "inputSchema": {
            "type": "object",
            "properties": {"include_unmanaged": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "call",
        "description": (
            "Ask one addressed live session a question and wait for its answer. "
            + _ONE_QUESTION
            + " "
            + ANTI_RETRIEVAL
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "target Bridge session id"},
                "question": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 60, "maximum": 60},
            },
            "required": ["to", "question"],
        },
    },
    {
        "name": "call_async",
        "description": (
            "Ask one addressed live session a question; the answer is pushed back "
            "to you later. " + _ONE_QUESTION + " " + ANTI_RETRIEVAL
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["to", "question"],
        },
    },
    {
        "name": "text",
        "description": (
            "Send an informational message to a live session. No reply is expected. "
            + ANTI_RETRIEVAL
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "message": {"type": "string"}},
            "required": ["to", "message"],
        },
    },
    {
        "name": "transcript",
        "description": ("Show recent Bridge coordination activity. " + ANTI_RETRIEVAL),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {"type": ["string", "null"], "default": None},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
]

REPLY_TOOL: dict[str, Any] = {
    "name": "reply",
    "description": (
        "Answer the inbound Bridge call you are currently handling. Only valid "
        "while handling a call; provide the answer and any blocked items."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "call_id": {"type": "string"},
            "answer": {"type": "string"},
            "blocked": {"type": "array", "items": {"type": "string"}, "default": []},
        },
        "required": ["call_id", "answer"],
    },
}


def all_tools() -> list[dict[str, Any]]:
    return [*PUBLIC_TOOLS, REPLY_TOOL]


def tool_names() -> list[str]:
    return [t["name"] for t in all_tools()]


# --- dispatch --------------------------------------------------------------

_MAX_BODY = 8000


def dispatch_tool(client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Translate one MCP tool call into a router op using ``client``.

    ``client`` is a connected :class:`~bridge.router_client.RouterClient` whose
    authenticated connection already carries this session's id, so ``from`` is
    never taken from ``arguments``.
    """
    args = dict(arguments or {})
    if name == "roster":
        return client.call(
            "roster", {"include_unmanaged": bool(args.get("include_unmanaged", False))}
        )
    if name == "call":
        return client.call(
            "call",
            {
                "to": args.get("to"),
                "question": _clip(args.get("question", "")),
                "timeout_s": int(args.get("timeout_s", 60)),
            },
        )
    if name == "call_async":
        return client.call(
            "call_async", {"to": args.get("to"), "question": _clip(args.get("question", ""))}
        )
    if name == "text":
        return client.call(
            "text", {"to": args.get("to"), "message": _clip(args.get("message", ""))}
        )
    if name == "transcript":
        return client.call(
            "transcript", {"peer": args.get("peer"), "limit": int(args.get("limit", 20))}
        )
    if name == "reply":
        return client.call(
            "reply",
            {
                "call_id": args.get("call_id"),
                "answer": _clip(args.get("answer", "")),
                "blocked": list(args.get("blocked") or []),
            },
        )
    raise KeyError(name)


def _clip(text: Any) -> str:
    return str(text)[:_MAX_BODY]
