"""Daemon-enforced guardrails: self-call prohibition, hop budget, rate cap,
queue cap, and reachability. These are checked in the router before an outbound
message is accepted, not merely suggested in prompts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .router import RouterError
from .store import MSG_DELIVERED, MSG_QUEUED

if TYPE_CHECKING:
    from .router import Router


def check_not_self(from_id: str, to_id: str) -> None:
    if from_id == to_id:
        raise RouterError("self_call", "a session cannot call or text itself")


def check_hop_budget(router: Router, from_id: str) -> None:
    """Hop budget = 1: while B is answering an inbound call, B may not dial out."""
    active = router.store.active_inbound_call(from_id)
    if active is not None:
        raise RouterError(
            "hop_budget",
            "you are answering an inbound Bridge call; use reply() and do not dial out",
        )


def check_reachable(router: Router, to_id: str) -> None:
    session = router.store.get_session(to_id)
    if session is None:
        raise RouterError("unreachable", unknown_target_message(to_id))
    if not session.is_managed:
        raise RouterError(
            "unreachable",
            f"{router.describe_target(to_id)} is not Bridge-managed;"
            f" restart it with `bridge {session.family}`",
        )


def unknown_target_message(to_id: str) -> str:
    """Explain an address that resolved to nothing.

    ``Router.resolve_target`` returns an unrecognised name unchanged, so a
    mistyped or never-registered alias lands here; say so, and name it, rather
    than leaving the user staring at a bare id they never typed.
    """
    from .contacts import ALIAS_NAME_RE, looks_like_session_id

    base = f"no session {to_id!r} is known to Bridge"
    if ALIAS_NAME_RE.match(to_id) and not looks_like_session_id(to_id):
        return (
            f"{base}, and {to_id!r} is not a known alias;"
            f" map it with `bridge alias {to_id} <session-id>`"
        )
    return base


def target_connected(router: Router, to_id: str) -> bool:
    session = router.store.get_session(to_id)
    return bool(session and session.reachable and router.notifier.connected(to_id))


def check_rate(router: Router, from_id: str, to_id: str) -> None:
    count = router.store.count_rate(from_id, to_id, router.config.rate_window_s)
    if count >= router.config.rate_cap:
        raise RouterError(
            "rate_capped",
            f"rate cap of {router.config.rate_cap} messages/hour reached for"
            f" {from_id}->{router.describe_target(to_id)}",
        )


def check_queue_capacity(router: Router, to_id: str) -> None:
    depth = router.store.queue_depth(to_id)
    if depth >= router.config.queue_cap:
        raise RouterError(
            "queue_full",
            f"target {router.describe_target(to_id)} has {depth} pending events"
            f" (cap {router.config.queue_cap})",
        )


def check_one_active_inbound(router: Router, to_id: str) -> None:
    if router.store.active_inbound_call(to_id) is not None:
        # An in-flight call occupies the target; further calls still queue, but a
        # second *delivered* call is prevented by the delivery pump. This check is
        # used at delivery time.
        raise RouterError(
            "busy_call",
            f"target {router.describe_target(to_id)} already has an active inbound call",
        )


def outbound_precheck(router: Router, from_id: str, to_id: str) -> None:
    """Common gate for call/call_async/text before enqueue."""
    check_not_self(from_id, to_id)
    check_hop_budget(router, from_id)
    check_reachable(router, to_id)
    check_rate(router, from_id, to_id)
    check_queue_capacity(router, to_id)


__all__ = [
    "MSG_DELIVERED",
    "MSG_QUEUED",
    "unknown_target_message",
    "check_not_self",
    "check_hop_budget",
    "check_reachable",
    "check_rate",
    "check_queue_capacity",
    "check_one_active_inbound",
    "target_connected",
    "outbound_precheck",
]
