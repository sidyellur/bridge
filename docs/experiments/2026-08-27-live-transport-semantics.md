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

Verdict: TBD (requires live Claude session + human observer)

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

Verdict: TBD (requires live Codex session + human observer)

---

## Experiment G — Busy-session serialization

**Question.** For both families, is an inbound call delivered during an unrelated
active turn serialized (queued until idle) with no accidental `turn/steer`?

**Procedure.** With each session mid-turn, deliver a call and observe whether the
vendor queues it natively or the adapter must buffer until an idle notification.
Prove no steer occurs.

Verdict: TBD (requires live Claude+Codex sessions + human observer)

---

## Experiment H — Lifecycle and reconnect

**Question.** How do session ids, reachability, queued deadlines, and resumption
behave when the router, channel adapter, App Server, and TUI are killed and
restarted one at a time?

**Procedure.** Kill/restart each component in turn; record how the roster's
`reachable`, queued call deadlines, and re-binding of the Bridge address behave.

Verdict: TBD (requires live Claude+Codex sessions + human observer)
