# Plan: rework the Codex adapter against the real App Server contract (codex-cli 0.151.0)

**Spec:** `docs/superpowers/specs/2026-09-07-codex-app-server-contract-design.md` (binding; it amends `2026-08-26-bridge-design.md` §6/§7). **Evidence:** the verified wire-contract report at `.superpowers/codex-contract-2026-09-07.md` (verbatim frames, field tables, the 24-row assumed→real diff, the §7 fixture). Where this plan and the spec disagree, the spec wins; where the spec and the report disagree, the report's live frames win.

## Why

Bridge's Codex side implements an invented contract and dies on the first frame against the real product:

1. **Wrong transport.** `codex app-server --listen unix://PATH` speaks **WebSocket (RFC 6455)** over the AF_UNIX stream; only `stdio://` is newline-delimited JSON. `RpcEndpoint` writes a raw JSON line, and the server drops it and closes the socket with no error (report §1). Nothing after that can work.
2. **Wrong handshake.** `codex_app_server.py:24-25,95` sends `protocolVersion: "codex-app-server/1"`; no protocol version exists anywhere in the protocol. `initialize`'s result is `{userAgent, codexHome, platformFamily, platformOs}` — no `protocolVersion`, no `serverInfo` — so `codex_app_server.py:99-103` raises `UnsupportedCodexVersion` on a perfectly healthy server (report §2, diff rows 2–4).
3. **Wrong everything else.** `runtime/status` does not exist (`ThreadStatus` is a tagged union delivered by `thread/status/changed`); `item/agent_message` does not exist (final text is `item/completed` with `item.type=="agentMessage"`); `turn/start` takes `threadId` not `thread_id` and returns `{turn:{id,…}}` not `{turn_id}`; there is no `thread/subscribe` — **`thread/resume` is the subscribe verb**, and in 0.151.0 it fails `-32601 "list_turns is not supported yet"` on exactly the thread Bridge wants. 24 rows in report §8.
4. **Missing discovery.** Bridge waits passively for a `thread/started` addressed to its connection. That broadcast *is* global (verified), but the remote TUI creates its thread eagerly on launch — possibly before Bridge connects — so Bridge must also seed from `thread/loaded/list` + `thread/read` and disambiguate threads it created itself (report §3).
5. **Wrong fanout assumption.** Settled by the 2026-09-07 spike: with A running a turn, a passive B (initialize only, no resume) received **only** `thread/status/changed` (`{type:"active",activeFlags:[]}` then `{type:"idle"}`). `turn/started`, `item/started`, `item/agentMessage/delta`, `item/completed`, `turn/completed`, `thread/tokenUsage/updated`, `account/rateLimits/updated` all reached A and none reached B. So status is global, turn/item traffic is subscriber-only, and an unsubscribed Bridge must still work (busy/idle only).
6. **Wrong disconnect semantics.** A client disconnect does not close the thread and emits no `thread/closed`; `adapters/codex.py:167-169` treats a dropped App Server connection as session death instead of re-binding the surviving thread.

## Global Constraints

- Python ≥ 3.11. Lint: `ruff` (line-length 100; select E,F,I,UP,B,W). pytest runs with `filterwarnings = error` — any warning is a failure.
- **Zero dependencies.** The WebSocket implementation is stdlib-only: `socket`, `os`, `base64`, `hashlib`, `struct`. No `websockets`, no `wsproto`, no asyncio, no threads of its own — `ws.py` is a codec the existing `RpcEndpoint` reader thread drives.
- The suite is hermetic: no vendor binaries, no network, no model tokens, no reads/writes of the maintainer's real `~/.claude.json`, `~/.claude/`, `~/.codex/`. **Never spawn the real `codex`.** Every path, executable, clock, and probe is injected (`tests/conftest.py` autouse `BRIDGE_HOME` fence + `_no_internet`, `tests/fakes/`).
- Run tests and lint exactly as: `cd /Users/siddharthyellur/bridge && mkdir -p /tmp/bt && TMPDIR=/tmp/bt .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/bt/p` and `.venv/bin/ruff check .` — the short `TMPDIR` is required because macOS's default tmp path exceeds the AF_UNIX socket path limit. Starting suite: **421 passed, 8 deselected**. Every task's commit leaves the full suite green.
- **JSONL framing is frozen.** `Framing.JSONL` must stay byte-for-byte what `mcp.py:117` writes today (`json.dumps(obj, separators=(",", ":")) + b"\n"`, always with `"jsonrpc": "2.0"`). The Claude channel adapter, the Bridge MCP server, and the router protocol are untouched, and `tests/test_claude_channel.py`, `tests/test_server.py`, `tests/test_router.py`, `tests/test_protocol.py` must not change.
- **One source of truth for wire names.** Every method/field/status literal is defined **once**, in `src/bridge/codex_app_server.py`, as a module constant. The fake imports those constants (`from bridge.codex_app_server import M_TURN_START, …`) — it never repeats a literal — and `tests/test_codex_client.py::test_pinned_contract_matches_the_fixture` asserts the constants and `tests/fixtures/codex_protocol/codex-0.151.0.json` agree in both directions (no fixture key without a constant, no constant without a fixture key).
- **Cross-task interface — `src/bridge/ws.py`** (Task 1, used by Tasks 2/3/4):
  - `WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"`; `class WebSocketError(Exception)`.
  - `accept_key(key: str) -> str` — `base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()`.
  - `client_handshake(sock, *, host: str = "localhost", path: str = "/") -> None` — raises `WebSocketError` on a non-101 status or a wrong `sec-websocket-accept`.
  - `server_handshake(sock) -> dict[str, str]` — returns request headers, keys lowercased; raises `WebSocketError` when `Sec-WebSocket-Key` is absent (writes no response).
  - `send_text(sock, payload: bytes, *, mask: bool) -> None` — one text frame, `fin=1`, 7/16/64-bit length.
  - `recv_message(sock, *, require_mask: bool = False) -> bytes | None` — **contract:** returns the payload of one complete application message (text/binary, continuations reassembled); returns `None` on a close frame or EOF; auto-replies pong to ping and keeps reading; ignores pong; raises `WebSocketError` on a reserved opcode, a fragmented or >125-byte control frame, or (when `require_mask=True`) an unmasked frame.
