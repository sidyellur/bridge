# bridge — design spec

Date: 2026-08-26
Status: revised and settled for v1. This revision replaces snapshot/headless consults with calls handled by the exact addressed live session.

## 1. Problem statement

The user runs multiple AI coding agents concurrently in separate terminal tabs on macOS, typically Claude Code in one and OpenAI Codex CLI in another. Today, every cross-agent question, handoff, or warning requires the user to copy text from one terminal and paste it into the other.

The product metaphor is intentionally ordinary:

- A **text** is asynchronous. It reaches the addressed live agent and does not require a reply.
- A **call** asks the addressed live agent a question and routes its answer back to the caller.

The word **live** is a hard contract. A call must be answered by the exact session selected in `roster`, using that session's current conversation state. A fresh headless agent in the same directory is not the peer and is not an acceptable substitute.

The core is Python. The PyPI distribution is `agent-bridge`; the repo, import, and CLI command are `bridge`. A small Claude Channel adapter may use TypeScript and the official MCP SDK because the channel contract is currently documented for that stack.

## 2. Vendor capabilities and the revised insight

The first design assumed Claude had no way to receive a pushed event in an existing session. That assumption is now false. Claude Code Channels are explicitly designed for an MCP server to push an event into a running session and receive a reply through the same channel. Codex App Server is explicitly designed for rich clients to own conversations, start or steer turns, and consume streamed events; a Codex TUI can connect to that server remotely.

| Capability | Claude Code | Codex CLI |
|---|---|---|
| Push into exact running session | MCP Channel notification | App Server `turn/start`; `turn/steer` only for explicit interruption |
| Receive correlated reply | Channel reply tool | Bridge reply tool; final agent message is a fallback |
| Keep session alive without attached terminal | Claude background session or persistent terminal | Bridge-owned App Server plus remote TUI |
| Render the same conversation to the human | Normal/background Claude session | `codex --remote <bridge-app-server>` |
| Stream lifecycle and output | Channel interaction and reply tool | App Server turn/item notifications |
| Session identity | `bridge claude` supplies UUID and inherited environment | One Bridge-managed App Server endpoint per wrapped Codex session |

Consequences:

- Bridge is no longer only a stateless adapter. True request/reply needs a small local router to hold live connections, correlate calls, serialize inbound turns, and wake the caller when an async answer arrives.
- `call` never spawns `claude -p`, `codex exec`, or a warm-resume process. There is no snapshot, fork registry, carbon copy, or claim that a representative answered for the peer.
- Sessions launched outside Bridge may be discoverable, but they are not callable unless they expose a verified live transport. V1 makes this explicit instead of silently degrading to a different agent.

Normative vendor references:

