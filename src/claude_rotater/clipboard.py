"""Put text on the system clipboard.

The TUI captures the mouse, so text on screen cannot be selected the usual
way (holding Option on macOS terminals, or Shift elsewhere, still can).
``y``/``Y`` copy through the platform's clipboard command instead: pbcopy on
macOS, wl-copy on Wayland, xclip or xsel on X11. With none of them (an SSH
session, a bare console) the caller falls back to the terminal's OSC 52
escape, which most modern terminals honour.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping

# A clipboard command that has not returned by now is not going to.
COPY_TIMEOUT_S = 2.0


def copy_command(
    *,
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The clipboard command for this session, or None when there is none."""
    env = os.environ if env is None else env
    if platform == "darwin":
        return ["pbcopy"] if which("pbcopy") else None
    if env.get("WAYLAND_DISPLAY") and which("wl-copy"):
        return ["wl-copy"]
    if env.get("DISPLAY"):
        if which("xclip"):
            return ["xclip", "-selection", "clipboard"]
        if which("xsel"):
            return ["xsel", "--clipboard", "--input"]
    return None


def system_copy(text: str, command: list[str] | None = None) -> bool:
    """Copy ``text`` with the clipboard command; False when there is none or
    it failed (the caller then tries OSC 52)."""
    command = command if command is not None else copy_command()
    if command is None:
        return False
    try:
        # Output to DEVNULL, never a pipe: xclip forks a child that keeps the
        # selection alive, and a pipe it inherited would hold run() open.
        subprocess.run(
            command,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=COPY_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
