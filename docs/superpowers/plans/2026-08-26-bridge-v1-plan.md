# bridge v1 — implementation plan

Date: 2026-08-26
Revised: 2026-09-05
Spec: `docs/superpowers/specs/2026-08-26-bridge-design.md`

This plan implements true live-session communication. A Bridge call is delivered to and answered by the exact addressed Claude or Codex session. No task may introduce a headless, snapshot, warm-resume, spool, or carbon-copy fallback.

Tasks are ordered by dependency and use tests before implementation. Run Task 1 first; begin scaffold and router implementation only after its required guarantees pass. Numbered tasks describe the eventual v1, but the milestone slices below govern execution order.

## Delivery milestones and spec reconciliation

This revision changes the original spec's scheduling, reply lifecycle, result schemas, and tool guidance. For those topics the explicit contracts below supersede spec §§6–10 for this implementation plan. Reconcile those spec sections as a follow-up documentation task before implementation; do not silently implement the old conflicting rules.

### Milestone A — transport proof

Complete Task 1 with disposable experiment scripts and minimal transport probes, not the production router. Record evidence for both actual TUIs, exact session identity, safe readiness/turn completion, and disconnect ambiguity. A failed mandatory guarantee blocks its adapter. No amount of fake-peer testing substitutes for this proof.

### Milestone B — private async alpha

Implement only the following slices of Tasks 2–9 and 11:
- Minimal package, authenticated local router, managed wrappers, roster, and both proven live adapters.
- Bounded FIFO, stable event IDs, explicit deadlines and failure outcomes, validated reply ownership, hop/rate limits, and visible exchanges.
- Agent tools: exactly `roster`, `call_async`, and `reply`. Deliver asynchronous results to the exact initiating session after its turn finishes. Do not implement synchronous waiting yet.
- Basic redacted lifecycle diagnostics and token-free health checks. Use temporary/manual configuration with documented undo steps; preserve existing user settings.
- Fail closed on restart/disconnect; never automatically replay an event whose vendor acceptance is uncertain. Durable recovery and a user-facing transcript are not alpha prerequisites.
- A small live end-to-end suite: both call directions, busy peers, reciprocal async requests, wrong-owner replies, reply-before-turn-end, expiry, and disconnect uncertainty.

In-memory alpha state is acceptable. A router restart must explicitly invalidate outstanding requests through adapter connection loss and a changed router epoch; never report success or silently replay them. If an accepted result cannot be pushed after restart, report its outcome as unknown in session diagnostics. Persistent recovery belongs to Milestone C.

Use the alpha in at least five real coding sessions before expanding scope. Record manual relays avoided, correct-peer selection, question-to-answer latency (including queue time), timeouts, and human interventions, without retaining private message bodies by default. Proceed only if repeat use reduces coordination work; otherwise revise the workflow before adding features.

### Milestone C — v1 hardening

Complete the remaining task slices: SQLite durability and conservative recovery, synchronous calls with wait-cycle protection, standalone text, user-facing transcript, robust reconnect, installer/uninstaller migration, full doctor, complete live suite, and packaging. Do not make these prerequisites for trying the private alpha.

## Required protocol and scheduling contracts

