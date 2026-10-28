"""Console-script entry point.

``rota feed`` runs on every Claude Code statusline refresh, and ``rota key``
on Claude Code's request path when an API key is active, so both are
dispatched here before ``claude_rotater.cli`` (which imports the switcher and
its HTTP/Keychain stack) is loaded. Everything else goes to the CLI.
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "feed":
        from claude_rotater.feed import main as feed_main

        sys.exit(feed_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "key":
        from claude_rotater.api_key_mode import main as key_main

        sys.exit(key_main(sys.argv[2:]))
    from claude_rotater.cli import main as cli_main

    cli_main()
