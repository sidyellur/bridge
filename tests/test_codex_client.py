"""Task 4 verify: ``CodexAppServerClient`` against the pinned 0.151.0 contract.

Every test here drives the real client over a ``socket.socketpair()`` against
``tests/fakes/codex_app_server.FakeCodexAppServer``, whose frames are verbatim
from the 2026-09-07 live probe. The fixture
``tests/fixtures/codex_protocol/codex-0.151.0.json`` is the pin; the constants
in ``bridge.codex_app_server`` are the single source of truth for wire names
and this file asserts the two agree in both directions.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from bridge.codex_app_server import (
    CLIENT_NOTIFICATIONS,
    CLIENT_REQUESTS,
    FORBIDDEN_METHODS,
    ITEM_AGENT_MESSAGE,
    LAUNCH_ARGV,
    MIN_CODEX_VERSION,
    N_INITIALIZED,
    N_ITEM_COMPLETED,
    N_ITEM_DELTA,
    N_THREAD_STARTED,
    N_THREAD_STATUS,
    N_TURN_COMPLETED,
    N_TURN_STARTED,
    OPT_OUT_NOTIFICATION_METHODS,
    PINNED_CODEX_VERSION,
    SERVER_NOTIFICATIONS,
    STATUS_IDLE,
    STATUS_WORKING,
    THREAD_STATUS_ACTIVE,
    THREAD_STATUS_TO_STATE,
    CodexAppServerClient,
    CodexThreadBusy,
    UnsupportedCodexVersion,
    parse_codex_version,
)
from bridge.mcp import INTERNAL_ERROR, METHOD_NOT_FOUND, JsonRpcError

from .fakes.codex_app_server import FakeCodexAppServer

SRC = Path(__file__).resolve().parent.parent / "src" / "bridge"
CLIENT_SOURCE = SRC / "codex_app_server.py"
ADAPTER_SOURCE = SRC / "adapters" / "codex.py"

FIXTURE = Path(__file__).parent / "fixtures" / "codex_protocol" / "codex-0.151.0.json"
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))


class Recorder:
    """Collects every client callback so a test can assert on the sequence."""

    def __init__(self) -> None:
        self.bound: list[str] = []
        self.statuses: list[str] = []
        self.messages: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.errors: list[str] = []
        self.disconnects = 0

    def kwargs(self) -> dict:
        return {
            "on_thread_bound": self.bound.append,
            "on_status": self.statuses.append,
            "on_agent_message": lambda turn_id, text: self.messages.append((turn_id, text)),
            "on_turn_completed": lambda turn_id, status: self.completed.append((turn_id, status)),
            "on_thread_error": self.errors.append,
            "on_disconnect": self._disconnect,
        }

    def _disconnect(self) -> None:
        self.disconnects += 1


@pytest.fixture
def client_pair():
    made: list[tuple[CodexAppServerClient, FakeCodexAppServer]] = []

    def make(*, client_cwd=..., client_version="0.1.0", **fake_kwargs):
        client_sock, server_sock = socket.socketpair()
        fake = FakeCodexAppServer(server_sock, **fake_kwargs)
        recorder = Recorder()
        client = CodexAppServerClient(
            client_sock,
            cwd=fake.cwd if client_cwd is ... else client_cwd,
            client_version=client_version,
            **recorder.kwargs(),
        ).start()
        made.append((client, fake))
        return client, fake, recorder

    yield make
    for client, fake in made:
        client.close()
        fake.close()


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _sync(client: CodexAppServerClient) -> list[str]:
    """Round-trip a request. The reader thread handles frames in order, so every
    notification the fake sent before this response has already been dispatched
    by the time it returns — no sleeps, no flaky waits."""
    return client.rpc.request("thread/loaded/list", {}).get("data") or []


def _bound(client: CodexAppServerClient, fake: FakeCodexAppServer) -> None:
    client.initialize()
    assert _wait(lambda: client.thread_id == fake.thread_id)


# --- the pin ---------------------------------------------------------------


def test_pinned_contract_matches_the_fixture():
    assert set(CONTRACT["client_requests"]) == set(CLIENT_REQUESTS)
    assert set(CONTRACT["client_notifications"]) == set(CLIENT_NOTIFICATIONS)
    assert set(CONTRACT["server_notifications"]) == set(SERVER_NOTIFICATIONS)
    assert tuple(CONTRACT["forbidden_methods"]) == FORBIDDEN_METHODS
    assert tuple(CONTRACT["launch_argv"]) == LAUNCH_ARGV
    assert CONTRACT["bridge_state_map"] == THREAD_STATUS_TO_STATE
    assert tuple(CONTRACT["min_codex_version"]) == MIN_CODEX_VERSION
    assert CONTRACT["codex_version"] == PINNED_CODEX_VERSION


# --- initialize ------------------------------------------------------------


def test_initialize_sends_client_info_and_the_opt_out_list(client_pair):
    client, fake, _rec = client_pair()
    client.initialize()

    params = fake.initialize_params
    assert params["clientInfo"] == {"name": "bridge", "title": "Bridge", "version": "0.1.0"}
    assert params["capabilities"]["optOutNotificationMethods"] == list(
        OPT_OUT_NOTIFICATION_METHODS
    )
    assert "protocolVersion" not in json.dumps(params)


def test_initialize_parses_the_codex_version_from_the_user_agent(client_pair):
    client, _fake, _rec = client_pair()
    result = client.initialize()

    assert client.codex_version == "0.151.0"
    assert client.codex_version_warning is None
    assert client.server_info["userAgent"] == result["userAgent"]

    for user_agent, expected in (
        ("bridge/0.151.0 (Mac OS 15.5.0; arm64) iTerm.app/3.6.11 (bridge; 0.1.0)", (0, 151, 0)),
        ("bridge/0.151.0-rc1 (Mac OS 15.5.0; arm64)", (0, 151, 0)),
        # Only the version token is read: neither the OS version nor the client
        # version trailing it may stand in for a missing codex version.
        ("codex/dev (Mac OS 15.5.0; arm64) iTerm.app/3.6.11 (codex; 0.1.0)", None),
        ("bridge/unknown (Fake OS 1.0; arm64) fake-term/0 (bridge; 0.1.0)", None),
        ("bridge/0.152 (Mac OS 15.5.0; arm64)", None),
        ("bridge/", None),
        ("no-slash-at-all", None),
    ):
        assert parse_codex_version(user_agent) == expected, user_agent


def test_initialize_rejects_a_version_below_the_minimum(client_pair):
    client, _fake, _rec = client_pair(codex_version="0.150.9")
    with pytest.raises(UnsupportedCodexVersion, match="predates the pinned App Server contract"):
        client.initialize()


def test_initialize_rejects_an_unparsable_user_agent(client_pair):
    client, _fake, _rec = client_pair(codex_version="unknown")
    with pytest.raises(UnsupportedCodexVersion, match="could not parse a codex version"):
        client.initialize()


def test_initialize_warns_but_proceeds_above_the_pin(client_pair):
    client, _fake, _rec = client_pair(codex_version="0.152.0")
    client.initialize()

    assert client.codex_version == "0.152.0"
    assert client.codex_version_warning is not None
    assert PINNED_CODEX_VERSION in client.codex_version_warning


def test_initialized_is_sent_without_a_params_key(client_pair):
    client, _fake, _rec = client_pair()
    sent: list[dict] = []
    original = client.rpc._send

    def spy(obj):
        sent.append(obj)
        original(obj)

    client.rpc._send = spy
    client.initialize()

    frame = next(f for f in sent if f.get("method") == N_INITIALIZED)
    assert "params" not in frame


# --- thread binding --------------------------------------------------------


def _seed_threads(client, fake, specs) -> None:
    """Create threads on the fake, then drop the broadcast binding so
    ``bind_thread`` is exercised on its own — the reconnect path, where Bridge
    was not connected when ``thread/started`` fired."""
    for thread_id, cwd in specs:
        fake.start_thread(thread_id, cwd)
    _sync(client)
    client.thread_id = None


def test_bind_prefers_the_thread_matching_the_session_cwd(client_pair):
    client, fake, rec = client_pair(eager_thread=False, client_cwd="/work/mine")
    client.initialize()
    _seed_threads(client, fake, [("t-other", "/work/other"), ("t-mine", "/work/mine")])
    rec.bound.clear()

    assert client.bind_thread() == "t-mine"
    assert client.thread_id == "t-mine"
    assert rec.bound == ["t-mine"]


def test_bind_falls_back_to_the_newest_thread(client_pair):
    now = {"ms": 1_700_000_000_000}
    client, fake, _rec = client_pair(
        eager_thread=False, client_cwd="/work/nowhere", now_ms=lambda: now["ms"]
    )
    client.initialize()
    fake.start_thread("t-old", "/work/a")
    now["ms"] += 60_000
    fake.start_thread("t-new", "/work/b")
    _sync(client)
    client.thread_id = None

    assert client.bind_thread() == "t-new"


def test_bind_ignores_threads_bridge_started_itself(client_pair):
    client, fake, _rec = client_pair(eager_thread=False, client_cwd="/work/mine")
    client.own_thread_ids.add("t-mine")
    client.initialize()
    _seed_threads(client, fake, [("t-mine", "/work/mine"), ("t-other", "/work/other")])

    assert client.bind_thread() == "t-other"


def test_bind_returns_none_when_there_are_no_candidates(client_pair):
    client, _fake, rec = client_pair(eager_thread=False)
    client.initialize()

    assert client.bind_thread() is None
    assert client.thread_id is None
    assert rec.bound == []


def test_bind_tolerates_a_thread_list_error_and_still_binds_from_the_broadcast(client_pair):
    """Discovery is best-effort. A ``thread/loaded/list`` refusal used to raise
    straight out of ``adapter.start()`` and tear the wrapper down with a raw
    JsonRpcError, contradicting the warn-never-hard-fail policy above the pin —
    and needlessly, because the global ``thread/started`` broadcast binds the
    TUI thread anyway."""
    client, fake, rec = client_pair(
        eager_thread=False,
        client_cwd="/work/mine",
        thread_list_error=JsonRpcError(METHOD_NOT_FOUND, "thread/loaded/list is unsupported"),
    )
    client.initialize()

    assert client.bind_thread() is None
    assert client.thread_id is None
    assert "unsupported" in (client.last_thread_error or "")
    assert rec.bound == []

    fake.start_thread("t-mine", "/work/mine")
    assert _wait(lambda: client.thread_id == "t-mine")
    assert rec.bound == ["t-mine"]


def test_a_thread_started_broadcast_after_connect_binds(client_pair):
    client, fake, rec = client_pair(eager_thread=False, client_cwd="/work/mine")
    client.initialize()
    assert client.thread_id is None

    fake.start_thread("thread-tui", "/work/mine")

    assert _wait(lambda: client.thread_id == "thread-tui")
    assert rec.bound == ["thread-tui"]
    assert client.subscribed is False


def test_a_second_thread_in_the_same_cwd_rebinds_last_wins(client_pair):
    client, fake, rec = client_pair(eager_thread=False, client_cwd="/work/mine")
    client.initialize()
    fake.start_thread("t1", "/work/mine")
    fake.start_thread("t2", "/work/mine")
    _sync(client)

    assert client.thread_id == "t2"
    assert rec.bound == ["t1", "t2"]


def test_a_thread_in_another_cwd_does_not_rebind(client_pair):
    client, fake, rec = client_pair(eager_thread=False, client_cwd="/work/mine")
    client.initialize()
    fake.start_thread("t1", "/work/mine")
    fake.start_thread("t2", "/work/elsewhere")
    _sync(client)

    assert client.thread_id == "t1"
    assert rec.bound == ["t1"]


# --- subscribe -------------------------------------------------------------


def test_subscribe_success_sets_subscribed(client_pair):
    client, fake, _rec = client_pair(resume_result="ok")
    _bound(client, fake)

    assert client.subscribe() is True
    assert client.subscribed is True
    assert fake.subscribed == {fake.thread_id}


def test_subscribe_tolerates_list_turns_not_supported(client_pair):
    client, fake, _rec = client_pair(resume_result="unsupported")
    _bound(client, fake)

    assert client.subscribe() is False
    assert client.subscribed is False
    assert "list_turns" in client.last_thread_error


def test_subscribe_tolerates_no_rollout(client_pair):
    client, fake, _rec = client_pair(resume_result="no_rollout")
    _bound(client, fake)

    assert client.subscribe() is False
    assert client.subscribed is False
    assert "no rollout" in client.last_thread_error


def test_subscribe_reraises_an_unexpected_error_code(client_pair):
    client, fake, _rec = client_pair(resume_result="ok")
    _bound(client, fake)

    def boom(_params):
        raise JsonRpcError(INTERNAL_ERROR, "internal explosion")

    fake.rpc.method("thread/resume", boom)

    with pytest.raises(JsonRpcError) as excinfo:
        client.subscribe()
    assert excinfo.value.code == INTERNAL_ERROR
    assert client.subscribed is False


# --- status ----------------------------------------------------------------


def test_thread_status_changed_maps_every_variant(client_pair):
    client, fake, rec = client_pair()
    _bound(client, fake)
    rec.statuses.clear()

    for status_type, expected in (
        ("notLoaded", STATUS_IDLE),
        ("active", STATUS_WORKING),
        ("idle", STATUS_IDLE),
        ("systemError", STATUS_IDLE),
    ):
        fake.emit_status(status_type)
        _sync(client)
        assert client.status == expected, status_type

    assert rec.statuses == [STATUS_WORKING, STATUS_IDLE]
    assert client.last_thread_error == "thread reported systemError"
    assert rec.errors == ["thread reported systemError"]

    fake.emit_status("active", ["waitingOnApproval"])
    _sync(client)
    assert client.status == STATUS_WORKING

    fake._notify(N_THREAD_STATUS, {"threadId": "someone-else", "status": {"type": "idle"}})
    _sync(client)
    assert client.status == STATUS_WORKING

    fake._notify(N_THREAD_STATUS, {"threadId": fake.thread_id, "status": {"type": "martian"}})
    _sync(client)
    assert client.status == STATUS_WORKING


def test_malformed_frames_are_ignored(client_pair):
    client, fake, rec = client_pair()
    _bound(client, fake)
    rec.statuses.clear()
    rec.bound.clear()

    fake._notify(N_THREAD_STATUS, {"threadId": fake.thread_id, "status": "active"})
    fake._notify(N_THREAD_STARTED, {"thread": "thread-abc"})
    _sync(client)

    assert client.status == STATUS_IDLE
    assert client.thread_id == fake.thread_id
    assert rec.statuses == []
    assert rec.bound == []


# --- turns -----------------------------------------------------------------


def test_start_turn_uses_camel_case_and_returns_result_turn_id(client_pair):
    client, fake, _rec = client_pair(resume_result="ok", auto_complete=False)
    _bound(client, fake)
    client.subscribe()

    turn_id = client.start_turn("hi")

    assert turn_id == "turn-1"
    assert fake.turns[0] == {
        "threadId": fake.thread_id,
        "input": [{"type": "text", "text": "hi"}],
    }


def test_start_turn_is_optimistically_working_before_the_active_broadcast(client_pair):
    """The live server answers ``turn/start`` and only then broadcasts
    ``thread/status/changed active``. Gating the second turn on that broadcast
    leaves a window the router's pump walks straight through — it drains every
    queued text back-to-back — so two turns would overlap on one thread. Bridge
    must consider the thread working the moment the response lands."""
    client, fake, _rec = client_pair(auto_complete=False, defer_turn_status=True)
    _bound(client, fake)

    client.start_turn("first")
    assert client.status == STATUS_WORKING  # no status frame has arrived yet

    with pytest.raises(CodexThreadBusy, match="never queues a second turn"):
        client.start_turn("second")
    assert len(fake.turns) == 1

    # The server's own frames stay authoritative in both directions.
    fake.emit_status(THREAD_STATUS_ACTIVE)
    _sync(client)
    assert client.status == STATUS_WORKING
    fake.complete_turn()
    _sync(client)
    assert client.status == STATUS_IDLE


def test_start_turn_refuses_while_working(client_pair):
    client, fake, _rec = client_pair(resume_result="ok")
    _bound(client, fake)
    fake.emit_status("active")
    _sync(client)
    assert client.status == STATUS_WORKING

    with pytest.raises(CodexThreadBusy, match="never queues a second turn"):
        client.start_turn("hi")
    assert fake.turns == []


def test_item_completed_agent_message_fires_the_callback_and_user_messages_are_ignored(
    client_pair,
):
    client, fake, rec = client_pair(resume_result="ok", agent_message="yes, ship it")
    _bound(client, fake)
    client.subscribe()

    turn_id = client.start_turn("ship it?")
    _sync(client)

    assert rec.messages == [(turn_id, "yes, ship it")]


def test_agent_message_deltas_accumulate_per_turn_and_items_win_over_deltas(client_pair):
    client, fake, _rec = client_pair(resume_result="ok", auto_complete=False)
    _bound(client, fake)
    client.subscribe()

    first = client.start_turn("a")
    fake.complete_turn(first, agent_message="hello there")
    _sync(client)

    second = client.start_turn("b")
    for delta in ("par", "tial"):
        fake._notify(
            N_ITEM_DELTA,
            {"threadId": fake.thread_id, "turnId": second, "itemId": "i-1", "delta": delta},
        )
    fake._notify(
        N_TURN_COMPLETED,
        {"threadId": fake.thread_id, "turn": {"id": second, "status": "completed"}},
    )
    _sync(client)

    assert client.final_message(first) == "hello there"
    assert client.final_message(second) == "partial"
    assert client.final_message("never-happened") is None


def test_turn_completed_reports_the_turn_status(client_pair):
    client, fake, rec = client_pair(resume_result="ok", auto_complete=False)
    _bound(client, fake)
    client.subscribe()

    first = client.start_turn("a")
    fake.complete_turn(first, agent_message="done")
    _sync(client)

    second = client.start_turn("b")
    fake.complete_turn(second, agent_message="", status="failed")
    _sync(client)

    assert rec.completed == [(first, "completed"), (second, "failed")]
    assert client.status == STATUS_IDLE


def test_another_threads_turn_and_item_stream_is_ignored(client_pair):
    """A rebind never cancels the old thread's ``thread/resume``, so one
    connection can keep receiving a thread it no longer speaks for. Its turns
    must not move this client's status, its final messages, or its callbacks —
    otherwise an abandoned thread could clear the adapter's active call or
    answer it with the wrong thread's text."""
    client, fake, rec = client_pair(resume_result="ok", auto_complete=False)
    _bound(client, fake)
    client.subscribe()
    other = "thread-other"
    assert client.thread_id != other
    rec.statuses.clear()

    fake._notify(N_TURN_STARTED, {"threadId": other, "turn": {"id": "turn-x"}})
    fake._notify(
        N_ITEM_DELTA,
        {"threadId": other, "turnId": "turn-x", "itemId": "i-x", "delta": "not for us"},
    )
    fake._notify(
        N_ITEM_COMPLETED,
        {
            "threadId": other,
            "turnId": "turn-x",
            "item": {"type": ITEM_AGENT_MESSAGE, "id": "i-x", "text": "wrong thread"},
        },
    )
    fake._notify(
        N_TURN_COMPLETED,
        {"threadId": other, "turn": {"id": "turn-x", "status": "completed"}},
    )
    _sync(client)

    assert rec.statuses == []
    assert rec.messages == []
    assert rec.completed == []
    assert client.status == STATUS_IDLE
    assert client.final_message("turn-x") is None

    # The bound thread's own stream still lands, so the guard filters rather
    # than deafens.
    turn_id = client.start_turn("ours")
    fake.complete_turn(turn_id, agent_message="ours")
    _sync(client)
    assert rec.messages == [(turn_id, "ours")]
    assert rec.completed == [(turn_id, "completed")]


