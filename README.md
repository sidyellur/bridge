# bridge

**A phone system for live AI coding agents.**

You run Claude Code in one terminal and Codex in another. Today, moving information between them means copy-pasting by hand. `bridge` lets the exact live sessions reach each other directly.

- **text** — send an asynchronous message to another live agent.
- **call** — ask another live agent a question and receive its answer.

“Live” is a product guarantee: the addressed session handles the message with its current conversation context. Bridge does not substitute a fresh headless agent, a resumed snapshot, or another process in the same directory.

## How it works

Bridge uses the vendors' live integration surfaces:

- Claude receives calls through a two-way [Claude Code Channel](https://code.claude.com/docs/en/channels).
- Codex sessions run on a Bridge-managed [Codex App Server](https://developers.openai.com/codex/app-server/), with the normal TUI attached remotely.
- A small local Bridge router correlates calls, queues events while a peer is busy, pushes asynchronous answers back to the caller, and records an audit transcript.

```text
live Claude session ←→ Claude Channel ←→ Bridge router ←→ Codex App Server ←→ live Codex TUI
```

Sessions are launched through wrappers so Bridge can guarantee identity and reachability:

```sh
bridge claude
bridge codex
```

An agent session opened outside those wrappers is not silently treated as callable. Bridge reports it as unmanaged or unreachable and explains how to resume it through Bridge.

## Design principles

- The addressed live session answers. No substitute agents.
- Busy sessions queue inbound calls; Bridge does not steer unrelated active work.
- Use Bridge to coordinate, never to retrieve information already available on disk.
- Calls request answers and do not grant permission to edit files or run commands.
- Offline means unreachable, never “answered by a snapshot.”
- Hop, rate, queue, and timeout limits prevent agent loops and cost blowups.

## Status

Design and implementation planning. The live transport experiments are the first release gate because Claude Channels and parts of Codex App Server are currently preview/experimental interfaces.

See:

- [Design spec](docs/superpowers/specs/2026-08-26-bridge-design.md)
- [V1 implementation plan](docs/superpowers/plans/2026-08-26-bridge-v1-plan.md)

## Packaging

The planned Python distribution is `agent-bridge`; the repo, import, and command are `bridge`. A small Channel adapter may ship alongside the Python core using the official MCP SDK.

## License

MIT
