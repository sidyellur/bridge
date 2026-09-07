"""Task 10 (Claude Channel launch) verify: version-based channel classification
(the preview hides the flags from ``--help``), and the subprocess probe against
a fake ``claude`` executable that never resolves to a real vendor binary.
"""

from __future__ import annotations

import pytest

from bridge.claude_probe import (
    DEV_CHANNEL_SPEC,
    DEV_CHANNELS_FLAG,
    PLUGIN_CHANNEL_SPEC,
    ChannelSupport,
    classify_channel_mode,
    detect_channel_mode,
    parse_version,
)

from .fakes.executables import make_capture_exe

PLUGIN_HELP = """
Usage: claude [options]

Channels:
  --channels <spec>   Configure a channel. Plugin channels become available
                       once your organization's channel marketplace allowlist
                       includes this plugin.
"""

# What the research-preview `claude --help` really prints: no channel flag at
# all, even though the binary accepts both of them.
UNSUPPORTED_HELP = """
Usage: claude [options]

  --model <model>     Select a model.
  --print             Print response and exit.
"""

DEV_ARGS = [DEV_CHANNELS_FLAG, DEV_CHANNEL_SPEC]
PLUGIN_ARGS = ["--channels", PLUGIN_CHANNEL_SPEC]


def test_development_launch_args_are_the_documented_flag_and_spec():
    mode = classify_channel_mode("2.1.263 (Claude Code)", UNSUPPORTED_HELP)
    assert mode.support is ChannelSupport.DEVELOPMENT
    assert mode.launch_args == ["--dangerously-load-development-channels", "server:bridge"]


@pytest.mark.parametrize(
    ("version", "help_text", "support", "launch_args"),
    [
        ("2.1.263 (Claude Code)", UNSUPPORTED_HELP, ChannelSupport.DEVELOPMENT, DEV_ARGS),
        ("2.1.234", UNSUPPORTED_HELP, ChannelSupport.DEVELOPMENT, DEV_ARGS),
        ("3.0.0", UNSUPPORTED_HELP, ChannelSupport.DEVELOPMENT, DEV_ARGS),
        ("2.1.233", UNSUPPORTED_HELP, ChannelSupport.UNSUPPORTED, []),
        ("garbage", UNSUPPORTED_HELP, ChannelSupport.UNSUPPORTED, []),
        ("2.1.263 (Claude Code)", PLUGIN_HELP, ChannelSupport.PLUGIN, PLUGIN_ARGS),
        ("1.0.0", PLUGIN_HELP, ChannelSupport.PLUGIN, PLUGIN_ARGS),
        ("garbage", PLUGIN_HELP, ChannelSupport.PLUGIN, PLUGIN_ARGS),
    ],
)
def test_classify_channel_mode_table(version, help_text, support, launch_args):
    mode = classify_channel_mode(version, help_text)
    assert mode.support is support
    assert mode.version == version
    assert mode.launch_args == launch_args


def test_classify_channel_mode_plugin_has_no_detail():
    assert classify_channel_mode("2.1.263", PLUGIN_HELP).detail == ""


def test_classify_channel_mode_development_detail_names_the_preview():
    mode = classify_channel_mode("2.1.263", UNSUPPORTED_HELP)
    assert mode.detail == "research preview: channel flags are accepted but hidden from --help"


def test_classify_channel_mode_old_version_detail_names_the_minimum():
    mode = classify_channel_mode("2.1.233", UNSUPPORTED_HELP)
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert "2.1.233" in mode.detail
    assert "2.1.234" in mode.detail


def test_classify_channel_mode_unparsable_version_detail_names_the_parse():
    mode = classify_channel_mode("garbage", UNSUPPORTED_HELP)
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert "parse" in mode.detail
    assert "garbage" in mode.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.1.263 (Claude Code)", (2, 1, 263)),
        ("claude 2.1.263", (2, 1, 263)),
        ("2.1.234", (2, 1, 234)),
        ("  3.0.0\n", (3, 0, 0)),
        ("garbage", None),
        ("", None),
        ("2.1", None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


def _fake_claude(
    bindir,
    capture,
    *,
    version: str,
    help_text: str,
    hang_on: tuple[str, ...] = (),
):
    extra = (
        f"if [a for a in sys.argv[1:] if a in {list(hang_on)!r}]:\n"
        "    time.sleep(30)\n"
        "elif '--version' in sys.argv[1:]:\n"
        f"    print({version!r})\n"
        "elif '--help' in sys.argv[1:]:\n"
        f"    print({help_text!r})\n"
    )
    make_capture_exe(bindir, "claude", capture, extra=extra)


def _env(bindir):
    return {"BRIDGE_CLAUDE_BIN": str(bindir / "claude"), "PATH": str(bindir)}


def test_detect_channel_mode_development_when_help_hides_the_flags(tmp_path):
    bindir = tmp_path / "bin"
    _fake_claude(bindir, tmp_path / "cap.jsonl", version="2.1.263", help_text=UNSUPPORTED_HELP)
    mode = detect_channel_mode(_env(bindir))
    assert mode.support is ChannelSupport.DEVELOPMENT
    assert mode.version == "2.1.263"
    assert mode.launch_args == DEV_ARGS


def test_detect_channel_mode_plugin(tmp_path):
    bindir = tmp_path / "bin"
    _fake_claude(bindir, tmp_path / "cap.jsonl", version="3.0.0", help_text=PLUGIN_HELP)
    mode = detect_channel_mode(_env(bindir))
    assert mode.support is ChannelSupport.PLUGIN
    assert mode.launch_args == PLUGIN_ARGS


def test_detect_channel_mode_unsupported_for_old_claude(tmp_path):
    bindir = tmp_path / "bin"
    _fake_claude(bindir, tmp_path / "cap.jsonl", version="0.9.0", help_text=UNSUPPORTED_HELP)
    mode = detect_channel_mode(_env(bindir))
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert mode.launch_args == []


def test_detect_channel_mode_missing_binary_is_unsupported():
    mode = detect_channel_mode({"PATH": "/nonexistent-dir-xyz"})
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert mode.detail


def test_detect_channel_mode_version_timeout_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.claude_probe._PROBE_TIMEOUT_S", 0.3)
    bindir = tmp_path / "bin"
    _fake_claude(
        bindir,
        tmp_path / "cap.jsonl",
        version="2.1.263",
        help_text=UNSUPPORTED_HELP,
        hang_on=("--version",),
    )
    mode = detect_channel_mode(_env(bindir))
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert "--version did not respond" in mode.detail


def test_detect_channel_mode_help_timeout_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.claude_probe._PROBE_TIMEOUT_S", 0.3)
    bindir = tmp_path / "bin"
    _fake_claude(
        bindir,
        tmp_path / "cap.jsonl",
        version="2.1.263",
        help_text=UNSUPPORTED_HELP,
        hang_on=("--help",),
    )
    mode = detect_channel_mode(_env(bindir))
    assert mode.support is ChannelSupport.UNSUPPORTED
    assert "--help did not respond" in mode.detail
    assert mode.version == "2.1.263"
