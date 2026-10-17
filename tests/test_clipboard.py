"""Clipboard copy: which command per platform, and the copy itself through a
stubbed command (never the real clipboard)."""

from __future__ import annotations

import sys

import pytest

from claude_rotater import clipboard


def which_of(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


@pytest.mark.parametrize(
    "platform, env, present, expected",
    [
        ("darwin", {}, ("pbcopy",), ["pbcopy"]),
        ("darwin", {}, (), None),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, ("wl-copy", "xclip"), ["wl-copy"]),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, ("xclip",),
         ["xclip", "-selection", "clipboard"]),
        ("linux", {"DISPLAY": ":0"}, ("xclip", "xsel"), ["xclip", "-selection", "clipboard"]),
        ("linux", {"DISPLAY": ":0"}, ("xsel",), ["xsel", "--clipboard", "--input"]),
        ("linux", {}, ("xclip", "wl-copy"), None),  # no display: SSH, a console
    ],
)
def test_copy_command(platform, env, present, expected):
    assert clipboard.copy_command(platform=platform, env=env, which=which_of(*present)) == expected


def test_system_copy_hands_the_text_to_the_command(tmp_path):
    out = tmp_path / "clip"
    stub = [sys.executable, "-c",
            f"import sys; open({str(out)!r}, 'wb').write(sys.stdin.buffer.read())"]
    assert clipboard.system_copy("me@x.com · 5h 20% · ünïcode", stub)
    assert out.read_text(encoding="utf-8") == "me@x.com · 5h 20% · ünïcode"


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-c", "import sys; sys.exit(1)"],  # the command failed
        ["/nonexistent/clipboard-tool"],  # not installed after all
    ],
)
def test_system_copy_reports_failure(command):
    assert clipboard.system_copy("x", command) is False


def test_no_command_means_no_copy(monkeypatch):
    monkeypatch.setattr(clipboard, "copy_command", lambda: None)
    assert clipboard.system_copy("x") is False


def test_a_hung_command_times_out(monkeypatch):
    monkeypatch.setattr(clipboard, "COPY_TIMEOUT_S", 0.2)
    assert clipboard.system_copy("x", [sys.executable, "-c", "import time; time.sleep(5)"]) is False
