"""The background paths (``rota auto``, the feed ingestor and watcher, the
token refresher, history, statusline setup) must never load the TUI stack:
Textual and Rich cost tens of MB of RSS in a process that otherwise idles
at ~15 MB. Checked in a fresh interpreter, since the test process itself
imports the TUI."""

from __future__ import annotations

import subprocess
import sys

HEADLESS_MODULES = (
    "claude_rotater.cli",
    "claude_rotater.autoswitch",
    "claude_rotater.feed",
    "claude_rotater.feed_ingest",
    "claude_rotater.feed_watch",
    "claude_rotater.token_refresh",
    "claude_rotater.usage_history",
    "claude_rotater.usage_store",
    "claude_rotater.estimator",
    "claude_rotater.poll_policy",
    "claude_rotater.statusline_setup",
)


def test_headless_modules_do_not_import_the_tui_stack():
    code = (
        "import sys\n"
        f"for name in {HEADLESS_MODULES!r}:\n"
        "    __import__(name)\n"
        "from claude_rotater.cli import _auto_command\n"
        "heavy = sorted(m for m in sys.modules\n"
        "               if m.split('.')[0] in ('textual', 'rich')\n"
        "               or m.startswith('claude_rotater.tui'))\n"
        "assert not heavy, heavy\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_background_paths_never_load_the_costs_code():
    """Costs run on demand from the TUI only: the engine, the feed and
    ``rota auto`` must not even import them, and the cost modules themselves
    stay free of the TUI stack."""
    code = (
        "import sys\n"
        f"for name in {HEADLESS_MODULES!r}:\n"
        "    __import__(name)\n"
        "from claude_rotater.cli import _auto_command\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('claude_rotater.cost'))\n"
        "assert not loaded, loaded\n"
        "import claude_rotater.costs, claude_rotater.cost_report\n"
        "heavy = sorted(m for m in sys.modules\n"
        "               if m.split('.')[0] in ('textual', 'rich')\n"
        "               or m.startswith('claude_rotater.tui'))\n"
        "assert not heavy, heavy\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