- **Answer completion and turn completion are separate.** A valid `reply` resolves the caller but does not release the callee's queue or outbound-message guard. Release those only on a correlated vendor turn-completion/safe-idle signal proven in E–G. Timeout likewise does not imply that the callee stopped working.
- **Idle observations are not reservations.** Test a human starting a turn between the router's idle check and vendor submission. Use proven vendor serialization/rejection semantics; requeue only when non-acceptance is known. Never steer or overlap an unrelated turn.
- **No exactly-once claim without vendor evidence.** Track queued, submitting, accepted, and processed separately. If a connection fails after possible acceptance but before acknowledgment, report `delivery_uncertain`; do not blindly retry. Stable router IDs prevent local duplicate enqueue/completion but do not prove vendor deduplication. Replay only after E–H establish a reliable vendor idempotency or reconciliation mechanism.
- **Synchronous calls are a later convenience.** Before enabling them, atomically maintain a wait-for graph. Reject a call that creates a wait cycle with `would_deadlock` before enqueueing it, suggesting `call_async`. Remove wait edges on reply, rejection, timeout, or disconnect. Test simultaneous reciprocal calls and a three-session cycle; the inbound hop guard alone cannot prevent either.
- **Canonical outcome shape.** Every operation returns `status` and a stable ID once accepted, plus `reason` for failures. Use `rejected` with reasons `rate_limited`, `queue_full`, `self_call`, `hop_limit`, `would_deadlock`, or `invalid_request`. Include `retry_after_s` where meaningful. Offline/unmanaged targets return `unreachable`.
- **Call outcomes.** Admission returns `queued`, `rejected`, or `unreachable`. Final outcomes are `answered`, `blocked`, `expired` (never submitted), `timeout` (accepted but no answer by deadline), `no_reply` (proven turn completion without reply), `delivery_uncertain`, or `unreachable` (known not delivered). A `submitting` event with ambiguous acceptance cannot be labeled expired or unreachable. Only actual answers carry `answered_by="live-session"`; unanswered outcomes have no fabricated answer.
- **Deadlines.** `call_async(to, question, timeout_s=300)` validates a positive timeout capped at 900 seconds, starting at router admission and including queue time. Synchronous calls retain a 60-second cap. Use a monotonic clock while running; persisted recovery must account conservatively for elapsed wall time and clock changes. Late replies are rejected and logged without clearing an active turn guard.
- **Result delivery has its own lifecycle.** An answered call can still have a queued/failed/uncertain result notification. Preserve the original outcome separately. Reserve a bounded result slot for each admitted async call at its source (20 pending result slots per session); return `rejected/queue_full` if none is available. Result notifications and `reply` do not consume the outbound request rate budget. Pending results expire after a documented 15-minute delivery window, releasing the reservation and surfacing a diagnostic.
- **Text outcomes in v1.** Admission follows the same rejection shape; delivery records `queued`, `delivered`, `processed`, `expired`, `unreachable`, or `delivery_uncertain`. Define a 300-second default TTL. Do not call transport acceptance "processed".
- **Tool guidance.** Use: “Read straightforward facts directly from disk; use Bridge for relevant peer findings, rationale, status, decisions, and coordination.” Asking which file/test matters is permitted; redundant bulk retrieval is discouraged. This replaces the absolute anti-retrieval sentence in the original spec.


## Conventions used by every task

- Python layout: `src/bridge/`, `tests/`, and `pyproject.toml`; distribution name `agent-bridge`, import and console command `bridge`.
- Claude Channel adapter assets live under `packages/claude-channel/` if the official TypeScript MCP SDK is required by Experiment E.
- `BRIDGE_HOME` overrides `~/.bridge`; every path, executable, clock, UUID source, and process launcher is injected in tests.
- Default tests never invoke real models or real user configuration. Fake Channel and App Server peers exercise protocol contracts.
- Live tests are marked `@pytest.mark.live`, excluded by default, and require explicit user presence and expected model usage.
- A task closes only when its listed `Verify` command succeeds.

---

### Task 1 — Live transport experiments E, F, G, H

**Why:** Legacy CLI Experiments A–D are complete and recorded in spec §13. The revised architecture introduces four different questions: Claude Channels and Codex App Server establish the live transport, but their busy-session, multi-client, identity, and reconnect details must be pinned before adapter code is written.

**Scope:** Run spec §14 plus the scheduling and uncertain-delivery cases above and record raw commands, installed versions, output, and one labeled `Verdict:` for each experiment in `docs/experiments/2026-08-27-live-transport-semantics.md`.

- E: Claude development Channel reaches the exact session, inherits `BRIDGE_SESSION_ID`, and returns a reply-tool call.
- F: a remote Codex TUI and Bridge client share one App Server; thread identity, turn events, and final-message correlation are proven.
- G: inbound delivery during unrelated active turns is serialized without accidental steering on both families; prove safe turn completion after an early reply, and test human-input races between idle detection and submission.
- H: disconnect/reconnect behavior is pinned using a minimal router probe; test disconnect after vendor acceptance but before acknowledgment, record whether reconciliation is possible, and document conservative failure where it is not.

