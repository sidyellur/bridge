"""Task 10 (Claude Channel launch) verify: pure --help/--version classification,
and the subprocess probe against a fake ``claude`` executable that never
resolves to a real vendor binary.
"""

from __future__ import annotations

from bridge.claude_probe import (
    DEV_CHANNEL_SPEC,
    PLUGIN_CHANNEL_SPEC,
    ChannelSupport,
    classify_help,
    detect_channel_mode,
)

from .fakes.executables import make_capture_exe

PLUGIN_HELP = """
Usage: claude [options]

Channels:
  --channels <spec>   Configure a channel. Plugin channels become available
                       once your organization's channel marketplace allowlist
                       includes this plugin.
"""

DEV_HELP = """
Usage: claude [options]

Channels (research preview):
  --channels <spec>   Configure a development channel while Claude Code
                       Channels remain a research preview.
"""

UNSUPPORTED_HELP = """
Usage: claude [options]

  --model <model>     Select a model.
  --print             Print response and exit.
"""


def test_classify_help_plugin_mode():
    mode = classify_help("2.1.0", PLUGIN_HELP)
    assert mode.support is ChannelSupport.PLUGIN
    assert mode.version == "2.1.0"
    assert mode.launch_args == ["--channels", PLUGIN_CHANNEL_SPEC]
    assert mode.detail == ""


def test_classify_help_development_mode():
    mode = classify_help("2.1.0", DEV_HELP)
    assert mode.support is ChannelSupport.DEVELOPMENT
    assert mode.launch_args == ["--channels", DEV_CHANNEL_SPEC]


def test_classify_help_unsupported_without_channels_flag():
    mode = classify_help("1.0.0", UNSUPPORTED_HELP)
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert mode.launch_args == []
    assert mode.detail != ""


def test_classify_help_channels_flag_without_marker_is_unsupported():
    # A bare --channels flag with no marketplace/dev wording must not be
    # guessed into either supported mode (spec §12: no drift-guessing).
    mode = classify_help("1.5.0", "Usage: claude [options]\n  --channels <spec>  Configure.\n")
    assert mode.support is ChannelSupport.UNSUPPORTED


def _fake_claude(bindir, capture, *, version: str, help_text: str):
    extra = (
        "if '--version' in sys.argv[1:]:\n"
        f"    print({version!r})\n"
        "elif '--help' in sys.argv[1:]:\n"
        f"    print({help_text!r})\n"
    )
    make_capture_exe(bindir, "claude", capture, extra=extra)


def test_detect_channel_mode_development(tmp_path):
    bindir = tmp_path / "bin"
    _fake_claude(bindir, tmp_path / "cap.jsonl", version="2.5.0", help_text=DEV_HELP)
    env = {"BRIDGE_CLAUDE_BIN": str(bindir / "claude"), "PATH": str(bindir)}
    mode = detect_channel_mode(env)
    assert mode.support is ChannelSupport.DEVELOPMENT
    assert mode.version == "2.5.0"
    assert mode.launch_args == ["--channels", DEV_CHANNEL_SPEC]


def test_detect_channel_mode_plugin(tmp_path):
    bindir = tmp_path / "bin"
    _fake_claude(bindir, tmp_path / "cap.jsonl", version="3.0.0", help_text=PLUGIN_HELP)
    env = {"BRIDGE_CLAUDE_BIN": str(bindir / "claude"), "PATH": str(bindir)}
    mode = detect_channel_mode(env)
    assert mode.support is ChannelSupport.PLUGIN
    assert mode.launch_args == ["--channels", PLUGIN_CHANNEL_SPEC]


def test_detect_channel_mode_unsupported_help(tmp_path):
    bindir = tmp_path / "bin"
    _fake_claude(bindir, tmp_path / "cap.jsonl", version="0.9.0", help_text=UNSUPPORTED_HELP)
    env = {"BRIDGE_CLAUDE_BIN": str(bindir / "claude"), "PATH": str(bindir)}
    mode = detect_channel_mode(env)
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert mode.launch_args == []


def test_detect_channel_mode_missing_binary_is_unsupported():
    mode = detect_channel_mode({"PATH": "/nonexistent-dir-xyz"})
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert mode.detail
