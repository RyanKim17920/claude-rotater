"""Tests for the CLI module."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_rotater import __version__
from claude_rotater import cli

# src layout: ensure subprocess can find claude_rotater
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

# A throwaway HOME for subprocesses. The in-process autouse Keychain/HOME guards
# do NOT reach child processes, so a spawned ``python -m claude_rotater`` would
# otherwise resolve to the developer's real ``~/.rota`` and run the
# data migration against real accounts (touching the real Keychain on macOS). An empty,
# isolated HOME has no ``sequence.json`` → the migration skips before any Keychain
# access, and no ``.claude.json`` → no account to read.
# Allocated under pytest's basetemp so its own retention reclaims it, rather
# than a sweep at exit that a signal-killed worker never reaches.
_ISOLATED_HOME: str | None = None


@pytest.fixture(autouse=True, scope="session")
def _isolated_subprocess_home(tmp_path_factory):
    global _ISOLATED_HOME
    _ISOLATED_HOME = str(tmp_path_factory.mktemp("subproc-home"))


def _subprocess_env(**extra: str) -> dict[str, str]:
    """Build env dict with PYTHONPATH pointing at src/ and an isolated HOME.

    HOME/USERPROFILE default to a throwaway dir so the spawned CLI never touches
    the developer's real backup dir or Keychain; callers may still override HOME
    explicitly (e.g. ``_subprocess_env(HOME=str(temp_home))``), in which case
    USERPROFILE mirrors it unless the caller set USERPROFILE too.
    """
    env = {**os.environ, **extra}
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    if "HOME" not in extra:
        # Falling through here would point the child at the real HOME.
        assert _ISOLATED_HOME is not None, "_isolated_subprocess_home did not run"
        env["HOME"] = _ISOLATED_HOME
        env["USERPROFILE"] = _ISOLATED_HOME
    elif "USERPROFILE" not in extra:
        env["USERPROFILE"] = extra["HOME"]
    # CLAUDE_CONFIG_DIR / ROTA_HOME / ROTA_IMPORT_FROM bypass HOME in path
    # resolution, so a developer with any of them exported would otherwise
    # point the spawned CLI back at real config/backup paths (and on macOS, the real Keychain). Drop them
    # unless a caller set them deliberately.
    for var in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "ROTA_HOME", "ROTA_IMPORT_FROM"):
        if var not in extra:
            env.pop(var, None)
    return env


class TestCLI:
    """Parsing and dispatch for the minimal surface: bare rota, switch, status."""

    def test_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_rotater", "--version"],
            capture_output=True, text=True, env=_subprocess_env(),
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_help_lists_exactly_the_public_commands(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_rotater", "--help"],
            capture_output=True, text=True, env=_subprocess_env(),
        )
        assert result.returncode == 0
        out = result.stdout
        assert "Multi-Account Switcher" in out
        commands = out.split("Commands:", 1)[1].split("options:", 1)[0]
        verbs = {
            words[1] for words in (ln.split() for ln in commands.splitlines())
            if len(words) > 1 and words[0] == "rota"
        }
        assert verbs == {"open", "switch", "status", "run", "auto"}
        assert "feed" not in commands  # hidden statusline verb
        assert "==SUPPRESS==" not in out

    def test_no_args_without_a_tty_is_a_clean_error(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_rotater"],
            capture_output=True, text=True, env=_subprocess_env(),
        )
        assert result.returncode == 2
        assert "no command given" in result.stderr

    @pytest.mark.parametrize("verb", [
        "list", "add", "add-token", "remove", "disable", "enable", "alias",
        "map", "unmap", "config", "export", "import", "import-usage", "swap",
        "move", "unclaimed", "menubar", "upgrade", "purge", "tui", "watch",
        "help", "--list", "--status", "--switch-to",
    ])
    def test_removed_verbs_are_rejected(self, verb, capsys):
        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["rota", verb]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        switcher_cls.assert_not_called()

    def test_debug_flag_accepted_after_the_verb(self):
        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["rota", "status", "--debug"]), \
             patch("os.geteuid", return_value=1000, create=True):
            cli.main()
        switcher_cls.assert_called_once_with(debug=True)


def _main(argv: list[str], *, settings=None):
    """Run cli.main() with a mocked switcher; returns the switcher mock."""
    from claude_rotater.settings import AutoSwitchSettings

    with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
         patch.object(sys, "argv", ["rota", *argv]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_rotater.settings.load_settings",
               return_value=settings or AutoSwitchSettings()):
        cli.main()
    return switcher_cls.return_value


def _rejected(argv: list[str], capsys) -> str:
    with patch.object(sys, "argv", ["rota", *argv]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
    assert excinfo.value.code == 2
    return capsys.readouterr().err


class TestSwitchCommand:
    def test_target_dispatches_switch_to(self):
        _main(["switch", "2"]).switch_to.assert_called_once_with(
            "2", json_output=False, force=False
        )

    def test_force_forwarded(self):
        _main(["switch", "2", "--force"]).switch_to.assert_called_once_with(
            "2", json_output=False, force=True
        )

    def test_bare_switch_rotates(self):
        _main(["switch"]).switch.assert_called_once_with(
            strategy=None, json_output=False, models=(), model_source=None
        )

    def test_strategy_forwarded(self):
        _main(["switch", "--strategy", "best"]).switch.assert_called_once_with(
            strategy="best", json_output=False, models=(), model_source=None
        )

    def test_strategy_falls_back_to_configured_model(self):
        from claude_rotater.settings import AutoSwitchSettings

        switcher = _main(["switch", "--strategy", "best"],
                         settings=AutoSwitchSettings(model="Fable"))
        switcher.switch.assert_called_once_with(
            strategy="best", json_output=False,
            models=("Fable",), model_source="autoswitch.model",
        )

    def test_model_flag_overrides_setting(self):
        from claude_rotater.settings import AutoSwitchSettings

        switcher = _main(
            ["switch", "--strategy", "next-available", "--model", "Opus, opus,Fable"],
            settings=AutoSwitchSettings(model="Sonnet"),
        )
        switcher.switch.assert_called_once_with(
            strategy="next-available", json_output=False,
            models=("Opus", "Fable"), model_source="cli",
        )

    def test_strategy_rejects_unknown_value(self, capsys):
        _rejected(["switch", "--strategy", "bogus"], capsys)

    def test_strategy_with_target_rejected(self, capsys):
        assert "bare 'switch'" in _rejected(["switch", "2", "--strategy", "best"], capsys)

    def test_model_without_strategy_rejected(self, capsys):
        assert "--model can only be used with" in _rejected(
            ["switch", "--model", "Fable"], capsys
        )

    def test_force_without_target_rejected(self, capsys):
        assert "--force can only be used with" in _rejected(["switch", "--force"], capsys)


class TestStatusCommand:
    def test_status_no_account(self, temp_home: Path):
        result = subprocess.run(
            [sys.executable, "-m", "claude_rotater", "status"],
            capture_output=True, text=True,
            env=_subprocess_env(HOME=str(temp_home)),
        )
        assert "No active Claude account" in result.stdout or result.returncode == 0

    def test_status_dispatches(self):
        _main(["status"]).status.assert_called_once_with(json_output=False)


class TestJsonOutputCli:
    """CLI wiring for ``--json``: single serialization, error envelope."""

    def test_status_json_serialized_to_stdout(self, capsys):
        payload = {"schemaVersion": 1, "activeAccountNumber": None}
        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["rota", "status", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True):
            switcher_cls.return_value.status.return_value = payload
            cli.main()
        switcher_cls.return_value.status.assert_called_once_with(json_output=True)
        assert json.loads(capsys.readouterr().out) == payload  # one object, no extra text

    def test_switch_json_forwarded_and_serialized(self, capsys):
        payload = {"schemaVersion": 1, "switched": True}
        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["rota", "switch", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()
        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=True, models=(), model_source=None,
        )
        assert json.loads(capsys.readouterr().out) == payload

    def test_switch_json_carries_model_fields_when_in_effect(self, capsys):
        payload = {"schemaVersion": 1, "switched": True}
        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", [
                 "rota", "switch", "--strategy", "best", "--model", "Fable", "--json",
             ]), \
             patch("os.geteuid", return_value=1000, create=True):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()
        out = json.loads(capsys.readouterr().out)
        assert out["models"] == ["Fable"]
        assert out["modelSource"] == "cli"
        assert out["switched"] is True

    def test_error_envelope_on_stdout_with_exit_1(self, capsys):
        from claude_rotater.exceptions import ConfigError

        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["rota", "status", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True):
            switcher_cls.return_value.status.side_effect = ConfigError("nope")
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)
        assert envelope["error"] == {"type": "ConfigError", "message": "nope"}
        assert captured.err == ""

    def test_human_error_goes_to_stderr(self, capsys):
        from claude_rotater.exceptions import ConfigError

        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["rota", "switch", "9"]), \
             patch("os.geteuid", return_value=1000, create=True):
            switcher_cls.return_value.switch_to.side_effect = ConfigError("no such account")
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert "no such account" in capsys.readouterr().err


class TestRunCommand:
    """`rota run` pre-dispatch: parsing, forwarding, and dispatch."""

    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path):
        # The fake switcher's data dir: where run.shareDefault is read from.
        self.backup_dir = tmp_path

    def _switcher(self):
        return patch(
            "claude_rotater.cli.ClaudeAccountSwitcher",
            **{"return_value.backup_dir": self.backup_dir},
        )

    def _dispatch(self, argv: list[str]):
        """Run cli.main() with a fake SessionManager; returns recorded calls."""
        calls = []

        class FakeSessionManager:
            def __init__(self, switcher):
                calls.append(("init", switcher))

            def run(
                self,
                identifier,
                claude_args,
                share=True,
                share_history=False,
                require_session=False,
            ):
                calls.append((
                    "run", identifier, claude_args, share, share_history,
                    require_session,
                ))

        with patch("claude_rotater.session.SessionManager", FakeSessionManager), \
             self._switcher(), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["rota", *argv]):
            cli.main()
        return calls

    def test_run_dispatches_with_defaults(self):
        calls = self._dispatch(["run", "2"])
        assert ("run", "2", [], True, False, False) in calls

    def test_run_by_email(self):
        calls = self._dispatch(["run", "user@example.com"])
        assert ("run", "user@example.com", [], True, False, False) in calls

    def test_no_share_flag(self):
        calls = self._dispatch(["run", "2", "--no-share"])
        assert ("run", "2", [], False, False, False) in calls

    def test_share_default_setting_turns_sharing_off(self):
        from claude_rotater.settings import set_setting

        set_setting(self.backup_dir, "run.shareDefault", "false")
        assert ("run", "2", [], False, False, False) in self._dispatch(["run", "2"])

    def test_share_flag_overrides_the_setting(self):
        from claude_rotater.settings import set_setting

        set_setting(self.backup_dir, "run.shareDefault", "false")
        assert ("run", "2", [], True, False, False) in self._dispatch(["run", "2", "--share"])

    def test_no_share_flag_overrides_the_default(self):
        from claude_rotater.settings import set_setting

        set_setting(self.backup_dir, "run.shareDefault", "true")
        assert ("run", "2", [], False, False, False) in self._dispatch(["run", "2", "--no-share"])

    def test_share_history_flag(self):
        calls = self._dispatch(["run", "2", "--share-history"])
        assert ("run", "2", [], True, True, False) in calls

    def test_no_share_history_flag(self):
        calls = self._dispatch(["run", "2", "--no-share-history"])
        assert ("run", "2", [], True, False, False) in calls

    def test_require_session_flag(self):
        calls = self._dispatch(["run", "2", "--require-session"])
        assert ("run", "2", [], True, False, True) in calls

    def test_tail_forwarded_verbatim(self):
        calls = self._dispatch(["run", "2", "--", "--resume", "--model", "x"])
        assert ("run", "2", ["--resume", "--model", "x"], True, False, False) in calls

    def test_tail_may_contain_run_flags(self):
        """Args after `--` are NOT parsed by rota, even if they look like ours."""
        calls = self._dispatch(["run", "2", "--", "--no-share"])
        assert ("run", "2", ["--no-share"], True, False, False) in calls

    def test_run_requires_an_account(self, capsys):
        # Directory mappings are gone: there is nothing to resolve without one.
        with patch.object(sys, "argv", ["rota", "run"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_run_unknown_flag_errors(self, capsys):
        with patch.object(sys, "argv", ["rota", "run", "2", "--bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_run_help(self, capsys):
        with patch.object(sys, "argv", ["rota", "run", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--no-share" in out and "run.shareDefault" in out
        assert "this terminal only" in out

    def test_main_help_mentions_run(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_rotater", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "run <num|email>" in result.stdout

    def test_session_error_exits_cleanly(self, capsys):
        class FailingSessionManager:
            def __init__(self, switcher):
                pass

            def run(
                self,
                identifier,
                claude_args,
                share=True,
                share_history=False,
                require_session=False,
            ):
                from claude_rotater.exceptions import SessionError

                raise SessionError("boom")

        with patch("claude_rotater.session.SessionManager", FailingSessionManager), \
             self._switcher(), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["rota", "run", "2"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 1
        assert "boom" in capsys.readouterr().err



class TestAutoCommand:
    """`rota auto` pre-dispatch: parsing, settings merge, exit codes, JSONL."""

    class FakeEngine:
        instances: list = []
        tick_outcome = None  # set per test (TickOutcome)

        def __init__(self, switcher, settings, on_event, *, dry_run=False,
                     state_path=None, clock=None, feed=None):
            self.switcher = switcher
            self.feed = feed
            self.settings = settings
            self.on_event = on_event
            self.dry_run = dry_run
            type(self).instances.append(self)

        def tick(self):
            from claude_rotater.autoswitch import TickOutcome

            return type(self).tick_outcome or TickOutcome.NO_ACTION

        def run_loop(self):
            return 0

        def stop(self):
            pass

    class FakeRefresher:
        """Stands in for TokenRefresher; ``run_loop`` replays ``events``."""

        instances: list = []
        events: list = []

        def __init__(self, switcher, *, on_event):
            self.switcher = switcher
            self.on_event = on_event
            self.ran = threading.Event()
            self.stopped = False
            type(self).instances.append(self)

        def run_loop(self):
            for event in type(self).events:
                self.on_event(event)
            self.ran.set()
            return 0

        def stop(self):
            self.stopped = True

    @pytest.fixture(autouse=True)
    def _fresh_fake(self):
        self.FakeEngine.instances = []
        self.FakeEngine.tick_outcome = None
        self.FakeRefresher.instances = []
        self.FakeRefresher.events = []

    def _run(self, argv: list[str], temp_home):
        with patch("claude_rotater.autoswitch.AutoSwitchEngine", self.FakeEngine), \
             patch("claude_rotater.token_refresh.TokenRefresher", self.FakeRefresher), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["rota", "auto", *argv]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code

    def test_once_exit_code_switched(self, temp_home):
        from claude_rotater.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.SWITCHED
        assert self._run(["--once"], temp_home) == 0

    def test_once_exit_code_no_action(self, temp_home):
        from claude_rotater.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.NO_ACTION
        assert self._run(["--once"], temp_home) == 2

    def test_once_exit_code_blocked(self, temp_home):
        from claude_rotater.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.BLOCKED
        assert self._run(["--once"], temp_home) == 3

    def test_loop_mode_returns_loop_exit(self, temp_home):
        assert self._run([], temp_home) == 0
        assert self.FakeEngine.instances  # loop path constructed the engine

    def test_loop_leaves_tokens_alone_by_default(self, temp_home):
        assert self._run([], temp_home) == 0
        assert self.FakeRefresher.instances == []

    def test_loop_hosts_the_token_refresher_and_stops_it(self, temp_home):
        self._opt_in_refresh()
        assert self._run([], temp_home) == 0
        refresher, = self.FakeRefresher.instances
        assert refresher.ran.wait(5)
        assert refresher.stopped

    @pytest.mark.parametrize("argv", [["--once"], ["--dry-run"]])
    def test_once_and_dry_run_never_refresh_tokens(self, temp_home, argv):
        self._run(argv, temp_home)
        assert self.FakeRefresher.instances == []

    @staticmethod
    def _opt_in_refresh():
        from claude_rotater.paths import get_backup_root
        from claude_rotater.settings import set_setting

        get_backup_root().mkdir(parents=True, exist_ok=True)
        set_setting(get_backup_root(), "accounts.proactiveTokenRefresh", "true")

    def _replay(self, argv, temp_home, capsys):
        from claude_rotater.token_refresh import RefreshEvent

        self._opt_in_refresh()

        self.FakeRefresher.events = [
            RefreshEvent("refreshed", "2", "a@x.com"),
            RefreshEvent("held", "3", "b@x.com"),
            RefreshEvent("dead", "4", "c@x.com", "invalid_grant"),
        ]
        self._run(argv, temp_home)
        assert self.FakeRefresher.instances[0].ran.wait(5)
        return capsys.readouterr().out

    def test_refresh_events_print_in_human_mode(self, temp_home, capsys):
        out = self._replay([], temp_home, capsys)
        assert "refreshed account 2 (a@x.com)'s token" in out
        assert "account 4 (c@x.com) needs a re-login" in out
        assert "b@x.com" not in out  # held is routine

    def test_refresh_events_are_json_lines_in_json_mode(self, temp_home, capsys):
        out = self._replay(["--json"], temp_home, capsys)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert [(e["event"], e["account"]) for e in events] == [
            ("token-refreshed", "2"), ("token-dead", "4"),
        ]
        assert events[1]["detail"] == "invalid_grant"
        assert all(e["schemaVersion"] == 1 for e in events)

    def test_flags_override_settings_json(self, temp_home):
        from claude_rotater.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 80.0, "cooldownSeconds": 42.0},
        }))
        self._run(["--once", "--threshold", "60"], temp_home)
        engine = self.FakeEngine.instances[-1]
        assert engine.settings.threshold == 60.0     # CLI wins
        assert engine.settings.cooldown_seconds == 42.0  # settings.json kept

    def test_dry_run_forwarded(self, temp_home):
        self._run(["--once", "--dry-run"], temp_home)
        assert self.FakeEngine.instances[-1].dry_run is True

    def test_headless_engine_ingests_this_profiles_feed(self, temp_home):
        from claude_rotater.paths import get_backup_root

        self._run(["--once"], temp_home)
        feed = self.FakeEngine.instances[-1].feed
        assert feed is not None
        assert feed.backup_dir == get_backup_root()

    def test_retired_interval_flag_is_accepted_and_ignored(self, temp_home, capsys):
        """Old service units pass --interval; they must keep starting."""
        assert self._run(["--once", "--interval", "30"], temp_home) == 2
        assert "--interval is no longer used" in capsys.readouterr().err
        assert not hasattr(self.FakeEngine.instances[-1].settings, "interval_seconds")

    def test_json_stdout_is_pure_jsonl(self, temp_home, capsys):
        from claude_rotater.autoswitch import NoSwitchEvent, TickOutcome

        class EmittingEngine(self.FakeEngine):
            def tick(self):
                self.on_event(NoSwitchEvent(reason="below-threshold"))
                self.on_event(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

        with patch("claude_rotater.autoswitch.AutoSwitchEngine", EmittingEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["rota", "auto", "--once", "--json"]):
            with pytest.raises(SystemExit):
                cli.main()
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            payload = json.loads(line)
            assert payload["event"] == "no-switch"
            assert payload["schemaVersion"] == 1

    def test_unknown_flag_errors(self, temp_home, capsys):
        with patch.object(sys, "argv", ["rota", "auto", "--bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_auto_help(self, capsys):
        with patch.object(sys, "argv", ["rota", "auto", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--once" in out
        assert "Exit codes" in out

    def test_main_help_mentions_auto(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_rotater", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "auto" in result.stdout

    def test_switcher_error_exits_1(self, temp_home, capsys):
        from claude_rotater.exceptions import ConfigError

        with patch("claude_rotater.cli.ClaudeAccountSwitcher",
                   side_effect=ConfigError("nope")), \
             patch.object(sys, "argv", ["rota", "auto", "--once"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert "nope" in capsys.readouterr().err  # printer.error -> stderr


class TestTuiDispatch:
    """Bare `rota` on a terminal is the one way into the TUI."""

    def _launch(self, *, tty: bool, result=0):
        import claude_rotater.tui as tui

        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return result

        with patch("claude_rotater.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(tui, "run", fake_run), \
             patch.object(sys, "argv", ["rota"]), \
             patch.object(sys.stdout, "isatty", lambda: tty), \
             patch.object(sys.stdin, "isatty", lambda: tty), \
             patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code, calls, switcher_cls.return_value

    def test_bare_rota_on_a_tty_opens_the_tui(self):
        code, calls, switcher = self._launch(tty=True)
        assert code == 0
        assert calls == [((switcher,), {})]

    def test_tui_exit_code_propagates(self):
        code, _calls, _switcher = self._launch(tty=True, result=3)
        assert code == 3

    def test_bare_rota_without_a_tty_never_opens_it(self):
        code, calls, _switcher = self._launch(tty=False)
        assert code == 2 and calls == []

    def test_auto_help_says_tui_runs_the_same_engine(self, capsys):
        with patch.object(sys, "argv", ["rota", "auto", "--help"]):
            with pytest.raises(SystemExit):
                cli.main()
        out = " ".join(capsys.readouterr().out.split())
        assert "runs this same engine live" in out
        assert "--dry-run" in out and "--once" in out


def test_importing_the_module_allocates_no_temp_dir(tmp_path, tmp_path_factory):
    """Import must allocate nothing; the fixture must allocate inside basetemp.

    The child gets a private TMPDIR of its own rather than watching the shared
    system one, which several checkouts write to concurrently. Both halves are
    needed: re-adding the module-level ``mkdtemp`` is caught only by the empty
    private tmp, and a fixture allocating outside basetemp only by containment.
    """
    child_tmp = tmp_path / "childtmp"
    child_tmp.mkdir()
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import tests.test_cli",
         str(Path(__file__).resolve().parent.parent)],
        capture_output=True, text=True,
        env=_subprocess_env(TMPDIR=str(child_tmp)), timeout=120,
    )
    assert child.returncode == 0, child.stderr
    allocated = list(child_tmp.iterdir())
    assert not allocated, f"import allocated {[a.name for a in allocated]}"

    home = Path(_subprocess_env()["HOME"])
    assert home.is_dir(), f"the isolated HOME is not a real directory: {home}"
    assert home.is_relative_to(tmp_path_factory.getbasetemp()), f"{home} escapes basetemp"
