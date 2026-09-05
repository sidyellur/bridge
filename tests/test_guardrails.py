"""Task 8 verify (guardrails): self-call, hop budget, rate cap, one active
inbound call, reachability, and the source-level forbidden-fallback assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge.router import RouterConfig, RouterError

SRC = Path(__file__).resolve().parent.parent / "src" / "bridge"


def _connect(r, sid, family="claude", state="idle"):
    r.notifier.connected_ids.add(sid)
    r.store.upsert_session(sid, family, state=state, reachable=True)


def test_no_self_call(router_core):
    _connect(router_core, "a")
    with pytest.raises(RouterError) as exc:
        router_core.dispatch("call", {"caller": "a", "to": "a", "question": "?"}, waiter="W")
    assert exc.value.code == "self_call"
    with pytest.raises(RouterError):
        router_core.dispatch("text", {"caller": "a", "to": "a", "message": "hi"})


def test_hop_budget_blocks_outbound_while_answering(router_core):
    r = router_core
    _connect(r, "b")
    _connect(r, "c")
    # A calls B -> delivered, B now has an active inbound call.
    r.dispatch("call", {"caller": "a", "to": "b", "question": "q"}, waiter="W")
    call_id = r.notifier.delivered[0][1]["call_id"]

    for op, args in [
        ("call", {"caller": "b", "to": "c", "question": "x"}),
        ("call_async", {"caller": "b", "to": "c", "question": "x"}),
        ("text", {"caller": "b", "to": "c", "message": "x"}),
    ]:
        with pytest.raises(RouterError) as exc:
            r.dispatch(op, args, waiter="W")
        assert exc.value.code == "hop_budget"

    # reply is still allowed while answering
    out, _ = r.dispatch("reply", {"caller": "b", "call_id": call_id, "answer": "done"})
    assert out == "respond"
    # after answering, B can dial out again
    r.dispatch("text", {"caller": "b", "to": "c", "message": "now ok"})


def test_rate_cap_per_ordered_pair(router_core):
    r = router_core
    r.config = RouterConfig(rate_cap=10, queue_cap=1000)
    _connect(r, "b", state="working")  # busy: queue, but rate still counts
    for _ in range(10):
        r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    with pytest.raises(RouterError) as exc:
        r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    assert exc.value.code == "rate_capped"


def test_rate_cap_reverse_direction_independent(router_core):
    r = router_core
    r.config = RouterConfig(rate_cap=2, queue_cap=1000)
    _connect(r, "a", state="working")
    _connect(r, "b", state="working")
    r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    with pytest.raises(RouterError):
        r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    # b -> a is a different ordered pair, still allowed
    out, _ = r.dispatch("text", {"caller": "b", "to": "a", "message": "x"})
    assert out == "respond"


def test_rate_window_resets(router_core, clock):
    r = router_core
    r.config = RouterConfig(rate_cap=1, queue_cap=1000)
    _connect(r, "b", state="working")
    r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    with pytest.raises(RouterError):
        r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    clock.advance(3601)
    out, _ = r.dispatch("text", {"caller": "a", "to": "b", "message": "x"})
    assert out == "respond"


def test_unreachable_unknown_and_unmanaged(router_core):
    r = router_core
    with pytest.raises(RouterError) as exc:
        r.dispatch("call", {"caller": "a", "to": "ghost", "question": "?"}, waiter="W")
    assert exc.value.code == "unreachable"

    r.store.upsert_session("u", "codex", state="idle", is_managed=False)
    with pytest.raises(RouterError) as exc2:
        r.dispatch("text", {"caller": "a", "to": "u", "message": "x"})
    assert exc2.value.code == "unreachable"
    assert "bridge codex" in exc2.value.message


def test_no_allow_writes_anywhere_in_source():
    for py in SRC.rglob("*.py"):
        assert "allow_writes" not in py.read_text(), f"allow_writes found in {py}"


def test_no_forbidden_fallback_in_source():
    # Fallback *invocations* are forbidden anywhere in the source.
    forbidden = [
        "claude -p",
        "codex exec",
        "carbon-copy",
        "carbon copy",
        "warm resume",
        "exec resume",
    ]
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        for pattern in forbidden:
            assert pattern not in text, f"forbidden fallback {pattern!r} found in {py}"

    # 'spool' may appear ONLY in install/doctor, and only to remove the obsolete
    # pre-release spool directory — never in the coordination path.
    for py in SRC.rglob("*.py"):
        if py.name in ("install.py", "doctor.py"):
            continue
        assert "spool" not in py.read_text(), f"spool referenced in {py}"


def test_call_ignores_write_elevation_arguments(router_core):
    r = router_core
    _connect(r, "b")
    # a hostile/legacy allow_writes arg must be ignored, not honored
    outcome, _ = r.dispatch(
        "call", {"caller": "a", "to": "b", "question": "q", "allow_writes": True}, waiter="W"
    )
    assert outcome == "defer"  # accepted as a normal call, elevation ignored
