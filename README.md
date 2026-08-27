# dial

**A phone system for AI coding agents.**

You run Claude Code in one terminal and Codex in another. Today, moving
information between them means copy-pasting by hand. `dial` lets them reach
each other directly — over [MCP](https://modelcontextprotocol.io), from either
side.

Two ways to reach an agent, borrowed from how people already talk to each other:

- **text** — fire-and-forget. Lands in the other agent's inbox, read on its own time.
- **call** — synchronous. You ask, it answers with its full accumulated context, you continue.

## Why an adapter, not a phone network

Both Claude Code and Codex already ship a working phone system — for their own
family. `ListAgents`/`SendMessage` reach other Claude sessions; `codex agents`
and `codex queue` reach other Codex sessions. Every session is addressable, and
both CLIs can resume a warm session headlessly.

What's missing is the **interconnect**: Claude can't dial a Codex thread, and
Codex can't dial a Claude session.

So `dial` is not a switchboard. It's the thing that lets two carriers route to
each other — one MCP server, registered on both sides, translating a shared tool
surface onto whichever native mechanism the target already provides. Session
lifecycle, persistence, and warm-context resume stay the vendors' problem.

## Status

Early. Design in progress — see `docs/superpowers/specs/`.

## License

MIT
