"""Task 9 verify (async results): the exact caller is woken; busy caller waits;
no duplicate result; expired async call still notifies; caller reconnect;
daemon restart preserves the queued result.
"""

from __future__ import annotations

import time

from bridge.store import KIND_CALL_RESULT

from .fakes.router_peer import RunningRouter


def _connect(r, sid, family="claude", state="idle"):
    r.notifier.connected_ids.add(sid)
    r.store.upsert_session(sid, family, state=state, reachable=True)


def test_result_wakes_exact_caller(router_core):
    r = router_core
    _connect(r, "a")
    _connect(r, "b")
    _o, result = r.dispatch("call_async", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    call_id = result["call_id"]
    r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "ans"})
    pushed = [e for sid, e in r.notifier.delivered if sid == "a" and e["kind"] == KIND_CALL_RESULT]
    assert pushed[0]["answer"] == "ans"
    assert pushed[0]["call_id"] == call_id


def test_busy_caller_waits_then_receives(router_core):
    r = router_core
    _connect(r, "a", state="working")  # caller busy
    _connect(r, "b")
    _o, result = r.dispatch("call_async", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    r.dispatch("reply", {"caller": "b", "call_id": result["call_id"], "answer": "ans"})
    # nothing delivered to busy caller yet
    assert not [e for sid, e in r.notifier.delivered if sid == "a"]
    # caller goes idle -> result flows
    r.dispatch("update_state", {"session_id": "a", "state": "idle"})
    pushed = [e for sid, e in r.notifier.delivered if sid == "a" and e["kind"] == KIND_CALL_RESULT]
    assert pushed and pushed[0]["answer"] == "ans"


def test_no_duplicate_result_on_second_reply(router_core):
    r = router_core
    _connect(r, "a")
    _connect(r, "b")
    _o, result = r.dispatch("call_async", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    call_id = result["call_id"]
    r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "one"})
    try:
        r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "two"})
    except Exception:
        pass
    pushed = [e for sid, e in r.notifier.delivered if sid == "a" and e["kind"] == KIND_CALL_RESULT]
    assert len(pushed) == 1


def test_expired_async_call_still_notifies(router_core, clock):
    r = router_core
    _connect(r, "a")
    _connect(r, "b")
    _o, result = r.dispatch("call_async", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    clock.advance(61)
    r.tick()
    pushed = [e for sid, e in r.notifier.delivered if sid == "a" and e["kind"] == KIND_CALL_RESULT]
    assert pushed and pushed[0]["status"] == "timeout"


def test_result_waits_for_disconnected_caller(router_core):
    r = router_core
    # caller registered but not connected
    r.store.upsert_session("a", "claude", state="idle", reachable=False)
    _connect(r, "b")
    _o, result = r.dispatch("call_async", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    r.dispatch("reply", {"caller": "b", "call_id": result["call_id"], "answer": "ans"})
    assert not [e for sid, e in r.notifier.delivered if sid == "a"]  # queued, not delivered
    # caller connects -> pump delivers
    _connect(r, "a")
    r.pump("a")
    pushed = [e for sid, e in r.notifier.delivered if sid == "a" and e["kind"] == KIND_CALL_RESULT]
    assert pushed and pushed[0]["answer"] == "ans"


def test_daemon_restart_preserves_queued_result(paths):
    # First router: async call answered while caller is offline; result queued.
    with RunningRouter(paths) as rr:
        target = rr.adapter("b")
        target.client.call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        target.subscribe()
        # register caller but do not subscribe (offline transport)
        rr.client(session_id="a").call(
            "register_session", {"session_id": "a", "family": "codex", "state": "idle"}
        )
        caller_tmp = rr.client(session_id="a")
        res = caller_tmp.call("call_async", {"to": "b", "question": "q"})
        call_id = res["call_id"]
        assert target.wait_for_count(1)
        # reply from the target via a client authenticated as b
        target.client.call("reply", {"call_id": call_id, "answer": "persisted"})
        time.sleep(0.1)

    # Second router on the same paths: caller reconnects and receives the result.
    with RunningRouter(paths) as rr2:
        caller = rr2.adapter("a")
        caller.client.call(
            "register_session", {"session_id": "a", "family": "codex", "state": "idle"}
        )
        caller.subscribe()
        assert caller.wait_for_count(1)
        assert caller.events[0]["kind"] == KIND_CALL_RESULT
        assert caller.events[0]["answer"] == "persisted"
