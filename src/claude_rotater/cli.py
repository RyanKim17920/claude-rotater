"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import json
import os
import sys

from claude_rotater import __version__, paths, printer
from claude_rotater.exceptions import ClaudeSwitchError
from claude_rotater.json_output import error_envelope
from claude_rotater.printer import dimmed, error, force_utf8_output
from claude_rotater.settings import load_ui_settings
from claude_rotater.switcher import ClaudeAccountSwitcher


def _prog_name() -> str:
    """The command name to show in usage/help.

    argparse otherwise defaults to ``os.path.basename(sys.argv[0])``, which for
    an installed entry-point shim renders as an ugly absolute path (e.g.
    ``python.exe C:\\Users\\me\\.local\\bin\\cswap``). We strip that down to the
    bare command the user typed (``cswap`` / ``claude-swap``), falling back to
    ``cswap`` for ``python -m claude_rotater`` and odd launchers.
    """
    name = os.path.basename(sys.argv[0] or "")
    for ext in (".exe", ".pyw", ".py"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name or name in {"__main__", "python", "python3", "py"}:
        return "cswap"
    return name


def _run_command(argv: list[str]) -> None:
    """Handle `cswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built so everything after
    ``--`` reaches claude untouched. Limitation: `run` must be the first
    argument (`cswap --debug run 2` is not supported; use
    `cswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap run 2
  cswap run user@example.com
  cswap run 2 --no-share
  cswap run 2 --share-history
  cswap run 2 --require-session
  cswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        metavar="NUM|EMAIL",
        help="Account to run (number or email)",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents/rules "
            "from ~/.claude into the session profile (and remove previously "
            "shared items)"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Share conversation history (projects/ and history.jsonl) from "
            "~/.claude into the session profile, so every account sees one "
            "unified history. History the profile already accumulated is "
            "merged into ~/.claude first. --no-share-history restores "
            "per-account history (the default). Not supported on Windows."
        ),
    )
    parser.add_argument(
        "--require-session",
        action="store_true",
        help=(
            "Refuse to launch when the account is already the active default "
            "login, instead of running plain claude on that login (which a "
            "later switch could pull out from under the session)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_rotater.session import SessionManager

        manager = SessionManager(switcher)

        manager.run(
            args.account,
            tail,
            share=not args.no_share,
            share_history=args.share_history,
            require_session=args.require_session,
        )
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _guard_root(switcher: ClaudeAccountSwitcher) -> None:
    """Refuse to run as root outside a container (shared by every command)."""
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)


def _auto_command(argv: list[str]) -> None:
    """Handle `cswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    parser = argparse.ArgumentParser(
        prog="cswap auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly). The TUI (bare cswap) runs "
            "this same engine live while it is open, so you only need "
            "cswap auto for headless use."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  cswap auto                       # foreground loop, switch at 90%% used
  cswap auto --threshold 80        # switch earlier
  cswap auto --model Fable         # also switch when the Fable weekly limit is hit
  cswap auto --json                # one JSON event per line (for scripts)
  cswap auto --once; echo $?       # single tick, outcome in exit code
  cswap auto --dry-run             # log decisions, never actually switch

Defaults live in settings.json in the backup root; flags override them.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    # Retired: the loop now sleeps until the next poll plan or statusline
    # feed change. Still accepted (and ignored) so existing service units
    # and cron lines keep starting.
    parser.add_argument("--interval", type=float, help=argparse.SUPPRESS)
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's binding 5h/7d window reaches "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "Also switch when a per-model weekly limit is hit, not just the "
            "account-wide 5h/7d windows. One name or a comma-separated list "
            "(e.g. Fable, Opus, Sonnet, Haiku, or 'Fable,Opus'), or 'all' "
            "for every per-model window an account reports"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=("best", "consume-first"),
        default=None,
        help=(
            "Target selection: 'best' (most quota left; default) or "
            "'consume-first' (proactively use the account whose weekly window "
            "resets soonest)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from claude_rotater.autoswitch import AutoSwitchEngine, AutoSwitchEvent
    from claude_rotater.printer import accent, yellowed
    from claude_rotater.settings import load_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        line = event.human()
        if event.kind == "switch":
            line = accent(line)
        elif event.kind in ("error", "account-quarantined"):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.interval is not None:
            print(
                "cswap auto: --interval is no longer used (polling follows "
                "each account's plan and the statusline feed); ignoring it",
                file=sys.stderr,
            )
        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        from claude_rotater.feed_ingest import FeedIngestor

        engine = AutoSwitchEngine(
            switcher,
            settings,
            jsonl_emit if args.json else human_emit,
            dry_run=args.dry_run,
            feed=FeedIngestor(switcher),
        )

        if args.once:
            sys.exit(engine.tick().value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        if not args.json:
            print(
                dimmed(
                    f"Auto-switch running: threshold {settings.threshold:.0f}%"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        # The headless loop keeps idle accounts' tokens warm like the TUI
        # does. Not in --dry-run (a refresh rotates and writes credentials)
        # and not with --once (a single tick has no timer to host).
        refresher = None if args.dry_run else _start_token_refresher(switcher, args.json)
        try:
            code = engine.run_loop()
        finally:
            if refresher is not None:
                refresher.stop()
        sys.exit(code)
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)


def _start_token_refresher(switcher: ClaudeAccountSwitcher, json_mode: bool):
    """Run a TokenRefresher on a daemon thread, reporting like ``cswap auto``.

    Safe beside a TUI or another ``cswap auto``: every refresh goes through
    ``consume_backup_grant``, which serializes grant use per slot.
    """
    import threading
    import time as _time

    from claude_rotater.printer import yellowed
    from claude_rotater.token_refresh import RefreshEvent, TokenRefresher

    def emit(event: RefreshEvent) -> None:
        if event.kind == "held":
            return  # routine; the TUI hides it too
        if json_mode:
            print(json.dumps(event.to_json(_time.time())), flush=True)
            return
        line = event.human()
        line = dimmed(line) if event.kind == "refreshed" else yellowed(line)
        print(f"{_time.strftime('%H:%M:%S')}  {line}", flush=True)

    refresher = TokenRefresher(switcher, on_event=emit)
    threading.Thread(target=refresher.run_loop, name="token-refresher", daemon=True).start()
    return refresher


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def _build_parser() -> argparse.ArgumentParser:
    """The parser for the commands that are not pre-dispatched."""
    prog = _prog_name()
    debug = argparse.ArgumentParser(add_help=False)
    debug.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser = argparse.ArgumentParser(
        prog=prog,
        usage="%(prog)s [command] [options]",
        description=f"""Multi-Account Switcher for Claude Code

Commands:
  {prog}                            open the TUI (accounts, logins, settings, live auto-switch)
  {prog} switch                     rotate to the next account
  {prog} switch <num|email>         switch to a specific account
  {prog} status [--json]            show the current account
  {prog} run <num|email> [-- ...]   run Claude Code as an account, this terminal only
  {prog} auto                       headless auto-switch loop (see {prog} auto -h)

Adding, removing, disabling and logging in accounts, and every setting,
live in the TUI.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[debug],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command", help=argparse.SUPPRESS)

    p_switch = sub.add_parser(
        "switch",
        parents=[debug],
        description="Rotate to the next account, or switch to one by number or email.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""Examples:
  {prog} switch user@example.com
  {prog} switch --strategy best           # the account with the most quota left
  {prog} switch --strategy next-available # rotate, skipping rate-limited accounts""",
    )
    p_switch.add_argument("target", nargs="?", metavar="NUM|EMAIL", help="Account to switch to")
    p_switch.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        help=(
            "With no target: pick by remaining 5h/7d quota. 'best' jumps to the "
            "account with the most headroom; 'next-available' rotates to the "
            "next account, skipping any at their limit"
        ),
    )
    p_switch.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "With --strategy: also count these models' per-model weekly limits "
            "(comma-separated display names, or 'all'). Defaults to the "
            "autoswitch.model setting"
        ),
    )
    p_switch.add_argument(
        "--force",
        action="store_true",
        help="With a target: activate it without backing up the current login first",
    )
    p_switch.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_status = sub.add_parser(
        "status",
        parents=[debug],
        description="Show the current account.",
    )
    p_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command != "switch":
        return
    if args.strategy is not None and args.target is not None:
        parser.error("--strategy can only be used with bare 'switch'")
    if args.model is not None and args.strategy is None:
        # Nothing usage-aware reads it otherwise: reject rather than ignore.
        parser.error("--model can only be used with 'switch --strategy'")
    if args.force and args.target is None:
        parser.error("--force can only be used with 'switch <num|email>'")


def _switch(switcher: ClaudeAccountSwitcher, args: argparse.Namespace) -> dict | None:
    if args.target is not None:
        return switcher.switch_to(args.target, json_output=args.json, force=args.force)

    from claude_rotater.settings import load_settings, parse_model_names

    # Only the usage-aware strategies read model limits: --model wins;
    # otherwise the persistent autoswitch.model setting applies (announced
    # by switch(), never silently).
    if args.strategy is None:
        models, model_source = (), None
    elif args.model is not None:
        models, model_source = parse_model_names(args.model), "cli"
    else:
        models = parse_model_names(load_settings(switcher.backup_dir).model)
        model_source = "autoswitch.model" if models else None
    payload = switcher.switch(
        strategy=args.strategy,
        json_output=args.json,
        models=models,
        model_source=model_source,
    )
    if payload is not None and models:
        payload["models"] = list(models)
        payload["modelSource"] = model_source
    return payload


def main() -> None:
    """Main entry point for the CLI."""
    force_utf8_output()
    _use_native_tls()
    argv = sys.argv[1:]
    if argv and argv[0] == "feed":
        # Hidden verb for Claude Code's statusline. The console script
        # dispatches it before this module loads (claude_rotater.entry); this
        # branch serves wrappers that call cli.main directly.
        from claude_rotater.feed import main as feed_main

        sys.exit(feed_main(argv[1:]))
    try:
        from claude_rotater.appearance import cli_should_probe, cli_theme
        # `run` execs a child that takes over the terminal, and `--json`
        # must stay machine-readable — never probe (and emit the OSC query)
        # in either case.
        probe = cli_should_probe(argv, colors_enabled=printer.colors_enabled())
        name = cli_theme(load_ui_settings(paths.get_backup_root()).theme, colors=probe)
        printer.set_theme(name)
    except Exception:
        pass  # theme is cosmetic; never block the CLI on it

    # `run` and `auto` keep their dedicated pre-dispatch parsers.
    if argv and argv[0] == "run":
        _run_command(argv[1:])
        return  # only reachable in tests where exec/exit is mocked
    if argv and argv[0] == "auto":
        _auto_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked

    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    if args.command is None and not (sys.stdout.isatty() and sys.stdin.isatty()):
        # Bare `cswap` opens the TUI (like lazygit/k9s), but only on a
        # terminal: scripts and pipes get the usage error instead.
        parser.error(f"no command given — try '{_prog_name()} -h'")
    json_mode = bool(getattr(args, "json", False))

    # Initialize the switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision) are
    # presented like every other ClaudeSwitchError: clean stderr line, exit
    # 1, no traceback. JSON-capable commands return a payload; this is the
    # single point that serializes it.
    payload: dict | None = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.command == "switch":
            payload = _switch(switcher, args)
        elif args.command == "status":
            payload = switcher.status(json_output=json_mode)
        else:
            from claude_rotater.tui import run as tui_run

            sys.exit(tui_run(switcher))
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error
        # envelope there (exit 1) instead of a red stderr line.
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)

    if json_mode and payload is not None:
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
