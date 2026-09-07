"""Issue #4 verify: the ``bridge lab`` harness for Experiments E-H.

Everything here is hermetic: fake ``claude``/``codex`` executables, an injected
doctor report, fake Claude Channel host / Codex App Server peers behind a real
``RouterServer``, a scripted prompter instead of ``input()``, and a synthetic
experiments-document fixture (never the real file). The *real* document is
only ever read -- one test asserts its four ``Verdict:`` lines are well-formed
(TBD, or PASS/FAIL with a run reference) and that ``lab_report`` agrees with
whatever the file currently says, because a verdict may only be written by a
human who ran the procedure.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from bridge.adapters.codex import CodexAdapter
from bridge.claude_channel import ClaudeChannelAdapter
from bridge.codex_app_server import CodexAppServerClient
from bridge.doctor import FAIL, OK, DoctorReport
from bridge.lab import capture
from bridge.lab.cli import (
    EXPERIMENTS,
    LabError,
    build_h_steps,
    correlate_turns,
    find_repo_root,
    lab_prepare,
    lab_report,
    lab_run,
    lab_verdict,
    replace_verdict,
)

from .fakes.claude_host import FakeClaudeHost
from .fakes.codex_app_server import FakeCodexAppServer
from .fakes.executables import make_capture_exe
from .fakes.router_peer import RunningRouter

CODEX_CWD = "/tmp/peer"
DOCS = Path(__file__).resolve().parent.parent / "docs" / "experiments"
REAL_DOC = DOCS / "2026-08-27-live-transport-semantics.md"

# A synthetic stand-in for the real experiments document: same section/
# heading/verdict-line shape ``lab_verdict`` and ``lab_report`` match against
# (see ``_section_bounds``/``find_verdict_line`` in ``bridge.lab.cli``), but
# with content the tests own outright -- never coupled to the live document's
# real-world resolution state.
SYNTHETIC_DOC_TEXT = """\
# Live-transport experiments E-H (test fixture)

### Running with `bridge lab`

This is a synthetic stand-in for the real experiments document, used only to
exercise `bridge lab verdict`/`bridge lab report` in tests.

## Experiment E — Claude Channel delivery and reply

**Question.** Does an event delivered over a Claude development Channel reach
the exact idle session?

Verdict: TBD (requires live Claude session + human observer)

---

## Experiment F — Codex App Server shared control

**Question.** Can a Bridge client and a remote Codex TUI share one App Server?

Verdict: TBD (requires live Codex session + human observer)

---

## Experiment G — Busy-session serialization

**Question.** Is an inbound call delivered during an unrelated active turn
serialized with no accidental `turn/steer`?

Verdict: TBD (requires live Claude+Codex sessions + human observer)

---

## Experiment H — Lifecycle and reconnect

**Question.** How do session ids, reachability, queued deadlines, and
resumption behave across restarts?