If any experiment disproves the live-session contract, stop before implementing its adapter and revise the spec. Do not add a snapshot fallback.

**Files:** `docs/experiments/2026-08-27-live-transport-semantics.md`.

**Depends on:** nothing. Requires live Claude and Codex sessions and a human observing both TUIs.

**Verify:** Four labeled verdicts E–H each include PASS/FAIL, exact versions/commands, observed session/thread identifiers, expected versus actual behavior, and raw output or human-observation evidence. All mandatory live identity, two-client visibility, reply correlation, busy serialization, turn completion, and disconnect-safety guarantees must PASS; FAIL/TBD blocks the dependent adapter. A missing optional final-message fallback is acceptable only when explicitly disabled. Review the evidence, not merely the verdict count.

---

### Task 2 — Package and protocol-test scaffold

**Why:** Every later task needs an installable CLI, hermetic state root, and fake peers capable of exercising both live protocols without spending tokens.

**Scope (TDD):** create `pyproject.toml`, version module, CLI with `--version`, pytest/ruff configuration, and fixtures for:

- temporary `BRIDGE_HOME` and fake user homes;
- frozen clock and seeded UUIDs;
- fake Unix-socket router client/server;
- fake Claude Channel host that performs MCP initialize, accepts channel notifications, and invokes `reply`;
- fake Codex App Server implementing initialize, thread lifecycle, `turn/start`, streamed item events, and `turn/completed`;
- fake `claude` and `codex` executables that capture argv/env and never resolve to real vendor binaries.

**Files:** `pyproject.toml`, `src/bridge/__init__.py`, `src/bridge/cli.py`, `tests/conftest.py`, `tests/fakes/`, `tests/test_scaffold.py`.

**Depends on:** Task 1 passing mandatory transport guarantees. Build protocol fakes from its captured evidence, not assumed vendor behavior.

**Verify:** `pip install -e . && bridge --version && pytest tests/test_scaffold.py` succeeds with network disabled.

---

### Task 3 — Router daemon, local authentication, and SQLite state

**Why:** Live request/reply requires a single owner for connections, delivery queues, deadlines, transcript events, and correlation.

**Scope (TDD):** tests first for:

- lazy daemon start and idempotent second-client connection;
- Unix socket and bearer token created with user-only permissions;
- authenticated length-prefixed JSON protocol with version negotiation;
- SQLite migrations for sessions, calls, messages, queue entries, and transcript events;
- WAL mode, one-writer behavior, transactional enqueue/reply completion, and restart recovery;
- bounded diagnostic logs with message bodies redacted by default;
- clean idle shutdown only when no connected sessions or active calls remain.

Then implement the router event loop and a typed client used by the CLI, MCP server, Channel adapter, and wrappers.

**Files:** `src/bridge/router.py`, `src/bridge/router_client.py`, `src/bridge/store.py`, `src/bridge/protocol.py`, `src/bridge/paths.py`, `tests/test_router.py`, `tests/test_store.py`, `tests/test_protocol.py`.

**Depends on:** Tasks 1 and 2. Alpha implements only the minimal router slice; SQLite/restart recovery is Milestone C.

**Verify:** `pytest tests/test_router.py tests/test_store.py tests/test_protocol.py` passes, including restart recovery and rejection of a wrong token.

---

### Task 4 — Managed-session wrappers, registry, and roster

**Why:** Exact addressing is easiest and safest when Bridge supplies identity at launch instead of inferring it from process tables and mutable vendor artifacts.

**Scope (TDD):** implement transparent wrappers:

- `bridge claude [args...]` generates/preserves a session UUID, exports Bridge identity, enables the configured Channel mode, passes through signals/exit code/terminal behavior, and registers lifecycle state.
- `bridge codex [args...]` allocates a per-session App Server socket, exports identity, and launches the TUI with `--remote` while preserving supported arguments.
- registry state transitions (`starting`, `idle`, `working`, `waiting`, `offline`) are driven by adapter connections and vendor events, never mtime alone.
- `roster` returns managed sessions with reachability, preview, cwd, `is_self`, and vendor diagnostic ids.
- optional unmanaged discovery is clearly `reachable=false`; attempts to call it return wrapper/resume instructions.

