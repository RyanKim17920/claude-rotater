"""Multi-account switcher for Claude Code.

The package attributes resolve lazily (PEP 562): importing any submodule runs
this file first, and ``cswap feed`` runs on every statusline refresh, where
pulling in the switcher (and with it the HTTP and Keychain stacks) would cost
over 100 ms per call for nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_rotater.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]


def __getattr__(name: str):
    if name == "__version__":
        from importlib.metadata import version

        value = version("claude-rotater")
    elif name == "ClaudeAccountSwitcher":
        from claude_rotater.switcher import ClaudeAccountSwitcher as value
    else:
        raise AttributeError(f"module 'claude_rotater' has no attribute {name!r}")
    globals()[name] = value
    return value
