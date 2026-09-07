# Plan: make Bridge's Claude side work against the real research-preview `claude`

**Spec:** `docs/superpowers/specs/2026-08-26-bridge-design.md` (§5 Claude Channel launch, §11 install/doctor, §12 vendor drift). Where this plan and the spec disagree, the spec wins, except where the vendor docs quoted below contradict the spec's assumptions — those are the reason for this plan.

## Why

A fresh `bridge install` + `bridge doctor` on the maintainer's machine (claude 2.1.263, codex 0.151.0) exposed three defects the hermetic suite cannot see, because they concern what the real `claude` binary does:

1. **Claude never loads the Bridge MCP server.** `install.py` writes `mcpServers.bridge` into `~/.claude/settings.json`. Claude Code ignores that key there; user-scope MCP servers live in the top-level `mcpServers` object of `~/.claude.json` (that is where this machine's `cleat` and `tether` servers are). `claude mcp get bridge` → `No MCP server named "bridge"`. `doctor` reports `[ok]` because it re-reads its own write. Also, the registered command is the bare word `bridge`, which is not on PATH for a venv install.
2. **The channel probe can never succeed during the preview.** The official docs (code.claude.com/docs/en/channels, "Research preview") say: *"Neither `--channels` nor `--dangerously-load-development-channels` appears in `claude --help` while the feature is in preview. The flags work even though they aren't listed."* Both literals are present in the 2.1.263 binary. `claude_probe.classify_help` greps `--help`, so it always returns `unsupported`, doctor FAILs, and `bridge lab prepare` refuses — blocking all four live experiments.
3. **Wrong development launch flag.** Bridge's development mode plans `--channels dev:bridge`. Per the channels reference ("Test during the research preview"): `--channels` accepts only allowlisted plugins and never bypasses the allowlist; a custom channel launches as `claude --dangerously-load-development-channels server:<mcp-server-name>` (or `plugin:<name>@<marketplace>`). `dev:` is not a spec. The bypass is per-entry and shows a confirmation prompt; startup prints a dim notice `Channels (experimental) messages from server:<name> inject directly in this session`.

Plus one lab defect found by inspection: `bridge lab run G --claude <id>` still targets Codex when a Codex session is live (`lab/cli.py:578` prefers `codex_id`, and `_resolve_session_ids` back-fills the other family from the roster).

## Global Constraints

- Python ≥ 3.11. Lint: `ruff` (line-length 100; select E,F,I,UP,B,W). pytest runs with `filterwarnings = error` — any warning is a failure.
- The suite is hermetic: no vendor binaries, no network, no model tokens, no reads/writes of the maintainer's real `~/.claude.json`, `~/.claude/`, `~/.codex/`. Every path, executable, clock, and probe is injected (see `tests/conftest.py`, `tests/fakes/`).
- Run tests and lint exactly as: `cd /Users/siddharthyellur/bridge && mkdir -p /tmp/bt && TMPDIR=/tmp/bt .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/bt/p` and `.venv/bin/ruff check .` — the short `TMPDIR` is required because macOS's default tmp path exceeds the AF_UNIX socket path limit. Starting suite: 359 passed, 8 deselected.
- The Claude MCP server key written to `~/.claude.json` is exactly `bridge`, and the development channel spec is exactly `server:bridge`. These two strings are one contract (Task 1 writes the key, Task 2 launches with the spec).
- Never overwrite a config file that fails to parse. `~/.claude.json` holds the user's Claude Code state; a parse failure must raise a clear error, never reset the file to `{}`.
- One commit per task, on the current feature branch. No push.
- Vendor facts quoted above are the source of truth for flag names and file locations; do not "detect" what the docs say is deliberately hidden.

---

## Task 1 — Register the Claude MCP server where Claude Code reads it, with a command that resolves

**Files:** `src/bridge/install.py`, `src/bridge/doctor.py`, `tests/test_install.py`, `tests/test_doctor.py`, `README.md` (Install section only).

**Scope (TDD — write the failing tests first):**

1. Replace `claude_settings_path(claude_home)` with `claude_mcp_config_path(claude_home) -> Path` returning `claude_home.parent / ".claude.json"`. Update every call site (install, doctor, tests). Tests keep using an injected `claude_home` under `tmp_path`, so the file lands at `tmp_path/.claude.json`.
2. `install(...)` and `uninstall(...)` take a new keyword `bridge_executable: str | None = None`. Add `default_bridge_executable() -> str`: if `Path(sys.argv[0]).name == "bridge"` and that path exists, return `str(Path(sys.argv[0]).resolve())`; else `shutil.which("bridge")` if found; else the bare string `"bridge"`. When the result is bare `"bridge"`, append a report note: `bridge is not on PATH: registered the bare command "bridge"; sessions cannot spawn the Bridge server until it resolves (e.g. symlink it into ~/.local/bin)`.
3. `_merge_json_mcp(path, family, command)` writes `{"type": "stdio", "command": <command>, "args": ["serve", "--family", <family>]}` under top-level `mcpServers.bridge`, preserving every other key in the file. If the file exists and is not valid JSON, raise `InstallError` (add this exception class in `install.py` if absent, and let the CLI print its message and exit 1) — do **not** reset the file. Keep the existing `_chmod_600` call but make it best-effort (ignore `OSError`).
4. The Codex TOML block uses the same resolved command: turn `CODEX_MCP_BLOCK` into `codex_mcp_block(command: str) -> str` producing `command = <json.dumps(command)>` (JSON string escaping is valid TOML basic-string escaping for paths).
5. Legacy migration: if `claude_home/settings.json` exists, parses, and has `mcpServers.bridge`, remove that key via the existing `_remove_json_mcp` and append the settings path to `report.removed` with a note `legacy registration in settings.json removed (Claude Code reads ~/.claude.json)`. `uninstall` removes the `bridge` entry from **both** files when present.
6. `doctor._check_claude_registration(claude_home, which=shutil.which)`: reads `~/.claude.json`. Missing → `WARN "~/.claude.json missing; run \`bridge install\`"`. Invalid JSON → `FAIL "~/.claude.json is not valid JSON"`. No `mcpServers.bridge` → `FAIL "bridge server not registered in ~/.claude.json"`. Present → resolve the command: absolute path must exist and be executable (`os.access(p, os.X_OK)`), else `FAIL "registered command <cmd> does not exist or is not executable"`; bare name must resolve via `which`, else `FAIL "registered command \`bridge\` is not on PATH"`. OK detail: `"~/.claude.json (<cmd>)"`. Additionally, if `claude_home/settings.json` still carries `mcpServers.bridge`, add a separate `WARN` row `("Claude MCP registration (legacy)", WARN, "stale entry in settings.json; run \`bridge install\`")`.
7. `doctor._check_codex_registration(codex_home, which=shutil.which)`: same command-resolution check on the `command = "..."` line inside the bridge TOML block (parse the quoted value with `json.loads`).
8. README Install section: state that `bridge install` registers the Claude server in `~/.claude.json` (user scope) with the absolute path of the `bridge` executable, and that `bridge doctor` verifies the registered command resolves.

**Tests (new or updated), all with injected `claude_home`/`codex_home` under `tmp_path` and an explicit `bridge_executable`:**
- install writes `tmp_path/.claude.json` with `mcpServers.bridge == {"type": "stdio", "command": "/abs/bridge", "args": ["serve", "--family", "claude"]}` and preserves unrelated pre-existing keys (seed the file with `{"numStartups": 3, "mcpServers": {"tether": {...}}}` and assert both survive).
- install on an unparsable `~/.claude.json` raises `InstallError` and leaves the file byte-for-byte unchanged.
- install migrates a legacy `settings.json` entry (removed, reported) and leaves other `settings.json` keys intact.
- install with bare `bridge_executable="bridge"` emits the not-on-PATH note only when the injected `which` returns `None` (add `which` injection to `install` too, defaulting to `shutil.which`).
- Codex `config.toml` block contains `command = "/abs/bridge"`.
- uninstall removes the entry from `~/.claude.json` and from legacy `settings.json`.
- doctor: each branch in step 6 and the codex check in step 7, including the legacy WARN row.

**Verify:** `pytest tests/test_install.py tests/test_doctor.py tests/test_cli.py` green; full suite green; `ruff check .` clean. Commit: `install/doctor: register the Claude MCP server in ~/.claude.json with a resolvable command`.

---

## Task 2 — Classify Claude Channel support by version and launch the documented development flag

**Files:** `src/bridge/claude_probe.py`, `src/bridge/launch.py`, `src/bridge/install.py` (messages only), `src/bridge/doctor.py` (messages only), `tests/test_claude_probe.py`, `tests/test_launch.py`, `tests/test_install.py`, `tests/test_doctor.py`, `README.md` (Channel modes section).

**Scope (TDD):**

1. In `claude_probe.py`: keep `CHANNELS_FLAG = "--channels"`, `MARKETPLACE_MARKERS`, `PLUGIN_ARG_MARKER = "plugin:"`, `DEFAULT_MARKETPLACE`, `PLUGIN_CHANNEL_SPEC`. Add `DEV_CHANNELS_FLAG = "--dangerously-load-development-channels"`, `DEV_CHANNEL_SPEC = "server:bridge"`, `MIN_CHANNELS_VERSION = (2, 1, 234)`. Delete `DEV_CHANNEL_MARKERS`; set `DEV_ARG_MARKER = DEV_CHANNELS_FLAG`.
2. Add `parse_version(text: str) -> tuple[int, int, int] | None`: first `(\d+)\.(\d+)\.(\d+)` match anywhere in `text` (handles `"2.1.263 (Claude Code)"`), else `None`.
3. Rename `classify_help` → `classify_channel_mode(version: str, help_text: str) -> ChannelMode` with this order:
   1. `--channels` in `help_text` **and** a marketplace marker → `PLUGIN`, `launch_args=[CHANNELS_FLAG, PLUGIN_CHANNEL_SPEC]` (unchanged behaviour, kept for when Bridge is on an allowlist).
   2. `parse_version(version) is None` → `UNSUPPORTED`, `detail=f"could not parse a claude version from {version!r}"`.
   3. parsed version `>= MIN_CHANNELS_VERSION` → `DEVELOPMENT`, `launch_args=[DEV_CHANNELS_FLAG, DEV_CHANNEL_SPEC]`, `detail="research preview: channel flags are accepted but hidden from --help"`.
   4. otherwise → `UNSUPPORTED`, `detail=f"claude {major}.{minor}.{patch} predates Claude Code Channels (needs >= 2.1.234)"`.
   Update `detect_channel_mode` to call it; keep its never-raises contract. Rewrite the module docstring to state the preview hides the flags (cite `code.claude.com/docs/en/channels`, "Research preview") and that classification is therefore by version, with `--help` consulted only for the plugin/marketplace upgrade path.
4. `launch.describe_channel_mode`: development is recognised by `DEV_CHANNELS_FLAG in channel_args`; plugin by `PLUGIN_ARG_MARKER` in the joined args; text for development becomes `"Claude Channel mode: development (research preview: --dangerously-load-development-channels server:bridge; organization policy may block inbound delivery)"`.
5. `install.py` development note becomes: `Claude Channel mode: development (research preview, claude <version>): launching with --dangerously-load-development-channels server:bridge. Claude asks once at startup to confirm the development channel; organization policy may still block inbound delivery (bridge doctor reports this as a warning).` Unsupported note becomes: `Claude channel unsupported (<detail>): this session will be inbound-unreachable. Outbound Bridge tools still work.` `doctor._channel_mode_check` WARN text: `research preview: --dangerously-load-development-channels server:bridge (claude <version>); organization policy may still block inbound delivery`.
6. README "Channel modes": rewrite the `development` bullet to give the real flag and spec, say the flags are deliberately absent from `claude --help` during the preview, that Bridge therefore classifies by version (≥ 2.1.234), and that Claude shows a one-time confirmation for the development channel at startup. Rewrite `unsupported` to say "claude older than 2.1.234, unparsable version, or binary not found".

**Tests:**
- `test_claude_probe.py`: replace the `DEV_HELP` fixtures. Table-drive `classify_channel_mode`: `("2.1.263 (Claude Code)", UNSUPPORTED_HELP) → DEVELOPMENT` with `launch_args == ["--dangerously-load-development-channels", "server:bridge"]`; `"2.1.234" → DEVELOPMENT`; `"2.1.233" → UNSUPPORTED` (detail mentions `2.1.234`); `"3.0.0" → DEVELOPMENT`; `"garbage" → UNSUPPORTED` (detail mentions parse); `(any version, PLUGIN_HELP) → PLUGIN`. `parse_version` unit tests. `detect_channel_mode` fake-claude tests: version `2.1.263` with a help text lacking any channels flag → DEVELOPMENT; missing binary → UNSUPPORTED; `--version` timeout → UNSUPPORTED.
- `test_launch.py`: `build_claude_argv` with the new args yields `[binary, "--session-id", id, "--dangerously-load-development-channels", "server:bridge", *user_args]`; `describe_channel_mode` for the new args, for plugin args, for empty.
- `test_install.py` / `test_doctor.py`: replace every `dev:bridge` expectation; the channel-args file written on DEVELOPMENT equals the new list; doctor WARN text contains the flag.
- Keep the existing fixture test asserting Bridge's channel `meta` keys match `^[A-Za-z0-9_]+$` (do not weaken it).

**Verify:** `pytest tests/test_claude_probe.py tests/test_launch.py tests/test_install.py tests/test_doctor.py` green; full suite green; `ruff check .` clean. Commit: `claude channel: classify by version and launch --dangerously-load-development-channels server:bridge`.

---

## Task 3 — `bridge lab run G` targets the family the operator asked for

**Files:** `src/bridge/lab/cli.py`, `tests/test_lab_cli.py`.

**Scope (TDD):**

1. `LabContext` gains `explicit_claude: bool = False` and `explicit_codex: bool = False`, set from the parsed `--claude` / `--codex` arguments **before** `_resolve_session_ids` back-fills from the roster.
2. `_run_g`: if exactly one of the two is explicit → that family's id is the target. If both are explicit → `raise LabError("Experiment G targets one family per run; pass only --claude or only --codex")`. If neither → keep today's fallback (codex first, then claude) and print `G target: <family> <id> (auto-picked; pass --claude or --codex to choose)` via `ctx.out`.
3. The `family` label recorded in the run record must match the targeted session's family (never label a Claude target as `"codex"`).

**Tests** (extend the existing scripted-router/fake-roster pattern in `tests/test_lab_cli.py`):
- `run G --claude <id>` with a reachable Codex session also present → the stimulus goes to the Claude id and the record's `family == "claude"`.
- `run G --claude <a> --codex <b>` → exits non-zero with the one-family message.
- `run G` with neither → targets Codex and prints the auto-picked line.

**Verify:** `pytest tests/test_lab_cli.py` green; full suite green; `ruff check .` clean. Commit: `bridge lab: run G targets the explicitly requested family`.

---

## Task 4 — Speak the documented Channel wire contract

**Why (added 2026-09-06 after Task 2, from reading `src/bridge/claude_channel.py` against the channels reference):** the adapter's handshake and event shapes do not match what Claude Code expects, so even with Tasks 1–3 an inbound call would be dropped silently:

- It declares the capability as `capabilities["claude/channel"]`. The reference (Server options table) requires `capabilities.experimental["claude/channel"] = {}` — "Presence registers the notification listener." Wrong placement = no listener.
- It emits `notifications/claude/channel` with params `{kind, call_id, from, message_id, text}`. The reference (Notification format) defines exactly two params: `content: string` (the `<channel>` body) and `meta: Record<string,string>` (each entry an attribute; keys must match `^[A-Za-z0-9_]+$`, others silently dropped).
- It decides `channel_enabled` from the *client's* initialize capabilities containing `claude/channel`, and otherwise records a `policy_error` and marks the session unreachable. The reference describes no such client-side signal: "If the session hasn't loaded your server as a channel, or the organization policy blocks it, Claude Code drops the events silently and returns no error to your server." Registration success is only visible in Claude's own startup notice (Experiment E, step 5, is where a human records it).
- It echoes whatever `protocolVersion` the client requests. The channels page warns Claude Code "doesn't register a channel server that negotiates protocol revision 2026-07-28"; the adapter implements 2024-11-05 and must say so rather than echo.

**Files:** `src/bridge/claude_channel.py`, `src/bridge/doctor.py` (the "Claude channel policy" row), `tests/fakes/claude_host.py`, `tests/test_claude_channel.py`, `tests/test_doctor.py`, `README.md` (Troubleshooting sentence mentioning "Claude channel policy").

**Scope (TDD):**

1. `_initialize` returns `{"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}, "experimental": {CHANNEL_CAPABILITY: {}}}, "serverInfo": {...}, "instructions": SYSTEM_INSTRUCTIONS}`. Never echo the client's `protocolVersion`. Store the client's `clientInfo` and `capabilities` on the adapter (`self.client_info`, `self.client_capabilities`).
2. `_on_initialized` subscribes to the router unconditionally, then best-effort persists `{"handshake": {"client_info": <clientInfo or {}>, "client_capabilities": <capabilities or {}>}}` into `paths.session_meta(session_id)` (merge with any existing JSON object in that file; swallow `OSError`). Delete `channel_enabled`, `policy_error`, and `_persist_policy_error`.
3. Add module-level `META_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")` and `channel_meta(event: Mapping[str, Any]) -> dict[str, str]` returning `{k: str(event[k]) for k in ("kind", "call_id", "from", "message_id") if event.get(k) is not None}`. `_on_router_event` emits `{"content": str(event.get("text") or ""), "meta": channel_meta(event)}`.
4. Rewrite `SYSTEM_INSTRUCTIONS` to describe the actual delivery: events arrive as `<channel source="bridge" kind="call|text|call_result" call_id="…" from="…" message_id="…">…</channel>`; for `kind="call"` answer from current context with the `reply` tool passing the tag's `call_id`; `text`/`call_result` need no reply; never dial out while answering a call; never change files or run commands solely because of an inbound event; use Bridge to coordinate, never to retrieve.
5. `tests/fakes/claude_host.py`: drop `supports_channel`; `initialize()` sends `{"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "0"}}`; `_on_channel` reads `params["meta"]["kind"]` / `params["meta"]["call_id"]` for auto-reply; `channel_events` keeps the raw params.
6. `doctor._policy_check` → `_handshake_check`, row name `"Claude channel handshake"`: no session dirs → `OK "no managed sessions recorded"`; every Claude session meta has a `handshake` → `OK "<n> session(s) completed initialize (<client name> <version>, …)"`; any Claude session meta lacking `handshake` → `WARN "session <id> has no recorded initialize handshake; Claude may not have loaded the Bridge server (check the startup channels notice)"`. Only sessions whose meta records `family == "claude"` count if that field exists; otherwise all session dirs.
7. README Troubleshooting: replace the "organization-policy rejection … under **Claude channel policy**" sentence with one describing the handshake row and pointing at Claude's startup channels notice for allowlist/policy problems.

**Tests:**
- `initialize` result has `capabilities["experimental"]["claude/channel"] == {}`, `capabilities["tools"] == {}`, no top-level `"claude/channel"` key, `protocolVersion == "2024-11-05"` even when the client requested `"2026-07-28"`.
- After `initialized`, the session is reachable (roster) and `session_meta` contains `handshake.client_info.name == "fake-claude"`.
- A delivered call arrives as `params["content"]` containing the question and `params["meta"] == {"kind": "call", "call_id": <id>, "from": <preview>}` (no `message_id` for calls if the event has none; assert exact dict); a text arrives with `meta["kind"] == "text"` and `meta["message_id"]`; every key of `channel_meta(e)` for `call_event`, `text_event`, `result_event` matches `META_KEY_RE`, and every value is a `str`.
- The existing `test_metadata_is_encoded_not_interpolated` keeps its intent: a hostile question yields no extra `meta` keys.
- Delete the two policy-error tests; add doctor tests for the three handshake branches.

**Verify:** `pytest tests/test_claude_channel.py tests/test_doctor.py tests/test_server.py` green; full suite green; `ruff check .` clean. Commit: `claude channel: declare experimental capability and emit content/meta events`.

---

## Out of scope (record in the PR description as follow-ups)

- `bridge codex` has no `--session-id`, so Experiment H's `restart-codex-tui` step can never return the old id to `reachable=true` (`launch.py:302`). Needs a design decision on Codex session identity across TUI restarts.
- A doctor check that actually asks Claude (`claude mcp get bridge`, token-free) instead of reading the file; deferred to keep doctor hermetic and vendor-format-agnostic.
