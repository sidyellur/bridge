# bridge v1 — implementation plan

Date: 2026-08-26
Spec: `docs/superpowers/specs/2026-08-26-bridge-design.md`

Ordered, independently executable tasks. Within each task, tests are written before implementation (TDD). Experiments and the identity registry come first because everything else depends on them.

## Conventions used by every task

- Layout: `src/bridge/` (import name `bridge`), `tests/`, `pyproject.toml` with distribution name `agent-bridge`, console script `bridge`.
- All unit/contract tests run hermetically (see Test Strategy): `BRIDGE_HOME` env var overrides `~/.bridge`, `CLAUDE_HOME`/`CODEX_HOME`-style path injection points to fixture trees, and fake `claude`/`codex` executables are provided on `PATH`.
- Live tests are marked `@pytest.mark.live` and are excluded by default; `pytest -m live` runs them with real CLIs.
- "Verify" is the exact command whose success closes the task.

---

### Task 1 — Experiments A, B, C, D

**Why:** Four behaviors of the vendor CLIs gate design decisions (warm resume for Claude, warm resume for Codex, `codex queue` idle semantics, Codex pid→thread mapping). Running them after writing the gated code means rewriting the gated code.

**Scope:** Run each experiment exactly as written in spec §14 against live TUIs. Record raw command output and the verdicts in `docs/experiments/2026-08-26-cli-semantics.md`: for A/B, fork-vs-append and lock behavior; for C, auto-process vs wait-for-Enter (this fixes the `text` wording used in Tasks 7 and 9); for D, the working pid→thread recipe (this fixes Task 3's Codex resolver). Each verdict is one labeled line so later tasks can cite it.

**Files:** `docs/experiments/2026-08-26-cli-semantics.md`.

**Depends on:** nothing. Requires a human at the keyboard (live TUIs) — schedule with the user.

**Verify:** the experiments file exists and contains a filled `Verdict:` line for each of A, B, C, D (no "TBD"). `grep -c '^Verdict:' docs/experiments/2026-08-26-cli-semantics.md` prints `4`.

---

### Task 2 — Package scaffold

**Why:** Everything else needs an installable package, a CLI entry point, and a test harness.

**Scope:** `pyproject.toml` (name `agent-bridge`, `bridge = "bridge.cli:main"` console script, pytest + ruff config, `live` marker registered), `src/bridge/__init__.py` with `__version__`, minimal `bridge.cli` with `--version`, `tests/conftest.py` providing the hermeticity fixtures: `bridge_home` (tmp `BRIDGE_HOME`), `fake_agents` (writes fake `claude`/`codex` shim scripts to a tmp dir prepended to `PATH`, behavior driven by env vars/fixture files), `frozen_clock`.

**Files:** `pyproject.toml`, `src/bridge/__init__.py`, `src/bridge/cli.py`, `tests/conftest.py`, `tests/test_scaffold.py`.

**Depends on:** nothing (parallel with Task 1).

**Verify:** `pip install -e . && bridge --version && pytest` all succeed.

---

### Task 3 — Identity registry & self-identification

**Why:** Foundational (spec §5). Without it, attribution, spool addressing, hop-counting, and roster `is_self` are unimplementable.

**Scope (TDD):** tests first for: writing `registry/<session_id>.json` from `SessionStart` hook JSON on stdin; heartbeat on `UserPromptSubmit`; liveness = pid alive AND process-name check (pid-reuse guard); parent-pid walk finding a `claude`/`codex` ancestor (test against a fake process-table abstraction, injected); Codex pid→thread resolution per Experiment D's verdict (test against fixture lock dirs / fake `codex agents` output); degraded mode when no identity resolves (returns `from="unknown"` + warning, never raises). Then implement `bridge/registry.py`, `bridge/identity.py`, and CLI subcommands `bridge hook session-start`, `bridge hook prompt-submit` (drain logic stubbed until Task 6).

**Files:** `src/bridge/registry.py`, `src/bridge/identity.py`, `src/bridge/cli.py`, `tests/test_registry.py`, `tests/test_identity.py`.

**Depends on:** Task 2; Experiment D verdict from Task 1.

**Verify:** `pytest tests/test_registry.py tests/test_identity.py` passes; live check: `echo '{"session_id":"test-uuid","cwd":"/tmp"}' | BRIDGE_HOME=/tmp/bh bridge hook session-start && cat /tmp/bh/registry/test-uuid.json` shows the entry.

---

### Task 4 — Roster

**Why:** The contacts app (spec §6). `call`/`text` are unusable without it; liveness rules and fork exclusion are subtle enough to need their own task.

**Scope (TDD):** tests first against fixture trees mimicking `~/.claude/projects/<slug>/*.jsonl` and `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` + `thread-writer-locks/`: liveness from registry-pid (Claude) and lock files (Codex), never mtime; last-user-message preview extraction from both jsonl formats (≤120 chars); exclusion of ids in `forks.json`; default exclusion of IDE-embedded sessions with `include_hidden=true` override; `is_self` marking. Then implement `bridge/roster.py` and `bridge roster` CLI (table output).

**Files:** `src/bridge/roster.py`, `src/bridge/cli.py`, `tests/test_roster.py`, `tests/fixtures/` (sample jsonl tails from both families — scrubbed real ones).

**Depends on:** Task 3.

**Verify:** `pytest tests/test_roster.py` passes; live check (`pytest -m live -k roster`): with one live Claude TUI and one live Codex TUI open, `bridge roster` lists both as live with correct previews and lists no bridge fork.

---

### Task 5 — Transcript & guardrails

**Why:** The audit surface (spec §11) and the enforcement point for rate cap and hop budget. Built before the consult engine so every later feature logs from day one.

**Scope (TDD):** tests first for: append-only JSONL writes (concurrent-writer safe via O_APPEND single-line writes); `transcript(peer, limit)` filtering; rate cap of 10/hour per ordered pair computed from the transcript with the legible error text pointing at `bridge transcript`; hop-budget check reading `BRIDGE_HOP` from the environment and refusing outbound verbs with the legible error. Then implement `bridge/transcript.py`, `bridge/guardrails.py`, `bridge transcript` CLI.

**Files:** `src/bridge/transcript.py`, `src/bridge/guardrails.py`, `src/bridge/cli.py`, `tests/test_transcript.py`, `tests/test_guardrails.py`.

**Depends on:** Task 2 (Task 3 for attribution fields; stub `from` acceptable in unit tests).

**Verify:** `pytest tests/test_transcript.py tests/test_guardrails.py` passes, including a test that the 11th message in an hour between one pair is refused and the error message contains `bridge transcript`.

---

### Task 6 — Ringer: spool + Claude drain hook + `codex queue` wrapper

**Why:** Delivery (spec §9). The ringer is a prerequisite for `text`, the carbon copy, and async answer delivery — three features, one mechanism.

**Scope (TDD):** tests first for: spool write (`spool/<session_id>/<ulid>.json`, atomic); drain in `bridge hook prompt-submit` emitting `additionalContext` hook output and removing drained files (crash-safe: remove after emit); `codex queue` wrapper (against the fake `codex` shim) including failure surfacing (`delivered: false`, error verbatim); `bridge inbox` CLI manual drain. Wording of delivery notes follows Experiment C's verdict. Then implement `bridge/ringer.py` and wire the drain into the Task 3 hook command.

**Files:** `src/bridge/ringer.py`, `src/bridge/cli.py`, `tests/test_ringer.py`.

**Depends on:** Tasks 3, 5 (delivery events are transcripted); Experiment C verdict.

**Verify:** `pytest tests/test_ringer.py` passes; live check (`pytest -m live -k ringer`): spool a message for a live Claude session, submit a prompt in that TUI, and confirm the message text appears in the session's jsonl; `codex queue` a message to a live Codex TUI and confirm arrival per the Experiment C semantics.

---

### Task 7 — Consult engine

**Why:** The core of `call` (spec §§8, 10, 13): fresh-headless spawn, capability contract, structured-response parsing, timeout, carbon copy.

**Scope (TDD):** tests first (all against fake `claude`/`codex` shims) for: fresh-headless spawn in the addressed session's cwd with the correct read-only flags (`--allowedTools "Read,Grep,Glob"` / `--sandbox read-only`) and write flags under `allow_writes=true`; `BRIDGE_HOP=1` in the callee env; fork-id recording into `forks.json` before output is read; capability preamble text containing the decline instruction; parsing of the fenced JSON `{answer, blocked, capability}` with fallback-to-raw on parse failure (plus `meta` note); `escalation` string generation when `blocked` is non-empty; timeout kill of the process group at `timeout_s` (≤60 enforced) with the timed-out result shape; carbon-copy gist (deterministic truncation, ≤200/≤300 chars) delivered via the Task 6 ringer, `cc_delivered` reflecting the outcome; transcript entries for consult and cc. Warm resume: implement `warm=true` only if Experiments A and B both returned safe verdicts; otherwise the parameter is not exposed and a test asserts it is absent.

**Files:** `src/bridge/consult.py`, `src/bridge/prompts.py` (preamble + gist templates), `tests/test_consult.py`.

**Depends on:** Tasks 4, 5, 6; Experiments A, B.

**Verify:** `pytest tests/test_consult.py` passes; live check (`pytest -m live -k consult_smoke`): a real `call` from the test harness to a live Codex session's cwd returns a structured answer and the cc lands in that Codex TUI.

---

### Task 8 — MCP server

**Why:** The product surface (spec §7). One server, both hosts, identical tools, anti-trigger descriptions.

**Scope (TDD):** tests first, driving the server over stdio with an MCP client (the official `mcp` Python SDK): tool list contains exactly `roster`, `call`, `call_async`, `text`, `transcript`; every tool description contains the anti-trigger sentence ("coordinate, never to retrieve"); `call` result shape matches spec §7; `BRIDGE_HOP=1` in the server's env makes `call`/`call_async`/`text` return the hop-budget refusal; degraded-identity warnings appear in results when self-identification fails. Then implement `bridge/server.py` (FastMCP over stdio) and `bridge serve` CLI, performing self-identification (Task 3) at startup.

**Files:** `src/bridge/server.py`, `src/bridge/cli.py`, `tests/test_server.py`.

**Depends on:** Tasks 3–7.

**Verify:** `pytest tests/test_server.py` passes; `bridge serve` responds to an MCP `initialize` + `tools/list` handshake piped over stdio.

---

### Task 9 — `call_async` worker

**Why:** The default verb (spec §8) needs its detached-worker mechanics: state file, delivery via ringer, orphan safety.

**Scope (TDD):** tests first for: worker forked into its own process group with pid + metadata in `calls/<call_id>.json`; on completion, result appended to transcript and delivered to the *caller* via the ringer (spool for a Claude caller, `codex queue` for a Codex caller); `delivery` field wording matches the caller's family honestly; hard 10-minute wall-clock self-limit; state file cleanup on completion; stale-worker detection logic (consumed by `bridge doctor` in Task 10). Fake shims simulate slow and hung callees.

**Files:** `src/bridge/async_call.py`, `tests/test_async_call.py`.

**Depends on:** Tasks 6, 7, 8.

**Verify:** `pytest tests/test_async_call.py` passes, including the hung-callee test proving the worker kills itself and leaves a "timed out" delivery in the caller's spool.

---

### Task 10 — Installer & doctor

**Why:** Spec §12: registration, hooks, permission allowlists, and guidance blocks are product, not docs. Without the allowlists, every call triggers an approval prompt and the product dies on contact.

**Scope (TDD):** tests first against a sandboxed fake `$HOME`: `bridge install` registers the MCP server on both sides (`claude mcp add` via shim; `[mcp_servers.bridge]` written into `~/.codex/config.toml`), installs both hooks into `~/.claude/settings.json` (merging, not clobbering, existing hooks), writes `mcp__bridge__*` permission entries and the Codex trust entry (degrading to printed manual instructions on unrecognized schema), appends sentinel-fenced guidance blocks to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, is idempotent (second run changes nothing — assert byte-identical files), and reports every file touched. `bridge doctor`: checks all install legs, registry writability, CLI flag availability (spec §16.2), and reaps orphaned async workers.

**Files:** `src/bridge/install.py`, `src/bridge/doctor.py`, `src/bridge/cli.py`, `tests/test_install.py`, `tests/test_doctor.py`.

**Depends on:** Tasks 3, 8, 9 (doctor reaps workers).

**Verify:** `pytest tests/test_install.py tests/test_doctor.py` passes, including the double-run idempotency test; live check: `bridge install` on the real machine, then `bridge doctor` reports all green and `claude mcp list` shows bridge.

---

### Task 11 — End-to-end live smoke suite

**Why:** Per-task TDD hides integration seams. This task proves the four canonical journeys across real CLIs, in both directions.

**Scope:** `tests/live/test_e2e.py` (`-m live`) plus a manual checklist in `docs/experiments/2026-08-26-e2e-checklist.md` for the steps that need a human watching two TUIs: (1) Claude→Codex `call` — structured answer back, cc visible in the Codex TUI; (2) Codex→Claude `text` — message appears in the Claude TUI at next turn; (3) Claude→Codex `call_async` — answer arrives via ringer; (4) hop budget — a callee's attempt to dial out is refused; (5) rate cap trips at 11 and the error names `bridge transcript`; (6) roster shows both live sessions, correct previews, no forks.

**Files:** `tests/live/test_e2e.py`, `docs/experiments/2026-08-26-e2e-checklist.md`.

**Depends on:** Tasks 1–10.

**Verify:** `pytest -m live tests/live/` passes with one live Claude TUI and one live Codex TUI open; every checklist item ticked.

---

### Task 12 — Packaging & README

**Why:** Ship as `agent-bridge` on PyPI; README currently predates the design.

**Scope:** README rewritten from the spec (what/why/install/verbs/guardrails, honest delivery semantics per Experiment C); `python -m build` produces sdist+wheel; `twine check` passes; version `0.1.0`. No publish in this task — publishing is a separate user decision.

**Files:** `README.md`, `pyproject.toml`.

**Depends on:** Tasks 2–11.

**Verify:** `python -m build && twine check dist/*` succeeds; `pip install dist/*.whl` in a fresh venv makes `bridge --version` work.

---

## Test Strategy

Per-task TDD alone hides cross-cutting gaps — integration seams, partial-degrade paths, and the fact that these tests can spawn real agent CLIs that are slow, non-deterministic, and cost money. This section is the cross-cutting contract.

### Levels

1. **Unit** (default `pytest`): pure logic — parsing, gist truncation, rate-cap math, liveness rules, escalation strings. No subprocesses, no real `$HOME`, no sleeps.
2. **Contract** (default `pytest`): code that shells out, run against **fake `claude`/`codex` shim executables** — small scripts installed on a tmp `PATH` by the `fake_agents` fixture. Shims echo their argv to a capture file (so tests assert exact flags: `--resume`, `--sandbox read-only`, `--allowedTools`, `queue --thread`) and emit canned stdout selected by the test (structured answer, malformed answer, lock error, hang). Hangs are simulated with a shim that sleeps past the timeout; timeout tests use sub-second `timeout_s` via an injection point, never real 60 s waits.
3. **Live** (`pytest -m live`, excluded by default, never in CI): real `claude`/`codex` CLIs, live TUIs where noted, real `~/.bridge` under a temporary `BRIDGE_HOME`. Run manually on the user's machine at Tasks 3, 4, 6, 7, 10, 11.

### Hermeticity controls

- **No test outside `-m live` may execute the real `claude` or `codex` binaries** — the `fake_agents` fixture prepends its shim dir to `PATH` and a conftest guard fails any default-run test whose subprocess resolves to a real vendor CLI path.
- **All filesystem roots are injected**: `BRIDGE_HOME` for bridge state; the Claude/Codex data dirs (`~/.claude/projects`, `~/.codex/sessions`, locks, configs) are read through a single `Paths` object constructed in one place and overridden by fixtures. No test touches the real home directory.
- **Time and randomness injected**: `frozen_clock` fixture for rate-cap and heartbeat tests; ULIDs/uuids from a seeded generator where ordering matters.
- **Process-table abstraction**: pid-walk and liveness tests run against an injected fake process table; the real `psutil`-backed implementation gets one thin contract test plus live coverage.
- **Determinism budget**: the default suite must run in under ~30 s with zero network and zero real agent spawns. Anything slower or flakier moves behind `-m live`.

### Coverage matrix

| Feature | Unit | Contract (shims) | Live |
|---|---|---|---|
| Registry write/heartbeat/liveness | ✓ | ✓ (hook stdin) | ✓ T3 |
| Self-identification + degraded mode | ✓ | ✓ (fake proc table) | ✓ T3/T11 |
| Roster liveness/previews/exclusions | ✓ (fixtures) | — | ✓ T4 |
| Transcript + rate cap | ✓ | — | ✓ T11 |
| Hop budget refusal | ✓ | ✓ (env inherit) | ✓ T11 |
| Spool write/drain/inbox | ✓ | ✓ (hook output) | ✓ T6 |
| `codex queue` wrapper + failure surfacing | — | ✓ | ✓ T6 |
| Consult spawn flags/cwd/capability | — | ✓ | ✓ T7 |
| Structured parse + fallback + escalation | ✓ | ✓ | ✓ T7 |
| Timeout kill + orphan reap | — | ✓ (hang shim) | ✓ T11 |
| Carbon copy | ✓ (gist) | ✓ (delivery) | ✓ T7/T11 |
| MCP surface + descriptions | — | ✓ (stdio client) | ✓ T10 |
| `call_async` worker + delivery | — | ✓ | ✓ T11 |
| Installer idempotency + merging | — | ✓ (fake HOME) | ✓ T10 |
| Doctor | ✓ | ✓ | ✓ T10 |

Partial-degrade paths get explicit rows inside their tasks' test files, not just happy paths: unknown identity, cc delivery failure, malformed callee output, unrecognized Codex config schema, dead spool target.

### Non-goals

- No performance or load testing — traffic is capped at 10/hour/pair by design.
- No testing of vendor internals (jsonl schema evolution, lock-file format stability) beyond the fixtures captured at Task 1/4 time; `bridge doctor` is the runtime detector for vendor drift, not the test suite.
- No CI execution of live tests; CI runs unit + contract only.
- No cross-machine or cloud scenarios (out of v1 scope).
- No mutation/property testing in v1.

---

## Task order summary

1 (experiments) and 2 (scaffold) in parallel → 3 (identity) → 4 (roster), 5 (transcript/guardrails) → 6 (ringer) → 7 (consult) → 8 (MCP server) → 9 (async worker) → 10 (installer/doctor) → 11 (live e2e) → 12 (packaging).