Tests assert exact argv/env with fake executables, terminal signal forwarding, stale registration cleanup, resume address preservation where Experiment H permits it, and no pid-to-thread guessing.

**Files:** `src/bridge/launch.py`, `src/bridge/registry.py`, `src/bridge/roster.py`, `src/bridge/cli.py`, `tests/test_launch.py`, `tests/test_registry.py`, `tests/test_roster.py`.

**Depends on:** Tasks 2 and 3; Experiment H from Task 1.

**Verify:** `pytest tests/test_launch.py tests/test_registry.py tests/test_roster.py` passes; fake wrapper tests prove `BRIDGE_SESSION_ID` reaches child MCP processes.

---

### Task 5 — Claude two-way Channel adapter

**Why:** This adapter is what makes Codex→Claude calls reach and wake the exact live Claude session.

**Scope (TDD):** implement the smallest documented two-way Channel:

- declare the `claude/channel` capability and normal MCP tool capability;
- authenticate and register its inherited Bridge session id with the router;
- translate router events into `notifications/claude/channel` with safely encoded metadata (`kind`, `call_id`, `from`);
- expose `reply(call_id, answer, blocked)` and forward it to the router;
- include system instructions explaining call, text, and async-result envelopes;
- enforce sender/session ownership, message size limits, reconnect behavior, and one active inbound call;
- surface research-preview allowlist or organization-policy failures rather than silently falling back.

Use the official TypeScript MCP SDK if Experiment E confirms that is required; otherwise a Python implementation is acceptable only with an equivalent contract test. The adapter must not install prompt hooks or spool files.

**Files:** `packages/claude-channel/` or `src/bridge/claude_channel.py`, `src/bridge/adapters/claude.py`, `tests/test_claude_channel.py`.

**Depends on:** Tasks 2–4; Experiments E and G.

**Verify:** `pytest tests/test_claude_channel.py` passes against the fake host; live check `pytest -m live -k claude_channel` proves an external event appears and elicits `reply` in the exact wrapped Claude session.

---

### Task 6 — Codex App Server adapter and remote TUI host

**Why:** This adapter lets Bridge start a turn in the exact Codex conversation the human sees, without lock races or a replacement agent.

**Scope (TDD):** implement:

- one local App Server process and Unix socket per wrapped Codex session;
- initialize/initialized handshake and protocol-version checks;
- remote-TUI attachment lifecycle and exact thread binding from App Server events;
- event subscription for runtime status, turn start/completion, agent-message deltas, tool calls, and disconnects;
- idle `turn/start` delivery; no `turn/steer` use in v1;
- final-agent-message capture for correlation diagnostics; optional reply fallback is Milestone C only and requires Experiment F evidence;
- clean signal forwarding, child reaping, reconnect, and unsupported-version errors.

Tests run solely against the fake App Server and fake TUI. Pin generated protocol fixtures to the supported Codex version instead of hand-writing drifting schemas.

**Files:** `src/bridge/adapters/codex.py`, `src/bridge/codex_app_server.py`, `src/bridge/launch.py`, `tests/test_codex_adapter.py`, `tests/fixtures/codex_protocol/`.

**Depends on:** Tasks 2–4; Experiments F, G, and H.

**Verify:** `pytest tests/test_codex_adapter.py` passes; live check `pytest -m live -k codex_app_server` starts a turn that appears in the exact remote TUI and yields correlated completion events.

---

### Task 7 — Per-session FIFO and live text delivery

**Why:** Calls and async answers need the same busy-safe delivery primitive; text is the smallest complete feature that proves it.

**Scope (TDD):** implement one FIFO per target with:

