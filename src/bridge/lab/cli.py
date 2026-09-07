"""``bridge lab prepare|run|verdict|report``.

Task 1 of the v1 plan requires, for each of Experiments E-H, "raw commands,
installed versions, output, and one labeled ``Verdict:``". This module makes
that runnable:

``prepare``
    Record ``claude``/``codex``/``bridge`` versions, gate on ``bridge doctor``
    (FAIL refuses to proceed), and create
    ``docs/experiments/runs/<UTC timestamp>/`` holding ``versions.json`` and one
    capture target per experiment.
``run E|F|G|H``
    Print the exact human steps, fire that experiment's stimulus through the
    running router, wait for the observable outcome, and print what was seen.
``verdict E PASS|FAIL "<evidence>"``
    Rewrite the matching ``Verdict:`` line in the experiments document in
    place. It refuses to write ``TBD``: only a human who ran the procedure may
    resolve a verdict, and only with evidence.
``report``
    The Task 1 verify as an exit code: exactly four ``Verdict:`` lines, none
    ``TBD``.

The lab is an observer with one stimulus each. It never steers a turn, never
resumes a session, and never invents an answer.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..claude_channel import RPC_ENDPOINT_NAME as CLAUDE_CHANNEL_SOURCE
from ..codex_app_server import (
    FORBIDDEN_METHODS,
    N_ITEM_COMPLETED,
    N_ITEM_DELTA,
    N_ITEM_STARTED,
)
from ..codex_app_server import RPC_ENDPOINT_NAME as CODEX_APP_SERVER_SOURCE
from ..paths import Paths
from ..router import FRAME_SOURCE as ROUTER_SOURCE
from ..server import RPC_ENDPOINT_NAME as BRIDGE_MCP_SOURCE
from .capture import (
    CAPTURE_ENV,
    CAPTURE_FILENAME,
    CAPTURE_FULL_ENV,
    DIRECTION_OUT,
    CaptureTail,
    frame_method,
    frame_params,
)

EXPERIMENTS = ("E", "F", "G", "H")
DOC_RELPATH = Path("docs/experiments/2026-08-27-live-transport-semantics.md")
RUNS_RELPATH = Path("docs/experiments/runs")
VERDICT_PREFIX = "Verdict:"
LAB_SESSION_ID = "bridge-lab"

#: Never issued, never expected. Seeing one *sent by Bridge* fails Experiment G
#: outright, whatever else happened.
FORBIDDEN_FRAME_METHODS = FORBIDDEN_METHODS

#: The real ``RpcEndpoint``/``RouterServer`` source names Bridge's own
#: components capture frames under (``bridge/server.py``,
#: ``bridge/claude_channel.py``, ``bridge/codex_app_server.py``,
#: ``bridge/router.py``) -- referenced, not duplicated, so this can't drift
#: from the actual endpoint names. A shared capture file also carries frames
#: from whatever fake/test peer Bridge is talking to (e.g. a test's
#: ``fake-codex-app-server``, or bare endpoint names like ``left``/``right``
#: in unrelated tests); a forbidden method on one of *those* is not Bridge
#: steering anything and must never count.
BRIDGE_SOURCES = (CODEX_APP_SERVER_SOURCE, CLAUDE_CHANNEL_SOURCE, BRIDGE_MCP_SOURCE, ROUTER_SOURCE)

CHANNEL_NOTIFICATION = "notifications/claude/channel"


class LabError(Exception):
    """A user-facing problem: a missing document, run directory, or session."""


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def find_repo_root(start: str | os.PathLike[str] | None = None) -> Path | None:
    """Walk up from ``start`` looking for the experiments document."""
    here = Path(start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / DOC_RELPATH).exists():
            return candidate
    return None


def resolve_doc(
    explicit: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise LabError(f"experiments document not found: {path}")
        return path
    root = find_repo_root(start)
    if root is None:
        raise LabError(
            f"could not find {DOC_RELPATH} above {Path(start or Path.cwd())}; "
            "run `bridge lab` from a Bridge checkout or pass --doc"
        )
    return root / DOC_RELPATH


def resolve_runs_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
) -> Path:
    if explicit is not None:
        return Path(explicit)
    root = find_repo_root(start)
    if root is None:
        raise LabError(
            f"could not find {DOC_RELPATH} above {Path(start or Path.cwd())}; "
            "run `bridge lab` from a Bridge checkout or pass --runs-dir"
        )
    return root / RUNS_RELPATH


def latest_run_dir(runs_dir: Path) -> Path | None:
    if not runs_dir.exists():
        return None
    candidates = sorted(p for p in runs_dir.iterdir() if p.is_dir())
    return candidates[-1] if candidates else None


def resolve_run_dir(
    explicit: str | os.PathLike[str] | None = None,
    runs_dir: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise LabError(f"run directory not found: {path}")
        return path
    resolved = latest_run_dir(resolve_runs_dir(runs_dir, start=start))
    if resolved is None:
        raise LabError("no run directory yet; run `bridge lab prepare` first")
    return resolved


def _display_path(path: Path, *, start: str | os.PathLike[str] | None = None) -> str:
    root = find_repo_root(start)
    if root is not None:
        try:
            return str(path.resolve().relative_to(root))
        except ValueError:
            pass
    return str(path)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def probe_version(
    binary: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = 20.0,
) -> str:
    """``<binary> --version``, captured verbatim. Never raises."""
    try:
        proc = run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
        return f"<unavailable: {exc}>"
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return text or f"<no output, exit {proc.returncode}>"


def _binary_record(
    family: str,
    env: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess],
) -> dict[str, str]:
    from ..launch import resolve_binary

    try:
        binary = resolve_binary(family, dict(env))
    except FileNotFoundError as exc:
        return {"binary": "", "version": f"<not found: {exc}>"}
    return {"binary": binary, "version": probe_version(binary, run=run)}


def _default_doctor(paths: Paths, env: Mapping[str, str]):
    from ..doctor import doctor

    home = Path(env.get("HOME", str(Path.home())))
    return doctor(paths=paths, claude_home=home / ".claude", codex_home=home / ".codex")


def lab_prepare(
    *,
    runs_dir: str | os.PathLike[str] | None = None,
    paths: Paths | None = None,
    env: Mapping[str, str] | None = None,
    doctor_fn: Callable[[], Any] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    now: Callable[[], float] = time.time,
    full: bool = False,
    out: Callable[[str], None] = print,
    start: str | os.PathLike[str] | None = None,
) -> int:
    environ = os.environ if env is None else env
    runs = resolve_runs_dir(runs_dir, start=start)
    paths = paths or Paths.resolve(env=dict(environ))

    report = (doctor_fn or (lambda: _default_doctor(paths, environ)))()
    if not report.ok:
        out(report.render())
        out("")
        out("bridge lab prepare: doctor reported FAIL; fix the above before running experiments.")
        return 1

    versions = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
        "bridge": __version__,
        "claude": _binary_record("claude", environ, run),
        "codex": _binary_record("codex", environ, run),
        "capture_full": bool(full),
        "doctor": {
            "ok": report.ok,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail} for c in report.checks
            ],
        },
    }

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    run_dir = runs / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "versions.json").write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")
    for name in EXPERIMENTS:
        target = run_dir / f"{name}.jsonl"
        if not target.exists():
            target.write_text("", encoding="utf-8")

    out(f"run directory: {_display_path(run_dir, start=start)}")
    out(f"  bridge  {versions['bridge']}")
    out(f"  claude  {versions['claude']['version']}  ({versions['claude']['binary'] or 'n/a'})")
    out(f"  codex   {versions['codex']['version']}  ({versions['codex']['binary'] or 'n/a'})")
    out("  doctor  healthy")
    out("")
    out("Next, in each terminal that will host a session, export the capture directory")
    out("BEFORE launching the vendor, so both directions of the wire are recorded:")
    out(f"  export {CAPTURE_ENV}={run_dir.resolve()}")
    if full:
        out(f"  export {CAPTURE_FULL_ENV}=1   # message bodies kept verbatim")
    out("Then: `bridge claude` in one terminal, `bridge codex` in another, and")
    out("`bridge lab run E` (then F, G, H) in a third.")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

STEPS: dict[str, tuple[str, ...]] = {
    "E": (
        "Experiment E - Claude Channel delivery and reply.",
        "1. Have one `bridge claude` session open and IDLE (no turn in flight).",
        "2. Watch that exact conversation in its TUI.",
        "3. The lab now delivers a [bridge call] envelope to it over the Channel.",
        "4. When the envelope appears, let the session answer with reply(call_id, ...).",
        "5. Record verbatim any research-preview allowlist or organization-policy error.",
    ),
    "F": (
        "Experiment F - Codex App Server shared control.",
        "1. Have one `bridge codex` session open (its remote TUI attached to the",
        "   per-session App Server socket) and IDLE.",
        "2. Note the thread id the TUI displays; it must match the bound thread below.",
        "3. The lab now causes a turn/start on that exact bound thread.",
        "4. Watch the TUI: the turn must appear in the displayed thread, not a new one.",
    ),
    "G": (
        "Experiment G - Busy-session serialization.",
        "1. Start a long, unrelated turn in the target session NOW and leave it running.",
        "2. The lab waits until the roster reports that session as `working`, then",
        "   delivers a Bridge `text` while it is mid-turn.",
        "3. Watch the TUI: the text must NOT interrupt or steer the running turn.",
        "4. Let the turn finish. The text must then be delivered on the next idle.",
        "5. Run this once per family (`--codex <id>`, then `--claude <id>`).",
    ),
    "H": (
        "Experiment H - Lifecycle and reconnect.",
        "1. The lab walks you through killing one component at a time.",
        "2. After each prompt, do exactly what it says, then press Enter.",
        "3. The lab records the roster `reachable` transition and the transcript delta",
        "   (queued/expired/timeout call deadlines) it observed for that step.",
    ),
}


@dataclass
class LabContext:
    name: str
    run_dir: Path
    client: Any
    claude_id: str | None
    codex_id: str | None
    timeout_s: float
    tail: CaptureTail
    out: Callable[[str], None]
    prompt: Callable[[str], str]
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    seed_call: bool = False
    router_kill: bool = True
    explicit_claude: bool = False
    explicit_codex: bool = False
    records: list[dict[str, Any]] = field(default_factory=list)

    def observe(self) -> list[dict[str, Any]]:
        fresh = self.tail.read_new()
        self.records.extend(fresh)
        return fresh

    def deadline(self) -> float:
        return self.now() + self.timeout_s


def _default_connect(paths: Paths | None, env: Mapping[str, str] | None) -> Any:
    from ..router import is_running
    from ..router_client import RouterClient

    resolved = paths or Paths.resolve(env=dict(os.environ if env is None else env))
    if not is_running(resolved):
        raise LabError(
            "bridge router is not running; open a `bridge claude` / `bridge codex` session first"
        )
    return RouterClient.connect(resolved, session_id=LAB_SESSION_ID, role="client")


def _roster(ctx: LabContext) -> list[dict[str, Any]]:
    payload = ctx.client.call("roster", {})
    return list(payload.get("sessions", []))


def _pick(sessions: Sequence[Mapping[str, Any]], family: str) -> str | None:
    for s in sessions:
        if s.get("family") == family and s.get("reachable"):
            return str(s.get("id"))
    return None


def _resolve_session_ids(ctx: LabContext) -> None:
    if ctx.claude_id and ctx.codex_id:
        return
    try:
        sessions = _roster(ctx)
    except Exception as exc:  # noqa: BLE001 - a dead router is reported, not raised here
        ctx.out(f"could not read the roster: {exc}")
        return
    ctx.claude_id = ctx.claude_id or _pick(sessions, "claude")
    ctx.codex_id = ctx.codex_id or _pick(sessions, "codex")


def _session_state(ctx: LabContext, session_id: str) -> Mapping[str, Any] | None:
    for s in _roster(ctx):
        if s.get("id") == session_id:
            return s
    return None


def _wait_for_state(ctx: LabContext, session_id: str, state: str, timeout: float) -> bool:
    end = ctx.now() + timeout
    while ctx.now() < end:
        entry = _session_state(ctx, session_id)
        if entry is not None and entry.get("state") == state:
            return True
        ctx.sleep(0.05)
    return False


def _wait_for_reachable(ctx: LabContext, session_id: str, want: bool, timeout: float) -> bool:
    end = ctx.now() + timeout
    while ctx.now() < end:
        entry = _session_state(ctx, session_id)
        if entry is not None and bool(entry.get("reachable")) == want:
            return True
        if entry is None and want is False:
            return True
        ctx.sleep(0.05)
    return False


def _transcript(ctx: LabContext, limit: int = 100) -> list[dict[str, Any]]:
    payload = ctx.client.call("transcript", {"limit": limit})
    return list(payload.get("entries", []))


def _entry_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("ts"),
        entry.get("kind"),
        entry.get("status"),
        entry.get("from"),
        entry.get("to"),
        entry.get("call_id"),
    )


def _transcript_keys(entries: Sequence[Mapping[str, Any]]) -> set[tuple[Any, ...]]:
    return {_entry_key(e) for e in entries}


def _new_entries(
    entries: Sequence[Mapping[str, Any]], seen: set[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    """New transcript entries, oldest first. The router returns newest first and
    caps the window, so a positional diff would be wrong."""
    return [dict(e) for e in reversed(list(entries)) if _entry_key(e) not in seen]


def _steer_frames(ctx: LabContext) -> list[dict[str, Any]]:
    """Forbidden-method frames that Bridge's own endpoints *sent*.

    A capture file is shared: it also holds frames from whatever peer Bridge
    is talking to (a live vendor CLI, or a test's fake App Server / arbitrary
    endpoint names). A forbidden method arriving *from* that peer, or
    appearing under a source Bridge never registers, is not Bridge steering
    anything -- only ``direction == "out"`` from one of :data:`BRIDGE_SOURCES`
    counts against Experiment G.
    """
    return [
        r
        for r in ctx.tail.all_records()
        if frame_method(r) in FORBIDDEN_FRAME_METHODS
        and r.get("direction") == DIRECTION_OUT
        and r.get("source") in BRIDGE_SOURCES
    ]


def _turn_id(container: Mapping[str, Any]) -> Any:
    """The turn id lives at ``turn.id`` everywhere in the real contract."""
    turn = container.get("turn")
    return turn.get("id") if isinstance(turn, Mapping) else None


_ITEM_NOTIFICATION_METHODS = (N_ITEM_STARTED, N_ITEM_DELTA, N_ITEM_COMPLETED)


def correlate_turns(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pair ``turn/start`` requests with their ``turn/started`` /
    ``turn/completed`` notifications through the returned ``turn_id``, and
    separately collect the turn ids seen on ``item/*`` notifications."""
    request_ids: set[Any] = set()
    started: set[str] = set()
    completed: set[str] = set()
    items: list[str] = []
    seen_items: set[str] = set()
    for rec in records:
        method = frame_method(rec)
        frame = rec.get("frame")
        if not isinstance(frame, Mapping):
            continue
        if method == "turn/start" and "id" in frame:
            request_ids.add(frame["id"])
        elif method == "turn/started":
            turn_id = _turn_id(frame_params(rec))
            if isinstance(turn_id, str):
                started.add(turn_id)
        elif method == "turn/completed":
            turn_id = _turn_id(frame_params(rec))
            if isinstance(turn_id, str):
                completed.add(turn_id)
        elif method in _ITEM_NOTIFICATION_METHODS:
            turn_id = frame_params(rec).get("turnId")
            if isinstance(turn_id, str) and turn_id and turn_id not in seen_items:
                seen_items.add(turn_id)
                items.append(turn_id)

    turn_ids: list[str] = []
    for rec in records:
        frame = rec.get("frame")
        if not isinstance(frame, Mapping) or "method" in frame:
            continue
        if frame.get("id") not in request_ids:
            continue
        result = frame.get("result")
        if isinstance(result, Mapping):
            turn_id = _turn_id(result)
            if isinstance(turn_id, str) and turn_id and turn_id not in turn_ids:
                turn_ids.append(turn_id)

    return {
        "turn_start_requests": len(request_ids),
        "turn_ids": turn_ids,
        "started": sorted(started),
        "completed": sorted(completed),
        "items": items,
        "correlated": [t for t in turn_ids if t in started and t in completed],
    }


