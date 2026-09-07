"""Detect how (or whether) the local ``claude`` binary supports Claude Code
Channels, per spec §5/§11.

The channel flags are *deliberately hidden* during the research preview:
``code.claude.com/docs/en/channels`` ("Research preview") states that neither
``--channels`` nor ``--dangerously-load-development-channels`` is listed in
``claude --help`` while the preview lasts, and that the flags work anyway.
Grepping ``--help`` for them therefore answers "unsupported" on every binary
that actually supports them, so Bridge classifies the development path by
*version* instead (:data:`MIN_CHANNELS_VERSION`). ``--help`` is still read, but
only for the plugin/marketplace upgrade path: once the Bridge plugin is on an
effective channel allowlist, ``--help`` advertises it and Bridge switches to
the fully supported ``--channels plugin:…`` launch.

Classification is split into a pure function, :func:`classify_channel_mode`,
that grades canned ``--version``/``--help`` text, and :func:`detect_channel_mode`,
which resolves the binary (honoring ``BRIDGE_CLAUDE_BIN``) and runs the two
token-free probe commands. Neither probe ever starts a model turn: both flags
are documented as immediate, non-interactive commands. Doctor and the installer
key off the same :class:`ChannelMode` result so detection never drifts between
the two call sites.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum

from .launch import resolve_binary

CHANNELS_FLAG = "--channels"
DEV_CHANNELS_FLAG = "--dangerously-load-development-channels"

# Substrings `--help` output is graded against for the plugin path. These are
# deliberately loose: doctor/install must degrade to the development path
# rather than guess when a vendor CLI's exact wording drifts, per spec §12
# ("Vendor protocol drift").
MARKETPLACE_MARKERS = ("marketplace", "allowlist")

# Markers embedded in the launch args this module hands back, so launch.py can
# describe the active mode from the args file alone (see describe_channel_mode
# in launch.py) without re-probing at every `bridge claude` startup.
PLUGIN_ARG_MARKER = "plugin:"
DEV_ARG_MARKER = DEV_CHANNELS_FLAG

DEFAULT_MARKETPLACE = "bridge-marketplace"
PLUGIN_CHANNEL_SPEC = f"plugin:bridge@{DEFAULT_MARKETPLACE}"
# `server:<mcp-server-name>`: the key `bridge install` registers Bridge's MCP
# server under in ~/.claude.json.
DEV_CHANNEL_SPEC = "server:bridge"

MIN_CHANNELS_VERSION = (2, 1, 234)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PROBE_TIMEOUT_S = 5.0


class ChannelSupport(StrEnum):
    """The three launch postures spec §5/§11 distinguish."""

    PLUGIN = "plugin"
    DEVELOPMENT = "development"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ChannelMode:
    support: ChannelSupport
    version: str = ""
    launch_args: list[str] = field(default_factory=list)
    detail: str = ""


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first ``major.minor.patch`` triple anywhere in ``text``, so both
    ``"2.1.263"`` and ``"2.1.263 (Claude Code)"`` parse."""
    m = _VERSION_RE.search(text)
    if m is None:
        return None
    return int(m[1]), int(m[2]), int(m[3])


def classify_channel_mode(version: str, help_text: str) -> ChannelMode:
    """Pure classification of canned ``--version``/``--help`` text. No
    subprocess involved."""
    text = help_text.lower()
    if CHANNELS_FLAG in help_text and any(m in text for m in MARKETPLACE_MARKERS):
        return ChannelMode(ChannelSupport.PLUGIN, version, [CHANNELS_FLAG, PLUGIN_CHANNEL_SPEC])

    parsed = parse_version(version)
    if parsed is None:
        return ChannelMode(
            ChannelSupport.UNSUPPORTED,
            version,
            [],
            detail=f"could not parse a claude version from {version!r}",
        )
    if parsed >= MIN_CHANNELS_VERSION:
        return ChannelMode(
            ChannelSupport.DEVELOPMENT,
            version,
            [DEV_CHANNELS_FLAG, DEV_CHANNEL_SPEC],
            detail="research preview: channel flags are accepted but hidden from --help",
        )
    current = ".".join(str(p) for p in parsed)
    minimum = ".".join(str(p) for p in MIN_CHANNELS_VERSION)
    return ChannelMode(
        ChannelSupport.UNSUPPORTED,
        version,
        [],
        detail=f"claude {current} predates Claude Code Channels (needs >= {minimum})",
    )


def detect_channel_mode(env: dict[str, str] | None = None) -> ChannelMode:
    """Resolve the ``claude`` binary and classify its ``--version``/``--help``.

    Never raises: a missing binary or a probe failure both classify as
    :attr:`ChannelSupport.UNSUPPORTED` with an explanatory ``detail`` so
    ``install``/``doctor`` can report rather than crash.
    """
    try:
        binary = resolve_binary("claude", env)
    except FileNotFoundError as exc:
        return ChannelMode(ChannelSupport.UNSUPPORTED, detail=str(exc))

    version = _run(binary, "--version", env)
    if version is None:
        return ChannelMode(ChannelSupport.UNSUPPORTED, detail=f"{binary} --version did not respond")
    help_text = _run(binary, "--help", env)
    if help_text is None:
        return ChannelMode(
            ChannelSupport.UNSUPPORTED, version.strip(), detail=f"{binary} --help did not respond"
        )
    return classify_channel_mode(version.strip(), help_text)


def _run(binary: str, flag: str, env: dict[str, str] | None) -> str | None:
    try:
        proc = subprocess.run(
            [binary, flag],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or "") + (proc.stderr or "")
