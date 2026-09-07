# Live-transport experiments E–H

Date: 2026-08-27
Spec: `docs/superpowers/specs/2026-08-26-bridge-design.md` §14
Plan: `docs/superpowers/plans/2026-08-26-bridge-v1-plan.md` Task 1

> **Status: NOT YET RUN.** Experiments E–H require two live vendor sessions
> (a real `claude` and a real `codex`) *and* a human observing both TUIs, on a
> host where Claude Code Channels (research preview) and the Codex App Server
> are available. They cannot be executed in CI or in the ephemeral build
> container used to develop the code, so every `Verdict:` below is `TBD` until a
> human runs the procedure on a suitable machine and records raw commands,
> installed versions, and output here.
>
> The adapters in `src/bridge/` are written against the documented contracts
> (`tests/fixtures/codex_protocol/v1.json` pins the Codex side) and are covered
> by hermetic contract tests. These experiments validate that those contracts
> match the real vendors before the `-m live` suites are trusted.

## How to run

On a macOS/Linux host with `claude` and `codex` installed and the Bridge
package installed (`pip install -e .` then `bridge install`):

1. Open two terminals. In one, `bridge claude`. In the other, `bridge codex`.
2. Follow each experiment's procedure, pasting the exact commands and their
   output under the experiment.
3. Replace the `Verdict:` line with a one-line PASS/FAIL plus the decisive
   evidence. Do not leave `TBD`.

### Running with `bridge lab`

`bridge lab` is the harness for this document. It does not decide anything —
it records versions, fires each experiment's stimulus through the running
router, captures both directions of the live wire traffic, and shows you what
it saw. **You** read the two TUIs and write the verdict.

```sh
# 1. Open a run: records `claude --version`, `codex --version`, `bridge
#    --version`, gates on `bridge doctor` (a FAIL refuses to proceed), and
#    creates docs/experiments/runs/<UTC timestamp>/ with versions.json and one
#    capture target per experiment.
bridge lab prepare               # add --full to keep message bodies verbatim

# 2. Export the capture directory it printed in EVERY terminal that will host a
#    session, BEFORE launching the vendor, so the adapters and the router write
#    their frames into the run.
export BRIDGE_LAB_CAPTURE=/abs/path/to/docs/experiments/runs/<stamp>
bridge claude                    # terminal 1
bridge codex                     # terminal 2

# 3. Run each experiment from a third terminal. Session ids come from
#    `bridge roster`, or pass them explicitly.
bridge lab run E --claude <claude-session-id>
bridge lab run F --codex  <codex-session-id>
bridge lab run G --codex  <codex-session-id>    # then again with --claude
bridge lab run H --claude <id> --codex <id>

# 4. Record what you observed. `TBD` is refused; the run directory is linked.
bridge lab verdict E PASS "envelope reached claude-…, reply(call_id) returned"