# --- experiment bodies -----------------------------------------------------

E_QUESTION = (
    "[bridge lab] Experiment E: confirm you received this call over the Bridge "
    "Channel by replying with a one-line acknowledgement."
)
F_MESSAGE = (
    "[bridge lab] Experiment F: this text should appear as a turn in the exact "
    "thread your remote TUI is displaying. No action needed."
)
G_MESSAGE = (
    "[bridge lab] Experiment G: delivered while you were mid-turn. It must not "
    "have interrupted that turn. No action needed."
)


def _require_session(ctx: LabContext, session_id: str | None, family: str) -> str:
    if not session_id:
        raise LabError(
            f"no reachable {family} session found; open one with `bridge {family}` "
            f"or pass --{family} <id>"
        )
    return session_id


def _channel_meta(record: Mapping[str, Any]) -> dict[str, Any]:
    """A channel notification carries its routing fields in ``params.meta``."""
    meta = frame_params(record).get("meta")
    return meta if isinstance(meta, dict) else {}


def _run_e(ctx: LabContext) -> tuple[int, dict[str, Any]]:
    target = _require_session(ctx, ctx.claude_id, "claude")
    ctx.tail.mark()
    ctx.out(f"stimulus: delivering a [bridge call] to {target} (timeout {int(ctx.timeout_s)}s)")

    from ..router_client import RouterClientError

    try:
        result = ctx.client.call(
            "call",
            {"to": target, "question": E_QUESTION, "timeout_s": int(ctx.timeout_s)},
            timeout=ctx.timeout_s + 5,
        )
    except RouterClientError as exc:
        result = {"status": "error", "reason": f"{exc.code}: {exc.message}"}

    ctx.sleep(0.1)  # let the last frames land in the capture file
    ctx.observe()
    call_id = result.get("call_id")
    notifications = [
        r
        for r in ctx.records
        if frame_method(r) == CHANNEL_NOTIFICATION and _channel_meta(r).get("kind") == "call"
    ]
    matched = [n for n in notifications if _channel_meta(n).get("call_id") == call_id]
    replied = result.get("status") == "answered"

    ctx.out(f"observed: channel notifications for this call: {len(matched)}")
    ctx.out(f"observed: call status: {result.get('status')}")
    if result.get("reason"):
        ctx.out(f"observed: reason: {result['reason']}")
    if replied:
        answered_by = (result.get("meta") or {}).get("answered_by")
        ctx.out(f"observed: reply recorded (answered_by={answered_by})")
    if not ctx.tail.exists():
        ctx.out(f"warning: no capture file; is {CAPTURE_ENV} exported in the session's terminal?")

    ok = bool(matched) and replied
    return (0 if ok else 1), {
        "session": target,
        "call_id": call_id,
        "call_status": result.get("status"),
        "channel_notifications": len(matched),
        "reply_recorded": replied,
        "capture_present": ctx.tail.exists(),
        "ok": ok,
    }