Verdict: TBD (requires live Claude+Codex sessions + human observer)
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class Out:
    """Collects printed lines instead of writing to stdout."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        self.lines.extend(str(line).splitlines() or [""])

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _healthy_report() -> DoctorReport:
    report = DoctorReport()
    report.add("router home permissions", OK, "0o700")
    report.add("Claude Channel mode", OK, "plugin channel active")
    return report


def _broken_report() -> DoctorReport:
    report = DoctorReport()
    report.add("router token permissions", FAIL, "0o644")
    return report


@pytest.fixture
def fake_vendor_env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    calls = tmp_path / "vendor-calls.jsonl"
    claude = make_capture_exe(bindir, "claude", calls, extra='print("claude 9.9.9 (fake)")')
    codex = make_capture_exe(bindir, "codex", calls, extra='print("codex-cli 0.42.0 (fake)")')
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": "/nonexistent",
        "BRIDGE_CLAUDE_BIN": str(claude),
        "BRIDGE_CODEX_BIN": str(codex),
    }


@pytest.fixture
def temp_doc(tmp_path: Path) -> Path:
    dest = tmp_path / "doc" / REAL_DOC.name
    dest.parent.mkdir(parents=True)
    dest.write_text(SYNTHETIC_DOC_TEXT, encoding="utf-8")
    return dest


def _wait_reachable(client, sid, *, state=None, timeout=3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in client.call("roster", {})["sessions"]:
            if s["id"] == sid and s["reachable"] and (state is None or s["state"] == state):
                return True
        time.sleep(0.02)
    return False


def _make_claude(rr, paths, session_id, *, auto_reply=None):
    host_sock, adapter_sock = socket.socketpair()
    adapter = ClaudeChannelAdapter(session_id, adapter_sock, paths=paths)
    adapter.connect_router(
        lambda on_event: rr.client(session_id=session_id, role="adapter", on_event=on_event)
    )
    adapter.start()
    host = FakeClaudeHost(host_sock, auto_reply=auto_reply)
    host.initialize()
    host.initialized()
    return adapter, host


def _make_codex(rr, session_id, *, auto_complete=True):
    client_sock, server_sock = socket.socketpair()
    server = FakeCodexAppServer(server_sock, cwd=CODEX_CWD, auto_complete=auto_complete)
    app = CodexAppServerClient(client_sock, cwd=CODEX_CWD).start()
    adapter = CodexAdapter(session_id, app)
    adapter.connect_router(
        lambda on_event: rr.client(session_id=session_id, role="adapter", on_event=on_event)
    )
    adapter.start()
    # `turn/*`/`item/*` are subscriber-only; the adapter does not subscribe
    # itself yet (Task 5), so F/G would see no turn frames without this.
    deadline = time.time() + 3.0
    while time.time() < deadline and not app.thread_id:
        time.sleep(0.02)
    app.subscribe()
    return adapter, server


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_records_versions_and_opens_a_run(tmp_path, paths, fake_vendor_env):
    runs = tmp_path / "runs"
    out = Out()
    rc = lab_prepare(
        runs_dir=runs,
        paths=paths,
        env=fake_vendor_env,
        doctor_fn=_healthy_report,
        now=lambda: 1756_000_000.0,
        out=out,
    )
    assert rc == 0

    run_dirs = list(runs.iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert run_dir.name.endswith("Z")

    versions = json.loads((run_dir / "versions.json").read_text())
    assert versions["bridge"]
    assert versions["claude"]["version"] == "claude 9.9.9 (fake)"
    assert versions["codex"]["version"] == "codex-cli 0.42.0 (fake)"
    assert versions["claude"]["binary"] == fake_vendor_env["BRIDGE_CLAUDE_BIN"]
    assert versions["doctor"]["ok"] is True
    assert [c["name"] for c in versions["doctor"]["checks"]][0] == "router home permissions"

    for name in EXPERIMENTS:
        assert (run_dir / f"{name}.jsonl").exists()
    assert capture.CAPTURE_ENV in out.text
    assert capture.CAPTURE_FULL_ENV not in out.text


def test_prepare_full_mentions_the_body_capture_switch(tmp_path, paths, fake_vendor_env):
    out = Out()
    rc = lab_prepare(
        runs_dir=tmp_path / "runs",
        paths=paths,
        env=fake_vendor_env,
        doctor_fn=_healthy_report,
        full=True,
        out=out,
    )
    assert rc == 0
    assert capture.CAPTURE_FULL_ENV in out.text
    run_dir = next((tmp_path / "runs").iterdir())
    assert json.loads((run_dir / "versions.json").read_text())["capture_full"] is True


def test_prepare_refuses_when_doctor_fails(tmp_path, paths, fake_vendor_env):
    runs = tmp_path / "runs"
    out = Out()
    rc = lab_prepare(
        runs_dir=runs,
        paths=paths,
        env=fake_vendor_env,
        doctor_fn=_broken_report,
        out=out,
    )
    assert rc == 1
    assert "router token permissions" in out.text
    assert "doctor reported FAIL" in out.text
    assert not runs.exists()


def test_prepare_records_a_missing_vendor_binary(tmp_path, paths):
    out = Out()
    rc = lab_prepare(
        runs_dir=tmp_path / "runs",
        paths=paths,
        env={"HOME": str(tmp_path), "PATH": "/nonexistent"},
        doctor_fn=_healthy_report,
        out=out,
    )
    assert rc == 0
    versions = json.loads((next((tmp_path / "runs").iterdir()) / "versions.json").read_text())
    assert "not found" in versions["claude"]["version"]
    assert "not found" in versions["codex"]["version"]


# ---------------------------------------------------------------------------
# run E
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "runs" / "20260905T000000Z"
    d.mkdir(parents=True)
    monkeypatch.setenv(capture.CAPTURE_ENV, str(d))
    monkeypatch.delenv(capture.CAPTURE_FULL_ENV, raising=False)
    return d


def _summary(run_dir: Path, name: str) -> dict:
    records = capture.read_capture(run_dir / f"{name}.jsonl")
    summaries = [r for r in records if r.get("record") == "summary"]
    assert len(summaries) == 1
    return summaries[0]


def test_run_e_sees_the_channel_notification_and_the_reply(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, host = _make_claude(rr, paths, "claude-1", auto_reply="experiment E ok")
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "claude-1")
            rc = lab_run(
                "E",
                run_dir=run_dir,
                claude_id="claude-1",
                timeout_s=8.0,
                connect=lambda: rr.client(session_id="bridge-lab"),
                out=out,
            )
        finally:
            adapter.close()
            host.close()

    assert rc == 0, out.text
    assert "Experiment E - Claude Channel delivery and reply." in out.lines
    summary = _summary(run_dir, "E")
    assert summary["ok"] is True
    assert summary["reply_recorded"] is True
    assert summary["channel_notifications"] >= 1
    assert summary["call_status"] == "answered"
    assert summary["capture_present"] is True


def test_run_e_fails_when_nobody_replies(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, host = _make_claude(rr, paths, "claude-1")  # no auto_reply
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "claude-1")
            rc = lab_run(
                "E",
                run_dir=run_dir,
                claude_id="claude-1",
                timeout_s=1.0,
                connect=lambda: rr.client(session_id="bridge-lab"),
                out=out,
            )
        finally:
            adapter.close()
            host.close()

    assert rc == 1
    summary = _summary(run_dir, "E")
    assert summary["ok"] is False
    assert summary["reply_recorded"] is False
    # The envelope still reached the exact session; only the reply is missing.
    assert summary["channel_notifications"] >= 1


def test_run_e_without_a_claude_session_explains_itself(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        rc = lab_run(
            "E",
            run_dir=run_dir,
            timeout_s=1.0,
            connect=lambda: rr.client(session_id="lab"),
            out=out,
        )
    assert rc == 2
    assert "no reachable claude session" in out.text


# ---------------------------------------------------------------------------
# run F
# ---------------------------------------------------------------------------


def test_run_f_correlates_turn_started_to_turn_completed(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, server = _make_codex(rr, "codex-1")
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "codex-1", state="idle")
            rc = lab_run(
                "F",
                run_dir=run_dir,
                codex_id="codex-1",
                timeout_s=8.0,
                connect=lambda: rr.client(session_id="bridge-lab"),
                out=out,
            )
        finally:
            adapter.close()
            server.close()

    assert rc == 0, out.text
    summary = _summary(run_dir, "F")
    assert summary["ok"] is True
    assert summary["correlated"], summary
    assert summary["correlated"][0] in summary["started"]
    assert summary["correlated"][0] in summary["completed"]
    assert summary["turn_start_requests"] >= 1


def test_run_f_fails_when_the_turn_never_completes(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, server = _make_codex(rr, "codex-1", auto_complete=False)
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "codex-1", state="idle")
            rc = lab_run(
                "F",
                run_dir=run_dir,
                codex_id="codex-1",
                timeout_s=0.5,
                connect=lambda: rr.client(session_id="bridge-lab"),
                out=out,
            )
        finally:
            adapter.close()
            server.close()

    assert rc == 1
    summary = _summary(run_dir, "F")
    assert summary["started"]  # the turn did start
    assert summary["completed"] == []  # it never completed
    assert summary["correlated"] == []


def test_correlate_turns_pairs_ids_through_the_response():
    records = [
        {"frame": {"id": 7, "method": "turn/start", "params": {}}},
        {"frame": {"id": 7, "result": {"turn": {"id": "turn-1"}}}},
        {"frame": {"method": "turn/started", "params": {"turn": {"id": "turn-1"}}}},
        {"frame": {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}},
        {"frame": {"method": "turn/started", "params": {"turn": {"id": "other"}}}},
    ]
    result = correlate_turns(records)
    assert result["turn_ids"] == ["turn-1"]
    assert result["correlated"] == ["turn-1"]
    assert "other" in result["started"]


# ---------------------------------------------------------------------------
# run G
# ---------------------------------------------------------------------------


def test_run_g_holds_while_working_delivers_on_idle_and_sees_no_steer(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, server = _make_codex(rr, "codex-1")
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "codex-1", state="idle")

            server.emit_status("active")
            deadline = time.time() + 3.0
            while time.time() < deadline:
                entry = next(s for s in ctrl.call("roster", {})["sessions"] if s["id"] == "codex-1")
                if entry["state"] == "working":
                    break
                time.sleep(0.02)

            releaser = threading.Timer(0.4, lambda: server.emit_status("idle"))
            releaser.start()
            try:
                rc = lab_run(
                    "G",
                    run_dir=run_dir,
                    codex_id="codex-1",
                    timeout_s=8.0,
                    connect=lambda: rr.client(session_id="bridge-lab"),
                    out=out,
                )
            finally:
                releaser.cancel()
                releaser.join(timeout=2.0)
        finally:
            adapter.close()
            server.close()

    assert rc == 0, out.text
    summary = _summary(run_dir, "G")
    assert summary["target_became_busy"] is True
    assert summary["held_while_busy"] is True
    assert summary["delivered_on_idle"] is True
    assert summary["steer_frames"] == 0
    assert server.forbidden_calls == []
    assert "no turn/steer frame in the capture" in out.text


def test_run_g_hard_fails_on_any_turn_steer_frame(paths, run_dir):
    """A steer frame in the capture fails G even if everything else looked fine."""
    wire = run_dir / capture.CAPTURE_FILENAME
    capture.CaptureWriter(wire).on_frame(
        "codex-app-server", "out", {"jsonrpc": "2.0", "id": 1, "method": "turn/steer"}
    )
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, server = _make_codex(rr, "codex-1")
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "codex-1", state="idle")
            server.emit_status("active")
            releaser = threading.Timer(0.4, lambda: server.emit_status("idle"))
            releaser.start()
            try:
                rc = lab_run(
                    "G",
                    run_dir=run_dir,
                    codex_id="codex-1",
                    timeout_s=6.0,
                    connect=lambda: rr.client(session_id="bridge-lab"),
                    out=out,
                )
            finally:
                releaser.cancel()
                releaser.join(timeout=2.0)
        finally:
            adapter.close()
            server.close()

    assert rc == 1
    assert "turn/steer" in out.text
    assert _summary(run_dir, "G")["steer_frames"] == 1


def test_run_g_targets_the_explicit_claude_id_even_with_a_reachable_codex_session(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        claude_adapter, claude_host = _make_claude(rr, paths, "claude-1")
        codex_adapter, codex_server = _make_codex(rr, "codex-1")
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "claude-1")
            assert _wait_reachable(ctrl, "codex-1", state="idle")

            ctrl.call("update_state", {"session_id": "claude-1", "state": "working"})
            deadline = time.time() + 3.0
            while time.time() < deadline:
                entry = next(
                    s for s in ctrl.call("roster", {})["sessions"] if s["id"] == "claude-1"
                )
                if entry["state"] == "working":
                    break
                time.sleep(0.02)

            releaser = threading.Timer(
                0.4,
                lambda: ctrl.call("update_state", {"session_id": "claude-1", "state": "idle"}),
            )
            releaser.start()
            try:
                rc = lab_run(
                    "G",
                    run_dir=run_dir,
                    claude_id="claude-1",
                    timeout_s=8.0,
                    connect=lambda: rr.client(session_id="bridge-lab"),
                    out=out,
                )
            finally:
                releaser.cancel()
                releaser.join(timeout=2.0)
        finally:
            claude_adapter.close()
            claude_host.close()
            codex_adapter.close()
            codex_server.close()

    assert rc == 0, out.text
    summary = _summary(run_dir, "G")
    assert summary["session"] == "claude-1"
    assert summary["family"] == "claude"


def test_run_g_refuses_when_both_families_are_explicit(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        rc = lab_run(
            "G",
            run_dir=run_dir,
            claude_id="claude-1",
            codex_id="codex-1",
            timeout_s=1.0,
            connect=lambda: rr.client(session_id="bridge-lab"),
            out=out,
        )

    assert rc == 2
    assert "Experiment G targets one family per run; pass only --claude or only --codex" in (
        out.text
    )


def test_run_g_auto_picks_codex_and_announces_it_when_neither_is_explicit(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        adapter, server = _make_codex(rr, "codex-1")
        try:
            ctrl = rr.client(session_id="ctrl")
            assert _wait_reachable(ctrl, "codex-1", state="idle")

            server.emit_status("active")
            deadline = time.time() + 3.0
            while time.time() < deadline:
                entry = next(s for s in ctrl.call("roster", {})["sessions"] if s["id"] == "codex-1")
                if entry["state"] == "working":
                    break
                time.sleep(0.02)

            releaser = threading.Timer(0.4, lambda: server.emit_status("idle"))
            releaser.start()
            try:
                rc = lab_run(
                    "G",
                    run_dir=run_dir,
                    timeout_s=8.0,
                    connect=lambda: rr.client(session_id="bridge-lab"),
                    out=out,
                )
            finally:
                releaser.cancel()
                releaser.join(timeout=2.0)
        finally:
            adapter.close()
            server.close()

    assert rc == 0, out.text
    summary = _summary(run_dir, "G")
    assert summary["session"] == "codex-1"
    assert summary["family"] == "codex"
    assert (
        "G target: codex codex-1 (auto-picked; pass --claude or --codex to choose)" in out.text
    )


# ---------------------------------------------------------------------------
# run H
# ---------------------------------------------------------------------------


def test_build_h_steps_covers_present_families_only():
    keys = [s.key for s in build_h_steps("c1", None)]
    assert keys == ["kill-claude-channel", "restart-claude", "kill-router"]
    keys = [s.key for s in build_h_steps(None, "x1", router_kill=False)]
    assert keys == ["kill-codex-app-server", "restart-codex-tui"]
    assert build_h_steps("c1", "x1")[-1].kills_router is True


def test_run_h_records_reachability_transitions_with_a_scripted_prompter(paths, run_dir):
    out = Out()
    live: dict[str, object] = {}
    prompts: list[str] = []

    with RunningRouter(paths) as rr:
        adapter, host = _make_claude(rr, paths, "claude-1")
        live["adapter"], live["host"] = adapter, host
        ctrl = rr.client(session_id="ctrl")
        assert _wait_reachable(ctrl, "claude-1")

        def scripted(message: str) -> str:
            """Performs the kill the operator would have performed by hand."""
            step = len(prompts)
            prompts.append(message)
            if step == 0:  # kill the Claude Channel adapter
                live["adapter"].close()
                live["host"].close()
            elif step == 1:  # restart it under the same Bridge session id
                live["adapter"], live["host"] = _make_claude(rr, paths, "claude-1")
            elif step == 2:  # stop the router
                rr.server.stop()
                time.sleep(0.3)
            return ""

        try:
            rc = lab_run(
                "H",
                run_dir=run_dir,
                claude_id="claude-1",
                timeout_s=3.0,
                connect=lambda: rr.client(session_id="bridge-lab"),
                prompt=scripted,
                out=out,
            )
        finally:
            for key in ("adapter", "host"):
                obj = live.get(key)
                if obj is not None:
                    obj.close()

    assert rc == 0, out.text
    assert len(prompts) == 3
    summary = _summary(run_dir, "H")
    steps = {s["key"]: s for s in summary["steps"]}
    assert steps["kill-claude-channel"]["observed_reachable"] is True
    assert steps["kill-claude-channel"]["expected_reachable"] is False
    assert steps["restart-claude"]["observed_reachable"] is True
    assert steps["restart-claude"]["expected_reachable"] is True
    assert "dropped" in steps["kill-router"]["router_connection"]
    assert summary["ok"] is True

    # The transcript delta for each step is kept as evidence in H.jsonl.
    records = capture.read_capture(run_dir / "H.jsonl")
    lifecycle = [r for r in records if r.get("record") == "lifecycle-step"]
    assert [r["key"] for r in lifecycle] == [
        "kill-claude-channel",
        "restart-claude",
        "kill-router",
    ]
    kill_delta = lifecycle[0]["transcript_delta"]
    assert any(e.get("status") == "disconnected" for e in kill_delta), kill_delta


def test_run_h_without_any_session_explains_itself(paths, run_dir):
    out = Out()
    with RunningRouter(paths) as rr:
        rc = lab_run(
            "H",
            run_dir=run_dir,
            timeout_s=1.0,
            connect=lambda: rr.client(session_id="lab"),
            prompt=lambda _m: "",
            out=out,
        )
    assert rc == 2
    assert "needs at least one session" in out.text


# ---------------------------------------------------------------------------
# run: shared behaviour
# ---------------------------------------------------------------------------


def test_run_rejects_an_unknown_experiment(run_dir):
    out = Out()
    assert lab_run("Z", run_dir=run_dir, out=out) == 2
    assert "unknown experiment" in out.text


def test_run_requires_a_prepared_run_directory(tmp_path):
    with pytest.raises(LabError, match="bridge lab prepare"):
        lab_run("E", runs_dir=tmp_path / "empty", out=Out())


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def test_verdict_rewrites_the_matching_line_in_place(temp_doc, tmp_path):
    run_dir = tmp_path / "runs" / "20260905T000000Z"
    run_dir.mkdir(parents=True)
    out = Out()
    rc = lab_verdict(
        "F",
        "pass",
        "thread-abc turn-1 started and completed; correlated by turn_id",
        doc=temp_doc,
        run_dir=run_dir,
        out=out,
    )
    assert rc == 0
    text = temp_doc.read_text()
    assert "Verdict: PASS — thread-abc turn-1 started and completed" in text
    assert "20260905T000000Z" in text
    # Only F changed; the other three are untouched.
    assert text.count("Verdict: TBD") == 3
    assert "Verdict: TBD (requires live Codex session" not in text


def test_verdict_refuses_tbd_and_empty_evidence(temp_doc, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    before = temp_doc.read_text()
    for value, evidence in (("TBD", "whatever"), ("pass", ""), ("PASS", "TBD"), ("maybe", "x")):
        out = Out()
        assert lab_verdict("E", value, evidence, doc=temp_doc, run_dir=run_dir, out=out) == 2
        assert "refusing" in out.text
    assert temp_doc.read_text() == before


def test_verdict_rejects_an_unknown_experiment(temp_doc, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = Out()
    assert lab_verdict("Q", "PASS", "x", doc=temp_doc, run_dir=run_dir, out=out) == 2
    assert "unknown experiment" in out.text


def test_verdict_requires_a_run_directory(temp_doc, tmp_path):
    out = Out()
    rc = lab_verdict("E", "PASS", "evidence", doc=temp_doc, runs_dir=tmp_path / "none", out=out)
    assert rc == 2
    assert "bridge lab prepare" in out.text
    assert "Verdict: TBD" in temp_doc.read_text()


def test_replace_verdict_is_section_scoped():
    text = "## Experiment E — a\n\nVerdict: TBD (e)\n\n## Experiment F — b\n\nVerdict: TBD (f)\n"
    new, old = replace_verdict(text, "F", "Verdict: PASS — ok")
    assert old == "Verdict: TBD (f)"
    assert new.endswith("## Experiment F — b\n\nVerdict: PASS — ok\n")
    assert "## Experiment E — a\n\nVerdict: TBD (e)\n" in new


def test_replace_verdict_reports_a_missing_section():
    with pytest.raises(LabError, match="Experiment G"):
        replace_verdict("## Experiment E — a\n\nVerdict: TBD\n", "G", "Verdict: PASS — x")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_fails_while_any_verdict_is_tbd(temp_doc):
    out = Out()
    assert lab_report(doc=temp_doc, out=out) == 1
    assert "still TBD" in out.text
    assert "NOT satisfied" in out.text


def test_report_passes_once_all_four_are_resolved(temp_doc, tmp_path):
    run_dir = tmp_path / "runs" / "20260905T000000Z"
    run_dir.mkdir(parents=True)
    for name in EXPERIMENTS:
        assert (
            lab_verdict(
                name, "PASS", f"observed {name} live", doc=temp_doc, run_dir=run_dir, out=Out()
            )
            == 0
        )
    out = Out()
    assert lab_report(doc=temp_doc, out=out) == 0
    assert "four labeled verdicts, none TBD" in out.text
    assert out.text.count("PASS") >= 4


def test_report_notices_a_missing_verdict_line(tmp_path):
    doc = tmp_path / "short.md"
    doc.write_text("## Experiment E — a\n\nVerdict: PASS — ok\n")
    out = Out()
    assert lab_report(doc=doc, out=out) == 1
    assert "expected exactly 4" in out.text


def test_the_real_document_has_one_verdict_line_per_experiment_and_report_reflects_it():
    """The harness must never fabricate a verdict: only a human who ran the
    procedure on live sessions may resolve one, and a resolved line must carry
    a run reference proving it. This test tracks whatever the live document
    currently says (TBD or resolved) rather than assuming a fixed state."""
    text = REAL_DOC.read_text()
    verdict_lines = [ln for ln in text.splitlines() if ln.startswith("Verdict:")]
    assert len(verdict_lines) == 4

    for name, line in zip(EXPERIMENTS, verdict_lines, strict=True):
        assert (
            line.startswith("Verdict: TBD")
            or line.startswith("Verdict: PASS —")
            or line.startswith("Verdict: FAIL —")
        ), f"Experiment {name}: unexpected verdict line {line!r}"
        if not line.startswith("Verdict: TBD"):
            assert "docs/experiments/runs/" in line or "(run: " in line, (
                f"Experiment {name}: resolved verdict has no run reference: {line!r}"
            )

    any_tbd = any(line.startswith("Verdict: TBD") for line in verdict_lines)
    expected_rc = 1 if any_tbd else 0

    out = Out()
    assert lab_report(doc=REAL_DOC, out=out) == expected_rc
    assert find_repo_root(REAL_DOC.parent) == REAL_DOC.resolve().parents[2]
    # ... and the harness is documented there, without touching those lines.
    assert "Running with `bridge lab`" in text


# ---------------------------------------------------------------------------
# wiring + guardrails
# ---------------------------------------------------------------------------


def test_cli_registers_bridge_lab(temp_doc, capsys):
    from bridge.cli import main

    assert main(["lab", "report", "--doc", str(temp_doc)]) == 1
    assert "still TBD" in capsys.readouterr().out
    assert main(["lab"]) == 2
    assert "bridge lab {prepare,run,verdict,report}" in capsys.readouterr().out


def test_lab_only_ever_watches_for_the_forbidden_method():
    from bridge.codex_app_server import FORBIDDEN_METHODS
    from bridge.lab import cli as lab_cli

    assert lab_cli.FORBIDDEN_FRAME_METHODS == FORBIDDEN_METHODS
    source = Path(lab_cli.__file__).read_text()
    # The lab never names a forbidden method itself: it scans a capture for the
    # client module's tuple, so no method literal appears here at all.
    for method in FORBIDDEN_METHODS:
        assert f'"{method}"' not in source