def test_an_unsubscribed_client_still_sees_busy_and_idle(client_pair):
    client, fake, rec = client_pair(resume_result="unsupported", agent_message="unheard")
    _bound(client, fake)
    assert client.subscribe() is False
    rec.statuses.clear()

    client.start_turn("go")
    _sync(client)

    assert rec.statuses == [STATUS_WORKING, STATUS_IDLE]
    assert rec.messages == []
    assert rec.completed == []


# --- guardrail -------------------------------------------------------------


def test_forbidden_methods_are_never_called(client_pair):
    client_source = CLIENT_SOURCE.read_text(encoding="utf-8")
    for source in (client_source, ADAPTER_SOURCE.read_text(encoding="utf-8")):
        for method in FORBIDDEN_METHODS:
            assert source.count(f'"{method}"') <= 1, method
        for line in source.splitlines():
            if any(method in line for method in FORBIDDEN_METHODS):
                assert "rpc.request(" not in line, line

    declaration = next(
        line for line in client_source.splitlines() if line.startswith("FORBIDDEN_METHODS")
    )
    assert all(f'"{method}"' in declaration for method in FORBIDDEN_METHODS)

    client, fake, _rec = client_pair(resume_result="ok", agent_message="ok")
    _bound(client, fake)
    client.subscribe()
    client.start_turn("hi")
    _sync(client)

    assert fake.forbidden_calls == []
