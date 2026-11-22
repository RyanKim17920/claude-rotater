"""The login driven in a pseudo-terminal (TUI ``l`` without leaving the TUI).

The parser tests feed it text shaped like the CLI's output. The PtyLogin tests
run tests/fixtures/fake_login_cli.py in a real pty: it prints what the CLI
prints and then succeeds, asks for a code, fails, hangs or ignores signals.
Nothing here runs the real CLI or opens a browser.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_rotater import login_pty
from claude_rotater.exceptions import LoginCancelled, LoginError
from claude_rotater.login_pty import LoginProgress, PtyLogin, clean_output, parse_progress

pytestmark = pytest.mark.skipif(not login_pty.pty_supported(), reason="no pseudo-terminals here")

FAKE = Path(__file__).parent / "fixtures" / "fake_login_cli.py"
URL = "https://example.invalid/oauth/authorize?code=true&client_id=fake&state=abc123"


class TestCleanOutput:
    def test_strips_colour_cursor_moves_and_hyperlinks(self):
        raw = "\x1b[2K\x1b[1G\x1b[33mvisit:\x1b[39m\r\n\x1b]8;;https://a.b/c\x1b\\https://a.b/c\x1b]8;;\x1b\\\r\n"
        assert clean_output(raw) == "visit:\nhttps://a.b/c\n"

    def test_osc_ended_by_bel_and_private_csi(self):
        assert clean_output("\x1b]0;title\x07\x1b[?25lhi\x1b[?25h") == "hi"

    def test_an_unfinished_escape_at_the_end_is_held_back(self):
        # The rest of the sequence is still on its way: none of it is text yet.
        assert clean_output("visit:\n\x1b]8;;https://a.b") == "visit:\n"
        assert clean_output("ok\x1b[3") == "ok"

    def test_carriage_returns_and_stray_controls(self):
        assert clean_output("a\r\nb\rc\x07\x08d") == "a\nb\ncd"


class TestParseProgress:
    def test_nothing_yet(self):
        assert parse_progress("") == LoginProgress()

    def test_url_and_paste_prompt(self):
        text = (
            "Opening browser to sign in…\nIf the browser didn't open, visit:\n"
            f"{URL}\nPaste code here if prompted > "
        )
        p = parse_progress(text)
        assert p.url == URL and p.asks_for_code and not p.succeeded and p.message == ""

    def test_a_url_wrapped_across_lines_is_joined(self):
        text = f"visit:\n{URL[:30]}\n{URL[30:60]}\n{URL[60:]}\nPaste code here if prompted > "
        assert parse_progress(text).url == URL

    def test_the_line_after_the_url_with_words_is_not_joined(self):
        text = f"visit:\n{URL}\nSigning in…\n"
        assert parse_progress(text).url == URL

    def test_no_browser_variant(self):
        p = parse_progress(f"Sign in at:\n{URL}\nPaste the code > ")
        assert p.url == URL and p.asks_for_code

    def test_signing_in_then_success(self):
        base = f"visit:\n{URL}\nPaste code here if prompted > \n"
        assert parse_progress(base + "Signing in…\n").signing_in
        done = parse_progress(base + "Signing in…\nLogin successful.\n")
        assert done.succeeded and not done.signing_in

    def test_the_cli_problem_line_is_the_message_and_the_code_is_masked(self):
        text = (
            f"visit:\n{URL}\nPaste code here if prompted > bad-code-123\n"
            "Invalid code bad-code-123. Paste the code from the page.\n"
            "Paste code here if prompted > "
        )
        p = parse_progress(text, secrets=("bad-code-123",))
        assert p.message == "Invalid code •••. Paste the code from the page."
        assert "bad-code-123" not in repr(p)

    def test_the_same_problem_twice_is_a_change(self):
        one = f"visit:\n{URL}\nInvalid code.\nPaste code here if prompted > "
        two = one + "x\nInvalid code.\nPaste code here if prompted > "
        a, b = parse_progress(one, secrets=["x"]), parse_progress(two, secrets=["x"])
        assert a.message == b.message == "Invalid code." and a != b

    def test_an_echoed_code_alone_is_never_a_message(self):
        p = parse_progress(f"visit:\n{URL}\nPaste code here if prompted > \nsecret-xyz\n", secrets=("secret-xyz",))
        assert p.message == ""

    def test_failure_line(self):
        p = parse_progress(f"visit:\n{URL}\n\nCouldn't log in: Request failed with status code 400\n")
        assert p.message == "Couldn't log in: Request failed with status code 400"


def _argv(mode_env: dict) -> tuple[list[str], dict]:
    env = {**os.environ, **mode_env}
    return [sys.executable, str(FAKE)], env


def _session(mode: str, **kw) -> tuple[PtyLogin, list[LoginProgress]]:
    seen: list[LoginProgress] = []
    argv, env = _argv({"FAKE_LOGIN_MODE": mode, **kw.pop("env", {})})
    return PtyLogin(argv, env, on_progress=seen.append, **kw), seen


def _run_in_thread(session: PtyLogin) -> tuple[threading.Thread, list]:
    outcome: list = []

    def target():
        try:
            session.run()
            outcome.append("ok")
        except BaseException as e:  # noqa: BLE001 -- the test inspects it
            outcome.append(e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, outcome


def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill(0): ask ps for its state
    import subprocess

    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class TestPtyLogin:
    def test_browser_callback_succeeds(self):
        session, seen = _session("callback")
        session.run()
        assert seen[-1].succeeded
        assert any(p.url == URL for p in seen)
        assert session.returncode == 0

    def test_runs_in_a_terminal_of_its_own(self):
        argv = [sys.executable, "-c", "import os,sys; print(os.isatty(0), os.isatty(1), os.isatty(2))"]
        lines: list[str] = []
        session = PtyLogin(argv, dict(os.environ), on_progress=lambda p: None)
        session.run()
        lines = session.output.split()
        assert lines == ["True", "True", "True"]

    def test_a_pasted_code_is_sent_and_a_bad_one_reported(self):
        session, seen = _session("code")
        t, outcome = _run_in_thread(session)
        assert _wait_for(lambda: session.progress.asks_for_code)
        assert session.send_code("nope-123")
        assert _wait_for(lambda: "Invalid code" in session.progress.message)
        assert "nope-123" not in session.progress.message
        assert session.send_code("good-code")
        t.join(10)
        assert outcome == ["ok"] and session.progress.succeeded
        assert all("good-code" not in p.message for p in seen)

    def test_a_wrapped_url_comes_back_whole(self):
        session, _ = _session("wrap")
        t, outcome = _run_in_thread(session)
        assert _wait_for(lambda: session.progress.asks_for_code)
        assert session.progress.url == URL
        session.send_code("good-code")
        t.join(10)
        assert outcome == ["ok"]

    def test_colour_and_cursor_noise_is_stripped(self):
        session, _ = _session("noisy")
        t, outcome = _run_in_thread(session)
        assert _wait_for(lambda: session.progress.url == URL)
        session.send_code("good-code")
        t.join(10)
        assert outcome == ["ok"]

    def test_failure_raises_with_the_cli_message(self):
        session, _ = _session("fail")
        with pytest.raises(LoginError) as info:
            session.run()
        assert not isinstance(info.value, LoginCancelled)
        assert "status code 400" in str(info.value)

    def test_cancel_reaches_a_raw_terminal_as_ctrl_c(self):
        session, _ = _session("raw", grace_s=5.0)
        t, outcome = _run_in_thread(session)
        assert _wait_for(lambda: session.progress.asks_for_code)
        started = time.monotonic()
        session.cancel()
        t.join(10)
        assert isinstance(outcome[0], LoginCancelled)
        assert session.returncode == 130  # the CLI's own exit, not a signal
        assert time.monotonic() - started < 4.0

    def test_cancel_terminates_the_whole_group(self, tmp_path):
        pidfile = tmp_path / "child.pid"
        session, _ = _session("hang", grace_s=0.3, env={"FAKE_LOGIN_PIDFILE": str(pidfile)})
        t, outcome = _run_in_thread(session)
        assert _wait_for(pidfile.exists) and _wait_for(lambda: pidfile.read_text().strip() != "")
        grandchild = int(pidfile.read_text())
        session.cancel()
        t.join(10)
        assert isinstance(outcome[0], LoginCancelled)
        assert session.returncode == -signal.SIGTERM
        assert _wait_for(lambda: not _alive(grandchild), 5.0)

    def test_a_child_ignoring_sigterm_is_killed(self):
        session, _ = _session("stubborn", grace_s=0.3)
        t, outcome = _run_in_thread(session)
        assert _wait_for(lambda: session.progress.asks_for_code)
        session.cancel()
        t.join(10)
        assert isinstance(outcome[0], LoginCancelled)
        assert session.returncode == -signal.SIGKILL

    def test_timeout_ends_the_login(self):
        session, _ = _session("hang", timeout_s=0.5, grace_s=0.2)
        with pytest.raises(LoginError, match="timed out"):
            session.run()
        assert session.returncode is not None

    def test_cancel_before_start_never_spawns(self):
        session, _ = _session("callback")
        session.cancel()
        with pytest.raises(LoginCancelled):
            session.run()
        assert session.returncode is None

    def test_send_code_after_the_end_is_refused(self):
        session, _ = _session("callback")
        session.run()
        assert session.send_code("late") is False

    def test_a_missing_binary_is_a_login_error(self, tmp_path):
        session = PtyLogin([str(tmp_path / "nope")], dict(os.environ), on_progress=lambda p: None)
        with pytest.raises(LoginError, match="Could not run"):
            session.run()

    def test_no_file_descriptors_leak(self):
        before = len(os.listdir("/dev/fd"))
        for mode in ("callback", "fail"):
            session, _ = _session(mode)
            try:
                session.run()
            except LoginError:
                pass
        assert len(os.listdir("/dev/fd")) <= before


class TestStagedLoginInAPty:
    """StagedLogin.pty_login + finish_pty: the same command and stage as the
    terminal login, run by PtyLogin, then confirmed with ``auth status``."""

    @staticmethod
    def _cli(tmp_path, mode="callback", logged_in=True) -> str:
        script = tmp_path / "fake-cli"
        status = '{"loggedIn": true}' if logged_in else '{"loggedIn": false}'
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$2" = status ]; then echo \'{status}\'; exit 0; fi\n'
            f'echo "$@" > "{tmp_path}/argv"; echo "$CLAUDE_CONFIG_DIR" > "{tmp_path}/config_dir"\n'
            f'FAKE_LOGIN_MODE={mode} exec "{sys.executable}" "{FAKE}"\n'
        )
        script.chmod(0o755)
        return str(script)

    def test_logs_in_the_stage_and_confirms(self, tmp_path):
        from claude_rotater.login import StagedLogin

        stage = StagedLogin(tmp_path / "backup", claude_bin=self._cli(tmp_path))
        seen: list[LoginProgress] = []
        try:
            session = stage.pty_login(on_progress=seen.append, email="who@example.com")
            assert stage.finish_pty(session) == {"loggedIn": True}
            assert (tmp_path / "argv").read_text().split() == [
                "auth", "login", "--claudeai", "--email", "who@example.com"
            ]
            assert (tmp_path / "config_dir").read_text().strip() == stage.config_dir
            assert seen[-1].succeeded
        finally:
            stage.cleanup()
        assert list((tmp_path / "backup" / "logins").iterdir()) == []

    def test_unconfirmed_login_is_an_error(self, tmp_path):
        from claude_rotater.login import StagedLogin

        stage = StagedLogin(tmp_path / "backup", claude_bin=self._cli(tmp_path, logged_in=False))
        try:
            with pytest.raises(LoginError, match="does not report a login"):
                stage.finish_pty(stage.pty_login(on_progress=lambda p: None))
        finally:
            stage.cleanup()

    def test_only_claude_ai_logins(self, tmp_path):
        from claude_rotater.login import StagedLogin

        stage = StagedLogin(tmp_path / "backup", claude_bin=self._cli(tmp_path))
        try:
            with pytest.raises(LoginError, match="Unknown login method"):
                stage.pty_login(on_progress=lambda p: None, method="console")
        finally:
            stage.cleanup()
