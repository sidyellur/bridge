"""``bridge lab``: the harness that turns live-transport Experiments E-H from a
vague manual chore into a runnable procedure with recorded evidence.

The lab never fabricates a verdict. It records installed versions, gates on
``bridge doctor``, fires the exact stimulus each experiment calls for through
the running router, captures both directions of the live wire traffic, and then
writes a *human-supplied* PASS/FAIL verdict into
``docs/experiments/2026-08-27-live-transport-semantics.md``.
"""

from __future__ import annotations

__all__ = ["capture", "cli"]
