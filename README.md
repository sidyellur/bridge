# bridge

**A phone system for live AI coding agents.**

You run Claude Code in one terminal and Codex in another. Today, moving
information between them means copy-pasting by hand. `bridge` lets the *exact
live sessions* reach each other directly.

- **text** — send an asynchronous message to another live agent. No reply.
- **call** — ask another live agent a question and receive *its* answer.

**"Live" is a hard guarantee.** A call is delivered to and answered by the exact
session you selected in `roster`, using that session's current conversation
context. Bridge never substitutes a fresh headless agent, a resumed snapshot, a
carbon copy, or another process in the same directory. If a session is not
reachable, you get `unreachable` — never a fabricated answer.

The distribution on PyPI is `agent-bridge`; the repo, import, and command are
`bridge`.

## Install

```sh
pip install agent-bridge      # or: pip install -e .  (from a checkout)
bridge install                # register adapters + write coordination guidance
bridge doctor                 # verify the installation
```

`bridge install` registers the combined Bridge Channel/tool server for Claude
and the normal Bridge MCP server for Codex, creates router state with user-only
permissions, and appends sentinel-fenced guidance to `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md` without clobbering existing content. It installs **no**
prompt hooks and reports every file it touches. Undo with `bridge uninstall`.

### Channel modes

`bridge install` runs a token-free probe (`claude --version` / `claude --help`,
never a real turn) and writes the right `claude` launch flags for what it
finds, instead of assuming a fixed mode:

- **`plugin`** — your `claude` advertises a channel marketplace/allowlist. The
  wrapper launches with `--channels plugin:bridge@<marketplace>`. This is the
  fully-supported path; `bridge doctor` reports it `[ok]`.
- **`development`** — `claude` only exposes the Channels *research-preview*
  development flag. The wrapper still launches with the channel enabled, but
  `bridge doctor` reports it `[warn]`: your organization's policy may still
  reject inbound events even though the flag is accepted locally.