def _run_f(ctx: LabContext) -> tuple[int, dict[str, Any]]:
    target = _require_session(ctx, ctx.codex_id, "codex")
    entry = _session_state(ctx, target) or {}
    ctx.tail.mark()
    ctx.out(f"stimulus: turn/start on the thread bound to {target}")

    result = ctx.client.call("text", {"to": target, "message": F_MESSAGE})
    ctx.out(f"observed: admission status: {result.get('status')}")

    end = ctx.deadline()
    correlation = correlate_turns(ctx.records)
    while ctx.now() < end:
        ctx.observe()
        correlation = correlate_turns(ctx.records)
        if correlation["correlated"]:
            break
        ctx.sleep(0.05)

    ctx.out(f"observed: turn/start requests: {correlation['turn_start_requests']}")
    ctx.out(f"observed: turn ids from responses: {correlation['turn_ids'] or '(none)'}")
    ctx.out(f"observed: turn/started: {correlation['started'] or '(none)'}")
    ctx.out(f"observed: turn/completed: {correlation['completed'] or '(none)'}")
    ctx.out(f"observed: correlated start->completed: {correlation['correlated'] or '(none)'}")
    ctx.out(f"observed: item notifications for turn ids: {correlation['items'] or '(none)'}")
    if not ctx.tail.exists():
        ctx.out(f"warning: no capture file; is {CAPTURE_ENV} exported in the session's terminal?")

    ok = bool(correlation["correlated"]) and result.get("status") in ("queued", "delivered")
    return (0 if ok else 1), {
        "session": target,
        "state_before": entry.get("state"),
        "admission_status": result.get("status"),
        "capture_present": ctx.tail.exists(),
        **correlation,
        "ok": ok,
    }


