"""Asynchronous call-result push.

When a ``call_async`` is answered (or times out), the router wakes the *exact*
original caller by enqueuing a ``call_result`` event on the caller's FIFO and
pumping it through that caller's live transport — Claude Channel notification or
Codex App Server turn. There is no on-disk queue file, inbox-polling tool, or
background-shell workaround: both sides have push-capable managed transports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .envelopes import result_event
from .store import KIND_CALL_RESULT

if TYPE_CHECKING:
    from .router import Router
    from .store import Call


def push_call_result(
    router: Router, caller_id: str, call: Call, status: str, *, via: str = "tool"
) -> None:
    """Enqueue a correlated result for ``caller_id`` and pump its FIFO.

    If the caller is currently busy the result waits behind its active turn; if
    the caller is disconnected it stays queued and is redelivered on reconnect.
    A duplicate result for an already-answered call is prevented upstream (the
    router only calls this once per resolution).
    """
    event = result_event(call.call_id, call.question, call.answer, call.blocked)
    event["status"] = status
    event["via"] = via
    message_id = router._idgen()
    body = {
        "event": event,
        "from_id": call.to_id,
        "gist": f"result:{status}",
        "call_id": call.call_id,
    }
    router.store.create_message(
        message_id, caller_id, KIND_CALL_RESULT, body, from_id=call.to_id, call_id=call.call_id
    )
    router.store.enqueue(caller_id, message_id)
    router.store.record_event(
        KIND_CALL_RESULT, "queued", from_id=call.to_id, to_id=caller_id, call_id=call.call_id
    )
    router.pump(caller_id)
