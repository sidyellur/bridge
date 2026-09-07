# Design: speak the real Codex App Server contract (codex-cli 0.151.0)

Amends `2026-08-26-bridge-design.md` §6 (Codex adapter) and §7 (calls). Where that spec assumed a wire contract, this document replaces the assumption with what was verified live on 2026-09-07 (Experiment F, run `docs/experiments/runs/20260907T035740Z`; probe report kept out of the repo under `.superpowers/codex-contract-2026-09-07.md`).

## What was wrong

Bridge's Codex adapter (`src/bridge/codex_app_server.py`, `src/bridge/adapters/codex.py`, fake `tests/fakes/codex_app_server.py`, fixture `tests/fixtures/codex_protocol/v1.json`) implements an invented contract (`protocolVersion: "codex-app-server/1"`, snake_case fields, `runtime/status`, `item/agent_message`). Against the real product it dies at the first frame:

1. `codex app-server --listen unix://PATH` speaks **WebSocket (RFC 6455) over the Unix socket**. Only `stdio://` is newline-delimited JSON. A raw JSON line is closed silently.
2. `initialize` negotiates **no protocol version**; its result is `{userAgent, codexHome, platformFamily, platformOs}`.
3. Everything is camelCase; the turn/item lifecycle, status model, and notification names differ from the fixture on every line (24-row diff in the probe report).

## Verified facts the design rests on

- **Framing**: HTTP Upgrade with only `Sec-WebSocket-Key` required (path/Host/Origin/subprotocol ignored, no auth on `unix://`); client frames must be masked; text frames; exactly one JSON-RPC message per frame; server pongs client pings, never pings, never echoes close (EOF = clean close). Server messages omit `"jsonrpc"`; every notification carries a top-level `emittedAtMs`.
- **Initialize**: params `clientInfo{name,title,version}` + optional `capabilities{optOutNotificationMethods:[...]}` (works); then `{"method":"initialized"}`. Codex version is parseable from `userAgent` (`<client>/<codexVersion> (...)`).
- **Threads**: the remote TUI creates its thread eagerly on launch. `thread/started {thread}` and `thread/status/changed {threadId, status}` are **global broadcasts** to every connection; `thread/loaded/list` is server-global; `thread/read {threadId, includeTurns:false}` returns the `Thread` (with `cwd`, `status`). There is no `thread/subscribe`; **`thread/resume {threadId, excludeTurns:true}` is the subscribe verb** — it succeeds on threads that have had a turn and fails with `-32601 "list_turns is not supported yet"` on the live TUI thread in 0.151.0 (and `-32600 "no rollout found"` on an empty thread). A client disconnect does not close the thread.
- **Fanout**: `turn/*` and `item/*` reach **subscribers only** (verified with a real turn); `thread/status/changed` reaches everyone.
- **Turns**: `turn/start {threadId, input:[{type:"text", text}]}` → `{turn:{id, status, items,…}}`. Then `turn/started {threadId, turn}` → `item/started`/`item/agentMessage/delta {threadId, turnId, itemId, delta}`/`item/completed {threadId, turnId, item}` (final text = `item.type=="agentMessage"`, `item.text`) → `turn/completed {threadId, turn}` (`turn.status` ∈ completed|interrupted|failed|inProgress). There is no `item/agentMessage` final notification and no `turn_id` anywhere.
- **Status**: `ThreadStatus` is `{type: notLoaded|idle|systemError|active, activeFlags?}`. Map `active`→`working`, everything else→`idle` (surface `systemError`).
- **Forbidden**: `turn/steer` (exists, `{threadId, expectedTurnId, input}`), `review/start`, `turn/interrupt`.

## Design

### 1. Transport: `src/bridge/ws.py` (new, stdlib only)

A minimal RFC 6455 implementation over an already-connected `socket.socket`, both roles:

- `client_handshake(sock, *, host="localhost", path="/")` — sends the Upgrade request with a random key, validates `101` and `Sec-WebSocket-Accept`.
- `server_handshake(sock)` — reads the request, requires `Sec-WebSocket-Key`, replies `101` with the computed accept (used by the fake App Server so tests exercise the real client code path).
- `send_text(sock, payload: bytes, *, mask: bool)` and `recv_message(sock) -> bytes | None` — frames: text/binary/continuation reassembly, ping→pong auto-reply, close→`None`, EOF→`None`. Client role masks; server role does not. Frame lengths 7/16/64-bit.

No asyncio, no threads of its own; it is a codec the existing reader thread drives.

### 2. `RpcEndpoint` gets pluggable framing (`src/bridge/mcp.py`)