def _run_g(ctx: LabContext) -> tuple[int, dict[str, Any]]:
    if ctx.explicit_claude and ctx.explicit_codex:
        raise LabError(
            "Experiment G targets one family per run; pass only --claude or only --codex"
        )
    if ctx.explicit_claude:
        target, family = ctx.claude_id, "claude"
    elif ctx.explicit_codex:
        target, family = ctx.codex_id, "codex"
    else:
        target = ctx.codex_id or ctx.claude_id
        family = "codex" if target == ctx.codex_id else "claude"
        if target:
            ctx.out(
                f"G target: {family} {target} (auto-picked; pass --claude or --codex to choose)"
            )
    if not target:
        raise LabError("Experiment G needs a target; pass --codex <id> or --claude <id>")
    ctx.tail.mark()

    ctx.out(f"waiting for {target} ({family}) to report state=working ...")
    became_busy = _wait_for_state(ctx, target, "working", ctx.timeout_s)
    if not became_busy:
        ctx.out("observed: the target never reported `working`; start a long turn and retry")
    else:
        ctx.out("observed: target is mid-turn")

    seen = _transcript_keys(_transcript(ctx))

    ctx.out(f"stimulus: delivering a Bridge text to {target} while it is working")
    result = ctx.client.call("text", {"to": target, "message": G_MESSAGE})
    held = result.get("status") == "queued"
    ctx.out(f"observed: admission status: {result.get('status')} (held while busy: {held})")

    ctx.out("waiting for the turn to finish and the held text to be delivered on idle ...")
    end = ctx.deadline()
    delivered = False
    while ctx.now() < end:
        ctx.observe()
        fresh = _new_entries(_transcript(ctx), seen)
        if any(e.get("kind") == "text" and e.get("status") == "delivered" for e in fresh):
            delivered = True
            break
        ctx.sleep(0.05)
    ctx.observe()
    ctx.out(f"observed: delivered after idle: {delivered}")

    steer = _steer_frames(ctx)
    if steer:
        ctx.out(
            f"FAIL: {len(steer)} forbidden frame(s) sent by Bridge - "
            "Bridge must never steer or interrupt"
        )
    else:
        ctx.out("observed: no turn/steer frame sent by Bridge")

    ok = became_busy and held and delivered and not steer
    return (0 if ok else 1), {
        "session": target,
        "family": family,
        "target_became_busy": became_busy,
        "admission_status": result.get("status"),
        "held_while_busy": held,
        "delivered_on_idle": delivered,
        "steer_frames": len(steer),
        "capture_present": ctx.tail.exists(),
        "ok": ok,
    }


