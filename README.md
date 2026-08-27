# bridge

**A phone system for AI coding agents.**

You run Claude Code in one terminal and Codex in another. Today, moving
information between them means copy-pasting by hand. `bridge` lets them reach
each other directly — over [MCP](https://modelcontextprotocol.io), from either
side.

Two ways to reach an agent, borrowed from how people already talk to each other:

- **text** — fire-and-forget. Lands in the other agent's inbox and on its screen,
  read on its own time.
- **call** — you ask a question, the peer answers from its own workspace, you
  continue.

## Why an adapter, not a message broker

Both Claude Code and Codex already ship a working phone system — for their own
family. `ListAgents`/`SendMessage` reach other Claude sessions; `codex agents`
and `codex queue` reach other Codex sessions. Every session is addressable, and
both CLIs can be invoked headlessly against a workspace.

What's missing is the **span between them**: Claude can't reach a Codex thread,
and Codex can't reach a Claude session.

So `bridge` is not a switchboard and owns no message infrastructure. It joins
two carriers that each already work — one MCP server, registered on both sides,
translating a shared tool surface onto whichever native mechanism the target
already provides. Session lifecycle, persistence, and delivery stay the
vendors' problem.

## Design principle

*Silent success is indistinguishable from failure in a tool whose entire job is
presence.* Every operation leaves a visible trace in **both** terminals — a
consult the peer never learns about is a call to a wax replica, not a
conversation.

## Status

Early. Design in progress — see `docs/superpowers/specs/`.

## Install

The distribution is published as `agent-bridge`; the command, import, and repo
are all `bridge`.

## License

MIT
