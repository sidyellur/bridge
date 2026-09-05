"""Task 9 verify (CLI): roster/transcript/text/call diagnostics and the
no-impersonation rule for outbound messages.
"""

from __future__ import annotations

import pytest

from bridge.cli import main
from bridge.cli_commands import cli_call, cli_text, cli_transcript
from bridge.roster import cli_roster

from .fakes.router_peer import RunningRouter


@pytest.fixture
def cli_env(paths, monkeypatch):
    monkeypatch.setenv("BRIDGE_HOME", str(paths.home))
    monkeypatch.delenv("BRIDGE_SESSION_ID", raising=False)
    return paths


def test_version_via_main(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_roster_when_router_stopped(cli_env, capsys):
    assert cli_roster() == 1
    assert "not running" in capsys.readouterr().out


def test_roster_lists_sessions(cli_env, capsys):
    with RunningRouter(cli_env) as rr:
        rr.client(session_id="s1").call(
            "register_session", {"session_id": "s1", "family": "claude", "state": "idle"}
        )
        assert cli_roster() == 0
        out = capsys.readouterr().out
        assert "s1" in out


def test_transcript_renders(cli_env, capsys):
    with RunningRouter(cli_env) as rr:
        rr.client(session_id="s1").call(
            "register_session", {"session_id": "s1", "family": "claude", "state": "idle"}
        )
        assert cli_transcript(limit=10) == 0


def test_text_requires_source_session(cli_env, capsys):
    with RunningRouter(cli_env):
        assert cli_text("b", "hi") == 2
        assert "source session" in capsys.readouterr().out


def test_call_requires_source_session(cli_env, capsys):
    with RunningRouter(cli_env):
        assert cli_call("b", "why?") == 2


def test_text_with_source_sends(cli_env, capsys, monkeypatch):
    with RunningRouter(cli_env) as rr:
        rr.client(session_id="ctrl").call(
            "register_session", {"session_id": "b", "family": "claude", "state": "idle"}
        )
        monkeypatch.setenv("BRIDGE_SESSION_ID", "codex-cli")
        # b is managed but not connected -> unreachable, exit code 1, but the
        # message is accepted by the router (source honored from env).
        rc = cli_text("b", "heads up")
        assert rc == 1
        assert "unreachable" in capsys.readouterr().out
