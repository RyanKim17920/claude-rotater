"""Textual-based interactive TUI for rota.

Entry point for bare ``rota`` in an interactive terminal. Heavy imports
(textual, rich) stay inside :func:`run` so the plain CLI paths —
``rota status``, cron's ``rota auto --once`` — never pay for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_rotater.switcher import ClaudeAccountSwitcher


def run(switcher: "ClaudeAccountSwitcher") -> int:
    """Run the TUI over an existing switcher. Returns the process exit code."""
    from claude_rotater.appearance import detect_terminal_background, drain_stdin
    from claude_rotater.tui.app import RotaApp

    # Sense the terminal background while we still own stdin in cooked mode
    # (Textual's driver starts inside app.run()). Always detect so cycling to
    # 'auto' works even when the initial theme is explicit. Both calls are
    # meant to fail safe on their own, but they're wrapped here too: a
    # detection bug must never crash the TUI launch.
    try:
        detected = detect_terminal_background()
    except Exception:
        detected = None
    app = RotaApp(switcher, detected=detected)
    # Drain any late OSC reply immediately before Textual's driver starts,
    # so it isn't reissued as keystrokes once the app takes over the terminal.
    try:
        drain_stdin()
    except Exception:
        pass
    app.run()
    return app.return_code or 0