@dataclass(frozen=True)
class LifecycleStep:
    key: str
    instruction: str
    session_id: str | None = None
    expect_reachable: bool | None = None
    kills_router: bool = False


def build_h_steps(
    claude_id: str | None, codex_id: str | None, *, router_kill: bool = True
) -> list[LifecycleStep]:
    steps: list[LifecycleStep] = []
    if claude_id:
        steps.append(
            LifecycleStep(
                "kill-claude-channel",
                f"Kill the Claude Channel adapter for {claude_id} "
                "(quit the `bridge claude` wrapper), then press Enter.",
                claude_id,
                False,
            )
        )
        steps.append(
            LifecycleStep(
                "restart-claude",
                f"Restart it with `bridge claude --session-id {claude_id}`, then press Enter.",
                claude_id,
                True,
            )
        )
    if codex_id:
        steps.append(
            LifecycleStep(
                "kill-codex-app-server",
                f"Kill the Codex App Server behind {codex_id} "
                "(kill the `codex app-server` process), then press Enter.",
                codex_id,
                False,
            )
        )
        steps.append(
            LifecycleStep(
                "restart-codex-tui",
                f"Bring {codex_id} back (restart `bridge codex`), then press Enter.",
                codex_id,
                True,
            )
        )
    if router_kill:
        steps.append(
            LifecycleStep(
                "kill-router",
                "Finally, stop the router with `bridge router stop`, then press Enter. "
                "The lab's own connection is expected to drop here; that is the "
                "observation.",
                None,
                None,
                kills_router=True,
            )
        )
    return steps