- **Cross-task interface — `RpcEndpoint`** (Task 2): `class Framing(enum.Enum): JSONL = "jsonl"; WS_CLIENT = "ws-client"; WS_SERVER = "ws-server"`. New keyword-only `RpcEndpoint` arguments, all defaulting to today's behaviour: `framing: Framing = Framing.JSONL`, `include_jsonrpc: bool = True`, `on_parse_error: Callable[[bytes], None] | None = None`. New keyword-only `notify` arguments: `extra: Mapping[str, Any] | None = None` (merged into the envelope, e.g. `emittedAtMs`), `omit_empty_params: bool = False` (send no `params` key at all when params is falsy — required for `{"method":"initialized"}`).
- **Cross-task interface — fixture key paths** (Task 3, read by Tasks 4/6): `codex_version`, `supported_codex_versions`, `min_codex_version`, `transport.{framing,upgrade_request_path,client_frames_must_be_masked,messages_per_frame,jsonrpc_field_present,server_echoes_close_frame}`, `client_requests.<method>.{params,params_required,result}`, `client_notifications`, `server_notifications.<method>` (list of param names), `notification_envelope_extra_fields`, `types.*`, `thread_discovery.*`, `known_broken_0_151_0.*`, `forbidden_methods`, `bridge_state_map`, `launch_argv`, `_regenerate`.
- **Cross-task interface — session meta keys** (Task 5): `Paths.merge_session_meta(session_id, {...})` writes `thread_id: str`, `subscribed: bool`, `codex_version: str`, and (only when set) `codex_version_warning: str`, `last_thread_error: str`.
- **Deadlock trap.** `RpcEndpoint` responses are read by its own reader thread, so **no notification handler may issue an `rpc.request`** — it would wait for a reply nobody can read. Everything a notification triggers (re-subscribe, router updates) is submitted to `CodexAdapter`'s existing worker thread (`adapters/codex.py:57-58,107-108`).
- One commit per task, on the current branch `fix/codex-app-server-contract`. No push. Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_011U468ZiD3UB7R1kQ4EHqNb
  ```

---

## Task 1 — `src/bridge/ws.py`: a stdlib RFC 6455 client and server codec

**Files:** `src/bridge/ws.py` (new), `tests/test_ws.py` (new).

**Scope (TDD — write the failing tests first):**

1. Module docstring: this is a codec over an already-connected `socket.socket`, written because `codex app-server --listen unix://` speaks WebSocket (report §1); no third-party dependency is permitted.
2. Constants: `WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"`, `WS_VERSION = "13"`, `OP_CONT = 0x0`, `OP_TEXT = 0x1`, `OP_BINARY = 0x2`, `OP_CLOSE = 0x8`, `OP_PING = 0x9`, `OP_PONG = 0xA`, `MAX_CONTROL_PAYLOAD = 125`. `class WebSocketError(Exception)`.
3. `accept_key(key)` per Global Constraints. Unit-check against the RFC 6455 §1.3 vector: `"dGhlIHNhbXBsZSBub25jZQ=="` → `"s3pPLMBiTxaQ9kYGzzhZRbK+xOo="`.
4. `client_handshake(sock, *, host="localhost", path="/")`: generate `base64.b64encode(os.urandom(16)).decode()`; send exactly
   `GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n`.
   Send **no** `Sec-WebSocket-Protocol` and expect none back (report §1). Read until `\r\n\r\n`; require the status line to contain `101`, else `WebSocketError(f"websocket handshake failed: {status_line!r}")`; require `sec-websocket-accept` (header names compared case-insensitively) to equal `accept_key(key)`, else `WebSocketError("bad Sec-WebSocket-Accept")`.
5. `server_handshake(sock)`: read until `\r\n\r\n`, parse headers into a lowercased dict, ignore path/Host/Origin/Authorization/subprotocol entirely (all verified irrelevant); if `sec-websocket-key` is missing, raise `WebSocketError("missing Sec-WebSocket-Key")` **without writing a response** (the real server just closes); else write
   `HTTP/1.1 101 Switching Protocols\r\nconnection: Upgrade\r\nupgrade: websocket\r\nsec-websocket-accept: {accept}\r\n\r\n` (lowercase header names, matching the verbatim capture) and return the headers.
6. `_recv_exact(sock, n) -> bytes | None`: loop `sock.recv`, returning `None` on EOF mid-frame (never partial).
7. `send_text(sock, payload, *, mask)`: byte 0 = `0x80 | OP_TEXT`; length 7-bit for `< 126`, `126` + `>H` for `<= 0xFFFF`, `127` + `>Q` above; mask bit `0x80` on byte 1 when `mask`, followed by a 4-byte `os.urandom(4)` key and the XOR'd payload; one `sendall`.
8. `recv_message(sock, *, require_mask=False)` per the Global Constraints contract. Implementation notes: reassemble `OP_CONT` into the opcode that started the message; on `OP_PING` reply `send_frame(sock, OP_PONG, payload, mask=require_mask is False)` — i.e. a client role masks its pong, a server role does not — then continue the loop; on `OP_PONG` continue; on `OP_CLOSE` return `None`; on any EOF return `None`.
9. **Trap 1** (server role): `require_mask=True` and `masked == False` → `WebSocketError("unmasked client frame")`. Callers close the socket; that is what the real server does (report §1: no close frame, just EOF).
10. **Trap 2** (documented at this layer, enforced in Task 3): a frame carrying two JSON objects is one message here; splitting or newline-joining inside a frame is a **client bug** — `send_text` must never append a newline and never coalesce.

**Tests (`tests/test_ws.py`, pure `socket.socketpair()` — a helper `_server_thread(sock)` runs `server_handshake` in a `threading.Thread`):**
- `test_accept_key_matches_the_rfc_vector` — the §1.3 vector above.
- `test_handshake_round_trip_over_a_socketpair` — client succeeds; the server's returned headers carry `sec-websocket-key`; the response's `sec-websocket-accept` equals `accept_key(key)`.
- `test_server_handshake_requires_only_the_key` — a request with no `Host`, an `Origin`, an `Authorization`, and a deep path still yields 101.
- `test_server_handshake_without_a_key_raises_and_writes_nothing` — `WebSocketError`, and the peer reads `b""` (no bytes written).
- `test_client_handshake_rejects_a_non_101_status` and `test_client_handshake_rejects_a_wrong_accept_digest` — hand-written responses.
- `test_client_frames_are_masked_and_server_frames_are_not` — inspect raw byte 1 `& 0x80` on both directions.
- `test_round_trip_across_all_three_length_forms` — payloads of 5, 200, and 70_000 bytes, each direction.
- `test_utf8_payload_round_trips` — a payload with `"é"` and `"日"`.
- `test_continuation_frames_are_reassembled` — hand-written `fin=0 OP_TEXT` + `fin=0 OP_CONT` + `fin=1 OP_CONT` → one message equal to the concatenation.
- `test_ping_is_answered_with_a_pong_and_reading_continues` — peer sends ping(`b"hi"`) then a text; `recv_message` returns the text and the peer reads a pong frame with payload `b"hi"`.
- `test_pong_frames_are_ignored`.
- `test_close_frame_returns_none` (peer sends `0x8` with `1000 "bye"`) and `test_eof_returns_none` (peer closes without a close frame).
- `test_server_role_rejects_an_unmasked_client_frame` — `require_mask=True` → `WebSocketError`.
- `test_client_role_accepts_unmasked_server_frames`.
- `test_fragmented_control_frame_is_rejected` and `test_oversized_control_frame_is_rejected` (`len > 125`).
- `test_send_text_never_appends_a_newline` — a payload of two JSON objects joined by `\n` arrives as exactly one message with the newline intact (the trap is the caller's, not the codec's).

**Verify:** `pytest tests/test_ws.py` green; full suite green; `ruff check .` clean. Commit: `ws: stdlib RFC 6455 client/server codec for the Codex App Server socket`.

---

## Task 2 — `RpcEndpoint` gets pluggable framing

**Files:** `src/bridge/mcp.py`, `tests/test_mcp.py` (new).

**Scope (TDD):**

