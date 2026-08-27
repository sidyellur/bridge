# bridge — design spec

Date: 2026-08-26
Status: settled (brainstorm + critique pass complete). Decisions made by the author of this document where the design left gaps are marked **[DECIDED HERE]**.

## 1. Problem statement

The user runs multiple AI coding agents concurrently in separate iTerm2 tabs on macOS — typically Claude Code in one and OpenAI Codex CLI in another. Today the only channel between them is the user's clipboard. Every cross-agent question, handoff, or heads-up requires a human to copy text out of one terminal and paste it into the other.

The framing that drove the design: *humans communicate through phone calls or text messages — why can't agents do the same?* A **text** is asynchronous: it lands in a mailbox and gets read later. A **call** is synchronous: you ask and wait for the answer. `bridge` gives both agent families both verbs, over MCP, from either side.

Language is Python (matches the user's other projects, `cleat` and `tether`). PyPI distribution name is `agent-bridge` (`bridge` and `bridge-mcp` are taken); repo, CLI command, and import name are all `bridge`.

## 2. The founding insight

Both vendors already ship a working agent phone system — but only for their own family. Every session is addressable, both CLIs can be driven headlessly, and both sides have a native "queue into a live session" or equivalent. **The only missing piece is the interconnect.**

| Capability | Claude Code | Codex CLI |
|---|---|---|
| Headless one-shot | `claude -p` | `codex exec` |
| Headless against an existing session | `claude -p --resume <uuid>` | `codex exec resume <id>` |
| Queue a message into a live session | in-harness `SendMessage` only — **no CLI path** | `codex queue --thread <uuid> --message <text>` |
| Enumerate sessions | jsonl files under `~/.claude/projects/<cwd-slug>/` | `codex agents`; jsonl under `~/.codex/sessions/YYYY/MM/DD/` |
| Authoritative "is it live?" signal | none native → bridge registry (hooks + pid) | `~/.codex/thread-writer-locks/*.lock` (lock UUIDs = live threads) |
| Host MCP servers | yes | yes |
| Turn-start hook injection | `SessionStart` / `UserPromptSubmit` hooks (receive `session_id`) | none |
| Wake-up when a background subprocess finishes | yes (harness re-invokes the agent) | no |

Consequences:

- bridge is an **adapter joining two carriers that each already work** — not a switchboard, not a broker. It owns no message infrastructure. Persistence, session lifecycle, and delivery remain the vendors' problem.
- The one genuinely asymmetric gap: **Codex→Claude async text has no CLI path**, because Claude's `SendMessage` is in-harness. bridge fills it with a spool file plus a Claude turn-start hook. That ~50-line shim is the only "infrastructure" bridge owns.

## 3. Governing principle

**Silent success is indistinguishable from failure in a tool whose entire job is presence.** Every bridge operation must leave a visible trace in *both* terminals: the caller sees the tool result; the callee's live session sees a carbon copy or a delivered message. No operation may complete invisibly on either side.

## 4. Architecture

One MCP server (stdio), registered on **both** Claude Code and Codex, exposing an **identical tool surface** to each, translating every operation onto whichever native mechanism the target family provides. Thin adapter. **No daemon.** Each host launches its own instance of the server; instances coordinate only through the filesystem (`~/.bridge/`) and the vendors' own CLIs.

```
Claude Code TUI ──stdio──> bridge MCP server ──┐
                                               ├── claude -p / codex exec   (consults)
Codex TUI ──────stdio──> bridge MCP server ────┤── codex queue              (ring Codex)
                                               ├── ~/.bridge/spool + hook   (ring Claude)
                                               └── ~/.bridge/{registry,transcript,...}
```

On-disk layout under `~/.bridge/` **[DECIDED HERE]**:

- `registry/<session_id>.json` — one file per session (see §5). Per-session files instead of one `registry.json` or SQLite: many concurrent hook writers, and atomic single-file writes need no locking.
- `spool/<session_id>/<ulid>.json` — undelivered messages for a Claude session.
- `transcript.jsonl` — append-only log of all cross-agent traffic (single writer per line via O_APPEND; lines are self-contained JSON).
- `forks.json` — session ids of headless instances bridge itself spawned (roster exclusion list).
- `calls/<call_id>.json` — in-flight async call state (worker pid, target, started-at).

## 5. Identity & registry (foundational)

A stdio MCP server is **not** told its host's session id. Everything downstream — attribution, transcript, hop-counting, spool addressing, roster's `is_self` — is unimplementable without solving this, so it is designed and built first.

**Claude side.** A `SessionStart` hook runs `bridge hook session-start`, which reads the hook JSON from stdin and writes `registry/<session_id>.json` containing `{session_id, family: "claude", pid, cwd, started_at, last_seen}`. `pid` is the hook's parent — the `claude` process. A `UserPromptSubmit` hook (`bridge hook prompt-submit`) heartbeats `last_seen` and drains the spool (§9). Liveness = registry entry whose `pid` is alive **and** whose process is actually a `claude` process (guards against pid reuse; check the process name/args) **[DECIDED HERE]**.

**Self-identification.** At startup the bridge server walks its parent-pid chain until it finds a `claude` or `codex` process. Claude: look that pid up in the registry. Codex: map the pid to a thread via `codex agents` output and the `thread-writer-locks` lock files (Experiment D, §14, pins the exact mapping; the fallback is "the live lock whose rollout file's mtime moved when this process last wrote").

**Codex side.** No hooks exist, so no registry writes. Codex liveness and identity come from Codex's own artifacts: `~/.codex/thread-writer-locks/*.lock` (live threads) and `codex agents`. The registry is a Claude-side construct; the roster merges both sources.

**Degraded mode.** If self-identification fails (hook not installed, orphan process), the server still serves `roster`, `call`, and `text` but stamps traffic `from: "unknown"` and disables the rate cap for that side with a warning in every tool result. It never silently drops attribution **[DECIDED HERE]**.

## 6. Addressing & roster

**Addresses are session UUIDs — and deliberately encode no location.** No host, no transport, no path. A future cloud transport is added by changing only the resolver; v1 builds nothing for cloud, it just preserves the opacity.

Nobody ever types a UUID. `roster()` is the contacts app:

- **Liveness is authoritative, never mtime.** mtime lies in both directions: an idle-but-live TUI looks dead, and headless `--resume` forks litter fresh-mtime corpses that look alive. Codex liveness = lock files / `codex agents`. Claude liveness = registry pid check.
- **Every row carries a last-user-message preview** (present in both jsonl formats). With 77 Claude sessions under the single cwd slug `-Users-siddharthyellur`, cwd disambiguates nothing; the preview is the only usable identifier.
- **Roster excludes bridge's own headless forks.** Every consult bridge spawns is created with a bridge-generated session id (`claude -p --session-id <uuid>`) or recorded from output, and that id goes into `forks.json` at spawn time. Codex forks: record the rollout uuid from `codex exec` output.
- **IDE-embedded sessions are excluded by default** (`roster(include_hidden=true)` shows them). Detection: registry entries whose process ancestry or env marks an IDE host **[DECIDED HERE]** — heuristic, refined during implementation.

## 7. Tool surface

Identical on both hosts. Signatures are exact; all tools return JSON.

```
roster(include_hidden: bool = false) ->
  { sessions: [ { id: str,                 # session uuid — the address
                  family: "claude"|"codex",
                  live: bool,
                  cwd: str,
                  last_user_message: str,  # preview, <=120 chars
                  last_active: iso8601,
                  is_self: bool } ],
    warnings: [str] }

call(to: str, question: str, allow_writes: bool = false, timeout_s: int = 60) ->
  { answer: str,
    blocked: [str],                        # things the callee declined to do
    capability: "read-only"|"writes-allowed",
    escalation: str|null,                  # exact re-dial with allow_writes=true, if blocked
    meta: { answered_by: "fresh-headless"|"warm-resume",
            peer_cwd: str, duration_s: float, cc_delivered: bool } }

call_async(to: str, question: str, allow_writes: bool = false) ->
  { call_id: str,
    delivery: str }                        # honest, family-specific description of how/when
                                           # the answer will arrive (see §8)

text(to: str, message: str) ->
  { delivered: bool,
    via: "codex-queue"|"claude-spool",
    note: str }                            # honest delivery semantics (Experiment C wording)

transcript(peer: str = null, limit: int = 20) ->
  { entries: [ { ts, from, to, kind: "call"|"call_async"|"text"|"cc",
                 gist: str, call_id: str|null } ] }
```

**Semantics.**

- `timeout_s` on `call` is capped at 60. A blocking 120 s MCP tool call flirts with host timeouts, and long syncs encourage exactly the expensive usage bridge is designed to avoid. `call_async` is the default verb and the tool descriptions say so.
- Every tool result includes any degraded-mode warnings (§5).
- `inbox` is **not** an MCP tool. Agent-facing polling habits are an anti-pattern here; delivery is push (§9). A manual drain exists only as the CLI command `bridge inbox` for the human.
- **Tool descriptions carry the anti-trigger** (§12), because tool descriptions are what models actually weigh: *"Use bridge to coordinate, never to retrieve. Never dial for anything discoverable on disk — the peer's repo is local; read it."*

## 8. Consult vs message split

Two distinct operations that must never be conflated:

- **A consult** (`call` / `call_async`) needs an *answer*. Default execution: **spawn a fresh headless instance of the peer's family, in the peer's cwd** (`claude -p` or `codex exec`, working directory = the addressed session's cwd). Fresh-headless is cheap (no context replay), safe (no writer-lock conflict), and answers most questions, because most questions are really about the peer's *repo*, which any instance in that cwd can read.
- **Warm resume** (`claude -p --resume <uuid>` / `codex exec resume <id>`) is used **only** where Experiments A/B prove it safe against a live session **and** the question genuinely needs the peer's in-flight reasoning. v1 exposes it as `call(..., warm: true)` only if the experiments pass; otherwise the parameter is absent and the spec's answer is "fresh-headless plus carbon copy" **[DECIDED HERE]**. Every warm resume replays the peer's full context — real tokens, real money — so it is never the default regardless of experiment outcome.
- **A message** (`text`) needs no answer. It goes to the *live* session via the ringer (§9).

**`call_async` mechanics** **[DECIDED HERE]**: the tool forks a detached worker process (own process group, pid recorded in `calls/<call_id>.json`) that runs the consult, appends the result to the transcript, and delivers the answer through the same ringer used for `text` — `codex queue` to a Codex caller, spool+hook to a Claude caller.

**The delivery paths are not equally good, and the guidance must lead with the better one.** An MCP tool cannot cause its host to wake up; only the host agent choosing a background subprocess can. So:

- **A Claude caller should reach for the Bash form first**: run `bridge call --to <id> "<q>"` via the Bash tool with `run_in_background=true`. The harness re-invokes the agent the moment the subprocess exits, so the answer arrives *mid-flow*. The guidance block (§12) teaches this as the default async path for Claude, and the `call_async` tool description says so too.
- **MCP `call_async` is the path for Codex**, which has no background wake-up regardless, and the fallback for a Claude caller that cannot use Bash.

The distinction matters because MCP `call_async` delivers to a Claude caller at its **next turn start** — which is after the human types something next, potentially many minutes later. Filing the true-wake-up path as a mere alternative would make the default path the worse one, contradicting §3. The `delivery` field states, honestly and per family, how and when the answer will actually arrive.

## 9. Delivery & the carbon copy (the ringer, and the wax-replica problem)

**The wax-replica problem is the single most important design constraint.** A headless consult answers from a *snapshot*: the live peer never learns the exchange happened. Ask the live Codex later "what did you tell Claude?" and it draws a blank — worse than no tool, because the human now holds a false model of what each agent knows.

**Fix: every consult carbon-copies a one-line gist into the live addressed session.**

- Target is Codex → `codex queue --thread <live-uuid> --message "[bridge cc] <caller> consulted a snapshot of this session. Q: <q, ≤200 chars> A: <a, ≤300 chars>"`.
- Target is Claude → write the same gist into `spool/<session_id>/`; the `UserPromptSubmit` hook drains the spool and injects the contents as `additionalContext` at the session's next turn.

Gist construction is **deterministic truncation, no LLM summarization** **[DECIDED HERE]** — a summarizer call would double the cost and add a failure mode to every consult.

The same two paths *are* the ringer for `text` and for async answers. Inbound-to-Codex is literally `codex queue`; inbound-to-Claude is spool+hook. ~50 lines, not a subsystem. This is why the ringer is **in v1** (reversing an earlier deferral): without delivery, `text()` is a dead-letter box and the carbon copy is impossible — and then the governing principle (§3) fails.

**Honesty requirement:** Experiment C (§14) determines whether `codex queue` against an *idle* TUI auto-processes or waits for the user's next Enter. Whichever it is, the `text` result's `note` field and the docs describe it truthfully — "a text message" vs "a drafts folder". The Claude spool is turn-gated by construction and is documented as such.

## 10. Capability & legible-refusal contract

v1 consults are **read-only by default**. But read-only does not fix mysteriousness — it relocates it: a callee that silently works around what it can't do is as opaque as one that silently does too much. So the contract has four legs:

1. **The callee is told its capability level in the prompt preamble** ("You are answering a bridge call. Capability: read-only. If the question requires writes or commands you cannot run, do not work around it — name it in `blocked`."), so it can decline up front instead of improvising.
2. **Responses are structured**: the callee is instructed to end with a fenced JSON block `{"answer": ..., "blocked": [...], "capability": "read-only"}`. bridge parses it; if parsing fails, the raw output becomes `answer` with `blocked: []` and a `meta` note that structure was lost **[DECIDED HERE]**.
3. **The caller surfaces refusals verbatim.** The `blocked` list passes through untouched; bridge never summarizes a refusal away, and the tool description instructs the calling agent to show it to the human.
4. **A blocked consult returns its own escalation string** — the exact re-dial (`call(to=..., question=..., allow_writes=true)`) for the *human* to approve. Full autonomy is a per-call opt-in (`allow_writes: true`), never a global posture.

Enforcement, not just persuasion **[DECIDED HERE]**: read-only Claude consults run `claude -p --allowedTools "Read,Grep,Glob"`; read-only Codex consults run `codex exec --sandbox read-only`. `allow_writes: true` lifts these to `--permission-mode acceptEdits` / `--sandbox workspace-write`. Exact flags are pinned during implementation against the installed CLI versions.

## 11. Guardrails

- **Hop budget = 1.** An agent answering a bridge call may not originate calls. This stops expensive 2-hop ping-pong, not just infinite recursion. Mechanism **[DECIDED HERE]**: every callee bridge spawns gets `BRIDGE_HOP=1` in its environment; the bridge MCP server inside that callee inherits it and refuses `call`/`call_async`/`text` with a legible error ("you are answering a bridge call; answer from your own context"). The preamble states the same rule so the model doesn't waste a turn discovering it.
- **Rate cap: ~10 messages/hour per ordered session pair**, computed from the transcript. The error message points at the transcript: "10 bridge messages between these two sessions in the last hour — run `bridge transcript` and reconsider whether this is coordination or a loop."
- **Transcript of all cross-agent traffic** (`~/.bridge/transcript.jsonl`): every call, text, cc, and async answer, with from/to/gist/timestamps. Exposed via the `transcript` tool and `bridge transcript` CLI. The transcript is the audit surface the governing principle requires.

## 12. Prompt guidance & installer responsibilities

**Prompt guidance is part of the product, not documentation.** Both agents' cwds are on the same disk, so most imagined Q&A doesn't need bridge at all — Claude can just *read* Codex's repo. The only content that genuinely requires reaching the agent is its **unwritten state**: in-flight intent, rationale, "are you about to change X", "here's what I just did and why."

`bridge install` therefore:

1. **Registers the MCP server on both sides** (user scope): `claude mcp add` for Claude; `[mcp_servers.bridge]` in `~/.codex/config.toml` for Codex.
2. **Installs the Claude hooks** (`SessionStart` → `bridge hook session-start`; `UserPromptSubmit` → `bridge hook prompt-submit`) in `~/.claude/settings.json`.
3. **Writes permission allowlist entries** into both agents' configs (`mcp__bridge__*` in Claude's permission allow list; the Codex equivalent trust entry). Per-call approval prompts would kill seamlessness before anything else got the chance. This is product, not docs.
4. **Writes the coordination block** into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, fenced with sentinel markers (`<!-- bridge:guidance:begin/end -->`) for idempotent re-runs **[DECIDED HERE]**:

   > Use bridge to **coordinate**, never to **retrieve**. Never dial for anything discoverable on disk — the peer's repo is local; read it. Dial when you need the peer's intent, in-flight plan, or a decision only it holds; to hand off work with context; or to announce a change that affects it. One call = one question; answer incoming calls from your existing context without dialing out. For shared scratch notes, use the shared scratchpad file, not a call.
   >
   > When you do not need the answer immediately, do not block: run `bridge call --to <id> "<question>"` as a **background Bash command**, and the answer will reach you the moment it is ready. Use the `call_async` tool only if you cannot run background commands.

   The second paragraph is written into the Claude block only; the Codex block instead points at the `call_async` tool, since Codex has no background wake-up (§8).

   The anti-trigger sentence also appears in every MCP tool description (§7), since tool descriptions are what models actually weigh.
5. `bridge install` is idempotent and reports every file it touched. `bridge doctor` verifies the whole installation (server registered both sides, hooks present, permissions present, registry writable) and reaps orphaned call workers.

The **shared scratchpad** is a v1 convention only — a file path both guidance blocks mention (`~/.bridge/scratchpad.md` **[DECIDED HERE]**) with no tooling behind it.

## 13. Failure modes

- **Sync call timeout.** At `timeout_s` (≤60), kill the callee's process group, return `{answer: "", blocked: ["timed out after Ns"], escalation: <re-dial as call_async>}`. The cc still fires, marked "timed out", so the live peer knows an attempt happened.
- **Orphaned subprocesses.** Every spawned consult runs in its own process group with pid recorded in `calls/`. `bridge doctor` reaps workers whose caller is gone; async workers self-limit to a hard 10-minute wall clock **[DECIDED HERE]**.
- **Two agents editing one file.** bridge does not lock files and does not pretend to. The guidance block prescribes the social fix: announce intent via `text` before touching a file the peer owns. The failure stays possible; bridge makes it *visible* (transcript + cc), which is its job.
- **Cost blowup.** Fresh-headless default (no context replay), hop budget 1, rate cap, 60 s sync cap. Warm resume — the expensive path — is opt-in and experiment-gated.
- **Hop loops / ping-pong.** Hop budget (env-inherited, server-enforced) plus rate cap. Two live agents politely texting each other forever is stopped by the pair rate cap.
- **Stale context.** Consults answer from a snapshot; the cc is the antidote on the peer's side, and `meta.answered_by` plus a "answered by a fresh instance in <cwd>, not the live session" note is the antidote on the caller's side. Payloads are **text plus inline content snapshots with the path as citation — no `{path, range}` references**, which go stale instantly because both agents edit constantly.
- **Ringer non-delivery.** `codex queue` failure or spool-write failure marks the operation `delivered: false` with the error verbatim — never silently dropped (§3). Spooled messages persist until drained; `bridge inbox` is the manual fallback.
- **Identity failure.** Degraded mode (§5): attribution becomes "unknown", warnings appear in every result, rate cap disabled for that side. `bridge doctor` diagnoses which leg (hook, registry, pid-walk) broke.

## 14. Experiments (run before the code they gate)

- **A — Claude warm-resume semantics.** Open a Claude TUI, get its uuid (`ls -t` the project dir), run `claude -p --resume <uuid> "reply PONG only"`, `ls -t` again. New file ⇒ fork; grown file ⇒ append. Then type a message in the TUI and inspect the jsonl tail's `parentUuid` chain for a branch. Gates: whether warm `call(warm=true)` exists for Claude targets, and roster fork-exclusion logic.
- **B — Codex warm-resume vs writer lock.** Same shape with `codex exec resume <live-locked-uuid> "reply PONG"` — lock error vs new rollout file? Gates: warm resume for Codex targets.
- **C — `codex queue` against an idle TUI.** `codex queue --thread <live-uuid> --message "PING"` while the TUI sits idle — auto-processed, or held until the user's next Enter? Gates: the honest wording of `text` ("text message" vs "drafts folder") in docs, tool descriptions, and the `note` field.
- **D — Codex pid → thread mapping** **[ADDED HERE]**: with two live Codex TUIs, determine how to map a `codex` process pid to its thread uuid (via `codex agents` output, lock-file contents, or `/proc`-equivalent inspection of open files with `lsof`). Gates: Codex-side self-identification (§5).

## 15. v1 scope

**IN:** identity registry + hooks; roster with authoritative liveness and last-message previews; `call_async` (default) and capped `call`, defaulting to fresh-headless-in-peer-cwd; `text` delivered via `codex queue` / Claude spool+hook; carbon copy of every consult to the live peer; installer writing permission allowlists and the CLAUDE.md/AGENTS.md coordination blocks; transcript; hop-budget-1 + rate cap; the capability/legible-refusal contract; `bridge doctor`; `bridge inbox` (manual drain only).

**OUT:** contacts.toml aliases (roster previews solve picking); `{path, range}` payloads; any cloud/remote resolver (keep address opacity, build nothing); scratchpad tooling (convention only); callee autonomy beyond per-call opt-in; `inbox()` as an agent-facing polling tool; warm resume as a default anywhere; any daemon.

## 16. Open questions

1. Experiments A–D (§14) — all four run before the code they gate; results recorded in `docs/experiments/`.
2. Exact CLI flags for capability enforcement (§10) may differ across installed versions of `claude`/`codex`; pinned during implementation, with `bridge doctor` checking flag availability.
3. IDE-embedded-session detection heuristic (§6) is best-effort in v1.
4. The Codex permission-allowlist mechanism (§12 item 3) depends on the installed Codex version's config schema; the installer degrades to printing manual instructions if the schema is unrecognized **[DECIDED HERE]**.