def _run_h(ctx: LabContext) -> tuple[int, dict[str, Any]]:
    if not (ctx.claude_id or ctx.codex_id):
        raise LabError("Experiment H needs at least one session; pass --claude and/or --codex")

    if ctx.seed_call:
        seed_target = ctx.claude_id or ctx.codex_id
        seeded = ctx.client.call(
            "call_async",
            {"to": seed_target, "question": "[bridge lab] Experiment H deadline probe."},
        )
        ctx.out(f"seeded an async call to {seed_target}: {seeded.get('status')}")

    steps = build_h_steps(ctx.claude_id, ctx.codex_id, router_kill=ctx.router_kill)
    seen = _transcript_keys(_transcript(ctx))
    observations: list[dict[str, Any]] = []
    ok = True

    from ..router_client import RouterClientError

    for step in steps:
        ctx.out("")
        ctx.out(f"[{step.key}] {step.instruction}")
        ctx.prompt("press Enter when done: ")

        observation: dict[str, Any] = {
            "record": "lifecycle-step",
            "key": step.key,
            "session": step.session_id,
            "expected_reachable": step.expect_reachable,
        }

        if step.kills_router:
            try:
                # Bounded: a router that was stopped never answers, and the
                # harness must not hang on its own last observation.
                ctx.client.call("roster", {}, timeout=min(5.0, max(1.0, ctx.timeout_s)))
                observation["router_connection"] = "still answering"
                observation["ok"] = False
                ok = False
                ctx.out("observed: the router still answered; it was not stopped")
            except RouterClientError as exc:
                observation["router_connection"] = f"dropped ({exc.code})"
                observation["ok"] = True
                ctx.out(f"observed: the lab's router connection dropped ({exc.code}), as expected")
            observations.append(observation)
            ctx.records.append(observation)
            break

        want = bool(step.expect_reachable)
        matched = _wait_for_reachable(ctx, step.session_id or "", want, ctx.timeout_s)
        try:
            entries = _transcript(ctx)
        except RouterClientError as exc:
            entries = []
            observation["transcript_error"] = exc.message
        delta = _new_entries(entries, seen)
        seen |= _transcript_keys(entries)

        observation["observed_reachable"] = matched
        observation["transcript_delta"] = delta
        observation["ok"] = matched
        observations.append(observation)
        ctx.records.append(observation)
        ok = ok and matched

        ctx.out(
            f"observed: reachable={step.expect_reachable} within {int(ctx.timeout_s)}s: {matched}"
        )
        for entry in delta:
            ctx.out(
                f"  transcript: {entry.get('kind')}/{entry.get('status')} "
                f"{entry.get('from') or '-'} -> {entry.get('to') or '-'} "
                f"{entry.get('call_id') or ''}".rstrip()
            )
        if not delta:
            ctx.out("  transcript: (no new entries)")

    if ctx.router_kill and not any(o["key"] == "kill-router" for o in observations):
        ctx.out("")
        ctx.out("note: the router-kill step was not reached.")

    return (0 if ok else 1), {
        "claude": ctx.claude_id,
        "codex": ctx.codex_id,
        "steps": [{k: v for k, v in o.items() if k != "transcript_delta"} for o in observations],
        "ok": ok,
    }


