"""Post-v1 A (aliases/contacts): validation, resolution precedence, file
permissions, router integration (roster ``alias`` field, guardrail labelling),
and the CLI.

Aliases are optional user state: with no contacts file every behaviour here
must be identical to v1, and an unknown name must fall through to the router's
normal ``unreachable`` guardrail rather than being invented anywhere.
"""

from __future__ import annotations

import json
import stat

import pytest

from bridge import contacts
from bridge.cli import main
from bridge.paths import Paths
from bridge.router import RouterError
from bridge.store import STATE_IDLE

SESSION_A = "0000aaaa-0000-4000-8000-000000000001"
SESSION_B = "0000bbbb-0000-4000-8000-000000000002"


@pytest.fixture
def cli_env(paths, monkeypatch):
    monkeypatch.setenv("BRIDGE_HOME", str(paths.home))
    monkeypatch.delenv("BRIDGE_SESSION_ID", raising=False)
    return paths


def _connect(router, sid, family="claude", state=STATE_IDLE):
    router.notifier.connected_ids.add(sid)
    router.store.upsert_session(sid, family, state=state, reachable=True)


# --- file I/O ---------------------------------------------------------------


def test_load_missing_file_is_empty(paths: Paths):
    assert not paths.contacts.exists()
    assert contacts.load(paths) == {}


def test_save_round_trips_and_is_user_only(paths: Paths):
    contacts.save(paths, {"web": SESSION_A, "infra": SESSION_B})
    assert stat.S_IMODE(paths.contacts.stat().st_mode) == 0o600
    assert contacts.load(paths) == {"web": SESSION_A, "infra": SESSION_B}


def test_save_leaves_no_temp_file(paths: Paths):
    contacts.save(paths, {"web": SESSION_A})
    assert sorted(p.name for p in paths.home.iterdir() if p.is_file()) == ["contacts.json"]


def test_load_rejects_malformed_json(paths: Paths):
    paths.contacts.write_text("{not json")
    with pytest.raises(contacts.ContactsError):
        contacts.load(paths)


def test_load_rejects_non_object(paths: Paths):
    paths.contacts.write_text(json.dumps(["web", SESSION_A]))
    with pytest.raises(contacts.ContactsError):
        contacts.load(paths)


def test_load_rejects_non_string_values(paths: Paths):
    paths.contacts.write_text(json.dumps({"web": 7}))
    with pytest.raises(contacts.ContactsError):
        contacts.load(paths)


def test_corrupt_file_is_ignored_on_the_routing_path(paths: Paths):
    """A broken contacts file must not break addressing: it just means no aliases."""
    paths.contacts.write_text("{not json")
    assert contacts.resolve(paths, "web") == "web"
    assert contacts.alias_for(paths, SESSION_A) is None
    assert contacts.alias_index(paths) == {}
    assert contacts.label(paths, SESSION_A) == SESSION_A


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize("name", ["web", "web-1", "web_1", "a.b", "A9", "x" * 40])
def test_valid_alias_names(name: str):
    assert contacts.validate_name(name) == name


@pytest.mark.parametrize("name", ["", "x" * 41, "has space", "sla/sh", "emoji-✨", "a:b"])
def test_invalid_alias_names(name: str):
    with pytest.raises(contacts.ContactsError):
        contacts.validate_name(name)


def test_uuid_shaped_alias_names_rejected():
    with pytest.raises(contacts.ContactsError) as exc:
        contacts.validate_name(SESSION_A)
    assert "session id" in str(exc.value)


def test_set_alias_rejects_invalid_name(paths: Paths):
    with pytest.raises(contacts.ContactsError):
        contacts.set_alias(paths, "not a name", SESSION_A)
    assert not paths.contacts.exists()


def test_set_alias_rejects_empty_session_id(paths: Paths):
    with pytest.raises(contacts.ContactsError):
        contacts.set_alias(paths, "web", "   ")


# --- resolution -------------------------------------------------------------


def test_resolve_maps_alias_to_session_id(paths: Paths):
    contacts.set_alias(paths, "web", SESSION_A)
    assert contacts.resolve(paths, "web") == SESSION_A


def test_resolve_leaves_unknown_names_unchanged(paths: Paths):
    contacts.set_alias(paths, "web", SESSION_A)
    assert contacts.resolve(paths, "ghost") == "ghost"
    assert contacts.resolve(paths, SESSION_B) == SESSION_B
    assert contacts.resolve(paths, "") == ""


