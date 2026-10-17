"""Steps the end-to-end tests run inside the sandbox, one per process.

These are the rota code paths with no CLI verb (adding the current login,
the staged login, the TUI), entered exactly where the TUI or the CLI enters
them, including ``profiles.apply()`` first. The sandbox gives each run a
fresh environment; nothing here knows it is a test.

Each step prints one JSON line on stdout as its last line::

    {"ok": true, ...} | {"ok": false, "error": "...", "type": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import json
import sys
import traceback


def _switcher():
    from claude_rotater import profiles

    profiles.apply()  # what cli.main does before anything resolves a dir
    from claude_rotater.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher()


def add_current(args) -> dict:
    """The TUI's ``a`` → ``c`` (current login): ``switcher.add_account()``."""
    sw = _switcher()
    sw.add_account(slot=args.slot, assume_yes=True)
    return {"backup_dir": str(sw.backup_dir)}


def login(args) -> dict:
    """The TUI's ``l``: StagedLogin.authenticate, then import_into, then
    cleanup, in the order ``RotaApp.start_login``/``_import_login`` run them."""
    from claude_rotater.login import StagedLogin

    sw = _switcher()
    stage = StagedLogin(sw.backup_dir)
    try:
        status = stage.authenticate(method=args.method, email=args.email)
        config_dir = stage.config_dir
        stage.import_into(sw)
    finally:
        stage.cleanup()
    return {"backup_dir": str(sw.backup_dir), "stage": config_dir, "status": status}


async def _tui(args) -> dict:
    from claude_rotater.tui.app import RotaApp

    sw = _switcher()
    app = RotaApp(sw, detected="dark")
    said: list[str] = []
    original_say = app.say

    def say(text, role="info"):
        said.append(f"{role}: {text}")
        original_say(text, role)

    app.say = say

    @contextlib.contextmanager
    def suspend():  # headless pilot: there is no terminal to hand over
        yield

    app.suspend = suspend

    async def settle(pilot):
        for _ in range(3):
            pending = [w for w in app.workers if w.group not in ("engine", "refresher")]
            if pending:
                await app.workers.wait_for_complete(pending)
            await pilot.pause()

    async with app.run_test(size=(140, 45)) as pilot:
        await settle(pilot)
        if args.tui_action == "switch":
            app.do_switch(args.target)
        elif args.tui_action == "login":
            await pilot.press("l")
        elif args.tui_action == "add-current":
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("c")
        await settle(pilot)
        busy = app.busy
    return {"said": said, "busy_after": busy}


def tui(args) -> dict:
    return asyncio.run(_tui(args))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="step", required=True)
    p = sub.add_parser("add-current")
    p.add_argument("--slot", type=int)
    p = sub.add_parser("login")
    p.add_argument("--method", default="claudeai")
    p.add_argument("--email")
    p = sub.add_parser("tui")
    p.add_argument("tui_action", choices=["switch", "login", "add-current"])
    p.add_argument("target", nargs="?")
    args = parser.parse_args(argv)
    # A hung step dumps every thread's stack instead of timing out silently.
    faulthandler.dump_traceback_later(60, exit=True)
    step = {"add-current": add_current, "login": login, "tui": tui}[args.step]
    try:
        out = {"ok": True, **step(args)}
    except BaseException as e:  # noqa: BLE001 -- reported to the test
        traceback.print_exc()
        out = {"ok": False, "type": type(e).__name__, "error": str(e)}
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