_RUNNERS: dict[str, Callable[[LabContext], tuple[int, dict[str, Any]]]] = {
    "E": _run_e,
    "F": _run_f,
    "G": _run_g,
    "H": _run_h,
}


def lab_run(
    name: str,
    *,
    run_dir: str | os.PathLike[str] | None = None,
    runs_dir: str | os.PathLike[str] | None = None,
    paths: Paths | None = None,
    env: Mapping[str, str] | None = None,
    claude_id: str | None = None,
    codex_id: str | None = None,
    timeout_s: float = 60.0,
    connect: Callable[[], Any] | None = None,
    prompt: Callable[[str], str] | None = None,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    seed_call: bool = False,
    router_kill: bool = True,
    start: str | os.PathLike[str] | None = None,
) -> int:
    key = (name or "").strip().upper()
    if key not in EXPERIMENTS:
        out(f"unknown experiment {name!r}; expected one of {', '.join(EXPERIMENTS)}")
        return 2
    resolved_run = resolve_run_dir(run_dir, runs_dir, start=start)

    for line in STEPS[key]:
        out(line)
    out("")

    client = (connect or (lambda: _default_connect(paths, env)))()
    ctx = LabContext(
        name=key,
        run_dir=resolved_run,
        client=client,
        claude_id=claude_id,
        codex_id=codex_id,
        timeout_s=timeout_s,
        tail=CaptureTail(resolved_run / CAPTURE_FILENAME),
        out=out,
        prompt=prompt or (lambda msg: input(msg)),
        sleep=sleep,
        now=now,
        seed_call=seed_call,
        router_kill=router_kill,
        explicit_claude=claude_id is not None,
        explicit_codex=codex_id is not None,
    )
    try:
        _resolve_session_ids(ctx)
        rc, summary = _RUNNERS[key](ctx)
    except LabError as exc:
        out(str(exc))
        return 2
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - the router may already be gone (Experiment H)
            pass

    summary = {"record": "summary", "experiment": key, **summary}
    _write_records(resolved_run / f"{key}.jsonl", ctx.records, summary)
    out("")
    out(f"evidence: {_display_path(resolved_run / f'{key}.jsonl', start=start)}")
    out(f"result: Experiment {key} {'looks PASS-shaped' if rc == 0 else 'did NOT pass'}")
    out(
        "Review the TUIs and the evidence yourself, then record the verdict:\n"
        f'  bridge lab verdict {key} PASS "<evidence>"'
    )
    return rc


def _write_records(
    path: Path, records: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=str) + "\n")
        fh.write(json.dumps(summary, default=str) + "\n")


# ---------------------------------------------------------------------------
# verdict / report
# ---------------------------------------------------------------------------


def _section_bounds(lines: Sequence[str], name: str) -> tuple[int, int]:
    heading = f"## Experiment {name}"
    start = None
    for i, line in enumerate(lines):
        if line.startswith(heading):
            start = i
            break
    if start is None:
        raise LabError(f"no '## Experiment {name}' heading in the experiments document")
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            return start, j
    return start, len(lines)


def find_verdict_line(text: str, name: str) -> tuple[int, str]:
    lines = text.splitlines()
    start, end = _section_bounds(lines, name)
    for i in range(start, end):
        if lines[i].strip().startswith(VERDICT_PREFIX):
            return i, lines[i]
    raise LabError(f"Experiment {name} has no '{VERDICT_PREFIX}' line")


def replace_verdict(text: str, name: str, new_line: str) -> tuple[str, str]:
    lines = text.splitlines()
    index, old = find_verdict_line(text, name)
    lines[index] = new_line
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + trailing, old


def lab_verdict(
    name: str,
    value: str,
    evidence: str,
    *,
    doc: str | os.PathLike[str] | None = None,
    run_dir: str | os.PathLike[str] | None = None,
    runs_dir: str | os.PathLike[str] | None = None,
    out: Callable[[str], None] = print,
    start: str | os.PathLike[str] | None = None,
) -> int:
    key = (name or "").strip().upper()
    verdict = (value or "").strip().upper()
    body = (evidence or "").strip()

    if key not in EXPERIMENTS:
        out(f"unknown experiment {name!r}; expected one of {', '.join(EXPERIMENTS)}")
        return 2
    if verdict not in ("PASS", "FAIL"):
        out(
            f"refusing to record {value!r}: a verdict is PASS or FAIL. "
            "TBD is what the document already says; only a human who ran the "
            "experiment may resolve it."
        )
        return 2
    if not body or body.upper() == "TBD" or "TBD" in body.upper().split():
        out("refusing to record a verdict without real evidence (and never 'TBD')")
        return 2

    try:
        doc_path = resolve_doc(doc, start=start)
        run_path = resolve_run_dir(run_dir, runs_dir, start=start)
    except LabError as exc:
        out(str(exc))
        return 2

    line = f"{VERDICT_PREFIX} {verdict} — {body} (run: {_display_path(run_path, start=start)})"
    try:
        new_text, old = replace_verdict(doc_path.read_text(encoding="utf-8"), key, line)
    except LabError as exc:
        out(str(exc))
        return 2
    doc_path.write_text(new_text, encoding="utf-8")
    out(f"{doc_path}")
    out(f"- {old.strip()}")
    out(f"+ {line}")
    return 0


