"""Task 11: end-to-end live smoke suite.

These tests are marked ``live`` and excluded from the default run (see
``addopts = -m 'not live'`` in pyproject). They require real ``claude`` and
``codex`` sessions launched via the Bridge wrappers, a working ``bridge
install``, and a human observing both TUIs. Run explicitly:

    BRIDGE_LIVE=1 pytest -m live tests/live/

They intentionally *skip* (never fabricate a pass) unless ``BRIDGE_LIVE=1`` and
both vendor binaries are present, so an accidental run in CI or a container is a
skip, not a false green. The human-observation items are tracked in
``docs/experiments/2026-08-26-e2e-checklist.md``.
"""

from __future__ import annotations

import os
import shutil

import pytest

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return (
        os.environ.get("BRIDGE_LIVE") == "1"
        and shutil.which("claude") is not None
        and shutil.which("codex") is not None
    )


requires_live = pytest.mark.skipif(
    not _live_enabled(),
    reason="live E2E requires BRIDGE_LIVE=1 and real claude+codex CLIs with a human observer",
)


@requires_live
def test_claude_to_codex_sync_call():
    pytest.skip("manual: drive a bridge claude session calling a bridge codex session")


@requires_live
def test_codex_to_claude_sync_call():
    pytest.skip("manual: drive a bridge codex session calling a bridge claude session")


@requires_live
def test_text_both_directions():
    pytest.skip("manual: text in both directions, no reply required")


@requires_live
def test_call_async_wakes_exact_caller():
    pytest.skip("manual: call_async in both directions wakes the exact caller")


@requires_live
def test_busy_target_waits_no_steer():
    pytest.skip("manual: a call to a busy target waits and never steers the active turn")


@requires_live
def test_hop_budget_and_rate_cap_live():
    pytest.skip("manual: callee cannot dial out while answering; 11th pair message rate-limited")


@requires_live
def test_killed_adapter_is_unreachable_never_headless():
    pytest.skip("manual: killing an adapter yields unreachable, never a headless answer")


@requires_live
def test_transcripts_agree():
    pytest.skip("manual: human transcripts and the Bridge transcript agree on every field")