# 5. The Task 1 verify, as an exit code.
bridge lab report
```

What each `run` does:

| | stimulus | success signal |
|---|---|---|
| `E` | delivers a `[bridge call]` envelope to the named Claude session | a `notifications/claude/channel` frame carrying that `call_id` **and** a recorded `reply` |
| `F` | causes a `turn/start` on the bound Codex thread | `turn/started` → `turn/completed` correlated through the `turn_id` the `turn/start` response returned |
| `G` | holds a `text` while the target reports `working`, then watches for delivery on idle | admission `queued`, delivery after idle, and **zero** `turn/steer` frames in the capture (a hard failure if any appears) |
| `H` | scripted "now kill *component*, press Enter" prompts | the roster `reachable` transition plus the transcript delta (queued/expired/timeout deadlines) for each step |

Capture notes:

* Traffic capture is strictly opt-in. With `BRIDGE_LAB_CAPTURE` unset nothing is
  hooked, opened, or written.
* Message bodies (`text`, `message`, `question`, `answer`) are redacted to
  `<redacted:N chars>` so a capture can be pasted here; routing metadata
  (`call_id`, `thread_id`, `turn_id`, `kind`, `from`) is preserved. Pass
  `bridge lab prepare --full` (and export `BRIDGE_LAB_CAPTURE_FULL=1`) when a
  verdict genuinely needs the bodies.
* Each `bridge lab run X` writes the frames it observed plus a summary record to
  `<run>/X.jsonl`. Cite that path in the verdict.

`bridge lab` never issues `turn/steer`, never resumes a session, and never
writes a verdict of its own.

---

## Experiment E — Claude Channel delivery and reply

**Question.** Does an event delivered over a Claude development Channel reach the
*exact* idle session, cause a turn, let the session call a reply tool, inherit
`BRIDGE_SESSION_ID`, and behave safely when the session is already working?
Record the policy/authentication requirements and whether the official
TypeScript MCP SDK is required or a Python channel suffices.

**Procedure.** Build the smallest local development channel; from a second
process, deliver a `[bridge call]` envelope to the wrapped Claude session and
confirm it appears in that conversation and elicits `reply(call_id, ...)`. Repeat
while the session is mid-turn to observe busy behavior. Capture any
research-preview allowlist or organization-policy errors verbatim.

Verdict: PASS — 2026-09-07 run 20260907T035740Z, claude 2.1.263 launched via bridge claude --dangerously-load-development-channels server:bridge. Human observed in the exact idle session: '← bridge: [bridge call] call_id: 407e1a9e-…' rendered inline, Claude called the reply tool in that conversation ('Called bridge'), reply accepted, no files changed / no commands run. Wire (E.jsonl): notifications/claude/channel {content, meta:{kind:call, call_id}} out at 03:59:18, tools/call reply with the same call_id in at 03:59:31 (13s), answered_by=live-session. Handshake record: client claude-code 2.1.263, client capabilities roots+elicitation only (no claude/channel client cap) — no allowlist/organization-policy warning reported. Python MCP server suffices; no TypeScript SDK needed. (run: docs/experiments/runs/20260907T035740Z)

---

## Experiment F — Codex App Server shared control

**Question.** Can a Bridge client and a remote Codex TUI share one App Server so
that the exact displayed thread is identifiable, a turn can be started while
idle, item/turn events stream, and the final agent message can (or cannot) be
correlated as a reply fallback?

**Procedure.** Start an App Server on a Unix socket; attach `codex --remote
unix://…`; connect a second Bridge client; identify the thread from
`thread/started`; issue `turn/start` while idle; record `turn/started`,
`item/agent_message*`, and `turn/completed`; determine whether the final message
reliably maps to the started turn.

Verdict: FAIL — 2026-09-07 run 20260907T035740Z, codex-cli 0.151.0: bridge codex crashed at initialize with JsonRpcError: connection closed. Reproduced by hand (App Server stderr empty): (1) TRANSPORT — codex app-server --listen unix://PATH speaks WebSocket over the Unix socket (HTTP Upgrade → 'HTTP/1.1 101 Switching Protocols'); a raw newline-JSON line is closed silently. Bridge sends JSONL, which only stdio:// accepts. (2) SCHEMA — over stdio Bridge's initialize is accepted but the result is {userAgent, codexHome, platformFamily, platformOs} with no protocolVersion/serverInfo/capabilities, so CodexAppServerClient.initialize would raise UnsupportedCodexVersion; the real protocol (docs + codex app-server generate-json-schema, 98 client methods) is camelCase (threadId, item/agentMessage/delta) and initialize takes clientInfo{name,title,version} only. Bridge's pinned codex-app-server/1 fixture was assumed, not recorded. Shared-App-Server semantics could not be tested. Follow-up: rework the Codex adapter (WebSocket-over-AF_UNIX client, fixture regenerated from the real schema) and re-run F. (run: docs/experiments/runs/20260907T035740Z)

---

## Experiment G — Busy-session serialization

**Question.** For both families, is an inbound call delivered during an unrelated
active turn serialized (queued until idle) with no accidental `turn/steer`?

**Procedure.** With each session mid-turn, deliver a call and observe whether the
vendor queues it natively or the adapter must buffer until an idle notification.
Prove no steer occurs.

Verdict: PASS — CLAUDE HALF (2026-09-07 run 20260907T035740Z, claude 2.1.263): human started a ~30s essay turn; lab delivered a [bridge text] at 04:04:48 mid-turn. Observed: the essay streamed to 'Worked for 30s · done' untouched; only then did '← bridge: [bridge text] from: bridge-lab …' render and Claude open a short follow-up turn: 'Noted … informational only, so no reply was sent and no action was taken.' No steer. Note: Bridge itself cannot hold for Claude — Claude Code sends the channel server no busy/turn signal (only initialize/initialized/tools/list/tools/call), so held_while_busy=false and the harness's ok=false are by construction; the no-steer guarantee for Claude rests on Claude Code's own channel queueing, which held. CODEX HALF: pending. (run: docs/experiments/runs/20260907T035740Z)

---

## Experiment H — Lifecycle and reconnect

**Question.** How do session ids, reachability, queued deadlines, and resumption
behave when the router, channel adapter, App Server, and TUI are killed and
restarted one at a time?

**Procedure.** Kill/restart each component in turn; record how the roster's
`reachable`, queued call deadlines, and re-binding of the Bridge address behave.

Verdict: TBD (requires live Claude+Codex sessions + human observer)
