"""Task 8 verify (guardrails): self-call, hop budget, rate cap, one active
inbound call, reachability, and the source-level forbidden-fallback assertion.

Issue #5 part D replaces the substring version of the source assertion with an
AST scan, so a docstring or comment that merely *names* a forbidden fallback no
longer trips it while a real invocation still does.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

import pytest

from bridge.router import RouterConfig, RouterError

SRC = Path(__file__).resolve().parent.parent / "src" / "bridge"

# Vendor binaries Bridge must never invoke as a substitute for the addressed
# live session (design spec sections 2 and 3).
VENDOR_BINARIES = {"claude", "codex"}

# Tokens from the abandoned pre-release designs. They may appear in prose
# (docstrings/comments) but never in a string literal the code actually uses.
FORBIDDEN_TOKENS = ("spool", "carbon-copy", "carbon copy", "warm resume")


# --- the scanner -----------------------------------------------------------


def _dotted_name(node: ast.expr) -> str:
    """``subprocess.run`` for an Attribute chain, ``Popen`` for a bare Name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_process_spawn(name: str) -> bool:
    """True for subprocess.*, os.exec*/os.spawn*/os.system, and any Popen."""
    root, _, tail = name.rpartition(".")
    leaf = root.split(".")[-1]
    if tail == "Popen" or leaf == "subprocess":
        return True
    if leaf == "os":
        return tail == "system" or tail.startswith("exec") or tail.startswith("spawn")
    return False


def _string_literals(node: ast.AST) -> list[str]:
    """Every string literal under ``node``, in source order."""
    found: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        found.append(node.value)
    for child in ast.iter_child_nodes(node):
        found.extend(_string_literals(child))
    return found


def _forbidden_invocation(argv: list[str]) -> str | None:
    """Inspect a spawn call's literal argv for a forbidden vendor invocation."""
    tokens = [word for literal in argv for word in literal.split()]
    for i, token in enumerate(tokens):
        binary = PurePosixPath(token).name
        if binary not in VENDOR_BINARIES:
            continue
        rest = tokens[i + 1 :]
        if binary == "claude" and "-p" in rest:
            return "`claude -p` headless fallback"
        if binary == "codex" and "exec" in rest:
            return "`codex exec` headless fallback"
        if "resume" in rest:
            return f"`{binary} resume` warm-resume fallback"
    return None


def _non_docstring_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """String literals that are not module/class/function docstrings.

    Comments never reach the AST at all, so they are excluded for free.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def scan_file(path: Path, *, allow_spool: bool = False) -> list[str]:
    """Return every forbidden-fallback problem found in one source file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    problems: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_process_spawn(_dotted_name(node.func)):
            found = _forbidden_invocation(_string_literals(node))
            if found:
                problems.append(f"{path.name}:{node.lineno}: spawns {found}")

    tokens = tuple(tok for tok in FORBIDDEN_TOKENS if not (allow_spool and tok == "spool"))
    for lineno, literal in _non_docstring_literals(tree):
        lowered = literal.lower()
        for token in tokens:
            if token in lowered:
                problems.append(f"{path.name}:{lineno}: string literal contains {token!r}")
    return problems


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
    """No module under src/bridge spawns a vendor fallback or keeps a spool.

    'spool' may appear ONLY in install/doctor, and only to remove the obsolete
    pre-release spool directory — never in the coordination path.
    """
    problems: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        problems += scan_file(py, allow_spool=py.name in ("install.py", "doctor.py"))
    assert problems == [], "forbidden fallbacks found:\n" + "\n".join(problems)


def test_scanner_catches_an_injected_headless_fallback(tmp_path):
    """The scan must still bite. Proven on a temp file, never the real tree."""
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text(
        '''"""This docstring names claude -p and codex exec on purpose."""

import subprocess


def consult(question):
    return subprocess.run(["claude", "-p", question], check=False)
'''
    )
    problems = scan_file(sneaky)
    assert len(problems) == 1
    assert "claude -p" in problems[0]


@pytest.mark.parametrize(
    "source, expected",
    [
        ('import subprocess\nsubprocess.Popen(["codex", "exec", "-"])\n', "codex exec"),
        ('from subprocess import Popen\nPopen(["claude", "resume", "x"])\n', "claude resume"),
        ('import subprocess\nsubprocess.run("claude -p hi", shell=True)\n', "claude -p"),
        ('import os\nos.system("codex exec -")\n', "codex exec"),
        ('import os\nos.execvp("claude", ["claude", "resume", "x"])\n', "claude resume"),
        ('import os\nos.spawnvp(0, "codex", ["codex", "exec"])\n', "codex exec"),
    ],
)
def test_scanner_catches_every_spawn_shape(tmp_path, source, expected):
    bad = tmp_path / "bad.py"
    bad.write_text(source)
    problems = scan_file(bad)
    assert problems and expected in problems[0]


def test_scanner_ignores_prose_and_legitimate_argv(tmp_path):
    """Docstrings, comments, and honest vendor argv must not trip the scan."""
    ok = tmp_path / "ok.py"
    ok.write_text(
        '''"""Bridge never spawns a spool, a carbon-copy, or a warm resume."""

import subprocess

# the obsolete carbon copy / warm resume design is gone


def launch(binary, session_id, user_args):
    # a real wrapper launch: a live TUI, not a headless fallback
    return subprocess.Popen([binary, "--session-id", session_id, *user_args])


def launch_codex(binary, socket_path):
    return subprocess.Popen([binary, "--remote", f"unix://{socket_path}"])
'''
    )
    assert scan_file(ok) == []


def test_scanner_flags_a_live_spool_literal(tmp_path):
    """A 'spool' string the code actually uses is a problem outside install/doctor."""
    f = tmp_path / "spooler.py"
    f.write_text('from pathlib import Path\n\nSPOOL = Path.home() / "spool"\n')
    assert scan_file(f)
    assert scan_file(f, allow_spool=True) == []


def test_call_ignores_write_elevation_arguments(router_core):
    r = router_core
    _connect(r, "b")
    # a hostile/legacy allow_writes arg must be ignored, not honored
    outcome, _ = r.dispatch(
        "call", {"caller": "a", "to": "b", "question": "q", "allow_writes": True}, waiter="W"
    )
    assert outcome == "defer"  # accepted as a normal call, elevation ignored
