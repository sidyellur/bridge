"""Per-target FIFO delivery.

One logical FIFO per target session carries texts, inbound calls, and async
call results. Delivery happens only when the target's adapter is connected and
the session is idle (busy-safe: an inbound event never steers an unrelated
active turn). Stable message ids plus explicit acknowledgements make reconnect
recovery duplicate-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import guardrails
from .envelopes import call_event, preview, text_event
from .store import (
    CALL_DELIVERED,
    KIND_CALL,
    KIND_TEXT,
    MSG_DELIVERED,
    MSG_PROCESSED,
    MSG_UNREACHABLE,
    STATE_IDLE,
    STATE_WAITING,
)

if TYPE_CHECKING:
    from .router import Router

_DELIVERABLE_STATES = {STATE_IDLE, STATE_WAITING}


def pump_target(router: Router, target_id: str) -> None:
    """Deliver as many queued events as the target can safely accept now."""
    session = router.store.get_session(target_id)
    if session is None or not guardrails.target_connected(router, target_id):
        return
    if session.state not in _DELIVERABLE_STATES:
        return

    while True:
        item = router.store.next_queued(target_id)
        if item is None:
            return

        if item.kind == KIND_CALL:
            # One active inbound call at a time.
            if router.store.active_inbound_call(target_id) is not None:
                return
            event = call_event(
                item.body["call_id"], item.body["from_preview"], item.body["question"]
            )
            if not router.notifier.deliver(target_id, event):
                return
            router.store.set_queue_status(item.message_id, MSG_DELIVERED)
            router.store.set_call_status(
                item.body["call_id"], CALL_DELIVERED, target_state_on_delivery=session.state
            )
            router.store.record_event(
                KIND_CALL,
                "delivered",
                from_id=item.body.get("from_id"),
                to_id=target_id,
                gist=_gist(item.body.get("question", "")),
                call_id=item.body["call_id"],
            )
            return  # occupy the target until reply/timeout

        # text / call_result: informational, no reply, keep draining.
        event = dict(item.body["event"])
        if not router.notifier.deliver(target_id, event):
            return
        router.store.set_queue_status(item.message_id, MSG_DELIVERED)
        router.store.record_event(
            item.kind,
            "delivered",
            from_id=item.body.get("from_id"),
            to_id=target_id,
            gist=_gist(item.body.get("gist", "")),
            call_id=item.body.get("call_id"),
        )


def do_text(router: Router, args: dict[str, Any]) -> dict[str, Any]:
    from_id = _require(args, "caller")
    to_id = _require(args, "to")
    message = str(args.get("message", ""))[:_MAX_BODY]

    guardrails.outbound_precheck(router, from_id, to_id)
    if not guardrails.target_connected(router, to_id):
        router.store.record_event(
            KIND_TEXT, MSG_UNREACHABLE, from_id=from_id, to_id=to_id, gist=_gist(message)
        )
        return {"message_id": "", "status": MSG_UNREACHABLE, "note": "target is not reachable"}

    message_id = router._idgen()
    src = router.store.get_session(from_id)
    from_preview = preview(from_id, src.last_user_message if src else "")
    event = text_event(message_id, from_preview, message)
    body = {"event": event, "from_id": from_id, "gist": message}
    router.store.create_message(message_id, to_id, KIND_TEXT, body, from_id=from_id)
    router.store.enqueue(to_id, message_id)
    router.store.record_rate(from_id, to_id)
    router.store.record_event(
        KIND_TEXT,
        "queued",
        from_id=from_id,
        to_id=to_id,
        gist=_gist(message),
        body={"message": message},
    )
    pump_target(router, to_id)

    item = router.store.get_queued(message_id)
    status = MSG_DELIVERED if (item and item.status == MSG_DELIVERED) else "queued"
    return {"message_id": message_id, "status": status, "note": ""}


def do_ack(router: Router, args: dict[str, Any]) -> dict[str, Any]:
    """An adapter acknowledges that a delivered event was absorbed."""
    message_id = _require(args, "message_id")
    item = router.store.get_queued(message_id)
    if item is None:
        return {"ok": False, "note": "unknown message id"}
    router.store.set_queue_status(message_id, MSG_PROCESSED)
    router.store.record_event(item.kind, MSG_PROCESSED, to_id=item.target_id, call_id=item.call_id)
    return {"ok": True}


_MAX_BODY = 8000


def _gist(text: str) -> str:
    text = text.strip().replace("\n", " ")
    return text[:120]


def _require(args: dict[str, Any], key: str) -> Any:
    from .router import RouterError

    if key not in args or args[key] in (None, ""):
        raise RouterError("bad_request", f"missing required field {key!r}")
    return args[key]


def _register() -> None:
    from .router import register_simple_op

    register_simple_op("text", do_text)
    register_simple_op("ack", do_ack)


_register()
