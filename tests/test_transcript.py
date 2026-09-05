"""Task 7 verify (transcript): an event per state transition, redaction, peer
filtering, and rendering.
"""

from __future__ import annotations

from bridge.transcript import gist, render_transcript


def _connect(router_core, sid, family="claude", state="idle"):
    router_core.notifier.connected_ids.add(sid)
    router_core.store.upsert_session(sid, family, state=state, reachable=True)


def test_gist_truncates_and_flattens():
    assert gist("a\nb\n" + "x" * 200).startswith("a b ")
    assert len(gist("y" * 500)) == 120


def test_text_records_queued_and_delivered_and_processed(router_core):
    r = router_core
    _connect(r, "b")
    _o, result = r.dispatch("text", {"caller": "a", "to": "b", "message": "hi"})
    r.dispatch("ack", {"caller": "b", "message_id": result["message_id"]})
    statuses = {e.status for e in r.store.recent(limit=50) if e.kind == "text"}
    assert {"queued", "delivered", "processed"} <= statuses


def test_call_records_queued_delivered_answered(router_core):
    r = router_core
    _connect(r, "b")
    r.dispatch("call", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    call_id = r.notifier.delivered[0][1]["call_id"]
    r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "ok"})
    statuses = {e.status for e in r.store.recent(limit=50) if e.kind == "call"}
    assert {"queued", "delivered", "answered"} <= statuses


def test_disconnect_recorded(paths):
    from .fakes.router_peer import RunningRouter

    with RunningRouter(paths) as rr:
        adapter = rr.adapter("b")
        adapter.client.call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        adapter.subscribe()
        adapter.client.close()
        import time

        time.sleep(0.2)
        ctrl = rr.client(session_id="ctrl")
        tx = ctrl.call("transcript", {"limit": 50})
        assert any(e["status"] == "disconnected" for e in tx["entries"])


def test_peer_filter(router_core):
    r = router_core
    _connect(r, "b")
    _connect(r, "c")
    r.dispatch("text", {"caller": "a", "to": "b", "message": "to b"})
    r.dispatch("text", {"caller": "a", "to": "c", "message": "to c"})
    _o, payload = r.dispatch("transcript", {"peer": "c", "limit": 50})
    assert payload["entries"]
    assert all(e["from"] == "c" or e["to"] == "c" for e in payload["entries"])


def test_render_transcript():
    payload = {
        "entries": [
            {
                "ts": 1_700_000_000.0,
                "from": "a",
                "to": "b",
                "kind": "call",
                "status": "answered",
                "gist": "why?",
                "call_id": "c1",
            }
        ]
    }
    out = render_transcript(payload)
    assert "call" in out and "answered" in out and "a -> b" in out
    assert render_transcript({"entries": []}) == "(no activity)"