`RpcEndpoint(sock, name=..., framing=Framing.JSONL | Framing.WS_CLIENT | Framing.WS_SERVER)`. JSONL stays the default (Claude channel, Bridge MCP server, router protocol are untouched). The reader loop delegates to the framing to pull one message; `_send` delegates to push one. Parsing tolerates messages without `"jsonrpc"`, and ignores unknown envelope keys (`emittedAtMs`). One message per frame, never newline-joined, for WS.

### 3. `CodexAppServerClient` rewritten against the pinned fixture

- `initialize()` sends `clientInfo{name:"bridge", title:"Bridge", version}` + `capabilities.optOutNotificationMethods` for `remoteControl/status/changed`, `fs/changed`, `account/rateLimits/updated`, `thread/tokenUsage/updated`; then `initialized`. Parses `codex_version` from `userAgent`; `MIN_CODEX_VERSION = (0, 151, 0)`; below it raise `UnsupportedCodexVersion`, above it proceed (warn via the adapter's transcript, never hard-fail on a patch bump). No `protocolVersion` anywhere.
- **Thread binding** (the F algorithm): after `initialized`, call `thread/loaded/list` and `thread/read` each id; keep listening for the `thread/started` broadcast; candidate = a thread Bridge did not create itself (Bridge never calls `thread/start` in production — it only reads), preferring `cwd == session cwd`, else the newest. Bind on the first candidate; re-bind if the TUI later starts a different thread in the same cwd (last wins). Then `subscribe()` = `thread/resume {threadId, excludeTurns:true}`, tolerating `-32601`/`-32600` (record `subscribed: bool`).
- **State**: `thread/status/changed` for the bound thread drives `working`/`idle` (global, works unsubscribed). `turn/started`/`turn/completed` are corroborating only.
- `start_turn(text) -> turn_id` uses camelCase params and returns `result["turn"]["id"]`. Refuses while `working` (Bridge never queues a second turn on a busy thread; delivery queueing is the router's job).
- Correlation: `item/completed` with `item.type=="agentMessage"` and matching `turnId` → `on_agent_message(turn_id, text)`; `turn/completed` → `on_turn_completed(turn_id, status)`. If not subscribed, these never arrive; the adapter must not wait on them — the reply path for calls is Codex invoking Bridge's `reply` MCP tool, unchanged.
- `FORBIDDEN_METHODS = ("turn/steer", "turn/interrupt", "review/start")` with the existing source-level assertion test.

### 4. Adapter (`src/bridge/adapters/codex.py`)

- Registers as `idle`; flips on status changes; records `thread_id`, `subscribed`, `codex_version` in the session meta (`Paths.merge_session_meta`) so `bridge doctor` and `bridge lab` can show them.
- Disconnect: mark `reachable=false`, keep the thread id, bounded-backoff reconnect (existing), then re-run bind (the thread outlives the connection) — no longer treats disconnect as session death.
- The Experiment-F "final-message fallback" env gate stays off by default; correlation is now proven, so the gate's doc string points at this spec.

### 5. Fixture and fake

- `tests/fixtures/codex_protocol/codex-0.151.0.json` replaces `v1.json`: the §7 fixture from the probe report (transport facts, the six client requests Bridge uses, the eight notifications, type subsets, known-broken list, forbidden methods, state map, launch argv). The fake App Server and the client both import it, so they cannot drift from each other.
- `tests/fakes/codex_app_server.py` speaks WebSocket via `ws.server_handshake` + unmasked text frames, omits `"jsonrpc"`, adds `emittedAtMs`, models: eager thread creation with a global `thread/started`, a global `thread/status/changed`, subscriber-only `turn/*`/`item/*`, `thread/resume` success/`-32601` (configurable), `thread/loaded/list`, `thread/read`. It fails (closes the socket) on an unmasked client frame or a newline-joined payload — the two real-world traps.

### 6. Lab

`_run_f`'s correlation reads `result.turn.id`, `turn/started`/`turn/completed` `params.turn.id`, and `item/*` `params.turnId`. `_run_g`'s Codex half keeps waiting for `working` (now real). `bridge lab prepare` records the codex version already.

### 7. Out of scope

- A drift tool over `codex app-server generate-json-schema` (fixture `_regenerate` note documents the manual procedure).
- Claude-side honesty about busy state (Claude sends no signal; separate follow-up).
- `bridge codex --session-id` for Experiment H's Codex-restart step.

## Testing

Hermetic as before: `ws.py` unit tests against a pure-Python peer (handshake accept digest, masking, fragmentation, ping/pong, close, the two traps); `RpcEndpoint` framing tests; `CodexAppServerClient` against the WS fake for every branch of bind/subscribe/status/turn; adapter reconnect tests; a source-level forbidden-methods test; lab F correlation test on the new keys. Live gate: re-run F, then G (Codex), then H.
