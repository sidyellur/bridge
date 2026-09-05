# End-to-end live smoke checklist

Date: 2026-08-26
Plan: `docs/superpowers/plans/2026-08-26-bridge-v1-plan.md` Task 11
Test: `tests/live/test_e2e.py` (marked `@pytest.mark.live`, excluded by default)

> **Status: requires a live environment.** These checks need one `bridge claude`
> and one `bridge codex` session open with a human watching both TUIs. Run with
> `pytest -m live tests/live/` on a host that has both vendor CLIs and a working
> Bridge install. Check each box only after observing it directly.

With one `bridge claude` and one `bridge codex` session open:

- [ ] 1. Claude→Codex synchronous `call` appears in the addressed Codex TUI and
      the reply returns to the initiating Claude turn.
- [ ] 2. Codex→Claude synchronous `call` appears in the addressed Claude
      conversation and returns to the initiating Codex turn.
- [ ] 3. `text` works in both directions with no reply required.
- [ ] 4. `call_async` in both directions wakes the exact caller with a
      correlated result.
- [ ] 5. A call sent while the target is busy waits and does not steer the
      unrelated turn.
- [ ] 6. The callee cannot dial out while answering; the 11th ordered-pair
      message is rate-limited.
- [ ] 7. Killing either adapter changes roster reachability and returns
      `unreachable`, never a headless answer.
- [ ] 8. Both human-visible transcripts and the Bridge transcript agree on
      call id, sender, target, and outcome.
