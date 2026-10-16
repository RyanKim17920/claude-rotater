"""Console-script entry point.

``cswap feed`` runs on every Claude Code statusline refresh, so it is
dispatched here before ``claude_swap.cli`` (which imports the switcher and
its HTTP/Keychain stack) is loaded. Everything else goes to the CLI.
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "feed":
        from claude_swap.feed import main as feed_main

        sys.exit(feed_main(sys.argv[2:]))
    from claude_swap.cli import main as cli_main

    cli_main()