1. Add `class Framing(enum.Enum)` with members `JSONL`, `WS_CLIENT`, `WS_SERVER` (values `"jsonl"`, `"ws-client"`, `"ws-server"`). Import `ws` lazily inside the WS branches so `mcp.py` keeps no import cost for JSONL users.
2. `RpcEndpoint.__init__` gains the three keyword-only arguments from Global Constraints (`framing`, `include_jsonrpc`, `on_parse_error`). Store them; everything else (`_buf`, `_methods`, `_notifications`, `_pending`, locks, thread name `rpc-{name}`) is unchanged.
3. `start()`: when `framing is Framing.WS_CLIENT`, call `ws.client_handshake(self._sock)` **synchronously before** starting the reader thread (so a handshake failure raises to the caller); for `WS_SERVER` and `JSONL`, start the thread unchanged. `_read_loop` begins with `if self._framing is Framing.WS_SERVER: ws.server_handshake(self._sock)` — the server handshake must happen **on the reader thread**, because a fake constructed before its peer connects would otherwise block its own constructor.
4. `_read_loop` body: keep today's `recv(65536)`/`\n`-partition loop verbatim under `Framing.JSONL` (`mcp.py:124-147`). For the WS framings, loop `payload = ws.recv_message(self._sock, require_mask=self._framing is Framing.WS_SERVER)`; `None` → `unexpected = not self._closed; break`; else `self._handle_line(payload)`. Catch `ws.WebSocketError` alongside `OSError` in the same `except` so a trap-triggering peer closes the connection and `_fail_pending()`/`on_close` still run (`mcp.py:139-147`).
5. `_send`: build the envelope, drop `"jsonrpc"` when `include_jsonrpc` is False, then `data = json.dumps(obj, separators=(",", ":")).encode("utf-8")`; JSONL appends `b"\n"` and `sendall`s exactly as today; WS calls `ws.send_text(self._sock, data, mask=self._framing is Framing.WS_CLIENT)` with **no newline and one message per frame**. Keep the `_send_lock` and the `except OSError: pass`.
6. `notify(method, params=None, *, extra=None, omit_empty_params=False)`: envelope is `{"jsonrpc": "2.0", "method": method}` (+ `"params"` unless `omit_empty_params and not params`, else `params or {}`), then `envelope.update(extra or {})`. `request` is unchanged.
7. `_handle_line`: on `json.JSONDecodeError`, call `self._on_parse_error(line)` when set, then return (today's silent-ignore stays the default). Add an explicit comment that dispatch already keys only on `"method"`/`"id"`, so a message without `"jsonrpc"` and a notification carrying an extra `emittedAtMs` are handled by construction — the tests below pin that.
8. Update the module docstring: the endpoint speaks newline-JSON **or** RFC 6455 text frames; JSONL is the default and is what MCP stdio and Bridge's router protocol use; WS exists for `codex app-server --listen unix://`.

**Tests (`tests/test_mcp.py`, `socket.socketpair()` pairs; one endpoint per end):**
- `test_jsonl_request_bytes_are_unchanged` — a raw peer socket receives exactly `b'{"jsonrpc":"2.0","id":1,"method":"m","params":{}}\n'`.
- `test_jsonl_notify_bytes_are_unchanged` — exactly `b'{"jsonrpc":"2.0","method":"n","params":{"a":1}}\n'`.
- `test_jsonl_round_trip_request_response_and_notification` — two `RpcEndpoint`s, default framing.
- `test_ws_client_to_ws_server_round_trip` — `WS_CLIENT` ↔ `WS_SERVER`: handshake, request/response, notification both directions.
- `test_ws_send_is_one_message_per_frame_with_no_newline` — read the raw frame off a hand-rolled peer; payload has no `\n` and parses as exactly one object.
- `test_ws_server_framing_handshakes_on_the_reader_thread` — construct the server endpoint and `start()` it **before** the client connects/handshakes; the round trip still completes.
- `test_response_without_jsonrpc_is_delivered_to_the_pending_request` — peer replies `{"id":1,"result":{"ok":true}}`.
- `test_error_response_without_jsonrpc_raises_jsonrpcerror` — `{"id":1,"error":{"code":-32600,"message":"Not initialized"}}` → `JsonRpcError(code=-32600)`.
- `test_notification_with_extra_envelope_keys_is_dispatched` — `{"method":"x","params":{},"emittedAtMs":1788754571273}` reaches the handler.
- `test_include_jsonrpc_false_omits_the_field` — no `"jsonrpc"` key in any sent envelope.
- `test_notify_extra_merges_envelope_keys` — `extra={"emittedAtMs": 7}` appears top-level, beside `method`/`params`.
- `test_notify_omit_empty_params_sends_no_params_key` — the envelope is exactly `{"jsonrpc":"2.0","method":"initialized"}`.
- `test_on_parse_error_receives_the_raw_payload_and_default_is_silent`.
- `test_ws_peer_close_fails_pending_requests_and_calls_on_close` — pending `request` raises `JsonRpcError(INTERNAL_ERROR, "connection closed")`; `on_close` fires once.

**Verify:** `pytest tests/test_mcp.py tests/test_claude_channel.py tests/test_server.py tests/test_router.py` green; full suite green; `ruff check .` clean. Commit: `mcp: pluggable JSONL/WebSocket framing on RpcEndpoint (JSONL byte-identical)`.

---

## Task 3 — Pin the 0.151.0 fixture and rewrite the fake App Server onto it

**Files:** `tests/fixtures/codex_protocol/codex-0.151.0.json` (new), `tests/fakes/codex_app_server.py` (new `FakeCodexAppServer`; the current class is renamed `LegacyFakeCodexAppServer` and kept for exactly one commit), `tests/test_codex_adapter.py` + `tests/test_lab_cli.py` (import-rename only), `tests/test_codex_fake.py` (new).

> **Why the legacy class survives one commit:** `tests/test_codex_adapter.py` and `tests/test_lab_cli.py` drive the *old* client against the *old* fake. Flipping the fake and the client in one commit would be a ~900-line change; flipping only the fake would leave the suite red. Renaming the legacy class (a two-line diff in each consumer) keeps every commit green, and Task 4 deletes it.

**Scope (TDD):**

1. Write `tests/fixtures/codex_protocol/codex-0.151.0.json` from report §7, adjusted to what Bridge actually uses:
   - `_comment` (verbatim from §7), `codex_version: "0.151.0"`, `supported_codex_versions: ["0.151.0"]`, `min_codex_version: [0, 151, 0]`.
   - `transport`: `{"listen_url": "unix://{socket_path}", "framing": "websocket", "upgrade_request_path": "/", "required_upgrade_headers": ["Upgrade","Connection","Sec-WebSocket-Key","Sec-WebSocket-Version"], "subprotocol": null, "client_frames_must_be_masked": true, "opcode": "text", "messages_per_frame": 1, "jsonrpc_field_present": false, "server_echoes_close_frame": false}`.
   - `client_requests`: `initialize` (`params ["clientInfo","capabilities"]`, required `["clientInfo"]`, result `["userAgent","codexHome","platformFamily","platformOs"]`), `thread/loaded/list` (`["cursor","limit"]`, `[]`, `["data","nextCursor"]`), `thread/read` (`["threadId","includeTurns"]`, `["threadId"]`, `["thread"]`), `thread/resume` (`["threadId","excludeTurns"]`, `["threadId"]`, `["thread"]`), `thread/unsubscribe` (`["threadId"]`, `["threadId"]`, `["status"]`), `turn/start` (`["threadId","input"]`, `["threadId","input"]`, `["turn"]`).
   - `client_notifications: ["initialized"]`.
   - `server_notifications`: `thread/started ["thread"]`, `thread/status/changed ["threadId","status"]`, `turn/started ["threadId","turn"]`, `turn/completed ["threadId","turn"]`, `item/started ["threadId","turnId","item","startedAtMs"]`, `item/agentMessage/delta ["threadId","turnId","itemId","delta"]`, `item/completed ["threadId","turnId","item","completedAtMs"]`.
   - `notification_envelope_extra_fields: ["emittedAtMs"]`; `types` exactly as §7 (`UserInput.text`, `Turn`, `TurnStatus`, `ThreadStatus`, `ThreadStatus.active_extra_fields`, `ThreadActiveFlag`, `AgentMessageThreadItem`, `Thread.required_subset`); `thread_discovery` as §7 plus `"turn_and_item_notifications_are_subscriber_only": true` and `"thread_status_changed_is_global_broadcast": true` (the 2026-09-07 two-client spike); `known_broken_0_151_0` as §7; `forbidden_methods: ["turn/steer","turn/interrupt","review/start"]`; `bridge_state_map: {"idle":"idle","active":"working","notLoaded":"idle","systemError":"idle"}`; `launch_argv: ["{binary}","app-server","--listen","unix://{socket_path}"]`; `_regenerate` verbatim from §7.
2. Rename the existing `FakeCodexAppServer` to `LegacyFakeCodexAppServer` (body untouched) and fix the two import sites (`tests/test_codex_adapter.py:28`, `tests/test_lab_cli.py:41`) plus the two construction sites (`tests/test_codex_adapter.py:47`, `tests/test_lab_cli.py:128`, and the `reconnect()` helper at `tests/test_codex_adapter.py:219`).
3. New `FakeCodexAppServer(sock, *, codex_version="0.151.0", thread_id="thread-abc", cwd="/tmp/peer", agent_message="", auto_complete=True, resume_result="ok", eager_thread=True, now_ms=None)`:
   - `RpcEndpoint(sock, name="fake-codex-app-server", framing=Framing.WS_SERVER, include_jsonrpc=False, on_parse_error=self._on_parse_error)`, then `.start()`.
   - `_notify(method, params)` → `self.rpc.notify(method, params, extra={"emittedAtMs": self._now_ms()})`; **every** notification goes through it.
   - `initialize` → `{"userAgent": f"bridge/{self.codex_version} (Fake OS 1.0; arm64) fake-term/0 (bridge; 0.1.0)", "codexHome": "/fake/.codex", "platformFamily": "unix", "platformOs": "macos"}`; record `params` in `self.initialize_params` (so a test can assert the opt-out list). A second `initialize` returns `JsonRpcError(-32600, "Already initialized")`; any other request before `initialize` returns `JsonRpcError(-32600, "Not initialized")`.
   - `initialized` notification → if `eager_thread`, `self.start_thread(self.thread_id, self.cwd)`, which appends to `self.threads` and **broadcasts** `thread/started {"thread": <Thread>}` then `thread/status/changed {"threadId": …, "status": {"type": "idle"}}` regardless of subscription.
   - `_thread_obj(thread_id, cwd, status)` → the `Thread.required_subset` fields plus the ones Bridge reads: `{"id", "sessionId", "status", "cwd", "createdAt", "updatedAt", "source": "vscode", "threadSource": "user", "cliVersion": codex_version, "name": None, "preview": "", "historyMode": "paginated", "path": f"/fake/rollout-{thread_id}.jsonl", "turns": []}`.
   - `thread/loaded/list` → `{"data": [t["id"] for t in self.threads], "nextCursor": None}`. `thread/read {threadId, includeTurns}` → `{"thread": …}`, or `JsonRpcError(-32601, "list_turns is not supported yet")` when `includeTurns` is true (known-broken row).
   - `thread/resume {threadId, excludeTurns}` → dispatch on `resume_result`: `"ok"` sets `self.subscribed.add(threadId)` and returns `{"thread": …}`; `"unsupported"` raises `JsonRpcError(-32601, "list_turns is not supported yet")`; `"no_rollout"` raises `JsonRpcError(-32600, f"no rollout found for thread id {threadId}")`. Neither failure subscribes (the real 0.151.0 side effect is a bug — report §3 — and the fake must not model it).
   - `thread/unsubscribe {threadId}` → `{"status": "unsubscribed" if threadId in self.subscribed else "notSubscribed"}`; discards.
   - `turn/start {threadId, input}` → append `params` to `self.turns`; `turn_id = f"turn-{n}"`; **result first** is not required, but the fanout is: emit `turn/started {"threadId", "turn": {…"status":"inProgress"}}`, `item/started {"threadId","turnId","item":{"type":"userMessage","id":…,"content":…},"startedAtMs":…}`, then (when `auto_complete`) `complete_turn(turn_id)`; return `{"turn": {"id": turn_id, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": None, "completedAt": None, "durationMs": None}}` (verbatim shape from the 2026-09-07 spike). Also emit `thread/status/changed {"type":"active","activeFlags":[]}` before and `{"type":"idle"}` after — globally.
   - `complete_turn(turn_id=None, agent_message=None, status="completed")` → `item/started` for an `agentMessage` item, one `item/agentMessage/delta` per word when a message is set, `item/completed {"threadId","turnId","item":{"type":"agentMessage","id":f"msg_{turn_id}","text":message,"phase":"final","delivery":None,"memoryCitation":None},"completedAtMs":…}`, then `turn/completed {"threadId","turn":{"id":turn_id,"status":status,…}}`.
   - **Fanout rule** (the spike's central fact): `turn/*` and `item/*` are sent **only if** `self.thread_id in self.subscribed`; `thread/started` and `thread/status/changed` are always sent.
   - `emit_status(type_, active_flags=None)`, `start_thread(thread_id, cwd)`, and read-only attributes `turns`, `threads`, `subscribed`, `forbidden_calls`, `parse_errors`, `initialize_params`.
   - Every forbidden method (`FORBIDDEN_METHODS` is imported in Task 4; here list the three literals once in a module constant `_FORBIDDEN = ("turn/steer", "turn/interrupt", "review/start")`) is registered to append to `self.forbidden_calls` and return `{}`.
   - `_on_parse_error(raw)` → append to `self.parse_errors` and `self.rpc.close()`. Two JSON objects in one frame is exactly what the real server does (report §1: `Failed to deserialize JSONRPCMessage: trailing characters`).
4. `tests/test_codex_fake.py` drives the fake with a **raw peer socket** and `bridge.ws` directly (the new client does not exist until Task 4): a helper `_peer(sock)` does `ws.client_handshake` and gives `send(obj)` / `recv(timeout)` helpers over `ws.send_text(..., mask=True)` / `ws.recv_message`.

**Tests (`tests/test_codex_fake.py`):**
- `test_fixture_pins_the_version_and_the_websocket_transport` — `codex_version == "0.151.0"`, `transport.framing == "websocket"`, `client_frames_must_be_masked is True`, `messages_per_frame == 1`, `jsonrpc_field_present is False`, `launch_argv == ["{binary}","app-server","--listen","unix://{socket_path}"]`.
- `test_fake_completes_the_websocket_handshake`.
- `test_every_server_message_omits_jsonrpc_and_notifications_carry_emitted_at_ms`.
- `test_initialize_returns_the_four_fields_and_a_parseable_user_agent`; `test_second_initialize_is_already_initialized_minus_32600`; `test_request_before_initialize_is_not_initialized`.
- `test_initialized_broadcasts_thread_started_and_an_idle_status`.
- `test_thread_loaded_list_and_thread_read_expose_cwd_and_status`; `test_thread_read_with_include_turns_is_known_broken`.
- `test_thread_resume_ok_subscribes_and_unsubscribe_reports_it` (the report §3 subscription probe: `unsubscribed` after a successful resume, `notSubscribed` without one).
- `test_thread_resume_unsupported_returns_32601_and_does_not_subscribe`; `test_thread_resume_no_rollout_returns_32600`.
- `test_turn_and_item_notifications_reach_only_a_subscribed_peer` — with `resume_result="unsupported"`, a `turn/start` yields **only** `thread/status/changed` active/idle; with `"ok"`, the full `turn/started → item/started → item/agentMessage/delta → item/completed → turn/completed` chain arrives.
- `test_turn_start_result_is_a_turn_object_with_an_id`.
- `test_start_thread_broadcasts_a_second_thread_started` (rebind fodder).
- `test_an_unmasked_client_frame_closes_the_connection` — write an unmasked text frame; the peer subsequently reads EOF.
- `test_two_json_objects_in_one_frame_close_the_connection` — one frame containing `{"id":1,…}\n{"id":2,…}`; `fake.parse_errors` has one entry and the peer reads EOF.
- `test_fake_only_speaks_methods_and_notifications_that_are_in_the_fixture` — collect the fake's registered method names and every method it can `_notify`, and assert each is a key of `client_requests`/`client_notifications`/`server_notifications` or a member of `forbidden_methods`.

**Verify:** `pytest tests/test_codex_fake.py tests/test_codex_adapter.py tests/test_lab_cli.py` green; full suite green; `ruff check .` clean. Commit: `tests: pin the codex 0.151.0 fixture and add a WebSocket fake App Server`.

---

## Task 4 — Rewrite `CodexAppServerClient` against the real contract

**Files:** `src/bridge/codex_app_server.py`, `tests/test_codex_client.py` (new), `tests/fakes/codex_app_server.py` (delete `LegacyFakeCodexAppServer`; import the constants), `tests/fakes/codex_exe.py`, `tests/test_codex_adapter.py` (flip to the new fake — behaviour changes land in Task 5), `tests/test_lab_cli.py` (`_make_codex` helper only), `tests/test_codex_launch.py` (the `protocol_version` knob becomes `codex_version`).

**Scope (TDD):**

1. Constants (the single source of truth; the fake and the fixture test import these):
   `M_INITIALIZE="initialize"`, `M_THREAD_LIST="thread/loaded/list"`, `M_THREAD_READ="thread/read"`, `M_THREAD_RESUME="thread/resume"`, `M_THREAD_UNSUBSCRIBE="thread/unsubscribe"`, `M_TURN_START="turn/start"`, `N_INITIALIZED="initialized"`, `N_THREAD_STARTED="thread/started"`, `N_THREAD_STATUS="thread/status/changed"`, `N_TURN_STARTED="turn/started"`, `N_TURN_COMPLETED="turn/completed"`, `N_ITEM_STARTED="item/started"`, `N_ITEM_DELTA="item/agentMessage/delta"`, `N_ITEM_COMPLETED="item/completed"`; `CLIENT_REQUESTS`/`CLIENT_NOTIFICATIONS`/`SERVER_NOTIFICATIONS` tuples built from them. Delete `PROTOCOL_VERSION` and `SUPPORTED_VERSIONS`. Keep `LAUNCH_ARGV`, `build_launch_argv`, `CodexAppServerProcess`, and `connect_app_server_socket` **unchanged** (diff row 24: verified correct).
   `MIN_CODEX_VERSION = (0, 151, 0)`; `PINNED_CODEX_VERSION = "0.151.0"`; `FORBIDDEN_METHODS = ("turn/steer", "turn/interrupt", "review/start")`; `OPT_OUT_NOTIFICATION_METHODS = ("remoteControl/status/changed", "fs/changed", "account/rateLimits/updated", "thread/tokenUsage/updated")`; `STATUS_IDLE="idle"`, `STATUS_WORKING="working"`; `THREAD_STATUS_TO_STATE = {"idle": STATUS_IDLE, "active": STATUS_WORKING, "notLoaded": STATUS_IDLE, "systemError": STATUS_IDLE}`.
2. `parse_codex_version(user_agent: str) -> tuple[int, int, int] | None` — first `(\d+)\.(\d+)\.(\d+)` in the segment after the first `/` (`"bridge/0.151.0 (Mac OS…"` → `(0,151,0)`), else `None`. Keep the raw string too (`self.codex_version: str`).
3. `CodexAppServerClient.__init__(sock, *, cwd=None, client_version="0.1.0", on_thread_bound=None, on_status=None, on_agent_message=None, on_turn_completed=None, on_thread_error=None, on_disconnect=None)`. Endpoint: `RpcEndpoint(sock, name="codex-app-server", framing=Framing.WS_CLIENT, on_close=self._on_rpc_closed)` (it keeps sending `"jsonrpc":"2.0"`, which the server accepts and ignores — report §1). State: `thread_id=None`, `status=STATUS_IDLE`, `subscribed=False`, `codex_version=""`, `codex_version_warning=None`, `last_thread_error=None`, `own_thread_ids: set[str]`, `server_info={}`, `_delta: dict[str,str]`, `_items: dict[str,str]`. `_register()` binds handlers for the five `N_*` notifications only — **delete the `thread/resume`-as-notification and `runtime/status` handlers** (diff rows 6, 7).
4. `initialize()`: request `M_INITIALIZE` with `{"clientInfo": {"name": "bridge", "title": "Bridge", "version": self._client_version}, "capabilities": {"optOutNotificationMethods": list(OPT_OUT_NOTIFICATION_METHODS)}}` — **no `protocolVersion`**. Store `self.server_info = result`; `self.codex_version = str(result.get("userAgent",""))`-derived. `parse_codex_version` returns `None` → `raise UnsupportedCodexVersion(f"could not parse a codex version from userAgent {ua!r}")`; parsed `< MIN_CODEX_VERSION` → `raise UnsupportedCodexVersion(f"codex {ver} predates the pinned App Server contract (Bridge needs >= 0.151.0)")`; parsed `> MIN_CODEX_VERSION` → set `self.codex_version_warning = f"codex {ver} is newer than the pinned contract {PINNED_CODEX_VERSION}; wire shapes are assumed stable"` and proceed (never hard-fail on a patch bump). Then `self.rpc.notify(N_INITIALIZED, None, omit_empty_params=True)` — the probe sent exactly `{"method":"initialized"}`.
5. `bind_thread(self) -> str | None` — the Experiment-F algorithm, called from the main thread only:
   `data = self.rpc.request(M_THREAD_LIST, {}).get("data") or []`; for each id `thread = self.rpc.request(M_THREAD_READ, {"threadId": tid, "includeTurns": False}).get("thread")` (skip ids that error); candidates = threads whose `id` is not in `own_thread_ids`; prefer `thread.get("cwd") == self.cwd`, else the greatest `int(thread.get("createdAt") or 0)`; on a winner call `self._bind(thread)`. Returns `self.thread_id`.
6. `_bind(thread)`: set `self.thread_id`, apply `thread.get("status")` through `_apply_thread_status`, then invoke `self._on_thread_bound(self.thread_id)`. **It never issues a request** (see the deadlock trap) — subscribing is the caller's job.
7. `_on_thread_started(params)` (reader thread): `thread = params.get("thread") or {}`; `tid = thread.get("id")`; ignore falsy ids and ids in `own_thread_ids`; bind when `self.thread_id is None` **or** `thread.get("cwd") == self.cwd` (last wins — the TUI restarting in the same cwd rebinds). Sets `self.subscribed = False` before calling `_bind`, because the new thread is definitely not subscribed.
8. `subscribe(self) -> bool`: no thread → `False`. `self.rpc.request(M_THREAD_RESUME, {"threadId": self.thread_id, "excludeTurns": True})`; on `JsonRpcError` with `code in (METHOD_NOT_FOUND, INVALID_REQUEST)` (i.e. `-32601` / `-32600`) set `self.subscribed = False`, record `self.last_thread_error = exc.message`, return `False`; any other code re-raises. Success → `self.subscribed = True`. Docstring: 0.151.0 fails this with `list_turns is not supported yet` for the live TUI thread, which costs Bridge the `turn/*`/`item/*` stream but **not** busy/idle (global) — see the 2026-09-07 two-client spike.
9. `_apply_thread_status(status)` / `_on_thread_status_changed(params)`: ignore params whose `threadId` differs from `self.thread_id`; `t = (status or {}).get("type")`; unknown type → ignore; `t == "systemError"` → set `self.last_thread_error = "thread reported systemError"` and call `on_thread_error`; map through `THREAD_STATUS_TO_STATE` and call `_set_status(state)` which fires `on_status` only on a change.
10. `start_turn(text) -> str`: no thread → `RuntimeError("no bound Codex thread yet")`; `self.status == STATUS_WORKING` → `raise CodexThreadBusy(f"thread {self.thread_id} is working; Bridge never queues a second turn")` (new exception class; Bridge gates on its own observed idle rather than trusting server-side queuing — report §5). Request `M_TURN_START` with `{"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}` (**camelCase**, diff row 8) and return `str((result.get("turn") or {}).get("id") or "")` (diff row 9).
11. Correlation handlers: `_on_turn_started(params)` → `_set_status(STATUS_WORKING)` (corroborating only). `_on_item_delta(params)` → `self._delta[params["turnId"]] += str(params.get("delta",""))`. `_on_item_completed(params)` → only when `(params.get("item") or {}).get("type") == "agentMessage"`: `text = str(item.get("text") or "")`, store in `self._items[params["turnId"]]`, call `on_agent_message(turn_id, text)` (a `userMessage` `item/completed` is ignored — it fires first in a real turn). `_on_turn_completed(params)` → `turn = params.get("turn") or {}`; `turn_id = turn.get("id","")`; `status = turn.get("status","")`; pop `self._items`/`self._delta` (items win, deltas are the fallback); `_set_status(STATUS_IDLE)`; call `on_turn_completed(turn_id, status)` then, when a message exists, it has already been delivered via `on_agent_message`. Expose `final_message(turn_id)` for the adapter's fallback.
12. Rewrite the module docstring: WebSocket transport, no protocol version, thread binding, `thread/resume`-as-subscribe with its 0.151.0 breakage, and that the pinned contract lives in `tests/fixtures/codex_protocol/codex-0.151.0.json`.
13. `tests/fakes/codex_exe.py`: delete `LegacyFakeCodexAppServer`'s inline JSONL server from `_SCRIPT_TEMPLATE`. Replace the whole `_app_server` body with: bind the socket + pidfile + SIGTERM handling exactly as today (`codex_exe.py:41-78,165-177` — that half is correct), then `sys.path[:0] = REPO_PATHS` (a generation-time constant: the repo root and `src`) and `from tests.fakes.codex_app_server import FakeCodexAppServer`, serve the accepted connection with it, and block until SIGTERM. Rename the `protocol_version` parameter to `codex_version: str = "0.151.0"` and thread it into the fake. Update the docstring: the fake exe is still a genuine subprocess, but it no longer re-implements the protocol (or RFC 6455) a second time inside a string template — it imports the one fake the rest of the suite uses.
14. Flip the consumers with **no behaviour change**: `tests/test_codex_adapter.py`'s `codex_factory` and `reconnect()` construct the new fake and `CodexAppServerClient(sock, cwd=...)`; `tests/test_lab_cli.py::_make_codex` likewise; `tests/test_codex_launch.py::test_unsupported_app_server_version_…` passes `codex_version="0.150.0"` and matches `"predates the pinned App Server contract"`. Delete `test_codex_adapter.py::test_pinned_contract_matches_fixture` (Task 4's new fixture test replaces it) and `test_unsupported_version_raises` (moved into `tests/test_codex_client.py`).

**Tests (`tests/test_codex_client.py`, against the Task 3 fake over `socket.socketpair()`):**
- `test_pinned_contract_matches_the_fixture` — both directions: every `client_requests` key is in `CLIENT_REQUESTS`, every `server_notifications` key is in `SERVER_NOTIFICATIONS`, `tuple(fixture["forbidden_methods"]) == FORBIDDEN_METHODS`, `tuple(fixture["launch_argv"]) == LAUNCH_ARGV`, `fixture["bridge_state_map"] == THREAD_STATUS_TO_STATE`, `tuple(fixture["min_codex_version"]) == MIN_CODEX_VERSION`, and no constant is missing from the fixture.
- `test_initialize_sends_client_info_and_the_opt_out_list` — `fake.initialize_params["clientInfo"] == {"name":"bridge","title":"Bridge","version":"0.1.0"}`, `capabilities.optOutNotificationMethods == list(OPT_OUT_NOTIFICATION_METHODS)`, and **no `protocolVersion` key anywhere in the params**.
- `test_initialize_parses_the_codex_version_from_the_user_agent` — `client.codex_version == "0.151.0"`, no warning.
- `test_initialize_rejects_a_version_below_the_minimum` (`"0.150.9"` → `UnsupportedCodexVersion` mentioning `0.151.0`); `test_initialize_rejects_an_unparsable_user_agent`; `test_initialize_warns_but_proceeds_above_the_pin` (`"0.152.0"` → no raise, `codex_version_warning` set).
- `test_initialized_is_sent_without_a_params_key`.
- `test_bind_prefers_the_thread_matching_the_session_cwd`; `test_bind_falls_back_to_the_newest_thread`; `test_bind_ignores_threads_bridge_started_itself` (seed `own_thread_ids`); `test_bind_returns_none_when_there_are_no_candidates`.
- `test_a_thread_started_broadcast_after_connect_binds` (fake constructed with `eager_thread=False`, then `fake.start_thread(...)`).
- `test_a_second_thread_in_the_same_cwd_rebinds_last_wins`; `test_a_thread_in_another_cwd_does_not_rebind`.
- `test_subscribe_success_sets_subscribed`; `test_subscribe_tolerates_list_turns_not_supported` (`resume_result="unsupported"` → returns False, `subscribed is False`, `last_thread_error` mentions `list_turns`); `test_subscribe_tolerates_no_rollout` (`-32600`); `test_subscribe_reraises_an_unexpected_error_code`.
- `test_thread_status_changed_maps_every_variant` — table over `notLoaded/idle/active/systemError` → `idle/idle/working/idle`, `active` with `activeFlags:["waitingOnApproval"]` still `working`, and a `threadId` for another thread changes nothing.
- `test_start_turn_uses_camel_case_and_returns_result_turn_id` — `fake.turns[0] == {"threadId": …, "input": [{"type":"text","text":"hi"}]}`.
- `test_start_turn_refuses_while_working` — `CodexThreadBusy`, and `fake.turns` is unchanged.
- `test_item_completed_agent_message_fires_the_callback_and_user_messages_are_ignored`.
- `test_agent_message_deltas_accumulate_per_turn_and_items_win_over_deltas`.
- `test_turn_completed_reports_the_turn_status` (`completed`, then a `failed` turn).
- `test_an_unsubscribed_client_still_sees_busy_and_idle` — `resume_result="unsupported"`; a turn produces `working`→`idle` and **no** `on_agent_message`/`on_turn_completed`.
- `test_forbidden_methods_are_never_called` — source-level, the existing pattern: read `codex_app_server.py` and `adapters/codex.py`; each of `"turn/steer"`, `"turn/interrupt"`, `"review/start"` appears at most once and only in the `FORBIDDEN_METHODS` tuple; the string `rpc.request(` is never on a line containing any of them. Plus a live assertion that `fake.forbidden_calls == []` after a full bind/subscribe/turn cycle.

**Verify:** `pytest tests/test_codex_client.py tests/test_codex_fake.py tests/test_codex_adapter.py tests/test_codex_launch.py tests/test_lab_cli.py` green; full suite green; `ruff check .` clean. Commit: `codex: speak the real App Server contract (WebSocket, camelCase, thread binding)`.

---

## Task 5 — Adapter: bind, subscribe, session meta, and survive a disconnect

**Files:** `src/bridge/adapters/codex.py`, `src/bridge/launch.py` (`_run_codex_wrapper` only), `tests/test_codex_adapter.py`, `tests/test_codex_launch.py`, `tests/test_codex_app_server_process.py` (fixture-path reference only).

**Scope (TDD):**

1. `CodexAdapter.__init__` gains `paths: Paths | None = None`; `_bind_app_callbacks` now wires `on_thread_bound`, `on_status`, `on_agent_message`, `on_turn_completed`, `on_thread_error`, `on_disconnect`.
2. `start()` order (replacing `adapters/codex.py:71-94`): `self._worker.start()`; `self.app.initialize()` (raises `UnsupportedCodexVersion` on drift — unchanged contract for `launch.py`); `register_session {session_id, family:"codex", cwd, state:"starting", is_managed:True}`; `self.app.bind_thread()`; `self.router.subscribe(session_id)`; `update_state {state: self.app.status or STATUS_IDLE}`; `self._write_session_meta()`.
3. `_on_thread_bound(thread_id)` submits **one** worker task that (a) `update_state {vendor_session_id: thread_id}`, (b) `self.app.subscribe()` — this is why it runs on the worker and never on the App Server reader thread, and (c) `self._write_session_meta()`. It is the single path for both the initial bind and a later rebind.
4. `_write_session_meta()`: when `self.paths` is set, `self.paths.merge_session_meta(self.session_id, {"thread_id": self.app.thread_id or "", "subscribed": bool(self.app.subscribed), "codex_version": self.app.codex_version})`, plus `codex_version_warning` / `last_thread_error` when those are set. `merge_session_meta` never raises (`paths.py:85-110`), so no try/except is needed.
5. `_on_status(status)` unchanged in shape (`idle`/`working` → `update_state`), but the values now come from `THREAD_STATUS_TO_STATE`. `_on_thread_error(detail)` submits a task that writes `last_thread_error` into the session meta and leaves the state alone (`systemError` maps to `idle`).
6. `_on_agent_message(turn_id, text)` stores `self._final[turn_id] = text`. `_on_turn_completed(turn_id, status)` pops it and keeps today's fallback semantics (`adapters/codex.py:127-142`): reply only when `self.fallback`, a call is active, `status == "completed"`, and the message is non-empty. Update the class docstring: correlation is proven but arrives **only when `subscribe()` succeeded**, which 0.151.0 usually refuses; the reply path for calls remains Codex calling Bridge's `reply` MCP tool.
7. `_on_router_event` wraps `self.app.start_turn(text)` in `try/except CodexThreadBusy`: on a refusal, do **not** ack and do not clear `_active_call` — the router still holds delivery on `working` (`router.py:199` pump), so this guard only ever fires on a race, and swallowing the ack keeps the message from being marked delivered.
8. `_handle_app_disconnected` / `_try_reconnect` (`adapters/codex.py:174-233`): after a successful `initialize()` on the replacement client, **re-run the bind** — `new_app.own_thread_ids = old_ids`, `new_app.bind_thread()` (the thread outlives the connection: report §6, diff row 18) — then `update_state {vendor_session_id, state, reachable: True}` and `_write_session_meta()`. Keep the bounded backoff, the attempt cap, and the "close the old client only on the worker thread once a replacement is in hand" comment. Update the module docstring: an App Server disconnect is no longer session death; the thread survives and is re-bound.
9. `launch.py::_run_codex_wrapper`: pass `paths=paths` to `CodexAdapter`, and after `adapter.start()` print `f"[bridge] codex {adapter.app.codex_version}: {adapter.app.codex_version_warning}"` to stderr when a warning is set. **`build_codex_argv`, `LAUNCH_ARGV`, `CodexAppServerProcess.wait_for_socket`, and the spawn/teardown order are unchanged** (diff row 24).

**Tests (`tests/test_codex_adapter.py`, rewritten around the new fake; the `codex_factory` fixture gains `resume_result` and `codex_version` knobs and passes `paths`):**
- `test_thread_binding_and_reachable` — the session reaches `reachable`/`idle` and `vendor_session_id == "thread-abc"`.
- `test_binds_a_thread_that_existed_before_bridge_connected` — `eager_thread=True` with the thread created before the client starts (seeded via `thread/loaded/list`).
- `test_session_meta_records_thread_id_subscribed_and_codex_version` — `json.loads(paths.session_meta(sid).read_text())` has `{"thread_id": "thread-abc", "subscribed": True, "codex_version": "0.151.0"}`.
- `test_session_meta_records_subscribed_false_when_resume_is_broken` — `resume_result="unsupported"`.
- `test_inbound_call_starts_turn_with_envelope` — `server.turns[0]["input"][0]["text"]` contains `[bridge call]` and the question; `server.forbidden_calls == []`.
- `test_final_message_fallback_answers_caller` (subscribed) and `test_no_fallback_when_not_subscribed` (`resume_result="unsupported"` → the call times out rather than being answered from nothing).
- `test_mcp_reply_path_wins_over_fallback` — unchanged intent, new fake (`auto_complete=False` + `server.complete_turn()`).
- `test_working_status_holds_delivery` — `server.emit_status("active")` → state `working`, a `text` is held, `server.turns` does not grow.
- `test_system_error_status_records_the_error_and_stays_idle`.
- `test_app_server_crash_marks_unreachable_and_records_disconnected` — unchanged.
- `test_reconnect_rebinds_the_same_thread_and_resubscribes` — the replacement fake serves the same `thread_id`; after recovery the roster is reachable, `vendor_session_id` is unchanged, and the session meta still records it.
- `test_reconnect_gives_up_after_exhausting_attempts` — unchanged.
- `test_a_rebind_to_a_new_tui_thread_updates_the_router_and_meta` — `server.start_thread("thread-def", cwd)` → `vendor_session_id` becomes `thread-def` and the meta follows.
- `tests/test_codex_launch.py`: the six existing tests keep their assertions; only the fake-exe knob (`codex_version`) and the unsupported-version match string change. Add `test_session_meta_is_written_for_a_wrapped_codex_session`.

**Verify:** `pytest tests/test_codex_adapter.py tests/test_codex_launch.py tests/test_codex_app_server_process.py` green; full suite green; `ruff check .` clean. Commit: `codex adapter: bind + subscribe the TUI thread, record session meta, re-bind on reconnect`.

---

## Task 6 — Lab correlation, README, and legacy cleanup

**Files:** `src/bridge/lab/cli.py`, `README.md`, `tests/test_lab_cli.py`, delete `tests/fixtures/codex_protocol/v1.json`.

**Scope (TDD):**

1. `correlate_turns` (`lab/cli.py:424-465`) on the real keys: `turn/start` request ids unchanged (`frame["id"]`); the **response** turn id is `frame["result"]["turn"]["id"]` (was `result["turn_id"]`); `turn/started` and `turn/completed` read `frame_params(rec)["turn"]["id"]` (was `params["turn_id"]`); additionally collect `frame_params(rec)["turnId"]` from `item/started`, `item/agentMessage/delta`, and `item/completed` into a new `items` set. Return `{"turn_start_requests", "turn_ids", "started", "completed", "items", "correlated"}` with `correlated` unchanged in meaning. Guard every lookup with `isinstance(..., Mapping)`.
2. `_run_f` prints one more line, `f"observed: item notifications for turn ids: {correlation['items'] or '(none)'}"`, and the summary carries `items`. Its docstring/`F_MESSAGE` are unchanged. `_run_g`'s Codex half is unchanged (`working` is now a real signal).
3. `FORBIDDEN_FRAME_METHODS = ("turn/steer", "turn/interrupt", "review/start")` (equal to `FORBIDDEN_METHODS`); the FAIL line becomes `f"FAIL: {len(steer)} forbidden frame(s) in the capture - Bridge must never steer or interrupt"`.
4. README: in **Launch flow** step 2, say the adapter connects over **WebSocket** (`codex app-server` speaks RFC 6455 on the Unix socket), binds the exact thread the remote TUI created, and best-effort subscribes with `thread/resume` — noting that codex 0.151.0 refuses that subscription for the live TUI thread, which costs Bridge the per-turn item stream but not busy/idle. In **How it works**, change the diagram label `App Server JSON-RPC` to `App Server JSON-RPC / WebSocket` and add one sentence that Bridge pins the contract at codex 0.151.0 (`tests/fixtures/codex_protocol/codex-0.151.0.json`) and warns rather than fails on a newer patch. Do not touch the Install, Usage, or Troubleshooting sections.
5. Delete `tests/fixtures/codex_protocol/v1.json`.
6. Grep sweep: `rg -n '"thread_id"|"turn_id"|runtime/status|codex-app-server/1|item/agent_message' src tests` must return nothing. (`self.app.thread_id` and the router's `vendor_session_id` are Python attribute names, not wire names, and stay.)

**Tests:**
- `test_correlate_turns_pairs_ids_through_the_result_turn_object` — records shaped like the real capture: `{"frame":{"id":7,"method":"turn/start","params":{}}}`, `{"frame":{"id":7,"result":{"turn":{"id":"01a07a20","status":"inProgress","items":[]}}}}`, `turn/started`/`turn/completed` with `params.turn.id`, plus a stray `turn/started` for `"other"`. Asserts `turn_ids == ["01a07a20"]`, `correlated == ["01a07a20"]`, `"other" in started`.
- `test_correlate_turns_collects_item_turn_ids` — `item/started`, `item/agentMessage/delta`, `item/completed` with `params.turnId` → `items == ["01a07a20"]`.
- `test_correlate_turns_ignores_legacy_turn_id_frames` — a record with `params.turn_id` contributes nothing.
- `test_run_f_correlates_turn_started_to_turn_completed` and `test_run_f_fails_when_the_turn_never_completes` — updated for the new fake (subscribed by default) and the `items` summary key.
- `test_run_f_reports_no_correlation_when_the_subscription_was_refused` — `resume_result="unsupported"`: rc is 1 and the summary shows `started == [] and items == []` while the admission status is still `queued`/`delivered` (this is the honest 0.151.0 verdict).
- `test_lab_only_ever_watches_for_the_forbidden_methods` — updated tuple; each of the three literals appears exactly once in `lab/cli.py`.
- `test_no_legacy_codex_wire_names_remain` — walk `src/` and `tests/` `.py` + `.json` files and assert none contains `"thread_id"`, `"turn_id"`, `runtime/status`, `codex-app-server/1`, or `item/agent_message` (excluding this plan and `docs/`).

**Verify:** `pytest tests/test_lab_cli.py` green; full suite green; `ruff check .` clean. Commit: `lab + docs: correlate turns on the real keys and drop the invented v1 contract`.

---

## Out of scope (spec §7 — record in the PR description as follow-ups)

- **A drift tool over `codex app-server generate-json-schema`.** The fixture's `_regenerate` string documents the manual procedure (report §7 sketches the ~40-line `tools/diff_codex_contract.py`); the generated schema is `[experimental]` and already disagrees with the live server in both directions, so a schema diff must always be paired with one live handshake.
- **Claude-side honesty about busy state.** Claude sends no busy signal, so `bridge lab run G --claude` still cannot prove hold-then-deliver; separate follow-up.
- **`bridge codex --session-id`** for Experiment H's Codex-restart step (`launch.py:302` has no way to return an old id to `reachable=true`); needs a design decision on Codex session identity across TUI restarts.
- **Live re-validation.** This plan's suite is hermetic. The verdict gate is unchanged: re-run Experiment F, then G (Codex), then H against a real `codex` 0.151.0 after Task 6 lands.