def test_known_session_id_wins_over_alias_of_the_same_string(paths: Paths):
    """An alias whose *name* is another session's id must never redirect it."""
    contacts.save(paths, {"web": SESSION_A, "shadow": SESSION_B})
    # 'shadow' resolves normally...
    assert contacts.resolve(paths, "shadow") == SESSION_B
    # ...but a string that is itself a mapped session id resolves to itself.
    assert contacts.resolve(paths, SESSION_B) == SESSION_B


def test_alias_for_and_label(paths: Paths):
    contacts.save(paths, {"web": SESSION_A, "aweb": SESSION_A})
    assert contacts.alias_for(paths, SESSION_A) == "aweb"  # sorted order, stable
    assert contacts.alias_for(paths, SESSION_B) is None
    assert contacts.label(paths, SESSION_A) == f"aweb ({SESSION_A})"
    assert contacts.label(paths, SESSION_B) == SESSION_B


def test_remove_alias(paths: Paths):
    contacts.set_alias(paths, "web", SESSION_A)
    assert contacts.remove_alias(paths, "web") is True
    assert contacts.load(paths) == {}
    assert contacts.remove_alias(paths, "web") is False


# --- router integration -----------------------------------------------------


def test_router_resolve_target_prefers_a_live_session_id(router_core, paths: Paths):
    """A real session id beats an alias name spelled the same way."""
    router_core.store.upsert_session("web", "claude", state=STATE_IDLE)
    contacts.set_alias(paths, "web", SESSION_A)
    assert router_core.resolve_target("web") == "web"


def test_router_resolve_target_uses_alias_when_no_such_session(router_core, paths: Paths):
    contacts.set_alias(paths, "web", SESSION_A)
    assert router_core.resolve_target("web") == SESSION_A
    assert router_core.resolve_target("ghost") == "ghost"


def test_call_text_and_call_async_accept_an_alias(router_core, paths: Paths):
    r = router_core
    _connect(r, SESSION_B)
    contacts.set_alias(paths, "web", SESSION_B)

    outcome, _ = r.dispatch("call", {"caller": "a", "to": "web", "question": "q"}, waiter="W")
    assert outcome == "defer"
    delivered_to, _event = r.notifier.delivered[0]
    assert delivered_to == SESSION_B
    # The call is recorded against the real id, never the alias.
    call = r.store.calls_awaiting_delivery() or [r.store.active_inbound_call(SESSION_B)]
    assert call[0].to_id == SESSION_B

    r.dispatch("reply", {"caller": SESSION_B, "call_id": call[0].call_id, "answer": "ok"})
    out = r.dispatch("text", {"caller": "a", "to": "web", "message": "hi"})
    assert out[1]["status"] in ("delivered", "queued")

    _, res = r.dispatch("call_async", {"caller": "a", "to": "web", "question": "q2"}, waiter="W")
    assert res["status"] != "unreachable"


def test_alias_is_resolved_before_guardrails(router_core, paths: Paths):
    """Aliasing yourself must trip the self-call guardrail, not sneak past it."""
    r = router_core
    _connect(r, SESSION_A)
    contacts.set_alias(paths, "me", SESSION_A)
    with pytest.raises(RouterError) as exc:
        r.dispatch("call", {"caller": SESSION_A, "to": "me", "question": "?"}, waiter="W")
    assert exc.value.code == "self_call"


def test_unknown_alias_is_unreachable_and_names_the_alias(router_core):
    with pytest.raises(RouterError) as exc:
        router_core.dispatch("text", {"caller": "a", "to": "web", "message": "hi"})
    assert exc.value.code == "unreachable"
    assert "web" in exc.value.message
    assert "bridge alias web" in exc.value.message


def test_unknown_uuid_message_does_not_suggest_an_alias(router_core):
    with pytest.raises(RouterError) as exc:
        router_core.dispatch("text", {"caller": "a", "to": SESSION_A, "message": "hi"})
    assert exc.value.code == "unreachable"
    assert "bridge alias" not in exc.value.message


def test_guardrail_messages_show_alias_and_id(router_core, paths: Paths):
    r = router_core
    contacts.set_alias(paths, "web", SESSION_B)
    label = f"web ({SESSION_B})"

    r.store.upsert_session(SESSION_B, "codex", state=STATE_IDLE, is_managed=False)
    with pytest.raises(RouterError) as exc:
        r.dispatch("text", {"caller": "a", "to": "web", "message": "x"})
    assert label in exc.value.message
    assert "bridge codex" in exc.value.message

    # rate cap
    r.store.upsert_session(SESSION_B, "codex", state="working", is_managed=True)
    for _ in range(r.config.rate_cap):
        r.store.record_rate("a", SESSION_B)
    with pytest.raises(RouterError) as exc2:
        r.dispatch("text", {"caller": "a", "to": "web", "message": "x"})
    assert exc2.value.code == "rate_capped"
    assert label in exc2.value.message


