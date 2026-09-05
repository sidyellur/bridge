"""``bridge`` command-line entry point.

Subcommands are wired up incrementally by later modules; this module owns
argument parsing, ``--version``, and dispatch. Wrappers (``bridge claude`` /
``bridge codex``), diagnostics (``roster`` / ``call`` / ``text`` /
``transcript``), and lifecycle (``install`` / ``uninstall`` / ``doctor`` /
``router``) all resolve here.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge", description="Live coordination between AI coding agents."
    )
    parser.add_argument("--version", action="version", version=f"bridge {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # Wrappers -------------------------------------------------------------
    p_claude = sub.add_parser("claude", help="launch a Bridge-managed Claude session")
    p_claude.add_argument("args", nargs=argparse.REMAINDER, help="arguments forwarded to claude")

    p_codex = sub.add_parser("codex", help="launch a Bridge-managed Codex session")
    p_codex.add_argument("args", nargs=argparse.REMAINDER, help="arguments forwarded to codex")

    # Diagnostics ----------------------------------------------------------
    p_roster = sub.add_parser("roster", help="list managed sessions")
    p_roster.add_argument("--include-unmanaged", action="store_true")
    p_roster.add_argument("--json", action="store_true", help="emit raw JSON")

    p_text = sub.add_parser("text", help="send an informational text to a session")
    p_text.add_argument("to")
    p_text.add_argument("message")

    p_call = sub.add_parser("call", help="ask a session a question and wait for its answer")
    p_call.add_argument("to")
    p_call.add_argument("question")
    p_call.add_argument("--timeout", type=int, default=60)

    p_transcript = sub.add_parser("transcript", help="show recent Bridge activity")
    p_transcript.add_argument("--peer", default=None)
    p_transcript.add_argument("--limit", type=int, default=20)

    # --- post-v1 polish (#5 A+C): aliases and retention ------------------
    p_alias = sub.add_parser("alias", help="name a session id so you never type a UUID")
    p_alias.add_argument("name", nargs="?", default=None, help="alias name (omit to list)")
    p_alias.add_argument("session_id", nargs="?", default=None, help="target Bridge session id")
    p_alias.add_argument("--rm", action="store_true", help="remove the named alias")
    # --- end post-v1 polish block ----------------------------------------

    # Lifecycle ------------------------------------------------------------
    p_install = sub.add_parser("install", help="install Bridge adapters and guidance")
    p_install.add_argument("--dry-run", action="store_true")
    sub.add_parser("uninstall", help="reverse a Bridge installation")
    sub.add_parser("doctor", help="diagnose the Bridge installation")

    p_router = sub.add_parser("router", help="router daemon control (internal)")
    p_router.add_argument(
        "router_action", choices=["run", "stop", "status"], nargs="?", default="status"
    )

    # MCP server (internal, launched by vendors) ---------------------------
    p_serve = sub.add_parser("serve", help="run the Bridge MCP tool server over stdio (internal)")
    p_serve.add_argument("--family", choices=["claude", "codex"], default="codex")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.command:
        parser.print_help()
        return 0

    # Dispatch is delegated to feature modules; imports are local so that
    # ``bridge --version`` never pays for heavier subsystems.
    if args.command == "roster":
        from .roster import cli_roster

        return cli_roster(include_unmanaged=args.include_unmanaged, as_json=args.json)
    if args.command == "text":
        from .cli_commands import cli_text

        return cli_text(args.to, args.message)
    if args.command == "call":
        from .cli_commands import cli_call

        return cli_call(args.to, args.question, timeout_s=args.timeout)
    if args.command == "transcript":
        from .cli_commands import cli_transcript

        return cli_transcript(peer=args.peer, limit=args.limit)
    # --- post-v1 polish (#5 A+C): aliases and retention ------------------
    if args.command == "alias":
        from .contacts import cli_alias

        return cli_alias(args.name, args.session_id, rm=args.rm)
    # --- end post-v1 polish block ----------------------------------------
    if args.command == "claude":
        from .launch import cli_launch_claude

        return cli_launch_claude(args.args)
    if args.command == "codex":
        from .launch import cli_launch_codex

        return cli_launch_codex(args.args)
    if args.command == "install":
        from .install import cli_install

        return cli_install(dry_run=args.dry_run)
    if args.command == "uninstall":
        from .install import cli_uninstall

        return cli_uninstall()
    if args.command == "doctor":
        from .doctor import cli_doctor

        return cli_doctor()
    if args.command == "router":
        from .router import cli_router

        return cli_router(args.router_action)
    if args.command == "serve":
        from .server import cli_serve

        return cli_serve(family=args.family)

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
