"""Live call state machine, reply validation, and expiry.

A call is delivered to the exact addressed live session and answered by that
session via ``reply``. Synchronous callers block on a deferred router response;
asynchronous callers get a pushed ``call_result`` event. No path spawns a
substitute agent, resumes a session, or fabricates an answer from transcript
artifacts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import guardrails
from .envelopes import preview
from .router import require_field
from .store import (
    CALL_ANSWERED,
    CALL_ANSWERING,
    CALL_BLOCKED,
    CALL_DELIVERED,
    CALL_QUEUED,
    CALL_TIMEOUT,
    CALL_UNREACHABLE,
    KIND_CALL,
)
from .transcript import gist

if TYPE_CHECKING:
    from .router import Router

_MAX_BODY = 8000


def do_call(router: Router, args: dict[str, Any], waiter: Any) -> tuple[str, Any]:
    from_id = require_field(args, "caller")
    # Aliases resolve before any guardrail runs, so every check, transcript row,
    # and rate counter downstream sees the real session id.
    to_id = router.resolve_target(require_field(args, "to"))
    question = str(require_field(args, "question"))[:_MAX_BODY]
    timeout_s = min(
        int(args.get("timeout_s", router.config.timeout_cap_s)), router.config.timeout_cap_s
    )

    guardrails.outbound_precheck(router, from_id, to_id)
    call_id = router._idgen()
    now = router._now()
    deadline = now + timeout_s

    if not guardrails.target_connected(router, to_id):
        router.store.create_call(
            call_id, from_id, to_id, question, deadline, kind="call", status=CALL_UNREACHABLE
        )
        router.store.record_event(
            KIND_CALL,
            CALL_UNREACHABLE,
            from_id=from_id,
            to_id=to_id,
            gist=gist(question),
            call_id=call_id,
        )
        return ("respond", _result(router, call_id, CALL_UNREACHABLE, "", [], now, None))

    _enqueue_call(router, call_id, from_id, to_id, question, deadline, kind="call")
    # Synchronous: defer the response until reply or timeout.
    router.pending_sync[call_id] = (waiter, deadline)
    return ("defer", None)


def do_call_async(router: Router, args: dict[str, Any], waiter: Any) -> tuple[str, Any]:
    from_id = require_field(args, "caller")
    to_id = router.resolve_target(require_field(args, "to"))
    question = str(require_field(args, "question"))[:_MAX_BODY]
    now = router._now()
    deadline = now + router.config.timeout_cap_s

    guardrails.outbound_precheck(router, from_id, to_id)
    call_id = router._idgen()

    if not guardrails.target_connected(router, to_id):
        router.store.create_call(
            call_id, from_id, to_id, question, deadline, kind="call_async", status=CALL_UNREACHABLE
        )
        router.store.record_event(
            KIND_CALL,
            CALL_UNREACHABLE,
            from_id=from_id,
            to_id=to_id,
            gist=gist(question),
            call_id=call_id,
        )
        return (
            "respond",
            {"call_id": call_id, "status": CALL_UNREACHABLE, "delivery": "target not reachable"},
        )

    _enqueue_call(router, call_id, from_id, to_id, question, deadline, kind="call_async")
    return (
        "respond",
        {
            "call_id": call_id,
            "status": CALL_QUEUED,
            "delivery": "queued for the target's next idle turn",
        },
    )


def do_reply(router: Router, args: dict[str, Any], waiter: Any) -> tuple[str, Any]:
    from .router import RouterError

    caller = require_field(args, "caller")
    call_id = require_field(args, "call_id")
    answer = str(args.get("answer", ""))[:_MAX_BODY]
    blocked = [str(b) for b in (args.get("blocked") or [])]
    via = str(args.get("via", "tool"))

    call = router.store.get_call(call_id)
    if call is None:
        raise RouterError("unknown_call", f"no call {call_id!r}")
    if call.to_id != caller:
        raise RouterError("foreign_call", "reply must come from the session that received the call")
    if call.status in (CALL_ANSWERED, CALL_BLOCKED):
        raise RouterError("already_answered", "this call was already answered")
    if call.status in (CALL_TIMEOUT, CALL_UNREACHABLE) or router._now() > call.deadline:
        router.store.record_event(
            KIND_CALL, "late_reply_rejected", from_id=caller, to_id=call.from_id, call_id=call_id
        )
        raise RouterError("expired", "call already timed out; late reply rejected")
    if call.status not in (CALL_DELIVERED, CALL_ANSWERING):
        raise RouterError("not_active", f"call is {call.status}, cannot be replied to")

    router.store.record_answer(call_id, answer, blocked)
    call = router.store.get_call(call_id)  # refresh with the recorded answer
    status = CALL_BLOCKED if blocked else CALL_ANSWERED
    router.store.record_event(
        KIND_CALL, status, from_id=caller, to_id=call.from_id, gist=gist(answer), call_id=call_id
    )
    result = _result(
        router,
        call_id,
        status,
        answer,
        blocked,
        call.created_at,
        call.target_state_on_delivery,
        via=via,
    )

    if call.kind == "call_async":
        _push_result(router, call.from_id, call, status, via=via)
    else:
        entry = router.pending_sync.pop(call_id, None)
        if entry is not None:
            router.notifier.resolve_sync(entry[0], result)

    router.pump(call.to_id)  # release the next queued item for the target
    return ("respond", {"accepted": True, "note": ""})


def expire_due(router: Router) -> None:
    for call in router.store.due_calls():
        if call.status not in (CALL_QUEUED, CALL_DELIVERED, CALL_ANSWERING):
            continue
        router.store.set_call_status(call.call_id, CALL_TIMEOUT)
        router.store.record_event(
            KIND_CALL, CALL_TIMEOUT, from_id=call.to_id, to_id=call.from_id, call_id=call.call_id
        )
        result = _result(
            router,
            call.call_id,
            CALL_TIMEOUT,
            "",
            [],
            call.created_at,
            call.target_state_on_delivery,
        )
        if call.kind == "call_async":
            _push_result(router, call.from_id, call, CALL_TIMEOUT)
        else:
            entry = router.pending_sync.pop(call.call_id, None)
            if entry is not None:
                router.notifier.resolve_sync(entry[0], result)
        router.pump(call.to_id)


# --- helpers ---------------------------------------------------------------


def _enqueue_call(
    router: Router,
    call_id: str,
    from_id: str,
    to_id: str,
    question: str,
    deadline: float,
    *,
    kind: str,
) -> None:
    router.store.create_call(
        call_id, from_id, to_id, question, deadline, kind=kind, status=CALL_QUEUED
    )
    src = router.store.get_session(from_id)
    from_preview = preview(from_id, src.last_user_message if src else "")
    body = {
        "call_id": call_id,
        "from_id": from_id,
        "from_preview": from_preview,
        "question": question,
    }
    router.store.create_message(call_id, to_id, KIND_CALL, body, from_id=from_id, call_id=call_id)
    router.store.enqueue(to_id, call_id)
    router.store.record_rate(from_id, to_id)
    router.store.record_event(
        KIND_CALL,
        "queued",
        from_id=from_id,
        to_id=to_id,
        gist=gist(question),
        call_id=call_id,
        body={"question": question},
    )
    router.pump(to_id)


def _push_result(router: Router, caller_id: str, call, status: str, *, via: str = "tool") -> None:
    """Wake the async caller's exact live session with a correlated result."""
    from .async_results import push_call_result

    push_call_result(router, caller_id, call, status, via=via)


def _result(
    router: Router,
    call_id: str,
    status: str,
    answer: str,
    blocked: list[str],
    created_at: float,
    target_state: str | None,
    *,
    via: str = "tool",
) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "status": status,
        "answer": answer,
        "blocked": blocked,
        "meta": {
            "answered_by": "live-session",
            "duration_s": round(router._now() - created_at, 3),
            "target_state_on_delivery": target_state,
            "via": via,
        },
    }


def _register() -> None:
    from .router import register_op

    register_op("call", do_call)
    register_op("call_async", do_call_async)
    register_op("reply", do_reply)


_register()