def test_queue_full_message_shows_alias(router_core, paths: Paths):
    from bridge.router import RouterConfig

    r = router_core
    r.config = RouterConfig(queue_cap=1, rate_cap=1000)
    contacts.set_alias(paths, "web", SESSION_B)
    _connect(r, SESSION_B, family="codex", state="working")  # busy: queues instead
    r.dispatch("text", {"caller": "a", "to": "web", "message": "one"})
    with pytest.raises(RouterError) as exc:
        r.dispatch("text", {"caller": "a", "to": "web", "message": "two"})
    assert exc.value.code == "queue_full"
    assert f"web ({SESSION_B})" in exc.value.message


def test_roster_includes_alias_field(router_core, paths: Paths):
    r = router_core
    r.store.upsert_session(SESSION_A, "claude", state=STATE_IDLE)
    r.store.upsert_session(SESSION_B, "codex", state=STATE_IDLE)
    contacts.set_alias(paths, "web", SESSION_A)

    payload = r.roster({})
    by_id = {s["id"]: s for s in payload["sessions"]}
    assert by_id[SESSION_A]["alias"] == "web"
    assert by_id[SESSION_B]["alias"] is None


def test_roster_alias_reflects_the_file_without_restart(router_core, paths: Paths):
    """The daemon caches nothing: `bridge alias` takes effect on the next roster."""
    r = router_core
    r.store.upsert_session(SESSION_A, "claude", state=STATE_IDLE)
    assert r.roster({})["sessions"][0]["alias"] is None
    contacts.set_alias(paths, "web", SESSION_A)
    assert r.roster({})["sessions"][0]["alias"] == "web"


def test_render_roster_shows_alias_column():
    from bridge.roster import render_roster

    out = render_roster(
        {
            "sessions": [
                {
                    "id": SESSION_A,
                    "alias": "web",
                    "family": "claude",
                    "state": "idle",
                    "reachable": True,
                    "cwd": "/w",
                }
            ],
            "warnings": [],
        }
    )
    assert "ALIAS" in out
    assert "web" in out


# --- tool surface -----------------------------------------------------------


def test_dispatch_tool_passes_an_alias_through_untouched():
    """Alias resolution belongs to the router, not the tool layer."""
    from bridge.tools import dispatch_tool

    sent: list[tuple[str, dict]] = []

    class _Client:
        def call(self, op, args, **kwargs):
            sent.append((op, args))
            return {}

    client = _Client()
    dispatch_tool(client, "call", {"to": "web", "question": "q"})
    dispatch_tool(client, "call_async", {"to": "web", "question": "q"})
    dispatch_tool(client, "text", {"to": "web", "message": "m"})
    assert [args["to"] for _op, args in sent] == ["web", "web", "web"]


# --- CLI --------------------------------------------------------------------


def test_cli_alias_set_list_and_remove(cli_env, capsys):
    assert main(["alias", "web", SESSION_A]) == 0
    assert SESSION_A in capsys.readouterr().out

    assert main(["alias"]) == 0
    out = capsys.readouterr().out
    assert "web" in out and SESSION_A in out

    assert main(["alias", "web"]) == 0
    assert capsys.readouterr().out.strip() == SESSION_A

    assert main(["alias", "--rm", "web"]) == 0
    assert "removed" in capsys.readouterr().out
    assert contacts.load(cli_env) == {}


def test_cli_alias_list_when_empty(cli_env, capsys):
    assert main(["alias"]) == 0
    assert "no aliases" in capsys.readouterr().out


def test_cli_alias_lookup_unknown(cli_env, capsys):
    assert main(["alias", "ghost"]) == 1
    assert "no alias" in capsys.readouterr().out


def test_cli_alias_rm_unknown(cli_env, capsys):
    assert main(["alias", "--rm", "ghost"]) == 1
    assert "no alias" in capsys.readouterr().out


def test_cli_alias_rm_without_name(cli_env, capsys):
    assert main(["alias", "--rm"]) == 2
    assert "needs an alias name" in capsys.readouterr().out


def test_cli_alias_rejects_bad_name(cli_env, capsys):
    assert main(["alias", "bad name", SESSION_A]) == 2
    assert "invalid alias name" in capsys.readouterr().out
    assert not cli_env.contacts.exists()


def test_cli_alias_write_is_user_only(cli_env):
    assert main(["alias", "web", SESSION_A]) == 0
    assert stat.S_IMODE(cli_env.contacts.stat().st_mode) == 0o600