- target reachability check and atomic enqueue;
- queue cap of 20 and explicit overflow result;
- delivery only when the adapter reports safe readiness according to Experiment G;
- Claude Channel delivery and Codex idle `turn/start` delivery;
- canonical text/event states and explicit admission rejections from the required contracts above;
- local deduplication using stable IDs, plus conservative vendor-boundary recovery: ambiguous acceptance produces `delivery_uncertain`, never unconditional resend;
- `text` envelope semantics that expect no reply;
- transcript events for every state transition.

**Files:** `src/bridge/delivery.py`, `src/bridge/transcript.py`, `tests/test_delivery.py`, `tests/test_transcript.py`.

**Depends on:** Tasks 3, 5, and 6; Experiment G.

**Verify:** `pytest tests/test_delivery.py tests/test_transcript.py` passes, including busy ordering, disconnect/retry, deduplication, expiry, and queue overflow.

---

### Task 8 — Live call state machine, reply, and guardrails

**Why:** This is the product's core promise: the selected live peer sees the question in its own conversation and returns a correlated answer.

**Scope (TDD):** implement:

- deterministic safe call envelope from spec §7;
- call admission, delivery phase, final outcome, callee turn state, and result-notification state tracked separately according to the required contracts;
- validated `reply` ownership, single answer completion, expiration, and `blocked` passthrough; an early reply/timeout never releases the callee queue or hop guard before correlated turn completion;
- Milestone C only: synchronous waiting capped at 60 seconds, with atomic wait-cycle detection and cleanup, without killing or forking the live target; alpha uses asynchronous calls;
- Milestone C only: Codex final-message fallback if enabled by Experiment F, labeled in result metadata; alpha requires explicit `reply` and reports `no_reply` on proven turn completion without it;
- daemon-enforced hop budget for the full inbound turn, including work after an early reply or timeout;
- no self-calls, 10/hour ordered-pair rate cap, one active inbound call per target, and no steer;
- answer-only permission contract: remove `allow_writes`; Bridge never changes the target's sandbox or approves tools;
- deterministic transcript gists with full bodies disabled by default.

Tests must prove `answered_by="live-session"`, reject replies from the wrong session, and assert that no delivery path invokes `claude -p`, `codex exec`, resume, spool, or carbon-copy behavior. Managed wrapper startup/resume is distinct from substituting a session to answer a call.

**Files:** `src/bridge/calls.py`, `src/bridge/guardrails.py`, `src/bridge/envelopes.py`, `tests/test_calls.py`, `tests/test_guardrails.py`.

**Depends on:** Tasks 4 and 7; Experiments E–G.

**Verify:** `pytest tests/test_calls.py tests/test_guardrails.py` passes, including wrong-owner reply, timeout/late reply, early reply followed by continued work, simultaneous reciprocal and three-session wait cycles (Milestone C), hop refusal, rate cap, and a forbidden-substitute assertion scoped to delivery paths (wrapper startup/resume remains allowed).

---

### Task 9 — MCP server and asynchronous result push

**Why:** Both live agents need the same ergonomic tools, and `call_async` must wake the original caller through its live transport when the answer arrives.

**Scope (TDD):** alpha exposes exactly `roster`, `call_async`, and protocol-facing `reply`; Milestone C adds `call`, `text`, and `transcript` over stdio MCP. Codex uses the normal Bridge MCP process; Claude adds the same tool handlers to its existing Channel process so no duplicate tool names are registered. Tests assert:

- exact schemas and result shapes from the revised required contracts above, reconciling spec §6 before implementation;
- the revised disk-versus-coordination guidance in applicable public tool descriptions;
- source identity comes only from inherited `BRIDGE_SESSION_ID`, never model-provided arguments;
- router authentication and disconnected-router errors;
- `call_async` returns after atomic enqueue and later pushes `kind=call_result` to the exact caller via Claude Channel or Codex App Server FIFO;
- caller disconnect, busy caller, duplicate result, reserved result-slot overflow, expired call, missing reply, uncertain result delivery, and stage-appropriate daemon restart behavior;
- inbound-call hop state rejects outbound messaging while leaving `reply` available.

Then add equivalent CLI commands for human diagnostics: `bridge roster`, `bridge text`, `bridge call`, and `bridge transcript`. The CLI does not impersonate an agent session unless an explicit managed source id is supplied internally by a wrapper.

