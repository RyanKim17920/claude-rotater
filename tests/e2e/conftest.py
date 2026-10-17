"""End-to-end tests: opt in with ``ROTA_E2E=1`` (macOS, Claude Code on PATH).

    ROTA_E2E=1 uv run --group dev pytest tests/e2e -n0 -v

Skipped by default. See ``sandbox.py`` for how they stay off the owner's
login keychain, config dirs and rota data.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys

import pytest

from tests.e2e.sandbox import Sandbox, real_claude

ENABLED = os.environ.get("ROTA_E2E") == "1"


def pytest_collection_modifyitems(config, items):
    if ENABLED and sys.platform == "darwin":
        return
    reason = ("end-to-end: set ROTA_E2E=1 to run" if not ENABLED
              else "end-to-end tests need macOS")
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.skip(reason=reason))


def _owner_keychain_state() -> tuple[str, str]:
    """The owner's own keychain search list and default, read with the
    owner's real HOME (the parent conftest swaps HOME for every test)."""
    env = {"HOME": pwd.getpwuid(os.getuid()).pw_dir, "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(["/usr/bin/security", *a], env=env, capture_output=True,
                                    text=True, timeout=30).stdout
    return run("list-keychains", "-d", "user"), run("default-keychain", "-d", "user")


# Each test runs twice: with the network down (all rota sees offline) and
# with Anthropic's OAuth endpoints answered by the sandbox's fake API, so the
# identity check, usage fetch and refresh paths run as they do for real.
@pytest.fixture(params=["offline", "api"])
def sandbox(request, tmp_path_factory):
    claude = real_claude()
    if claude is None:
        pytest.skip("no claude binary found (set ROTA_E2E_CLAUDE)")
    before = _owner_keychain_state()
    box = Sandbox(tmp_path_factory.mktemp("rota-e2e"), claude)
    try:
        box.create()
        if request.param == "api":
            box.start_api()
        yield box
    finally:
        box.destroy()
        assert _owner_keychain_state() == before, "the owner's keychain search list changed"