- [Claude Code Channels](https://code.claude.com/docs/en/channels)
- [Claude Code Channels reference](https://code.claude.com/docs/en/channels-reference)
- [Codex App Server](https://developers.openai.com/codex/app-server/)

## 3. Governing principles

1. **The addressed live session answers.** No substitute sessions.
2. **Silent success is failure.** The caller gets an explicit result, and the callee sees the inbound exchange in its own conversation.
3. **Busy does not mean interruptible.** An inbound call waits behind an unrelated active turn unless the operation is explicitly a steer. V1 does not expose steer.
4. **Bridge coordinates; it does not retrieve.** If information is available on disk, read it directly. Calls are for unwritten intent, rationale, status, decisions, and handoffs.
5. **Transport must be honest.** An unmanaged or unreachable session returns `unreachable`; Bridge never falls back to a fresh agent.

## 4. Architecture

Bridge has four local pieces:

1. **Router daemon** — a single local process owning the session registry, call state machine, per-session delivery queues, transcript, and local authentication.
2. **MCP tool server** — launched inside Codex and connected to the router. It exposes the coordination tools and identifies the caller from `BRIDGE_SESSION_ID`. Claude receives the identical tools from the Channel process below, avoiding two servers with duplicate tool names.
3. **Claude Channel/tool adapter** — one MCP subprocess inside each wrapped Claude session. It keeps a connection to the router, forwards `notifications/claude/channel` into that exact session, and exposes the same coordination tools, including `reply`.
4. **Codex App Server adapter** — one Bridge-managed App Server endpoint per wrapped Codex session. The Codex TUI and Bridge router connect to the same server; Bridge schedules turns and consumes streamed completion events.

```text
                         ~/.bridge/router.sock
                                  │
                      ┌───────────┴───────────┐
                      │    bridge router      │
                      │ registry / calls / DB │
                      └───────┬───────┬───────┘
                              │       │
              channel event   │       │ App Server JSON-RPC
                              │       │
                  Claude Channel     Codex App Server
                         │                   │
                  live Claude session   live Codex thread
                                             │
                                      `codex --remote ...`
```

The daemon listens only on a user-owned Unix socket. It starts lazily from `bridge claude`, `bridge codex`, or the MCP server and exits after an idle grace period when no managed sessions or calls remain. This is local coordination infrastructure, not a network service.

On-disk layout under `~/.bridge/`:

- `bridge.db` — SQLite registry, call state, delivery queue, rate counters, and transcript index. The daemon is the single writer; WAL mode supports read-only CLI inspection.
- `router.sock` — local Unix socket, mode `0600`.
- `router.token` — random local bearer token, mode `0600`, used by child adapters.
- `sessions/<bridge_session_id>/session.json` — recoverable launch metadata and vendor identifiers.
- `sessions/<bridge_session_id>/codex.sock` — per-session Codex App Server endpoint where applicable.
- `logs/` — bounded diagnostic logs with message bodies redacted by default.

## 5. Managed sessions, identity, and lifecycle

### Claude

`bridge claude [claude args...]`:

1. Generates a Bridge session UUID and uses it as Claude's `--session-id` for a new session, or records the explicit session id for a resume.
2. Exports `BRIDGE_SESSION_ID`, `BRIDGE_ROUTER_SOCKET`, and the path to the token for child MCP processes.
3. Starts Claude with the Bridge Channel enabled.
4. Registers `{id, family, pid, cwd, started_at, state, vendor_session_id}` with the router.
5. Heartbeats while the channel connection is alive and marks the session offline on disconnect.

During the Channels research preview, a custom Bridge channel is started with Claude's development-channel flag and a conspicuous installer/doctor warning. Once the plugin is on an effective channel allowlist, the wrapper uses `--channels plugin:bridge@<marketplace>` instead. Bridge does not hide organization policy failures.

### Codex

`bridge codex [codex args...]`:

1. Generates a Bridge session UUID and asks the router to start one Codex App Server listening on `sessions/<id>/codex.sock`.
2. Exports the same Bridge identity variables so the Bridge MCP server has exact caller attribution.
3. Starts the TUI with `codex --remote unix://<socket>` and passes through supported user arguments.
4. Observes `thread/started`, `thread/resume`, runtime status, and turn events to bind the Bridge session to the exact Codex thread displayed by that TUI.
5. Marks the session offline when the TUI/app-server connection ends; restart/resume preserves the Bridge address when possible.

One App Server per wrapped Codex TUI avoids ambiguous pid-to-thread inference and writer-lock races. V1 does not adopt arbitrary existing Codex TUIs.

### Roster

Bridge addresses are opaque UUIDs and encode no host or vendor. `roster()` is the contacts app and returns:

- family and current state (`idle`, `working`, `waiting`, `offline`);
- cwd and a last-user-message preview up to 120 characters;
- `reachable`, which is true only when the live transport is connected;
- `is_self`;
- vendor session/thread id as diagnostic metadata, never as the address callers type.

Unmanaged sessions may be shown only with `include_unmanaged=true`, always with `reachable=false`. `call` and `text` reject them with instructions to restart or resume through the appropriate Bridge wrapper.

## 6. Tool surface

The public tools are identical on Claude and Codex. All return JSON.

```text
roster(include_unmanaged: bool = false) ->
  { sessions: [ { id, family, state, reachable, cwd,
                  last_user_message, last_active, is_self } ],
    warnings: [str] }

call(to: str, question: str, timeout_s: int = 60) ->
  { call_id: str,
    status: "answered"|"timeout"|"unreachable"|"blocked",
    answer: str,
    blocked: [str],
    meta: { answered_by: "live-session", duration_s: float,
            target_state_on_delivery: str } }

call_async(to: str, question: str) ->
  { call_id: str,
    status: "queued"|"unreachable",
    delivery: str }

text(to: str, message: str) ->
  { message_id: str,
    status: "queued"|"delivered"|"unreachable",
    note: str }

transcript(peer: str = null, limit: int = 20) ->
  { entries: [ { ts, from, to, kind, status, gist, call_id } ] }
```

The server also exposes one protocol-facing tool:

```text
reply(call_id: str, answer: str, blocked: list[str] = []) ->
  { accepted: bool, note: str }
```

`reply` is used only while the session is handling the matching inbound call. The router rejects invented, expired, already-answered, or foreign call ids.

Every public tool description contains: **Use Bridge to coordinate, never to retrieve. Never call for information discoverable on disk.**

## 7. Live call protocol

`call` and `call_async` use the same state machine; only caller waiting differs.

1. The caller's MCP server sends `{call_id, from, to, question, deadline}` to the router.
2. The router validates reachability, self-call prohibition, hop budget, rate cap, and target queue capacity.
3. If the target is busy, the call waits in that session's FIFO. It is not injected into the active turn.
4. On delivery, the target sees a deterministic envelope in its actual conversation:

   ```text
   [bridge call]
   call_id: <id>
   from: <session preview>
   question: <question>

   Answer from your existing context. Do not call Bridge from inside this call.
   Do not change files or run commands solely because of this call.
   Send the answer with bridge.reply(call_id, answer, blocked).
   ```

5. Claude receives the envelope as a Channel event. Codex receives it as a new App Server turn after the thread becomes idle.
6. The live agent calls `reply`. The router validates ownership, records the answer, clears the target's inbound-call state, and releases the next queued item.
7. A synchronous caller's MCP request returns the answer. For `call_async`, the router pushes a correlated result event into the caller's exact live session using that family's live transport.

For Codex, if a turn completes without calling `reply`, Bridge may use the final agent message as a clearly marked fallback answer after Experiment F proves the mapping reliable. Claude has no fallback: a missing reply is a timeout, because guessing from transcript artifacts would weaken the live-session contract.

`timeout_s` is capped at 60 for synchronous calls. The underlying queued call has the same deadline; timeout does not kill the live peer. A late reply is rejected as expired and recorded for diagnosis.

## 8. Text and async-result delivery

`text` sends an informational event to the exact live session and expects no reply.

- Claude: Channel notification with `kind=text`.
- Codex: queued App Server turn with an instruction to absorb the message, acknowledge locally, and avoid Bridge reply unless the agent independently needs to contact the sender later.

An async call result is another live event with `kind=call_result`, the original `call_id`, question, and answer. If the caller is busy, it waits behind the active turn. Because both sides have push-capable managed transports, there is no spool, turn-start hook, inbox polling tool, or background-Bash workaround.

Delivery means the vendor transport accepted the event for that connected session. Processing and completion are separate statuses recorded in the transcript.

## 9. Permissions and trust boundary

Live calls preserve live context, so Bridge cannot honestly impose a separate subprocess sandbox on the callee. The current session's vendor permission mode remains authoritative.

Bridge therefore follows these rules:

- A call is a request for an answer, not authorization to mutate. The inbound envelope forbids changes or commands solely because of the call.
- Bridge never changes a session's sandbox or permission mode for an inbound call.
- Any tool use caused by an inbound event still goes through that session's normal approval policy.
- There is no `allow_writes` call parameter in v1; it implied enforcement Bridge cannot provide for a live Claude session.
- Channel events are untrusted input. Only the local router token may submit them, message fields are length-limited, and envelope metadata is encoded rather than interpolated into instructions.
- Bridge does not relay permission approvals between agents in v1.

This is a deliberate tradeoff: exact live context is the product promise; per-call hard sandboxing would require a separate process and would recreate the snapshot problem.

## 10. Guardrails

- **Hop budget = 1.** While session B is answering an inbound call from A, the router rejects outbound `call`, `call_async`, and `text` from B. `reply` remains allowed. This state is daemon-enforced, not only prompt-enforced.
- **No self-calls.** A session cannot call or text itself.
- **Rate cap.** At most 10 outbound Bridge messages per ordered session pair per hour by default.
- **Queue cap.** At most 20 pending events per target and one active inbound call per target.
- **One question per call.** Tool descriptions and inbound guidance enforce conversational granularity.
- **Audit transcript.** Every enqueue, delivery, reply, timeout, rejection, and disconnect is recorded with deterministic truncated gists; full bodies are retained only when the user enables them.
- **No implicit actions.** Calls request answers. Cross-agent work delegation requires a normal human-approved task outside the v1 call contract.

## 11. Installer and launch experience

`bridge install`:

1. Installs the combined Bridge Channel/tool server for Claude and the normal Bridge MCP tool server for Codex.
2. Configures the Claude adapter/plugin and reports whether it must use the research-preview development flag or is allowed normally.
3. Creates the router token and state directory with user-only permissions.
4. Writes sentinel-fenced coordination guidance into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` without clobbering existing content.
5. Installs no Claude prompt hooks and no spool-drain behavior.
6. Reports every changed file and command needed to undo the installation.

The normal entry points after installation are:

```text
bridge claude [args...]
bridge codex [args...]
bridge roster
bridge doctor
```

The wrappers should feel transparent: signals, exit status, terminal size changes, and supported CLI arguments pass through. They print the Bridge address once at startup and otherwise leave the vendor TUI alone.

`bridge doctor` verifies:

- router socket/token ownership and permissions;
- Claude Channel availability, version, organization policy, and configured launch mode;
- Codex App Server and remote-TUI flag availability;
- MCP registration on both families;
- stale session cleanup and app-server process health;
- a local loopback protocol probe without spending model tokens.

## 12. Failure modes

- **Target offline.** Return `unreachable`; never spawn a replacement. Async queued calls are failed and the caller receives a result event if still connected.
- **Target busy.** Queue until idle or deadline. Never steer an unrelated active turn in v1.
- **Router restart.** Recover queued call metadata from SQLite, reconnect adapters, and fail calls whose live endpoints did not return within a short recovery window.
- **Claude Channel unavailable or policy-blocked.** `bridge doctor` explains the exact limitation. The session may use Bridge tools outbound but is marked inbound-unreachable.
- **Codex App Server disconnect.** Mark the session unreachable, preserve transcript/call state, and let the wrapper offer a clean resume.
- **Agent omits `reply`.** The call times out; Codex final-message fallback is permitted only if its experiment verdict validates correlation.
- **Permission prompt.** The live session follows its normal policy. Bridge neither approves nor denies on another agent's behalf.
- **Two agents edit one file.** Bridge does not lock files. Agents should text intent before touching shared ownership; git/worktree discipline remains external.
- **Loop or cost blowup.** Hop budget, rate cap, queue cap, deadlines, and transcript make the behavior bounded and visible.
- **Vendor protocol drift.** Adapters are version-gated; doctor reports unsupported versions rather than guessing.

## 13. Resolved legacy CLI experiments

The first architecture's experiments were completed on 2026-08-26 against disposable sessions in `~/bridge-lab`. Their results remain ground truth even though the associated headless architecture is no longer used:

- **Claude live resume is unsafe.** `claude -p --resume <live-id>` appends without a writer lock. The open TUI does not observe the append, keeps a stale parent pointer, and the user's next message creates a divergent branch in the same conversation file. Bridge must never resume a live Claude session and must never offer warm resume as a fallback.
- **Codex live resume fails safely.** `codex exec resume <locked-id>` returns `-32600` because the thread already has an active writer. Bridge still does not use resume for live calls.
- **`codex queue` is a real mailbox.** It accepts a message for a non-running persisted thread and the thread processes it on its next run. A new Codex session with no rollout is not yet addressable. This remains useful diagnostic information, but managed v1 calls use App Server turns.
- **Legacy Codex pid mapping is possible through `lsof`.** The TUI holds thread lock file descriptors; one TUI can hold a main and child lock, so inference is not sufficient for exact v1 identity. The managed one-App-Server-per-wrapper design removes the ambiguity.
- Claude holds no session file open, so it has no equivalent `lsof` identity fallback. mtime is not a liveness signal. Parent-pid walking worked but is also replaced by wrapper-supplied identity.
- For Codex CLI resume syntax, `--sandbox` must precede `resume`. This is retained only as a regression note; v1 live calls do not resume.

These findings directly justify the hard no-substitute/no-resume rules in §§2–3 and the forbidden-fallback tests in Task 8.

## 14. New live-transport experiments required before gated implementation

Results are recorded verbatim under `docs/experiments/`.

- **E — Claude Channel delivery and reply.** Build the smallest local development channel. Verify that an event reaches the exact idle session, causes a turn, can call a reply tool, inherits `BRIDGE_SESSION_ID`, and behaves safely when the session is already working. Record policy/authentication requirements.
- **F — Codex App Server shared control.** Start an App Server on a Unix socket, attach a remote TUI, connect a second Bridge client, identify the exact displayed thread, start a turn while idle, receive item/turn events, and verify whether the final agent message can be correlated as fallback.
- **G — Busy-session serialization.** For both families, send a call during an unrelated active turn. Establish whether vendor-native queuing is sufficient or the adapter must buffer until an idle notification. The verdict must prove no accidental `turn/steer` behavior.
- **H — Lifecycle and reconnect.** Kill/restart the router, channel adapter, App Server, and TUI one at a time. Pin how session ids, reachability, queued deadlines, and resumption behave.

## 15. v1 scope

**IN:** local router daemon; Bridge-managed Claude and Codex wrappers; Claude two-way Channel adapter; per-session Codex App Server with remote TUI; live `roster`, `call`, `call_async`, `text`, `reply`, and `transcript`; busy-session FIFO; exact-session async-result push; SQLite state; hop/rate/queue guardrails; installer; doctor; live cross-family tests.

**OUT:** snapshot/headless consult fallback; warm resume; carbon copies; spool files; Claude prompt hooks; arbitrary-session adoption; cross-machine/cloud routing; permission relay; per-call write elevation; file locking; aliases/contacts file; agent-facing inbox polling; voice/audio; more than one hop.

## 16. Open questions

1. New live-transport Experiments E–H must be completed before their gated adapters are finalized. Legacy Experiments A–D are resolved in §13.
2. Claude Channels are a research preview; the distribution path for a custom production channel may require marketplace/organization allowlisting. V1 supports the documented development path with an explicit warning.
3. Codex App Server WebSocket/remote transports are documented as experimental. V1 uses a local Unix socket only and pins supported Codex versions.
4. Claude does not provide a hard per-event sandbox override. The v1 call contract is answer-only guidance plus the session's existing permissions, not cryptographic enforcement.
5. The exact definition of Claude `busy` available to a custom channel must be fixed by Experiment G; delivery ordering is a release blocker.
