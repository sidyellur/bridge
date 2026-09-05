"""Task 4 verify (roster): reachability, is_self, previews, and rendering."""

from __future__ import annotations

from bridge.roster import fetch_roster, render_roster
from bridge.store import STATE_IDLE

from .fakes.router_peer import RunningRouter


def test_render_empty():
    assert "no sessions" in render_roster({"sessions": [], "warnings": []})


def test_render_marks_self_and_warnings():
    payload = {
        "sessions": [
            {
                "id": "aaaa",
                "family": "claude",
                "state": "idle",
                "reachable": True,
                "cwd": "/w",
                "last_user_message": "hi",
                "is_self": True,
            }
        ],
        "warnings": ["something is off"],
    }
    out = render_roster(payload)
    assert "*aaaa" in out
    assert "! something is off" in out
    assert "last: hi" in out


def test_fetch_roster_reachability_and_is_self(paths):
    with RunningRouter(paths) as rr:
        # target with a live adapter -> reachable
        adapter = rr.adapter("target")
        adapter.client.call(
            "register_session", {"session_id": "target", "family": "codex", "state": STATE_IDLE}
        )
        adapter.subscribe()

        payload = fetch_roster(paths)
        entry = next(s for s in payload["sessions"] if s["id"] == "target")
        assert entry["reachable"] is True
        assert entry["is_self"] is False  # fetch_roster connects as a fresh client


def test_roster_preview_capped_at_120(paths):
    with RunningRouter(paths) as rr:
        c = rr.client(session_id="s")
        c.call("register_session", {"session_id": "s", "family": "claude", "state": "idle"})
        c.call("update_state", {"session_id": "s", "last_user_message": "x" * 500})
        payload = c.call("roster", {})
        entry = next(s for s in payload["sessions"] if s["id"] == "s")
        assert len(entry["last_user_message"]) == 120