**Files:** `src/bridge/server.py`, `src/bridge/async_results.py`, `src/bridge/cli.py`, `tests/test_server.py`, `tests/test_async_results.py`, `tests/test_cli.py`.

**Depends on:** the stage-appropriate slices of Tasks 3–8; alpha does not depend on sync calls, text, persistent recovery, or a user-facing transcript.

**Verify:** `pytest tests/test_server.py tests/test_async_results.py tests/test_cli.py` passes; stdio initialize + tools/list reports three tools for alpha and six for v1; both agents see consistent outcome schemas.

---

### Task 10 — Installer, uninstaller, and doctor

**Why:** True live communication only works when sessions are launched with the correct Channel/App Server wiring. Installation and diagnosis are part of the product contract.

**Scope (TDD):** against a fake home and fake CLIs:

- install/register the combined Bridge Channel/tool server for Claude and the normal Bridge MCP server for Codex;
- configure the Claude Channel adapter and choose documented normal or development launch mode based on detected support;
- create router state/token with mode `0700`/`0600` as appropriate;
- append idempotent sentinel-fenced guidance to CLAUDE.md and AGENTS.md explaining wrapper use, exact live semantics, direct-read/coordination guidance, reply, and full-turn hop rules;
- install no Claude prompt hooks and remove only obsolete Bridge-owned hook/spool configuration from prior pre-release installs;
- preserve unrelated settings byte-for-byte and report every touched path;
- provide a reversible `bridge uninstall` that does not delete transcripts unless explicitly requested;
- doctor checks Channel version/policy, App Server/remote flags, MCP registration, socket/token permissions, protocol versions, adapter health, and stale children using token-free local probes where possible.

**Files:** `src/bridge/install.py`, `src/bridge/doctor.py`, `src/bridge/cli.py`, `tests/test_install.py`, `tests/test_doctor.py`.

**Depends on:** Tasks 4–9.

**Verify:** `pytest tests/test_install.py tests/test_doctor.py` passes, including byte-identical second install, safe migration from the obsolete hook design, and reversible uninstall.

---

### Task 11 — End-to-end live smoke suite

**Why:** Unit and protocol tests cannot prove that two vendor TUIs visibly share the same conversations Bridge drives.

**Scope:** add `tests/live/test_e2e.py` and `docs/experiments/2026-08-26-e2e-checklist.md`. With one `bridge claude` and one `bridge codex` session open, prove:

1. Claude→Codex synchronous call appears in the addressed Codex TUI and the reply returns to the initiating Claude turn.
2. Codex→Claude synchronous call appears in the addressed Claude conversation and returns to the initiating Codex turn.
3. Text works in both directions with no reply required.
4. `call_async` in both directions wakes the exact caller with a correlated result.
5. A call sent while the target is busy waits and does not steer the unrelated turn.
6. The callee cannot dial out while answering; the 11th pair message is rate-limited.
7. Killing either adapter changes roster reachability and returns `unreachable`, never a headless answer.
8. Both human-visible transcripts and Bridge diagnostics/transcript agree on call id, sender, target, and outcome.
9. An early reply followed by continued callee work does not release its FIFO or hop guard.
10. Reciprocal async calls finish after original turns become idle; v1 synchronous reciprocal and three-session cycles return `would_deadlock` rather than mutually timing out.
11. Disconnect after possible acceptance produces `delivery_uncertain` unless proven reconciliation resolves it, with no blind duplicate delivery.
12. Human-input races preserve serialization; async result reservations, expiry, and undeliverable notifications surface explicit outcomes.

**Files:** `tests/live/test_e2e.py`, `docs/experiments/2026-08-26-e2e-checklist.md`.

**Depends on:** alpha slices of Tasks 1–9 for the initial live suite (manual setup permitted); Tasks 1–10 for the complete v1 suite. Run alpha tests before installer work.

**Verify:** `pytest -m live tests/live/` passes and every human-observation checklist item is checked.

---

### Task 12 — Packaging and README