def lab_report(
    *,
    doc: str | os.PathLike[str] | None = None,
    out: Callable[[str], None] = print,
    start: str | os.PathLike[str] | None = None,
) -> int:
    try:
        doc_path = resolve_doc(doc, start=start)
    except LabError as exc:
        out(str(exc))
        return 1
    text = doc_path.read_text(encoding="utf-8")
    all_lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(VERDICT_PREFIX)]

    problems: list[str] = []
    if len(all_lines) != len(EXPERIMENTS):
        problems.append(
            f"expected exactly {len(EXPERIMENTS)} '{VERDICT_PREFIX}' lines, found {len(all_lines)}"
        )
    for name in EXPERIMENTS:
        try:
            _, line = find_verdict_line(text, name)
        except LabError as exc:
            problems.append(str(exc))
            continue
        stripped = line.strip()
        out(f"{name}: {stripped}")
        if "TBD" in stripped.upper():
            problems.append(f"Experiment {name} is still TBD")

    out("")
    if problems:
        for p in problems:
            out(f"problem: {p}")
        out("bridge lab report: Task 1 is NOT satisfied")
        return 1
    out("bridge lab report: four labeled verdicts, none TBD")
    return 0


# ---------------------------------------------------------------------------
# argparse dispatch (wired from bridge.cli)
# ---------------------------------------------------------------------------


def add_lab_parser(sub: Any) -> Any:
    p_lab = sub.add_parser("lab", help="run the live-transport Experiments E-H harness")
    lab_sub = p_lab.add_subparsers(dest="lab_action", metavar="<action>")

    p_prepare = lab_sub.add_parser("prepare", help="record versions, gate on doctor, open a run")
    p_prepare.add_argument("--runs-dir", default=None)
    p_prepare.add_argument(
        "--full", action="store_true", help="capture message bodies verbatim (default: redacted)"
    )

    p_run = lab_sub.add_parser("run", help="run one experiment's stimulus and record what it saw")
    p_run.add_argument("experiment", choices=[*EXPERIMENTS, *[e.lower() for e in EXPERIMENTS]])
    p_run.add_argument("--claude", default=None, help="Bridge session id of the Claude session")
    p_run.add_argument("--codex", default=None, help="Bridge session id of the Codex session")
    p_run.add_argument(
        "--run", dest="run_dir", default=None, help="run directory (default: latest)"
    )
    p_run.add_argument("--runs-dir", default=None)
    p_run.add_argument("--timeout", type=float, default=60.0)
    p_run.add_argument("--seed-call", action="store_true", help="H: seed an async call first")
    p_run.add_argument("--skip-router-kill", action="store_true", help="H: omit the router step")

    p_verdict = lab_sub.add_parser("verdict", help="record a PASS/FAIL verdict with evidence")
    p_verdict.add_argument("experiment", choices=[*EXPERIMENTS, *[e.lower() for e in EXPERIMENTS]])
    p_verdict.add_argument("value", help="PASS or FAIL (TBD is refused)")
    p_verdict.add_argument("evidence", help="the decisive evidence, in one line")
    p_verdict.add_argument("--doc", default=None)
    p_verdict.add_argument("--run", dest="run_dir", default=None)
    p_verdict.add_argument("--runs-dir", default=None)

    p_report = lab_sub.add_parser("report", help="Task 1 verify: four verdicts, none TBD")
    p_report.add_argument("--doc", default=None)

    return p_lab


def dispatch_lab(args: argparse.Namespace) -> int:
    action = getattr(args, "lab_action", None)
    if action == "prepare":
        return lab_prepare(runs_dir=args.runs_dir, full=args.full)
    if action == "run":
        return lab_run(
            args.experiment,
            run_dir=args.run_dir,
            runs_dir=args.runs_dir,
            claude_id=args.claude,
            codex_id=args.codex,
            timeout_s=args.timeout,
            seed_call=args.seed_call,
            router_kill=not args.skip_router_kill,
        )
    if action == "verdict":
        return lab_verdict(
            args.experiment,
            args.value,
            args.evidence,
            doc=args.doc,
            run_dir=args.run_dir,
            runs_dir=args.runs_dir,
        )
    if action == "report":
        return lab_report(doc=args.doc)
    print("usage: bridge lab {prepare,run,verdict,report}")
    return 2
