"""Task 7 verify (delivery): busy ordering, queue cap + overflow, statuses,
reconnect dedup, text-has-no-reply semantics.
"""

from __future__ import annotations

import time

import pytest

from bridge.router import RouterConfig, RouterError
from bridge.store import MSG_DELIVERED, MSG_PROCESSED

from .fakes.router_peer import RunningRouter


def _connect(router_core, sid, family="claude", state="idle"):
    router_core.notifier.connected_ids.add(sid)
    router_core.store.upsert_session(sid, family, state=state, reachable=True)


def test_busy_holds_then_idle_delivers_in_fifo_order(router_core):
    r = router_core
    _connect(r, "b", state="working")
    r.dispatch("text", {"caller": "a", "to": "b", "message": "m1"})
    r.dispatch("text", {"caller": "c", "to": "b", "message": "m2"})
    assert r.notifier.delivered == []  # working: held

    r.dispatch("update_state", {"session_id": "b", "state": "idle"})
    delivered = [e for _sid, e in r.notifier.delivered]
    assert [e["message"] for e in delivered] == ["m1", "m2"]  # FIFO


def test_text_unreachable_when_no_adapter(router_core):
    r = router_core
    r.store.upsert_session("b", "claude", state="idle", reachable=False)
    _outcome, result = r.dispatch("text", {"caller": "a", "to": "b", "message": "hi"})
    assert result["status"] == "unreachable"


def test_text_status_delivered_when_idle(router_core):
    r = router_core
    _connect(r, "b")
    _outcome, result = r.dispatch("text", {"caller": "a", "to": "b", "message": "hi"})
    assert result["status"] == "delivered"


def test_queue_cap_and_overflow(router_core):
    r = router_core
    r.config = RouterConfig(rate_cap=1000)  # isolate the queue cap
    _connect(r, "b", state="working")  # busy so nothing drains
    for i in range(r.config.queue_cap):
        r.dispatch("text", {"caller": f"s{i}", "to": "b", "message": "x"})
    assert r.store.queue_depth("b") == r.config.queue_cap
    with pytest.raises(RouterError) as exc:
        r.dispatch("text", {"caller": "s999", "to": "b", "message": "x"})
    assert exc.value.code == "queue_full"


def test_call_occupies_target_until_reply(router_core):
    r = router_core
    _connect(r, "b")
    outcome, _ = r.dispatch("call", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    assert outcome == "defer"
    assert r.notifier.delivered[0][1]["kind"] == "call"

    # a text sent while the call is active is held
    r.dispatch("text", {"caller": "c", "to": "b", "message": "later"})
    kinds = [e["kind"] for _s, e in r.notifier.delivered]
    assert "text" not in kinds

    # answer the call -> caller resolved and the held text now flows
    call_id = r.notifier.delivered[0][1]["call_id"]
    r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "done"})
    assert r.notifier.resolved and r.notifier.resolved[0][1]["answer"] == "done"
    kinds = [e["kind"] for _s, e in r.notifier.delivered]
    assert "text" in kinds


def test_delivery_status_transitions(router_core):
    r = router_core
    _connect(r, "b")
    _o, result = r.dispatch("text", {"caller": "a", "to": "b", "message": "hi"})
    mid = result["message_id"]
    assert r.store.get_queued(mid).status == MSG_DELIVERED
    r.dispatch("ack", {"caller": "b", "message_id": mid})
    assert r.store.get_queued(mid).status == MSG_PROCESSED


def test_reconnect_redelivers_same_message_id_then_ack_stops(paths):
    with RunningRouter(paths) as rr:
        adapter = rr.adapter("b")
        adapter.client.call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        adapter.subscribe()

        sender = rr.client(session_id="a")
        sender.call("text", {"to": "b", "message": "hi"})
        assert adapter.wait_for_count(1)
        mid = adapter.events[0]["message_id"]

        # disconnect WITHOUT acking
        adapter.client.close()
        time.sleep(0.2)

        adapter2 = rr.adapter("b")
        adapter2.client.call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        adapter2.subscribe()
        assert adapter2.wait_for_count(1)
        assert adapter2.events[0]["message_id"] == mid  # redelivered, dedupe by id

        # ack now; a third connection must not see it again
        adapter2.client.call("ack", {"message_id": mid})
        time.sleep(0.1)
        adapter3 = rr.adapter("b")
        adapter3.client.call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        adapter3.subscribe()
        assert not adapter3.wait_for_count(1, timeout=0.5)


def test_text_has_no_reply_semantics(router_core):
    r = router_core
    _connect(r, "b")
    _o, result = r.dispatch("text", {"caller": "a", "to": "b", "message": "fyi"})
    # a text creates no call record
    assert result["message_id"]
    assert r.store.get_call(result["message_id"]) is None