**Why:** Ship the real live-session product, not the obsolete snapshot architecture.

**Scope:** rewrite README with:

- exact live-session promise and `bridge claude` / `bridge codex` launch flow;
- architecture diagram and `call`/`text` examples;
- explicit requirement that targets be Bridge-managed and reachable;
- Claude Channels research-preview and Codex App Server experimental notices;
- permission model, guardrails, troubleshooting, uninstall, and privacy defaults;
- no claims about headless consults, warm resume, carbon copies, spool, or prompt hooks.

Build version `0.1.0`; include any Channel adapter assets in distribution; verify sdist/wheel contents and fresh-environment installation. Publishing remains a separate user decision.

**Files:** `README.md`, `pyproject.toml`, packaging manifests as required.

**Depends on:** Tasks 2–11.

**Verify:** `python -m build && twine check dist/*` succeeds; installing the wheel in a fresh environment makes `bridge --version` and `bridge doctor` run and finds the packaged Channel adapter.

---

## Test strategy

### Levels

1. **Unit** — state machines, validation, queue ordering, rate math, envelopes, migrations, and result shapes. No subprocesses or sockets where a pure fake suffices.
2. **Protocol contract** — real Unix sockets and subprocess boundaries against fake Claude Channel and Codex App Server peers. No vendor binary or model call.
3. **Live** — explicit `-m live` tests against installed CLIs and visible wrapped TUIs. Never run in CI or by default.

### Hermeticity controls

- A default-suite guard fails if a subprocess resolves to the real `claude` or `codex` binary.
- Every home/config/state/socket path comes from an injected `Paths` object rooted in a pytest temp directory.
- Tests use frozen time, seeded ids, short injected deadlines, and deterministic event streams; no sleeps for synchronization.
- Fake peers can pause a turn, disconnect mid-event, duplicate acknowledgements, omit `reply`, emit malformed frames, and reconnect.
- Config tests start from nontrivial existing files and assert unrelated bytes/keys survive.
- The default suite targets under 30 seconds, zero network, and zero model tokens.

### Coverage matrix

| Feature | Unit | Protocol | Live |
|---|---|---|---|
| Router auth, DB, restart recovery | ✓ | ✓ | ✓ T11 |
| Wrapper identity and reachability | ✓ | ✓ | ✓ T4/T11 |
| Claude Channel push + reply | ✓ | ✓ | ✓ T5/T11 |
| Codex remote TUI + live turn | ✓ | ✓ | ✓ T6/T11 |
| Busy FIFO and deduplication | ✓ | ✓ | ✓ T11 |
| Text both directions | ✓ | ✓ | ✓ T11 |
| Sync call and validated reply | ✓ | ✓ | ✓ T11 |
| Async result wakes exact caller | ✓ | ✓ | ✓ T11 |
| Hop, rate, queue, self-call guards | ✓ | ✓ | ✓ T11 |
| Offline means unreachable | ✓ | ✓ | ✓ T11 |
| MCP schemas and descriptions | ✓ | ✓ | ✓ T10 |
| Installer migration/idempotency | ✓ | ✓ | ✓ T10 |
| Doctor and protocol drift | ✓ | ✓ | ✓ T10 |

Every degrade path is explicit: policy-blocked Channel, unsupported App Server version, target disconnect, router restart, queue overflow, permission wait, missing reply, late reply, and unmanaged session. None may invoke a substitute agent.

### Non-goals

- No load test beyond queue/rate boundary checks.
- No CI use of real vendor sessions.
- No cross-machine transport, cloud relay, arbitrary-session adoption, permission relay, or file locking.
- No testing of headless/resume behavior because it is not part of the product.

## Task order summary

Task 1 transport proof → reconcile affected spec contracts → Task 2 → alpha slices of 3–4 → 5–6 → 7–9 async-only slices → Task 11 alpha live suite → five-session usage review → remaining Tasks 3–9 durability/sync/text/transcript work → Task 10 installer/doctor → Task 11 full live suite → Task 12 packaging. Never defer the first real two-terminal workflow until after installer and recovery infrastructure.
