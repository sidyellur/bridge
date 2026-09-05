"""Detect how (or whether) the local ``claude`` binary supports Claude Code
Channels, per spec §5/§11.

Classification is split into a pure function, :func:`classify_help`, that
grades canned ``--version``/``--help`` text, and :func:`detect_channel_mode`,
which resolves the binary (honoring ``BRIDGE_CLAUDE_BIN``) and runs the two
token-free probe commands. Neither probe ever starts a model turn: both flags
are documented as immediate, non-interactive commands.

During the Channels research preview, Claude only exposes a development
channel flag. Once the Bridge plugin is on an effective marketplace/allowlist,
``--help`` is expected to advertise a plugin channel spec instead. Doctor and
the installer key off the same :class:`ChannelMode` result so detection never
drifts between the two call sites.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import StrEnum

from .launch import resolve_binary

CHANNELS_FLAG = "--channels"

# Substrings `--help` output is graded against. These are deliberately loose:
# doctor/install must degrade to "unsupported" rather than guess when a vendor
# CLI's exact wording drifts, per spec §12 ("Vendor protocol drift").
MARKETPLACE_MARKERS = ("marketplace", "allowlist")
DEV_CHANNEL_MARKERS = ("development", "research preview", "dev-channel")

# Markers embedded in the launch args this module hands back, so launch.py can
# describe the active mode from the args file alone (see describe_channel_mode
# in launch.py) without re-probing at every `bridge claude` startup.
PLUGIN_ARG_MARKER = "plugin:"
DEV_ARG_MARKER = "dev:"

DEFAULT_MARKETPLACE = "bridge-marketplace"
PLUGIN_CHANNEL_SPEC = f"plugin:bridge@{DEFAULT_MARKETPLACE}"
DEV_CHANNEL_SPEC = "dev:bridge"

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


def classify_help(version: str, help_text: str) -> ChannelMode:
    """Pure classification of canned ``--help`` text. No subprocess involved."""
    text = help_text.lower()
    has_channels_flag = CHANNELS_FLAG in help_text
    has_marketplace_marker = has_channels_flag and any(m in text for m in MARKETPLACE_MARKERS)
    has_dev_marker = has_channels_flag and any(m in text for m in DEV_CHANNEL_MARKERS)

    if has_marketplace_marker:
        return ChannelMode(ChannelSupport.PLUGIN, version, [CHANNELS_FLAG, PLUGIN_CHANNEL_SPEC])
    if has_dev_marker:
        return ChannelMode(ChannelSupport.DEVELOPMENT, version, [CHANNELS_FLAG, DEV_CHANNEL_SPEC])
    return ChannelMode(
        ChannelSupport.UNSUPPORTED,
        version,
        [],
        detail="claude --help does not advertise a channels flag",
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
    return classify_help(version.strip(), help_text)


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