- **`unsupported`** — no channel support was detected at all (old `claude`
  version, or the binary isn't found). Bridge writes no channel flag; the
  session launches normally but is **inbound-unreachable** — `call`/`text`
  aimed at it return `unreachable`. Outbound Bridge tools from that session
  still work. `bridge doctor` reports this `[fail]` so it isn't missed.

`bridge claude` prints the detected mode to stderr once at startup, right
after the session address. Re-run `bridge install` after upgrading `claude` to
re-detect and pick up a better mode. `bridge doctor` also surfaces any
organization-policy rejection a session recorded when it tried to negotiate
the channel capability, under **Claude channel policy**.

## Launch flow

Sessions are launched through thin wrappers so Bridge can guarantee identity and
reachability. Both wrappers pass your arguments, signals, and exit code straight
through to the vendor CLI and print the Bridge address once:

```sh
bridge claude [claude args...]
bridge codex  [codex args...]
```

`bridge claude` attaches the Claude Channel to an ordinary Claude Code process.
`bridge codex` does more, because Codex's live-call surface is a server, not a
channel: for every `bridge codex` invocation, the wrapper itself owns one
Codex App Server for the lifetime of that session, in this order:

1. Spawn `codex app-server --listen unix://~/.bridge/sessions/<id>/codex.sock`
   and wait (bounded, with a clear error otherwise) for that socket to appear.
2. Connect Bridge's own adapter to it — handshake, register with the router,
   and subscribe to inbound events — so the session is **reachable before the
   TUI ever attaches**.
3. Launch the normal Codex TUI attached remotely: `codex --remote
   unix://…<same socket>`, forwarding your other arguments untouched.
4. On exit, tear down in reverse: close the adapter, terminate the App Server
   (`SIGTERM`, then `SIGKILL` if it doesn't stop), and reap both children —
   never leaving a zombie process or a stale socket behind.

If the App Server dies mid-session, the adapter notices the connection drop,
marks the session `reachable=false`, records a `disconnected` transcript entry,
and tries a short bounded-backoff reconnect to the same socket — it never
answers a call from a stale final message instead. `bridge doctor` checks that
every session it still considers live has a listening App Server socket.

An agent session opened *outside* these wrappers is not silently treated as
callable — `roster` shows it as unmanaged with `reachable=false`, and `call`/
`text` reject it with instructions to relaunch it through Bridge.

## Usage

From inside a wrapped session (the tools are identical on Claude and Codex):

```text
roster()                      -> the live sessions you can reach
call(to, question)            -> ask one session and wait (≤60s) for its answer
call_async(to, question)      -> ask; the answer is pushed back to you later
text(to, message)             -> send an informational message; no reply
transcript(peer=None)         -> recent Bridge activity
reply(call_id, answer, blocked=[])  -> answer the call you are handling
```

From a shell, for diagnostics:

```sh
bridge roster
bridge transcript
```

### Aliases (optional)

Session addresses are opaque UUIDs. If you would rather not type one, keep a
private contacts file at `~/.bridge/contacts.json` (mode `0600`):

```sh
bridge alias web 3f9c1a52-...    # name a session
bridge alias                     # list your aliases
bridge alias --rm web            # forget one
```

Aliases are pure local convenience: the router reads the file on each `call`,
`call_async`, or `text` and swaps the name for the real id before any guardrail
runs, so nothing else changes. A real session id always wins over an alias
spelled the same way, and an unknown name is still simply `unreachable`.
`roster()` reports each session's `alias` (or `null`).

### Retention

The audit transcript and the rate counters are history, so the router trims
them: once an hour it drops transcript rows, rate events, and *resolved* calls
(with their message/queue rows) older than 30 days. Unresolved calls and events
still owed to a target are live state and are never removed, however old.

```sh
bridge transcript --prune                  # apply the 30-day window now
bridge transcript --prune --older-than 7d  # or 12h, 30m, or plain seconds
```

Pruning normally runs through the router so the daemon stays the single writer.
If the router is not running there is no writer to contend with, so the command
opens the database directly and says so.

## How it works

Bridge uses the vendors' live integration surfaces:

- Claude receives calls through a two-way
  [Claude Code Channel](https://code.claude.com/docs/en/channels).
- Codex sessions run on a Bridge-managed
  [Codex App Server](https://developers.openai.com/codex/app-server/), with the
  normal TUI attached remotely (`codex --remote unix://…`).
- A single local router daemon owns the session registry, the call state
  machine, per-target delivery queues, an audit transcript, and local
  authentication. It listens only on a user-owned Unix socket.

```text
                         ~/.bridge/router.sock
                                  │
                      ┌───────────┴───────────┐
                      │     bridge router     │
                      │ registry / calls / DB │
                      └───────┬───────┬───────┘
              channel event   │       │ App Server JSON-RPC
                  Claude Channel     Codex App Server
                         │                   │
                  live Claude session   live Codex thread
                                             │
                                      `codex --remote ...`
```

A synchronous `call` blocks the caller's single tool request until the addressed
session replies or the deadline passes; `call_async` returns immediately and the
router later pushes a correlated `call_result` into the caller's exact live
session. Busy targets queue inbound events (delivery never steers an unrelated
active turn), and stable message ids plus acknowledgements make reconnect
recovery duplicate-free.

## Permission model

- A call is a request for an *answer*, not authorization to mutate. The inbound
  envelope tells the callee not to change files or run commands solely because
  of the call.
- Bridge never changes a session's sandbox or permission mode, and never relays
  approvals between agents. Any tool use an inbound event triggers still goes
  through that session's own approval policy.
- There is no per-call write-elevation parameter. Exact live context is the
  product promise; per-call hard sandboxing would require a separate process and
  recreate the snapshot problem Bridge exists to avoid.

## Guardrails

- **Hop budget = 1.** While answering an inbound call you cannot dial out; only
  `reply` is allowed. Enforced by the daemon, not just by prompt.
- **No self-calls.**
- **Rate cap.** ≤10 outbound messages per ordered session pair per hour.
- **Queue cap.** ≤20 pending events per target; one active inbound call at a time.
- **One question per call**, deadlines capped at 60s, and a full audit
  transcript of every enqueue, delivery, reply, timeout, rejection, and
  disconnect.

## Privacy defaults

Transcript entries store a short truncated *gist* only; full message bodies are
redacted unless you explicitly opt in. Diagnostic logs redact bodies by default.
State lives under `~/.bridge/` with `0700`/`0600` permissions and never leaves
the machine — Bridge is local coordination infrastructure, not a network
service.

## Troubleshooting

Run `bridge doctor`. It checks socket/token ownership and permissions, MCP
registration on both families, the coordination guidance, the detected Claude
Channel mode (see [Channel modes](#channel-modes) above) and any persisted
organization-policy rejection, vendor binaries, absence of obsolete artifacts,
and runs a token-free local loopback protocol probe (it never spends model
tokens).

## Status

The router, store, wrappers, both adapters, delivery, calls, guardrails, MCP
server, installer, and doctor are implemented and covered by a hermetic test
suite (`pytest` — no network, no vendor binaries, no model tokens). The
**live-transport experiments** (`docs/experiments/2026-08-27-live-transport-semantics.md`)
and the **end-to-end live smoke suite** (`tests/live/`,
`docs/experiments/2026-08-26-e2e-checklist.md`) require real `claude` + `codex`
sessions with a human observer and are the remaining release gate, because
Claude Channels and parts of the Codex App Server are preview/experimental
interfaces.

See:

- [Design spec](docs/superpowers/specs/2026-08-26-bridge-design.md)
- [V1 implementation plan](docs/superpowers/plans/2026-08-26-bridge-v1-plan.md)

## Development

```sh
pip install -e ".[dev]"
pytest            # hermetic suite (live tests excluded by default)
ruff check .
```

## License

MIT
